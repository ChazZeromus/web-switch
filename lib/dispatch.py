from typing import Callable, Dict, Type, Tuple, NewType
from threading import Lock
import asyncio
import uuid
from lib.event_loop import EventLoopThread
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
				self.dispatcher = self.dispatch()
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
		self.actions = {}  # type: Dict[str, Tuple[Callable, Dict[str, Type]]]

		self._verify_param_names(common_params)

		self.common_params = common_params

		self.loop_thread = EventLoopThread()

		self._response_futures = {}  # type: Dict[Tuple[str, uuid.UUID], 'AwaitResponse']
		self._lock = Lock()

		self._stopping = False

		logger.debug('Creating ResponseDispatcher')

	def start(self):
		logger.debug('Spinning up loop thread')
		self.loop_thread.start()
		self.loop_thread.wait_result()

	def stop(self):
		self._stopping = True

		pending = self._response_futures.values()

		if pending:
			logger.info(f'Raising DispatchStopping to {len(pending)} pending coroutine actions')

			for await_response in pending:
				future = await_response.get_current_future()
				if future:
					future.set_exception(DispatchStopping())

		logger.debug('Shutting down loop thread, and joining thread')
		self.loop_thread.shutdown_loop()
		self.loop_thread.join()

	def _set_future(self, key: Tuple[str, uuid.UUID], await_response: 'AwaitResponse'):
		with self._lock:
			self._response_futures[key] = await_response

	def _delete_future(self, key: Tuple[str, uuid.UUID]):
		with self._lock:
			del self._response_futures[key]

	def _future_exists(self, key: Tuple[str, uuid.UUID]):
		with self._lock:
			return key in self._response_futures

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

	def add(self, action: str, func: Callable, params: Dict[str, Type]):
		if action in self.actions:
			raise Exception(f'Action {action!r} already exists in dispatch')

		self._verify_param_names(params)

		self.actions[action] = (func, {**self.common_params, **params})

	def await_response(self, await_response: 'AwaitResponse', params: Dict[str, Type]) -> asyncio.Future:
		"""
		Prepares AwaitResponse object and create a future to await on
		"""
		if self._stopping:
			raise DispatchStopping()

		future = self.loop_thread.event_loop.create_future()
		await_response.set(future, params)

		logger.debug(f'Awaiting {await_response!r}')

		return future

	def dispatch(self, instance: object, action: str, args: Dict, response_id: str = None):
		"""
		Dispatches an action onto an instance given the name of the action and the arguments
		associated. And optionally provide a repsonse ID for coroutine actions.
		:param instance:
		:param action:
		:param args:
		:param response_id:
		:return:
		"""
		entry = self.actions.get(action)

		if entry is None:
			raise DispatchNotFound()

		func, params = entry

		logger.debug(f'Dispatching for {func}')

		if response_id is None:
			self._verify_params(action, args, params)

			if asyncio.iscoroutinefunction(func):
				await_response = AwaitResponse(dispatcher=self, action=action, default_params=params)

				args['await_response'] = await_response

				key = (action, await_response.guid)

				self._set_future(key, await_response)

				logger.debug(f'Action is coroutine, created AwaitResponse: {await_response}')

				ret = func(instance, **args)

				def remove_future_callback(future):
					logger.debug(f'Action coroutine completed, removing {await_response}')
					self._delete_future(key)

				self.loop_thread.run_coroutine_threadsafe(ret)\
					.add_done_callback(remove_future_callback)
			else:
				func(instance, **args)
		else:
			try:
				key = (action, uuid.UUID(response_id))
			except ValueError:
				raise DispatchError('Invalid response id')

			await_response = self._response_futures.get(key)

			if await_response is None:
				raise DispatchError('Response session does not exist')

			future = await_response.get_current_future()
			params = await_response.get_current_params()

			if not future or params is None:
				raise DispatchError('No response has been awaited')

			logger.debug(f'Dispatching continuation of {await_response}')

			self._verify_params(action, args, await_response.get_current_params())

			def set_future_callback():
				logger.debug(f'Fulfilled {await_response}')
				future.set_result(args)

			self.loop_thread.call_soon_threadsafe(set_future_callback)

	def __call__(self) -> 'ResponseDispatcher':
		"""
		Create a copy of the ResponseDispatch object. Useful since you don't
		want a single instance handling all instances that use the ResponseDispatch
		object.
		:return:
		"""
		new_dispatcher = ResponseDispatcher(common_params=self.common_params)
		new_dispatcher.actions = self.actions

		return new_dispatcher

	def add_dispatch(self, action: str, params: Dict[str, Type] = None) -> Callable:
		"""
		Decorator to add actions from methods
		:param action:
		:param params:
		:return:
		"""
		def decorate(func: Callable):
			# TODO: Use inspect.signature to auto-parameterize and verify
			# TODO: Also enforce await_response parameter if coroutine
			if action in self.actions:
				raise Exception(f'Dispatched function {func!r} already exists')

			self.add(action, func, params or {})

			return func

		return decorate


class AwaitResponse(object):
	"""
	A callable object that returns a future for awaiting for responses in a single action.
	"""
	def __init__(self, dispatcher: 'ResponseDispatcher', action: str, default_params: Dict[str, Type]):
		self._dispatcher = dispatcher
		self._action = action
		self._default = default_params
		self._current_future = None  # type: asyncio.Future
		self._current_params = None  # type: Dict[str, Type]
		self.guid = uuid.uuid4()

	def set(self, future: asyncio.Future, params: Dict[str, Type]):
		self._current_future = future
		self._current_params = params

	def get_current_future(self) -> asyncio.Future:
		return self._current_future

	def get_current_params(self) -> Dict[str, Type]:
		return self._current_params

	def __call__(self, params=None):
		params = self._default if params is None else params
		return self._dispatcher.await_response(self, params)

	def __repr__(self):
		return f'AwaitResponse(action: {self._action!r}, guid: {self.guid}, param: {self._current_params!r})'

	def __str__(self):
		return repr(self)


class DispatchError(Exception):
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


