import asyncio
import logging
import threading
import traceback
from typing import Any, Optional, Coroutine, Union, Type, Callable, TypeVar, Awaitable, cast
from queue import Queue as thread_Queue
from multiprocessing import Queue as mp_Queue
import concurrent.futures as futures

from .logger import g_logger


InitReturn = TypeVar('InitReturn')
ShutdownReturn = TypeVar('ShutdownReturn')


class EventLoopManager(threading.Thread):
	"""
	A wrapper around asyncio event loops that provides two main boilerplate solutions:
		- entry points for initialization and shutdown async code
		- call forwarding into event loop

	Both of which are thread-safe outside the event loop. Additionally the underlying
	executor of the event loop can be started with a ProcessPoolExecutor and is
	transparent to the manager as its role is simply a dedicated thread to run the
	event loop.
	"""
	event_loop_manager_latest_id = 0

	def __init__(
		self,
		init_async_func: Optional[Callable[..., Awaitable[InitReturn]]] = None,
		shutdown_async_func: Optional[Callable[..., Awaitable[ShutdownReturn]]] = None,
		loop: Optional[asyncio.AbstractEventLoop] = None,
		executor: Optional[futures.Executor] = None,
		*, daemon: bool = False
	) -> None:
		"""
		:param init_async_func: Async function to run when event loop starts.
		:param shutdown_async_func: Async function to run when event loop stops
		       from `shutdown_loop()`
		:param loop: Optional event loop for thread to manage. If None a new is
		       created.
		:param executor: An optional executor for the asyncio event_loop to use.
		:param daemon: Whether the event loop thread is daemonized.
		"""
		EventLoopManager.event_loop_manager_latest_id += 1
		self.id: int = EventLoopManager.event_loop_manager_latest_id

		self.logger = g_logger.getChild(f'EventLoopManager:{self.id}')
		self.logger.debug('Creating EventLoopManager')

		super(EventLoopManager, self).__init__(
			args=(),
			kwargs={},
			daemon=daemon
		)

		self._queue_factory: Union[Type[thread_Queue], Type[mp_Queue]]

		if isinstance(executor, futures.ProcessPoolExecutor):
			self._queue_factory = mp_Queue
		else:
			self._queue_factory = thread_Queue

		self.event_loop = loop or asyncio.new_event_loop()

		if executor:
			self.event_loop.set_default_executor(executor)

		self._init_event = threading.Event()

		self.exception_traceback: Optional[str] = None
		self.exception: Optional[BaseException] = None
		self._result: Optional[InitReturn] = None

		async def _noop_init() -> InitReturn:
			return cast(InitReturn, True)

		async def _noop_shutdown() -> ShutdownReturn:
			return cast(ShutdownReturn, True)

		self._init_async_func: Callable[..., Awaitable[InitReturn]] = init_async_func or _noop_init
		self._shutdown_async_func: Callable[..., Awaitable[ShutdownReturn]] = shutdown_async_func or _noop_shutdown

	async def _run_init_async(self) -> InitReturn:
		self.logger.debug('Running init_async')
		return await asyncio.ensure_future(
			self._init_async_func(),
			loop=self.event_loop
		)

	async def _run_shutdown_async(self) -> ShutdownReturn:
		self.logger.debug('Running shutdown_async')
		return await asyncio.ensure_future(
			# TODO: Some weird mypy bug
			cast(Awaitable[ShutdownReturn], self._shutdown_async_func()),
			loop=self.event_loop
		)

	def run(self) -> None:
		"""
		Starts event manager thread and runs async initialization function if one was provided.
		After calling run, you can call `wait_result()` wait until initialization function is completed.
		:return:
		"""
		self.logger.debug('Thread started')
		self.exception = None
		self._result = None
		self._init_event.clear()

		try:
			self._result = self.event_loop.run_until_complete(self._run_init_async())
		except Exception as e:
			self.logger.debug('Init async raised an exception')
			self.exception_traceback = traceback.format_exc()
			self.exception = e

		self.logger.debug('Notifying of result')
		self._init_event.set()

		self.logger.debug('Calling run_forever()')
		self.event_loop.run_forever()

		self.logger.debug('Run forever finished, shutting down asyncgens')

		self.event_loop.run_until_complete(self.event_loop.shutdown_asyncgens())

		if self.exception is None:
			self.event_loop.run_until_complete(self._run_shutdown_async())
			self.event_loop.close()
			self.logger.debug('Calling event_loop.close()')

	def join(self, timeout: Optional[float] = None) -> None:
		"""
		Same as `Thread.join()`, should be called after shutdown_loop() is called.
		:param timeout:
		:return:
		"""
		self.logger.debug('Joining thread')
		super(EventLoopManager, self).join(timeout=timeout)

	def wait_result(self) -> InitReturn:
		"""
		Blocks until initialization function has compeleted.
		:return:
		"""
		self.logger.debug('Waiting on init_async result')

		self._init_event.wait()

		if self.exception:
			self.logger.debug('Exception caught')
			raise self.exception

		self.logger.debug(f'Retrieved result from init_async: {self._result!r}')

		assert self._result is not None

		return self._result

	def shutdown_loop(self) -> None:
		self.logger.debug('Calling shutdown_loop()')
		# Get all uncompleted tasks to wait on, noteworthy that we're calling this
		# before calling func() and creating it as a task as to not wait for itself
		# and thus causing .result() to wait indefinitely.
		pending = asyncio.all_tasks(loop=self.event_loop)

		self.logger.debug(f'Waiting on {len(pending)} tasks')

		async def wait_pending_callback() -> None:
			# Note that we will get a CancelledError upon calling .result() if
			# returns_exception is not set, this is due to the list of pending
			# tasks containing the async-for in on_connect being already cancelled
			# because of the route_thread issuing close() calls, and you can't
			# await a cancelled Task or it raises an cancelled exception.
			await asyncio.gather(*pending, return_exceptions=True)

		self.logger.info('Finishing remaining tasks before shutting down event loop')

		asyncio.run_coroutine_threadsafe(wait_pending_callback(), loop=self.event_loop).result()

		self.logger.info('Shutting down event loop thread')
		self.event_loop.call_soon_threadsafe(self.event_loop.stop)

	def run_coroutine_threadsafe(self, coro: Coroutine) -> futures.Future:
		return asyncio.run_coroutine_threadsafe(coro, loop=self.event_loop)

	def call_soon_threadsafe(self, callback: Callable, *args: Any) -> asyncio.Handle:
		return self.event_loop.call_soon_threadsafe(callback, *args)
