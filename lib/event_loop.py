import asyncio
import logging
import threading
import traceback
from typing import Callable, Optional
from queue import Queue as thread_Queue
from multiprocessing import Queue as mp_Queue
import concurrent.futures as futures
from lib.logger import g_logger

class EventLoopThread(threading.Thread):
	event_loop_thread_latest_id = 0

	def __init__(
			self,
			init_async_func: Callable = None,
			shutdown_async_func: Callable = None,
			loop: asyncio.AbstractEventLoop = None,
			executor: futures.Executor = None,
			group=None,
			target=None,
			name=None, *,
			daemon=None):

		EventLoopThread.event_loop_thread_latest_id += 1
		self.id = EventLoopThread.event_loop_thread_latest_id

		self.logger = g_logger.getChild(f'EventLoopThread:{self.id}')
		self.logger.debug('Creating EventLoopThread')

		super(EventLoopThread, self).__init__(
			group=group,
			target=target,
			name=name,
			args=(),
			kwargs={},
			daemon=daemon
		)

		if isinstance(executor, futures.ProcessPoolExecutor):
			self._queue_factory = mp_Queue
		else:
			self._queue_factory = thread_Queue

		self.event_loop = loop or asyncio.new_event_loop()

		if executor:
			self.event_loop.set_default_executor(executor)

		self.init_event = threading.Event()

		self.exception_traceback = None
		self.exception = None
		self.result = None

		self._init_async_func = init_async_func
		self._shutdown_async_func = shutdown_async_func

	def create_queue(self):
		self._queue_factory()

	async def run_init_async(self):
		self.logger.debug('Running init_async')
		if self._init_async_func:
			return await asyncio.ensure_future(self._init_async_func(), loop=self.event_loop)
		return True

	async def run_shutdown_async(self):
		self.logger.debug('Running shutdown_async')
		if self._shutdown_async_func:
			return await asyncio.ensure_future(self._shutdown_async_func(), loop=self.event_loop)
		return True

	def run(self):
		self.logger.debug('Thread started')
		self.exception = None
		self.result = None
		self.init_event.clear()

		try:
			self.result = self.event_loop.run_until_complete(self.run_init_async())
		except Exception as e:
			self.logger.debug('Init async raised an exception')
			self.exception_traceback = traceback.format_exc()
			self.exception = e

		self.logger.debug('Notifying of result')
		self.init_event.set()

		self.logger.debug('Calling run_forever()')
		self.event_loop.run_forever()

		self.logger.debug('Run forever finished, shutting down asyncgens')

		self.event_loop.run_until_complete(self.event_loop.shutdown_asyncgens())

		if self.exception is None:
			self.event_loop.run_until_complete(self.run_shutdown_async())
			self.event_loop.close()
			self.logger.debug('Calling event_loop.close()')

	def join(self, timeout: Optional[float] = None):
		self.logger.debug('Joining thread')
		super(EventLoopThread, self).join(timeout=timeout)

	def wait_result(self):
		self.logger.debug('Waiting on init_async result')

		self.init_event.wait()

		if self.exception:
			self.logger.debug('Exception caught')
			raise self.exception

		self.logger.debug(f'Retrieved result from init_async: {self.result!r}')

		return self.result

	def shutdown_loop(self):
		self.logger.debug('Calling shutdown_loop()')
		# Get all uncompleted tasks to wait on, noteworthy that we're calling this
		# before calling func() and creating it as a task as to not wait for itself
		# and thus causing .result() to wait indefinitely.
		pending = asyncio.Task.all_tasks(loop=self.event_loop)

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

	def run_coroutine_threadsafe(self, coro) -> futures.Future:
		return asyncio.run_coroutine_threadsafe(coro, loop=self.event_loop)

	def call_soon_threadsafe(self, callback, *args):
		return self.event_loop.call_soon_threadsafe(callback, *args)
