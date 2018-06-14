#!/usr/bin/env python3

import asyncio
import websockets
import json
from queue import Queue
import time
import logging
import threading
import traceback
from typing import *

from websockets.server import WebSocketServerProtocol
from websockets.exceptions import ConnectionClosed

from lib.event_loop import EventLoopThread
from lib.message import Message
from lib.router.connection import Connection, ConnectionList
from lib.router.errors import RouterError, RouterConnectionError, RouterServerError

def _route_thread(
		message_callback: Callable[[Connection, str], None],
		remove_callback: Callable[[Connection], None],
		conn_list: ConnectionList,
		receive_queue: Queue,
		logger: logging.Logger) -> None:
	logger.info('Main thread started')

	while True:
		conn, data = receive_queue.get()

		if conn is None:
			if conn_list:
				logger.error('Request to end main thread but connections still exist!')
			break

		assert isinstance(conn, Connection)

		# Empty data is a close request
		if data is None:
			remove_callback(conn)
			continue

		if conn.closed:
			logger.warning(f'Dropping message since conn {conn!r} is closing: {data!r}')
			continue

		logger.debug(f'Received from {conn!r}: {data!r}')

		message_callback(conn, data)


class Router(object):
	router_last_id = 0

	def __init__(self, host: str, port: int, max_queue_size: int = 100):
		self.host, self.port = host, port

		self.connection_list = ConnectionList()

		self.receive_queue = Queue(max_queue_size)

		self.closed = False

		self.event_loop = asyncio.new_event_loop()  # type: asyncio.AbstractEventLoop
		self.event_loop.set_debug(True)

		self.connection_index = 0

		Router.router_last_id += 1
		self.id = Router.router_last_id

		self.last_connection_id = 0

		self.logger = logging.getLogger(f'Router:{self.id}')
		self.logger.setLevel(logging.DEBUG)
		self.logger.debug('Creating Router server')

		self.server_thread = threading.Thread(target=self._serve_forever)
		self._interrupt_event = threading.Event()

		self._close_lock = threading.Lock()

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
				self.on_remove,
				self.connection_list,
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
			self.on_start()

			self.logger.info(f'Serving {self.host}:{self.port}')

			self._interrupt_event.wait()

			self.on_stop()
		else:
			self.logger.error(f'{loop_thread.exception_traceback} Could not start server!')

		# Mark router as closed so new connections are dropped in the meantime
		self._set_closed()

		self.logger.info(f'Closing {len(self.connection_list)} connections')

		# Close all connections and wait so remove handlers have had a chance to run
		self.connection_list.close(reason='Server shutting down')
		# self.receive_queue.put((conn, None))

		# Don't post main-thread destruction yet as there could still be some connections
		# that haven't been closed and removed from the connections list yet.

		# Instead wait up to 10 seconds for connections pool to empty, if not just continue
		wait_time = 10
		sleep_time = 1.0 / wait_time
		sleep_count = int(wait_time / sleep_time)

		self.logger.info(f'Waiting up to {wait_time} seconds for remaining connections to close')
		for i in range(sleep_count):
			if self.connection_list:
				time.sleep(sleep_time)
			else:
				break

		if self.connection_list:
			self.logger.warning(f'Timed out and {len(self.connection_list)} connections still exist')

		self.logger.info('Waiting for main thread to finish')
		self.receive_queue.put((None, None))

		main_thread.join()

		loop_thread.shutdown_loop()
		loop_thread.join()

		self.logger.info('Socket server shutdown.')

	def _is_closed(self):
		with self._close_lock:
			return self.closed

	def _set_closed(self):
		with self._close_lock:
			return self.closed

	async def handle_connect(self, websocket: [WebSocketServerProtocol, AsyncIterable], path: str) -> None:
		if self._is_closed():
			self.logger.warning('Server is closing, rejecting connection', websocket)
			return

		connection = Connection(
			conn_list=self.connection_list,
			event_loop=self.event_loop,
			router=self,
			ws=websocket,
			path=path,
		)

		self.logger.debug(f'connected: {path!r} {connection}')

		try:
			# See if subclass returns a new connection
			self.on_new(connection, path)

			# Reject if it was closed or nothing was returned
			if connection.closed:
				raise RouterConnectionError(connection.close_reason)

			# Add connection to pool if everything went well
			self.connection_list.add(connection)

		# Handle controlled errors by displaying response_error
		except RouterError as e:
			self.logger.warning(f'Rejected {connection} for {e!r}')

			self.send_messages([connection], Message.error_from_exc(e))

			connection.close(reason=str(e))
			await connection.wait_closed()

		# Handle unexpected errors by displaying a close reason if one was set if handler
		# closed manually, or show generic error to client like a 500 status.
		except Exception as e:
			self.logger.error(f'{traceback.format_exc()}\nRejected {connection} for unexpected error {e!r}')

			reason = connection.close_reason or 'Unexpected server error'

			self.send_messages([connection], Message.error_from_exc(e))

			connection.close(reason=reason)
			await connection.wait_closed()
			raise

		try:
			async for message in websocket:
				self.receive_queue.put((connection, message))

		except ConnectionClosed as e:
			self.logger.warning(f'Connection {connection!r} closed unexpectedly (code: {e.code!r}, reason: {e.reason!r})')

		connection.close()
		await connection.wait_closed()

		self.logger.debug(f'Connection coroutine ended for {connection}')

	def _handle_message(self, connection: Connection, data: str) -> None:
		try:
			json_obj = json.loads(data)  # type: object

			if not isinstance(json_obj, dict):
				raise Exception('Root value of payload must be object')

		except json.JSONDecodeError as e:
			self.logger.error(f'Could not decode json {data!r} from {connection!r}: {e!r}')

			self.send_messages([connection], Message.error_from_exc(RouterError('Decode error', str(e))))
			raise

		message = Message().load(json_obj)

		try:
			self.on_message(connection, message)

		except RouterError as e:
			self.logger.warning(f'Generated response error: {e!r}')
			self.send_messages([connection], Message.error_from_exc(e))

		except Exception as e:
			self.logger.error(f'{traceback.format_exc()}\nUnhandled response exception {e}')
			self.send_messages([connection], Message.error_from_exc(e))
			raise

	def send_messages(self, recipients: List[Connection], message: Message) -> None:
		def async_callback():
			payload = message.json()

			for recipient in recipients:
				asyncio.ensure_future(recipient.ws.send(payload), loop=self.event_loop)

		self.event_loop.call_soon_threadsafe(async_callback)

	def on_stop(self):
		pass

	def on_start(self):
		pass

	def on_new(self, connection: Connection, path: str) -> None:
		pass

	def on_message(self, connection: Connection, message: Message):
		pass

	def on_remove(self, connection: Connection) -> None:
		pass


__all__ = ['Router']