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

from .connection import Connection, ConnectionList
from .errors import RouterError, RouterConnectionError, RouterServerError
from ..event_loop import EventLoopManager
from ..message import Message
from ..logger import g_logger


def _route_thread(
		message_callback: Callable[[Connection, str], None],
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

		if conn.closed:
			logger.warning(f'Dropping message since conn {conn!r} is closing: {data!r}')
			continue

		logger.debug(f'Received from {conn!r}: {data!r}')

		message_callback(conn, data)


class Router(object):
	"""
	Simple extendable Routing class that processes websocket connections for the following events:
		on_stop()
		on_start()
		on_new(connection, path)
		on_message(connection, message)
		on_remove(connection)

	And provides the following *protected* methods:
		send_messages()
		try_send_messages()
	"""
	router_last_id = 0

	def __init__(self, host: str, port: int, max_queue_size: int = 100) -> None:
		self.host, self.port = host, port

		self.connection_list = ConnectionList()

		self.receive_queue: Queue = Queue(max_queue_size)

		self.closed = False

		self.event_loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
		self.event_loop.set_debug(True)

		self.connection_index = 0

		Router.router_last_id += 1
		self.id = Router.router_last_id

		self.last_connection_id = 0

		self.__logger = g_logger.getChild(f'Router:{self.id}')
		self.__logger.debug('Creating Router server')

		self._server_thread = threading.Thread(target=self._serve_forever)
		self._interrupt_event = threading.Event()
		self._ready_event = threading.Event()

		self._close_lock = threading.Lock()

	def get_logger(self) -> logging.Logger:
		"""
		Gets logger for this Router instance. Useful for subclasses.
		:return:
		"""
		return self.__logger

	def serve(self, *, daemon: bool = False, block_until_ready: bool = True) -> None:
		"""
		Starts websocket router.
		:param daemon: Whether or not to immediately return. If not then this function blocks
		until the server stops.
		:param block_until_ready: Whether or not to block (even if daemon is true) until the websocket
		server is ready. This solves issues where immediately connecting to this server after this call
		can fail.
		"""
		self.__logger.info('Start signaled')
		self._ready_event.clear()
		self._server_thread.start()

		if block_until_ready:
			self._ready_event.wait()

		if not daemon:
			try:
				while True:
					time.sleep(1)
			except KeyboardInterrupt:
				self.__logger.warning('Caught keyboard interrupt, ending server thread')

			self.stop_serve()

		self.__logger.info('Start completed')

	def stop_serve(self) -> None:
		"""
		Signal the termination of the websocket server and block until the background thread finishes.
		:return:
		"""
		self.__logger.info('Stop signaled')
		self._interrupt_event.set()
		self._server_thread.join()
		self.__logger.info('Stop completed')

	def _serve_forever(self) -> None:
		self._interrupt_event.clear()

		asyncio.set_event_loop(self.event_loop)
		serve_task = websockets.serve(self.handle_connect, self.host, self.port)
		server = serve_task.ws_server

		async def async_init_callback() -> bool:
			await serve_task
			return True

		async def async_shutdown_callback() -> None:
			self.__logger.info('Shutting down socket server')
			server.close()
			self.__logger.info('Waiting for websocket server to die')
			await server.wait_closed()

		event_manager: EventLoopManager = EventLoopManager(
			loop=self.event_loop,
			init_async_func=async_init_callback,
			shutdown_async_func=async_shutdown_callback,
		)
		event_manager.start()

		main_thread = threading.Thread(
			target=_route_thread,
			args=(
				self._handle_message,
				self.connection_list,
				self.receive_queue,
				self.__logger,
			),
		)
		main_thread.start()

		success = False

		try:
			success = event_manager.wait_result()
		except Exception as e:
			self.__logger.error(f'{event_manager.exception_traceback}\nCould not start server: {e!r}')

		if success:
			self.on_start()
			self._ready_event.set()  # Signal ready when start handler finishes

			self.__logger.info(f'Serving {self.host}:{self.port}')
			self._interrupt_event.wait()

			self.on_stop()
		else:
			self.__logger.error(f'{event_manager.exception_traceback} Could not start server!')
			self._ready_event.set()  # Signal ready when an error occurred in on_start() or event_manager.wait_result()

		# Mark router as closed so new connections are dropped in the meantime
		self._set_closed()

		self.__logger.info(f'Closing {len(self.connection_list)} connections')

		# Close all connections and wait so remove handlers have had a chance to run
		self.connection_list.close(reason='Server shutting down')

		# Don't post main-thread destruction yet as there could still be some connections
		# that haven't been closed and removed from the connections list yet.

		# Instead wait up to 10 seconds for connections pool to empty, if not just continue
		wait_time = 10
		sleep_time = 0.001
		sleep_count = int(wait_time / sleep_time)

		self.__logger.info(f'Waiting up to {wait_time} seconds for remaining connections to close')
		for i in range(sleep_count):
			if self.connection_list:
				time.sleep(sleep_time)
			else:
				break

		if self.connection_list:
			self.__logger.warning(f'Timed out and {len(self.connection_list)} connections still exist')

		self.__logger.info('Waiting for main thread to finish')
		self.receive_queue.put((None, None))

		main_thread.join()

		event_manager.shutdown_loop()
		event_manager.join()

		self.__logger.info('Socket server shutdown.')

	def _is_closed(self) -> bool:
		with self._close_lock:
			return self.closed

	def _set_closed(self) -> None:
		with self._close_lock:
			self.closed = True

	async def handle_connect(self, websocket: WebSocketServerProtocol, path: str) -> None:
		if self._is_closed():
			self.__logger.warning('Server is closing, rejecting connection', websocket)
			return

		connection = Connection(
			conn_list=self.connection_list,
			event_loop=self.event_loop,
			router=self,
			ws=websocket,
			path=path,
		)

		self.__logger.debug(f'connected: {path!r} {connection}')

		try:
			# See if subclass returns a new connection
			self.on_new(connection, path)

			# Reject if it was closed or nothing was returned
			if connection.close_issued:
				raise RouterConnectionError(connection.close_reason or 'No reason')

			# Add connection to pool if everything went well
			self.connection_list.add(connection)

		# Handle controlled errors by displaying response_error
		except RouterError as e:
			self.__logger.warning(f'Rejected {connection} for {e!r}')

			# If not closed, try to send error
			if not connection.close_issued:
				await self.send_messages([connection], Message.error_from_exc(e))
				connection.close(reason=str(e))

			await connection.wait_closed()
			return

		# Handle unexpected errors by displaying a close reason if one was set if handler
		# closed manually, or show generic error to client like a 500 status.
		except Exception as e:
			self.__logger.error(f'{traceback.format_exc()}\nRejected {connection} for unexpected error {e!r}')

			if not connection.close_issued:
				reason = connection.close_reason or 'Unexpected server error'
				await self.send_messages([connection], Message.error_from_exc(e))
				connection.close(reason=reason)

			await connection.wait_closed()
			raise

		# Async-loop for new messages for this connection
		try:
			async for message in websocket:
				self.receive_queue.put((connection, message))

			self.__logger.info(f'WebSocket loop for {connection!r} completed')

		except ConnectionClosed as e:
			self.__logger.warning(f'Connection {connection!r} closed unexpectedly (code: {e.code!r}, reason: {e.reason!r})')

		connection.close()
		await connection.wait_closed()

		self.on_remove(connection)

		self.__logger.debug(f'Connection coroutine ended for {connection}')

	def _handle_message(self, connection: Connection, data: str) -> None:
		try:
			json_obj: object = json.loads(data)

			if not isinstance(json_obj, dict):
				raise Exception('Root value of payload must be object')

		except json.JSONDecodeError as e:
			self.__logger.error(f'Could not decode json {data!r} from {connection!r}: {e!r}')

			self.try_send_messages([connection], Message.error_from_exc(RouterError('Decode error', str(e))))
			raise

		message = Message().load(json_obj)

		try:
			self.on_message(connection, message)

		except RouterError as e:
			self.__logger.warning(f'Caught router error during handling: {e!r}')
			self.try_send_messages([connection], Message.error_from_exc(e))

		except Exception as e:
			self.__logger.error(f'{traceback.format_exc()}\nUnhandled exception while handling: {e}')
			self.try_send_messages([connection], Message.error_from_exc(e))
			raise

	async def send_messages(
		self,
		recipients: List[Connection],
		message: Message
	) -> Iterable:
		"""
		Sends a message to list of connections.
		:param recipients: List of connections to send message to
		:param message: Message payload
		:return: Returns async result from each send() command. Result should
		be None for each connection unless an error occurred, then it would be an exception.
		"""
		payload = message.json()

		gens = []

		for conn in recipients:
			self.__logger.debug(f'Sending data {message!r} to {conn!r}')
			gens.append(conn.ws.send(payload))

		return cast(Iterable, await asyncio.gather(
			*gens,
			return_exceptions=True,
		))

	def try_send_messages(self, recipients: List[Connection], message: Message) -> None:
		"""
		Same as send_messages() but is thread-safe and logs any errors. Typically used to send
		messages and treat errors passively.
		:param recipients: Connections to send to
		:param message: Message payload
		:return:
		"""
		async def _async_call() -> None:
			try:
				results = await self.send_messages(recipients, message)
			except Exception as e:
				self.__logger.error(f'Error when trying to try send_message(): {e!r}')

			errors = []
			passive = []

			for i, r in enumerate(results):
				conn = recipients[i]

				if not isinstance(r, Exception):
					continue

				if isinstance(r, websockets.ConnectionClosed):
					passive.append((conn, r))
				else:
					errors.append((conn, r))

			if errors:
				self.__logger.error(f'Failed to send messages to client(s): {errors!r}')

			if passive:
				self.__logger.warning(f'Send message attempt failed: {passive!r}')

		asyncio.run_coroutine_threadsafe(_async_call(), self.event_loop)

	def on_stop(self) -> None:
		pass

	def on_start(self) -> None:
		pass

	def on_new(self, connection: Connection, path: str) -> None:
		pass

	def on_message(self, connection: Connection, message: Message) -> None:
		pass

	def on_remove(self, connection: Connection) -> None:
		pass


__all__ = ['Router']
