from typing import Callable, Dict, Type, Tuple, NewType
from threading import Lock
import asyncio
import uuid
import inspect
from lib.event_loop import EventLoopThread
from enum import Enum
import logging

logger = logging.getLogger('dispatch')
logger.setLevel(logging.DEBUG)


# TODO: Add timeout to await_dispatch and 'bounding' parameters to prevent a single
# TODO: connection from constantly creating pending coroutines.
# TODO: When sending errors, provide the response_id if possible
# TODO: For coroutine actions, perhaps implement a sort of session heartbeat for possible
# TODO: long periods of waiting?
class ResponseDispatcher(object):
	"""
	Asyncio based message dispatcher. Classes can use the `add_action()` decorator to
	define actions to be automatically be dispatched when calling `dispatch()`

	Classes typically use ResponseDispatcher like so:

		class Foo:
			dispatch = ResponseDispatcher()

			def __init__(self):
				self.dispatcher = self.dispatch(self)
				self.dispatcher.start()

			def dispatching_method(self, data: Dict):
				self.dispatcher.dispatch(data['action'], data['data'])

			@dispatch.add_action('status', params={'device': str})
			def status_action(self, device: str):
				do_something

	If the method of the action is a coroutine, the coroutine can await on responses
	that were dispatched for that particular action by using the callable that is always
	provided

	Since all dispatching occurs through one method `dispatch()`, routing which messages to what
	awaiting action is done by specifying an extra argument `response_id` to `dispatch()`. Applications
	just need to ensure clients must provide a response ID if they're responding to that particular action.

	To await a response, first make sure the action's method is a coroutine and access the awaitable through
	an always-provided 'await_response` argument:

		@dispatch.add_action('status', params={'device': str})
		async def status_action(self, device: str, await_response: AwaitResponse):
			do_something()

			response = await await_response({'confirmation': bool})

			do_something
	"""

	reserved_params = {'await_response'}

	def __init__(self, common_params: Dict[str, Type]):
		self.actions = {}  # type: Dict[str, 'Action']

		self._verify_param_names(common_params)

		self.common_params = common_params

		self.loop_thread = None  # type: EventLoopThread

		self._await_responses = {}  # type: Dict[Tuple[str, uuid.UUID], 'AwaitResponse']
		self._active_sources = {}  # type: Dict[Tuple[str, object], 'AwaitResponse']
		self._lock = Lock()

		self._stopping = False

		self._exc_handler = None  # type: Callable[[object, str, Exception], None]

		logger.debug('Creating ResponseDispatcher')

	def start(self):
		if self.loop_thread is None:
			self.loop_thread = EventLoopThread()

		logger.debug('Spinning up loop thread')
		self.loop_thread.start()
		self.loop_thread.wait_result()

	def stop(self):
		self._stopping = True

		pending = self._await_responses.values()

		if pending:
			logger.info(f'Cancelling {len(pending)} pending coroutine actions')

			for await_response in pending:
				future = await_response.get_current_future()
				if future:
					future.cancel()

		logger.debug('Shutting down loop thread, and joining thread')
		self.loop_thread.shutdown_loop()
		self.loop_thread.join()

	def _set_await_response(self, source: object, key: Tuple[str, uuid.UUID], await_response: 'AwaitResponse'):
		with self._lock:
			self._await_responses[key] = await_response

			action, uuid = key

			action_key = (action, source)

			self._active_sources[action_key] = await_response

	def delete_await_response(self, key: Tuple[str, uuid.UUID]):
		with self._lock:
			await_response = self._await_responses[key]

			del self._await_responses[key]

			action, uuid = key

			action_key = (action, await_response.get_source())

			del self._active_sources[action_key]

			del await_response

	def _await_response_exists(self, key: Tuple[str, uuid.UUID]):
		with self._lock:
			return key in self._await_responses

	@classmethod
	def _verify_param_names(cls, params: Dict):
		disallowed = set(params.keys()) & cls.reserved_params

		if disallowed:
			raise Exception(f'Reserved parameters {disallowed!r} are not allowed')

	@classmethod
	def _verify_params(cls, action: str, args: Dict, params: Dict[str, Type]):
		for param, param_type in params.items():
			if param not in args:
				raise DispatchMissingArgumentError(f'Missing {param!r}')

			value = args.get(param)

			if not isinstance(value, param_type):
				raise DispatchArgumentError(
					f'Invalid type provided for action {action!r}: '
					f'{value!r} is not a {param_type}'
				)

	def add(self, action_name: str, action: 'Action'):
		if action_name in self.actions:
			raise Exception(f'Action {action_name!r} already exists in dispatch')

		self._verify_param_names(action.params)

		self.actions[action_name] = action

	def await_response(self, await_response: 'AwaitResponse', params: Dict[str, Type], timeout: float = None) -> asyncio.Future:
		"""
		Prepares AwaitResponse object and create a future to await on
		"""
		if self._stopping:
			raise DispatchStopping()

		future = self.loop_thread.event_loop.create_future()  # type: asyncio.Future
		await_response.set(future, params)

		logger.debug(f'Awaiting {await_response!r}')

		if timeout is not None:
			# Creating the timeout should be relatively safe. The only places where it can be cancelled is next dispatch
			# or cancel requests. Next dispatch must occur next event since this await_response itself is an event. Cancel
			async def async_timeout():
				await asyncio.sleep(timeout, loop=self.loop_thread.event_loop)
				future.set_exception(DispatchAsyncTimeout())
				logger.warning(f'Awaiting response {await_response} timed out in {timeout}')

			# TODO: Maybe we can possibly be sure that we can be safe if the dispatch class stops before
			# TODO: create_timeout_callback() is called by using the result of run_coroutine_threadsafe to cancel
			# TODO: in stop(). And if no stop is issued, the concurrent future can be swapped with asyncio's. They
			# TODO: both have .handle() methods, we could type that attribute as a Cancellabled or something.
			def create_timeout_callback():
				logger.debug(f'Also awaiting response with timeout of {timeout}')
				asyncio.sleep(timeout)
				timeout_future = asyncio.ensure_future(async_timeout(), loop=self.loop_thread.event_loop)
				await_response.set_timeout_future(timeout_future)

			self.loop_thread.call_soon_threadsafe(create_timeout_callback)

		return future

	def dispatch(self, source: object, action_name: str, args: Dict, response_id: str = None):
		"""
		Dispatches an action onto an instance given the name of the action and the arguments
		associated. And optionally provide a repsonse ID for coroutine awaiting actions.
		:param target: Instance to call dispatch actions on (instance of method)
		:param source: An object representing the source of dispatch, can be None for no source. A source is required
		for await responses cancel existing await responses.
		:param action_name: Action name
		:param args: Action arguments
		:param response_id:
		:return:
		"""
		action = self.actions.get(action_name)

		if action is None:
			raise DispatchNotFound()

		logger.debug(f'Dispatching for {action}')

		# If there is no response ID this is a new action
		if response_id is None:
			done_callback = None
			async_dispatch_callback = None

			# Verify parameters are correct
			self._verify_params(action_name, args, action.params)

			# Run as coroutine if action's func is a coroutine
			if action.is_coro:
				# If the action calls for an await_response, provide it as an argument
				if action.provide_await:

					# Before making a new AwaitResponse, check to see if this action is async-exclusive
					if action.exclusive_async:
						# If it is then we need to check for in-flight AwaitResponses and cancel them

						source_key = (action_name, source)

						pending_await = self._active_sources.get(source_key)

						if pending_await is not None:
							future = pending_await.get_current_future()
							if future:
								logger.info(f'Cancelling pending exclusive {action_name!r} action')
								# Mark as removed so done callback doesn't try to remove it again
								pending_await.mark_removed()
								# Immediately remove it now
								self.delete_await_response((action_name, pending_await.guid))

								def cancel_callback():
									# Cancel future and timeout if any
									future.cancel()
									pending_await.cancel_timeout()

								self.loop_thread.call_soon_threadsafe(cancel_callback)

					await_response = AwaitResponse(
						source=source,
						dispatcher=self,
						action=action_name,
						default_params=action.params
					)

					args['await_response'] = await_response

					key = (action_name, await_response.guid)

					# Prepare the AwaitResponse
					self._set_await_response(source, key, await_response)

					logger.debug(f'Action is coroutine, created AwaitResponse: {await_response}')

					# Callback to destroy AwaitResponse
					def remove_await_callback(future: asyncio.Future):
						logger.debug(f'Action coroutine completed, removing {await_response}')
						await_response.remove_and_cancel_timeout()

					done_callback = remove_await_callback

				async def async_dispatch_callback():
					await action.func(action.instance, **args)
			else:
				async def async_dispatch_callback():
					action.func(action.instance, **args)

			event_loop = self.loop_thread.event_loop

			async def dispatch_async():
				try:
					await async_dispatch_callback()

				except Exception as e:
					def exc_callback(exc):
						self._exc_handler(source, action_name, exc)

					event_loop.call_soon(exc_callback, e)

			fut = self.loop_thread.run_coroutine_threadsafe(dispatch_async())

			if done_callback:
				fut.add_done_callback(done_callback)

		# If this is a coroutine in progress, then continue its await_response
		elif action.is_coro and action.provide_await:

			# Try to parse given response id
			try:
				key = (action_name, uuid.UUID(response_id))
			except ValueError:
				raise DispatchError('Invalid response id')

			# Try to retrieve AwaitResponse

			await_response = self._await_responses.get(key)

			if await_response is None:
				raise DispatchError('Response session does not exist')

			# Make sure that a future was even set

			future = await_response.get_current_future()
			params = await_response.get_current_params()

			if not future or params is None:
				raise DispatchError('No response has been awaited')

			# TODO: When should the timeout cancel occur? Or better yet should the timeout be cancelled if
			# TODO: an exception rose in the dispatch?

			# Verify parameters and issue callback

			logger.debug(f'Dispatching continuation of {await_response}')

			self._verify_params(action_name, args, await_response.get_current_params())

			def set_future_result_callback():
				logger.debug(f'Fulfilled {await_response}')
				# Cancel timeout since no exceptions rose
				await_response.cancel_timeout()
				future.set_result(args)

			self.loop_thread.call_soon_threadsafe(set_future_result_callback)

		else:
			logger.error(f'UUID collision for {action!r}')
			raise DispatchError(
				'Somehow we may have UUID collision-ed with another action of same name but of differing'
				' synchronicity.'
			)

	def set_exc_handler(self, exc_handler: Callable[[object, str, Exception], None]):
		self._exc_handler = exc_handler

	def __call__(self, instance: object, exception_handler: Callable[[object, str, Exception], None]) -> 'ResponseDispatcher':
		"""
		Create a copy of the ResponseDispatch object. Useful since you don't
		want a single instance handling all instances that use the ResponseDispatch
		object.
		:return:
		"""
		new_dispatcher = ResponseDispatcher(common_params=self.common_params)
		new_dispatcher.actions = {name: action.clone() for name, action in self.actions.items()}

		for action in new_dispatcher.actions.values():
			action.instance = instance

		new_dispatcher.set_exc_handler(exception_handler)

		return new_dispatcher

	def add_dispatch(
		self,
		action_name: str,
		exclusive_async: bool = True,
		params: Dict[str, Type] = None,
		timeout: float = None,
	) -> Callable:
		"""
		Decorator to add actions from methods
		:param action_name:
		:param exclusive_async:
		:param params:
		:param timeout:
		:return:
		"""
		params = params or {}

		def decorate(func: Callable):
			# TODO: Use inspect.signature to auto-parameterize and verify
			if action_name in self.actions:
				raise Exception(f'Dispatched function {func!r} already exists')

			signature = inspect.signature(func)

			has_kwargs = len(list(filter(lambda param: param.kind.name == 'VAR_KEYWORD', signature.parameters.values()))) > 0

			is_coro = asyncio.iscoroutinefunction(func)

			if not has_kwargs:
				required = set(params.keys())
				provided = signature.parameters.keys()

				missing_params = list(required - provided)

				if missing_params:
					raise Exception('Callable {func!r} does not provide required params: {missing_params!r}')

				provide_await = is_coro and 'await_response' in provided
			else:
				provide_await = True

			action = Action(
				instance=None,
				func=func,
				params={**self.common_params, **params},
				is_coro=is_coro,
				provide_await=provide_await,
				exclusive_async=exclusive_async,
				timeout=timeout,
			)

			self.add(action_name, action)

			return func

		return decorate


class Action(object):
	def __init__(
		self,
		instance: object,
		func: Callable,
		params: Dict[str, Type],
		is_coro: bool,
		provide_await: bool,
		exclusive_async: bool = True,
		timeout: float = None,
	):
		self.instance = instance
		self.func = func
		self.params = params
		self.is_coro = is_coro
		self.provide_await = provide_await
		self.exclusive_async = exclusive_async
		self.timeout = timeout

	def clone(self):
		return Action(
			instance=self.instance,
			func=self.func,
			params=self.params,
			is_coro=self.is_coro,
			provide_await=self.provide_await,
			exclusive_async=self.exclusive_async,
			timeout=self.timeout
		)

	def __repr__(self):
		return f'Action(func: {self.instance.__class__.__name__}.{self.func.__name__})'

	def __str__(self):
		return repr(self)

class AwaitResponse(object):
	"""
	A callable object that returns a future for awaiting for responses in a single action.
	"""
	def __init__(
		self,
		dispatcher: 'ResponseDispatcher',
		source: object,
		action: str,
		default_params: Dict[str, Type],
	):
		self._dispatcher = dispatcher
		self._source = source
		self._action = action
		self._default = default_params
		self._current_future = None  # type: asyncio.Future
		self._current_params = None  # type: Dict[str, Type]
		self._timeout_future = None  # type: asyncio.Future
		self.guid = uuid.uuid4()

		self.removed = False

	def mark_removed(self):
		self.removed = True

	def remove_and_cancel_timeout(self):
		if self.removed:
			logger.info('AwaitResponse already removed')
			return

		self._dispatcher.delete_await_response((self._action, self.guid))
		self.mark_removed()

		self.cancel_timeout()

	def cancel_timeout(self):
		if self._timeout_future:
			self._timeout_future.cancel()
			self._timeout_future = None

	def set_timeout_future(self, future):
		self._timeout_future = future

	def set(self, future: asyncio.Future, params: Dict[str, Type]):
		self._current_future = future
		self._current_params = params

	def get_current_future(self) -> asyncio.Future:
		return self._current_future

	def get_current_params(self) -> Dict[str, Type]:
		return self._current_params

	def get_source(self):
		return self._source

	def __call__(self, params=None, timeout: float = None):
		params = self._default if params is None else params
		return self._dispatcher.await_response(self, params=params, timeout=timeout)

	def __repr__(self):
		return f'AwaitResponse(action: {self._action!r}, guid: {self.guid}, param: {self._current_params!r})'

	def __str__(self):
		return repr(self)


class DispatchError(Exception):
	pass


class DispatchAsyncTimeout(Exception):
	pass


class DispatchNotFound(DispatchError):
	pass


class DispatchStopping(DispatchError):
	pass


class DispatchArgumentError(DispatchError):
	def __init__(self, argument_name: str, *args, **kwargs):
		super(DispatchArgumentError, self).__init__(*args, **kwargs)

		self.argument_name = argument_name


class DispatchMissingArgumentError(DispatchArgumentError):
	def __init__(self, argument_name: str, *args, **kwargs):
		super(DispatchArgumentError, self).__init__(argument_name=argument_name, *args, **kwargs)


