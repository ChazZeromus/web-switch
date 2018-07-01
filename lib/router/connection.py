import asyncio
import logging
import threading
from typing import Optional, List

import websockets
from websockets import WebSocketServerProtocol

from lib.logger import g_logger


class ConnectionList:
	def __init__(self) -> None:
		self.connections: List[Connection] = []
		self._lock = threading.Lock()
		self.last_connection_id = 0

	def generate_id(self) -> int:
		with self._lock:
			self.last_connection_id += 1
			return self.last_connection_id

	def add(self, connection: 'Connection'):
		with self._lock:
			self.connections.append(connection)
			connection.in_pool = True

	def remove(self, connection: 'Connection'):
		with self._lock:
			self.connections.remove(connection)

	def __bool__(self) -> bool:
		with self._lock:
			return bool(self.connections)

	def __len__(self) -> int:
		with self._lock:
			return len(self.connections)

	def copy(self) -> List['Connection']:
		with self._lock:
			return self.connections[:]

	def close(self, reason: str):
		with self._lock:
			for conn in self.connections:
				conn.close(reason=reason)


class Connection(object):
	def __init__(
		self,
		conn_list: ConnectionList,
		event_loop: asyncio.AbstractEventLoop,
		ws: WebSocketServerProtocol = None,
		**extra_kwargs
	) -> None:
		self.extra = extra_kwargs
		self.ws: WebSocketServerProtocol = ws
		self.event_loop = event_loop
		self.conn_list = conn_list

		self.closed = False
		self._close_issued = False
		self._close_event = asyncio.Event(loop=self.event_loop)

		self.close_reason: Optional[str] = None
		self.close_code: Optional[str] = None
		self.in_pool = False

		self.conn_id = conn_list.generate_id()

		self._close_lock = threading.Lock()

		self.logger = g_logger.getChild(f'Connection:{self.conn_id}')
		self.logger.debug(f'Created connection {self!r}')

	def copy_to_subclass(self, subclassed_object: 'Connection'):
		"""
		Copy contents of normal Connection object to subclass instance of Connection.
		:param subclassed_object:
		:return:
		"""
		if self.conn_list is not subclassed_object.conn_list:
			raise Exception('Cannot copy to sub-class of different router')

		for v in ('extra', 'ws', 'closed', 'in_pool', 'close_reason', 'close_code', 'conn_id', 'conn_list'):
			my_value = getattr(self, v)
			setattr(subclassed_object, v, my_value)

	def __repr__(self):
		if self.ws:
			addr, port, *_ = self.ws.remote_address

			tags = [
				f'id:{self.conn_id}',
				f'remote:{addr}:{port}',
			]
		else:
			tags = [f'id:{self.conn_id}', 'broadcast']

		if self._close_issued:
			tags.append('closed')

		return 'Connection({})'.format(','.join(tags))

	def __str__(self):
		if self.ws:
			tags = [
				f'id:{self.conn_id}',
			]
		else:
			tags = [f'id:{self.conn_id}', 'broadcast']

		if self._close_issued:
			tags.append('closed')

		return 'Connection({})'.format(','.join(tags))

	def close(self, code: int = 1000, reason: str = ''):
		with self._close_lock:
			if self._close_issued:
				self.logger.warning('Close already issued')
				return

			self._close_issued = True
			self.logger.debug('Issuing close')

		self.close_code = code or 1000
		self.close_reason = reason or ''

		async def async_callback():
			with self._close_lock:
				self._close_issued = True

			if self.closed:
				self.logger.warning('Close callback cancelled since already closed')
				return True

			if self.in_pool:
				self.conn_list.remove(self)
				# self.router.connections.remove(self)
				#
				# # Signal main thread that connection has been removed
				# self.router.receive_queue.put((self, None))
				# self.in_pool = False

			try:
				await self.ws.close(code=self.close_code, reason=self.close_reason)
			except websockets.InvalidState:  # Expected if connection closed first
				pass
			except Exception as e:
				self.logger.error(f'Exception while attempting to close connection: {e!r}')

			self._close_event.set()

			self.logger.debug(f'Closed with code {self.close_code} and reason {self.close_reason}')

			self.closed = True

			return True

		def callback():
			self.logger.debug('Scheduling close callback')
			self.close_future = asyncio.ensure_future(async_callback(), loop=self.event_loop)

		self.event_loop.call_soon_threadsafe(callback)

	async def wait_closed(self):
		self.logger.debug('Waiting closed')
		await self._close_event.wait()
		self.logger.debug('Close arrived')
