#!/usr/bin/env python3

import asyncio
import websockets
import json
from queue import Queue
import time
import logging
import threading
import traceback
from copy import deepcopy
from typing import *

from websockets.server import WebSocketServerProtocol
from websockets.exceptions import ConnectionClosed

from lib.event_loop import EventLoopThread

client_count = 0


class WebswitchError(BaseException):
	def __init__(self, error_type: str, message: str, **data):
		super(WebswitchError, self).__init__()
		self.message = message
		self.error_type = error_type
		self.error_data = data

	def __repr__(self):
		return (
			f'WebswitchError('
			'response_error={self.response_error!r},'
			'error_type={self.error_type!r},'
			'data={self.error_data!r})'
		)

	def __str__(self):
		return f"{self.error_type}: {self.message}"


class WebswitchResponseError(WebswitchError):
	def __init__(self, message: str, **data):
		super(WebswitchResponseError, self).__init__(error_type='response', message=message, **data)


class WebswitchConnectionError(WebswitchError):
	def __init__(self, message: str, **data):
		super(WebswitchConnectionError, self).__init__(error_type='connection', message=message, **data)


class WebswitchServerError(WebswitchError):
	def __init__(self, message: str, **data):
		super(WebswitchServerError, self).__init__(error_type='server', message=message, **data)


class Message(object):
	def __init__(
			self,
			data: dict = None,
			success: bool = None,
			error: str = None,
			error_data: Dict = None
		):
		self.data = data or {}
		self.success = success
		self.error = error
		self.error_data = error_data

	def load(self, json_data):
		self.data = json_data.copy()

		self.success = json_data.get('success')
		self.error = json_data.get('error')
		self.error_data = json_data.get('error_data')

		for key in ('success', 'error', 'error_data'):
			if key in self.data:
				del self.data[key]

		return self

	@classmethod
	def error(cls, message, **error_data):
		return Message(success=False, error=message, error_data=error_data)

	@classmethod
	def error_from_exc(cls, exception: BaseException):
		if isinstance(exception, WebswitchError):
			error_data = exception.error_data.copy()

			# Try to decode error data, if successful then we can serialize it
			# if not then turn it into a repr'd string and send that instead.
			for key, value in error_data.items():
				try:
					json.dumps(value)
				except json.JSONDecodeError:
					error_data[key] = repr(value)

			return cls.error(
				message=exception.message,
				error_data={**error_data, type: exception.error_type}
			)

		return cls.error(str(exception), error_data={'data': repr(exception)})

	def _render_tags(self):
		return []

	def __str__(self):
		tags = ' '.join(self._render_tags())
		return f'Message({tags}): {self.data!r}'

	def __repr__(self):
		return str(self)

	def extend(self, **kwargs):
		self.data.update(**kwargs)
		return self

	def clone(self):
		return Message(
			data=deepcopy(self.data),
			success=self.success,
			error=self.error,
		)


class Client(object):
	def __init__(self, router: 'Router', ws: WebSocketServerProtocol = None, **extra_kwargs):
		self.extra = extra_kwargs
		self.ws = ws  # type: WebSocketServerProtocol
		self.router = router

		self.closed = False
		self._close_issued = False
		self._close_event = asyncio.Event(loop=self.router.event_loop)

		self.close_reason = None  # type: Optional[str]
		self.close_code = None  # type: Optional[int]
		self.in_pool = False

		router.connection_index += 1

		self.client_id = router.connection_index

		self._close_lock = threading.Lock()

		self.logger = logging.getLogger(f'Client:{self.client_id}')
		self.logger.setLevel(logging.DEBUG)
		self.logger.debug(f'Created client {self!r}')

	def copy_to_subclass(self, subclassed_object: 'Client'):
		"""
		Copy contents of normal Client object to subclass instance of Client.
		:param subclassed_object:
		:return:
		"""
		if self.router is not subclassed_object.router:
			raise Exception('Cannot copy to sub-class of different router')

		for v in ('extra', 'ws', 'closed', 'in_pool', 'close_reason', 'close_code', 'client_id'):
			my_value = getattr(self, v)
			setattr(subclassed_object, v, my_value)

	def __repr__(self):
		if self.ws:
			addr, port, *_ = self.ws.remote_address

			tags = [
				f'id:{self.client_id}',
				f'router:{self.router.id}',
				f'remote:{addr}:{port}',
			]
		else:
			tags = [f'id:{self.client_id}', 'broadcast']

		if self._close_issued:
			tags.append('closed')

		return 'Client({})'.format(','.join(tags))

	def __str__(self):
		if self.ws:
			tags = [
				f'id:{self.client_id}',
			]
		else:
			tags = [f'id:{self.client_id}', 'broadcast']

		if self._close_issued:
			tags.append('closed')

		return 'Client({})'.format(','.join(tags))

	def send(self, message: Message):
		self.router.send_messages([self], message)

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
				self.router.connections.remove(self)

				# Signal main thread that client has been removed
				self.router.receive_queue.put((self, None))
				self.in_pool = False

			try:
				await self.ws.close(code=self.close_code, reason=self.close_reason)
			except websockets.InvalidState:  # Expected if client closed first
				pass
			except Exception as e:
				self.logger.error(f'Exception while attempting to close connection: {e!r}')

			self._close_event.set()

			self.logger.debug(f'Closed with code {self.close_code} and reason {self.close_reason}')

			self.closed = True

			return True

		def callback():
			self.logger.debug('Scheduling close callback')
			self.close_future = asyncio.ensure_future(async_callback(), loop=self.router.event_loop)

		self.router.event_loop.call_soon_threadsafe(callback)

	async def wait_closed(self):
		self.logger.debug('Waiting closed')
		await self._close_event.wait()
		self.logger.debug('Close arrived')


def _route_thread(
		message_callback: Callable[[Client, str], None],
		remove_callback: Callable[[Client], None],
		connections: List[Client],
		receive_queue: Queue,
		logger: logging.Logger) -> None:
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
			logger.warning(f'Dropping message since client {client!r} is closing: {data!r}')
			continue

		logger.debug(f'Received from {client!r}: {data!r}')

		message_callback(client, data)


class Router(object):
	router_last_id = 0

	def __init__(self, host: str, port: int, max_queue_size: int = 100):
		self.host, self.port = host, port

		self.connections = []  # type: List[Client]

		self.receive_queue = Queue(max_queue_size)

		self.closed = False

		self.event_loop = asyncio.new_event_loop()  # type: asyncio.AbstractEventLoop
		self.event_loop.set_debug(True)

		self.connection_index = 0

		Router.router_last_id += 1
		self.id = Router.router_last_id

		self.logger = logging.getLogger(f'Router:{self.id}')
		self.logger.setLevel(logging.DEBUG)
		self.logger.debug('Creating Router server')

		self.server_thread = threading.Thread(target=self._serve_forever)
		self._interrupt_event = threading.Event()

	@staticmethod
	def stringify_message(message: Message, **extra):
		payload = {
			**message.data,
			**extra,
		}

		if message.success is not None:
			payload['success'] = message.success

		if message.error or (message.success is not None and not message.success):
			payload['error'] = message.error

		if message.error_data:
			payload['error_data'] = message.error_data

		return json.dumps(payload)

	def serve(self, daemon=False):
		self.server_thread.start()

		if not daemon:
			try:
				while True:
					time.sleep(1)
			except KeyboardInterrupt:
				self.logger.warning('Caught keyboard interrupt, ending server thread')

			self.stop_serve()

	def stop_serve(self):
		self._interrupt_event.set()
		self.server_thread.join()

	def _serve_forever(self):
		self._interrupt_event.clear()

		asyncio.set_event_loop(self.event_loop)
		serve_task = websockets.serve(self.handle_connect, self.host, self.port)
		server = serve_task.ws_server

		async def async_init_callback():
			await serve_task
			return True

		async def async_shutdown_callback():
			self.logger.info('Shutting down socket server')
			server.close()
			self.logger.info('Waiting for websocket server to die')
			await server.wait_closed()

		loop_thread = EventLoopThread(
			loop=self.event_loop,
			init_async_func=async_init_callback,
			shutdown_async_func=async_shutdown_callback,
		)
		loop_thread.start()

		main_thread = threading.Thread(
			target=_route_thread,
			args=(
				self._handle_message,
				self.handle_remove,
				self.connections,
				self.receive_queue,
				self.logger,
			),
		)
		main_thread.start()

		success = False

		try:
			success = loop_thread.wait_result()
		except Exception as e:
			self.logger.error(f'{loop_thread.exception_traceback}\nCould not start server: {e!r}')

		if success:
			self.handle_start()

			self.logger.info(f'Serving {self.host}:{self.port}')

			self._interrupt_event.wait()

			self.handle_stop()
		else:
			self.logger.error(f'{loop_thread.exception_traceback} Could not start server!')

		# Mark router as closed so new connections are dropped in the meantime
		self.closed = True

		self.logger.info(f'Closing {len(self.connections)} connections')

		for client in self.connections[:]:
			client.close(reason='Server shutting down')

		# Don't post main-thread destruction yet as there could still be some clients
		# that haven't been closed and removed from the connections list yet.

		# Instead wait up to 10 seconds for connections pool to empty, if not just continue
		self.logger.info('Waiting up to 10 seconds for remaining connections to close')
		for i in range(100):
			if self.connections:
				time.sleep(0.1)
			else:
				break

		if self.connections:
			self.logger.warning(f'Timed out and {len(self.connections)} connections still exist')

		self.logger.info('Waiting for main thread to finish')
		self.receive_queue.put((None, None))

		main_thread.join()

		loop_thread.shutdown_loop()
		loop_thread.join()

		self.logger.info('Socket server shutdown.')

	async def handle_connect(self, websocket: [WebSocketServerProtocol, AsyncIterable], path: str) -> None:
		if self.closed:
			self.logger.warning('Server is closing, rejecting connection', websocket)
			return

		client = Client(router=self, ws=websocket, path=path)

		self.logger.debug(f'connected: {path!r} {client}')

		try:
			# See if subclass returns a new client
			new_result = self.handle_new(client, path)

			# Reject if it was closed or nothing was returned
			if not new_result or new_result.closed:
				raise WebswitchConnectionError(client.close_reason)

			# If we received a new client, verify it has the same websocket
			if new_result is not client:
				# If the returned instance does not have one set already, set it for them
				if new_result.ws is None:
					client.copy_to_subclass(new_result)

				# Only verify if a websocket was set
				if new_result.ws is not client.ws:
					raise WebswitchServerError(
						f'Client returned by {self.__class__.__name__!r} sub-class does'
						'not contain the same websocket!'
					)
				# If not, then assume no copy was done and do it for the subclass.
				else:
					client.copy_to_subclass(new_result)

				# If we we did, then we're good!

				client = new_result

			# Add client to pool if everything went well
			self.connections.append(client)
			client.in_pool = True

		# Handle controlled errors by displaying response_error
		except WebswitchError as e:
			self.logger.warning(f'Rejected {client} for {e!r}')

			client.send(Message.error_from_exc(e))

			client.close(reason=str(e))
			await client.wait_closed()

		# Handle unexpected errors by displaying a close reason if one was set if handler
		# closed manually, or show generic error to client like a 500 status.
		except Exception as e:
			self.logger.error(f'{traceback.format_exc()}\nRejected {client} for unexpected error {e!r}')

			reason = client.close_reason or 'Unexpected server error'

			client.send(Message.error_from_exc(e))

			client.close(reason=reason)
			await client.wait_closed()
			raise

		try:
			async for message in websocket:
				self.receive_queue.put((client, message))

		except ConnectionClosed as e:
			self.logger.warning(f'Client {client!r} closed unexpectedly (code: {e.code!r}, reason: {e.reason!r})')
			client.close()
			await client.wait_closed()

		self.logger.debug(f'Connection coroutine ended for {client}')

	def _handle_message(self, client: Client, data: str) -> None:
		try:
			json_obj = json.loads(data)  # type: object

			if not isinstance(json_obj, dict):
				raise Exception('Root value of payload must be object')

		except json.JSONDecodeError as e:
			self.logger.error(f'Could not decode json {data!r} from {client!r}: {e!r}')
			client.send(Message.error_from_exc(WebswitchError('Decode error')))
			raise

		message = Message().load(json_obj)

		try:
			self.handle_message(client, message)

		except WebswitchError as e:
			self.logger.warning(f'Generated response error: {e.response_error}')
			client.send(Message.error_from_exc(e))

		except Exception as e:
			self.logger.error(f'{traceback.format_exc()}\nUnhandled response exception {e}')
			client.send(Message.error_from_exc(e))
			raise

	def handle_stop(self):
		pass

	def handle_start(self):
		pass

	def handle_new(self, client: Client, path: str) -> Optional[Client]:
		return client

	def handle_message(self, client: Client, message: Message):
		pass

	def handle_remove(self, client: Client) -> None:
		pass

	def send_messages(self, recipients: List[Client], message: Message) -> None:
		def async_callback():
			payload = self.stringify_message(message)

			for recipient in recipients:
				asyncio.ensure_future(recipient.ws.send(payload), loop=self.event_loop)

		self.event_loop.call_soon_threadsafe(async_callback)


__all__ = ['WebswitchError', 'Client', 'Message', 'Router']