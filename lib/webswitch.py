#!/usr/bin/env python3

import asyncio
import websockets
from websockets.server import WebSocketServerProtocol
from websockets.exceptions import ConnectionClosed
import json
from queue import Queue
from threading import Thread
import time
import logging
import threading
from typing import *

client_count = 0

logger = logging.getLogger('web-switch')
logger.setLevel(logging.DEBUG)

class Message(object):
	def __init__(
			self,
			data: Any = None,
			sender: 'Client' = None,
			success: bool = None,
			error: str = None
		):
		self.data = data
		self.sender = sender
		self.success = success
		self.error = error

	def load(self, json_data):
		self.data = json_data

		self.success = json_data.get('success')
		self.error = json_data.get('error')

	@classmethod
	def error(cls, message):
		return Message(success=False, error=message)

	def json(self, **kwargs):
		payload = {
			'data': self.data
		}

		if self.success is not None:
			payload['success'] = self.success

		if self.error or (self.success is not None and not self.success):
			payload['error'] = self.error

		payload.update(kwargs)

		return json.dumps(payload)

	def send(self, recipients: List['Client']):
		if self.sender is None:
			raise Exception('Need a sender in order to send')

		return self.sender.router.send_messages(sender=self.sender, recipients=recipients, message=self)

	def __str__(self):
		return 'Message({}): {!r}'.format(
			' '.join([
				'client: {!r}'.format(self.sender.client_id if self.sender else None),
			]),
			self.data
		)

	def __repr__(self):
		return str(self)


class Client(object):
	def __init__(self, router: 'Router', ws: WebSocketServerProtocol = None, **extra_kwargs):
		self.extra = extra_kwargs
		self.ws = ws
		self.router = router

		self.closed = False
		self.close_reason = None  # type: Optional[str]
		self.close_code = None  # type: Optional[int]
		self.in_pool = False

		router.connection_index += 1

		self.client_id = router.connection_index

	def copy_to_subclass(self, subclassed_object: 'Client'):
		if self.router is not subclassed_object.router:
			raise Exception('Cannot copy to sub-class of different router')

		for v in ('extra', 'ws', 'closed', 'in_pool', 'close_reason', 'close_code', 'client_id'):
			my_value = getattr(self, v)
			setattr(subclassed_object, v, my_value)

	def __repr__(self):
		tags = [
			'id:{}'.format(self.client_id),
			'addr:{!r}'.format(self.ws.remote_address),
			'port:{!r}'.format(self.ws.port),
		]

		if self.closed:
			tags.append('closed')

		return 'Client({})'.format(' '.join(tags))

	def __str__(self):
		return repr(self)

	def send(self, data: str):

		def callback():
			async def func():
				await self.ws.send(data)
				logger.debug('Sent payload to {!r}: {!r}'.format(self, data))

			asyncio.ensure_future(func(), loop=self.router.event_loop)

		self.router.event_loop.call_soon_threadsafe(callback)

	def close(self, code=1000, reason=''):
		with threading.Lock():
			if self.closed:
				return

			self.closed = True

		def callback():
			async def func():
				if self.in_pool:
					self.router.connections.remove(self)

					# Signal main thread that client has been removed
					self.router.receive_queue.put((self, None))
					self.in_pool = False

				await self.ws.close(code=code, reason=reason)

				logger.info('Closed {!r}'.format(self))

			asyncio.ensure_future(func(), loop=self.router.event_loop)

		self.close_code = code
		self.close_reason = reason

		self.router.event_loop.call_soon_threadsafe(callback)


def _route_thread(
		message_callback: Callable[[Client, str], None],
		remove_callback: Callable[[Client], None],
		connections: List[Client],
		loop: asyncio.AbstractEventLoop,
		receive_queue: Queue) -> None:
	logger.info('Main thread started')

	while True:
		client, data = receive_queue.get()

		if client is None:
			if connections:
				logger.error('Request to end main thread but connections still exist!')
			break

		assert isinstance(client, Client)

		# Empty data is a close request
		if data is None:
			remove_callback(client)
			continue

		if client.closed:
			logger.warning('Dropping message since client {!r} is closing: {!r}'.format(client, data))
			continue

		logger.debug('Received from {!r}: {!r}'.format(client, data))

		message_callback(client, data)


class Router(object):
	def __init__(self, host: str, port: int, max_queue_size: int = 100):
		self.host, self.port = host, port

		self.connections = []  # type: List[Client]

		self.receive_queue = Queue(max_queue_size)

		self.closed = False

		self.event_loop = asyncio.new_event_loop()  # type: asyncio.AbstractEventLoop
		self.event_loop.set_debug(True)

		self.connection_index = 0

	def serve(self):
		asyncio.set_event_loop(self.event_loop)
		serve_task = websockets.serve(self.on_connect, 'localhost', 8765)
		server = serve_task.ws_server

		successfully_started_server = False
		server_condition = threading.Condition()

		server_condition.acquire()

		def loop_thread() -> None:
			nonlocal successfully_started_server

			asyncio.set_event_loop(self.event_loop)

			try:
				self.event_loop.run_until_complete(serve_task)
				successfully_started_server = True
			except Exception as e:
				logger.error('Could not start server: {}'.format(e))

			with server_condition:
				server_condition.notify()

			self.event_loop.run_forever()

			self.event_loop.run_until_complete(self.event_loop.shutdown_asyncgens())

			if successfully_started_server:
				logger.info('Shutting down socket server')
				server.close()

				logger.info('Waiting for websocket server to die')
				self.event_loop.run_until_complete(server.wait_closed())
				self.event_loop.close()

		loop_thread = Thread(target=loop_thread)
		loop_thread.start()

		main_thread = Thread(
			target=_route_thread,
			args=(self._handle_message, self.handle_remove, self.connections, self.event_loop, self.receive_queue),
		)
		main_thread.start()

		logger.info('Serving {}:{}'.format(self.host, self.port))

		with server_condition:
			server_condition.wait()

		if successfully_started_server:
			try:
				# time.sleep(6)
				while True:
					time.sleep(1)

			except KeyboardInterrupt:
				logger.warning('SIGINT caught, closing server')
		else:
			logger.error('Could not start server!')

		self.closed = True

		logger.info('Closing {} connections'.format(len(self.connections)))

		for client in self.connections[:]:
			client.close(reason='Server shutting down')

		# Don't post main-thread destruction yet as there could still be some clients
		# that haven't been closed and removed from the connections list yet.

		# Instead wait up to 10 seconds for connections pool to empty, if not just continue
		logger.info('Waiting up to 10 seconds for remaining connections to close')
		for i in range(10):
			if self.connections:
				time.sleep(1)
			else:
				break

		if self.connections:
			logger.warning('Timed out and {} connections still exist'.format(len(self.connections)))

		logger.info('Waiting for main thread to finish')
		self.receive_queue.put((None, None))

		main_thread.join()

		# Get all uncompleted tasks to wait on, noteworthy that we're calling this
		# before calling func() and creating a task as to not wait for itself
		# and thus causing .result() to wait indefinitely.
		pending = asyncio.Task.all_tasks()

		async def func() -> None:
			# Note that we will get a CancelledError upon calling .result() if
			# returns_exception is not set, this is due to the list of pending
			# tasks containing the async-for in on_connect being already cancelled
			# because of the route_thread issuing close() calls, and you can't
			# await a cancelled Task or it raises an cancelled exception.
			await asyncio.gather(*pending, return_exceptions=True)

		logger.info('Finishing remaining tasks')

		asyncio.run_coroutine_threadsafe(func(), loop=self.event_loop).result()

		logger.info('Shutting down event loop thread')
		self.event_loop.call_soon_threadsafe(self.event_loop.stop)

		loop_thread.join()

		logger.info('Socket server shutdown.')

	async def on_connect(self, websocket: [WebSocketServerProtocol, AsyncIterable], path: str) -> None:
		if self.closed:
			logger.warning('Rejecting connection', websocket)
			websocket.close()
			return

		client = Client(router=self, ws=websocket, path=path)

		logger.debug('connected: {!r} {}'.format(path, client))

		# See if subclass returns a new client
		new_result = self.handle_new(client, path)

		try:
			# Reject if it was closed or nothing was returned
			if not new_result or new_result.closed:
				raise Exception(client.close_reason)

			# If we received a new client, verify it has the same websocket
			if new_result is not client:
				if new_result.ws is not client.ws:
					raise Exception('Client returned by {!r} sub-class does not contain the same websocket!'.format(
						self.__class__.__name__
					))

				# If we we did, then we're good!

				client = new_result

		except Exception as e:
			await client.ws.close()

			close_reason = client.close_reason

			logger.warning('Rejected {} for {!r}'.format(
				client,
				close_reason if close_reason else 'some reason'
			))

			return

		self.connections.append(client)
		client.in_pool = True

		try:
			async for message in websocket:
				self.receive_queue.put((client, message))

		except ConnectionClosed as e:
			logger.warning('Client {!r} closed unexpectedly (code: {!r}, reason: {!r})'.format(client, e.code, e.reason))
			client.close()

		logger.debug('Connection coroutine ended for {}'.format(client))

	def _handle_message(self, client: Client, data: str) -> None:
		try:
			json_obj = json.loads(data)  # type: object

			if not isinstance(json_obj, dict):
				raise Exception('Root value of payload must be object')

		except Exception as e:
			logger.error('Could not decode json {!r} from {!r}: {!r}'.format(data, client, e))
			client.send(Message.error('Decode error').json())
			return

		message = Message()
		message.load(json_obj)
		message.sender = client

		logger.debug('Received {}'.format(message))

		# client.send('That a nice message')
		self.handle_message(client, message)

	def handle_new(self, client: Client, path: str) -> Optional[Client]:
		return client

	def handle_message(self, client: Client, message: Message):
		pass

	def handle_remove(self, client: Client) -> None:
		pass

	def send_messages(self, sender: Client, recipients: List[Client], message: Message) -> asyncio.Future:
		payload = message.json(sender=sender.client_id)

		futures = [
			asyncio.ensure_future(recipient.ws.send(payload), loop=self)
			for recipient in recipients
		]

		return asyncio.gather(*futures, loop=self.event_loop, return_exceptions=True)
