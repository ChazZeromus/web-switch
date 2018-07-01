from typing import Callable, Dict, List, Type, Tuple, Optional, Union, NamedTuple, Iterable, Any, Set, Coroutine
from threading import Lock
import asyncio
import uuid
import inspect
import re
import logging
import itertools
from copy import deepcopy
from concurrent.futures import TimeoutError as ConcurrentTimeoutError

from lib.event_loop import EventLoopThread
from lib.logger import g_logger


# TODO: In the case where an instance of the user sends a dispatch way too early and already receives a response
# TODO: but ends up missing it because it asks for it too late, maybe make a message queue for AwaitDispatches?
# TODO: And also make that queue be able to expire? But then we'd have to keep track of completed action GUIDs
# TODO: so we know a client is sending data to an expired conversation.


class ParameterSet(NamedTuple):
	"""
	NamedTuple that defines an action's exposed parameters (ones that provided at time of dispatch)
	and an action's intrinsic parameters (given to the action by the user of ResponseDispatcher
	through argument hooks)
	"""
	exposed: Dict[str, Type] = dict()
	intrinsic: Dict[str, Type] = dict()

	def all(self):
		return {**self.exposed, **self.intrinsic}

	def __add__(self, other: Tuple):
		if isinstance(other, ParameterSet):
			return ParameterSet(
				exposed={**self.exposed, **other.exposed},
				intrinsic={**self.intrinsic, **other.intrinsic},
			)
		return super(other)

# TODO: When sending errors, provide the response_id if possible
# TODO: For coroutine actions, perhaps implement a sort of session heartbeat for possible
# TODO: long periods of waiting?


class ResponseDispatcher(object):
	"""
	Asyncio based message dispatcher. Classes can use the `add_action()` decorator to
	define actions to be automatically be dispatched when calling `dispatch()`

	Classes typically use ResponseDispatcher like so:

		class Foo:
			def __init__(self):
				self.dispatcher = self.dispatch(self, params={})
				self.dispatcher.start()

			def dispatching_method(self, data: Dict):
				self.dispatcher.dispatch(data['action'], data['data'])

			@add_action('status', params={'device': str})
			def status_action(self, device: str):
				do_something

	If the method of the action is a coroutine, the coroutine can await on responses
	that were dispatched for that particular action by using the callable that is always
	provided

	Since all dispatching occurs through one method `dispatch()`, routing which messages to what
	awaiting action is done by specifying an extra argument `response_id` to `dispatch()`. Applications
	just need to ensure clients must provide a response ID if they're responding to that particular action.

	To await a response, first make sure the action's method is a coroutine and access the awaitable through
	an always-provided 'await_dispatch` argument:

		@dispatch.add_action('status', params={'device': str})
		async def status_action(self, device: str, await_dispatch: AwaitDispatch):
			do_something()

			response = await await_dispatch({'confirmation': bool})

			do_something
	"""

	reserved_params = {'await_dispatch'}

	latest_id = 0

	def __init__(
		self,
		instance: object,
		common_params: ParameterSet,
		exception_handler: Callable[[object, str, Exception, Optional[uuid.UUID]], Coroutine],
		complete_handler: Optional[Callable[[object, str, Any, Optional[uuid.UUID]], None]] = None,
		argument_hook: Optional[Callable[[Dict, object, 'Action'], Dict]] = None,
		common_async_params: ParameterSet = ParameterSet(),
	) -> None:
		"""
		:param instance: Value of the 'self' parameter when dispatching actions.
		:param common_params: Common parameters that this dispatcher will include in addition to each action's individual
		params for non-asynchronous actions
		:param exception_handler: Handler to call when an a dispatched action raises an exception.
		:param argument_hook: Hook to call to modify arguments before finally dispatching action. Useful to modify
		things like AwaitDispatch object.
		:param common_async_params: Common parameters that this dispatcher will include in addition to each action's individual
		params for asynchronous actions
		"""
		ResponseDispatcher.latest_id += 1
		self.id = ResponseDispatcher.latest_id

		self.instance = instance

		# Verify params
		self._validate_param_set(common_params)
		self._validate_param_set(common_async_params)

		self.async_params = common_async_params
		self.non_async_params = common_params

		self.loop_thread = None  # type: EventLoopThread

		self._active_ads = {}  # type: Dict[Tuple[str, uuid.UUID], 'AbstractAwaitDispatch']
		self._active_ads_per_source = {}  # type: Dict[Tuple[str, object], 'AbstractAwaitDispatch']
		self._active_dispatches = set()  # type: Set[asyncio.Future]
		self._lock = Lock()

		self._stopping = False
		self._stopped = False

		self._exc_handler = exception_handler
		self._complete_handler = complete_handler

		self._arg_hook = argument_hook

		self.actions = {}  # type: Dict[str, 'Action']

		self.logger = g_logger.getChild(f'ResponseDispatch:{self.id}')
		self.logger.debug(f'Creating {self!r}')

		self._build_actions()

	def __repr__(self):
		params = ','.join([str(self.non_async_params), str(self.async_params)])
		return f'ResponseDispatch(id:{self.id}, params:({params}))'

	def __str__(self):
		return repr(self)

	def start(self):
		if self.loop_thread is None:
			self.loop_thread = EventLoopThread()

		self.logger.debug('Spinning up loop thread')
		self.loop_thread.start()
		self.loop_thread.wait_result()

	def stop(self, timeout: float = None):
		if self._stopping:
			self.logger.warning('Stop dispatcher already requested')
			return

		self.logger.debug(f'Stop dispatcher requested with timeout of {timeout}')
		self._stopping = True

		# Cancel any awaiting dispatches
		pending_awaits = []

		for provider in self._active_ads.values():
			future = provider.get_await_dispatch().get_current_future()
			if future:
				pending_awaits.append(future)

			self.logger.info(
				f'Stop requested with {len(pending_awaits)} awaits and {len(self._active_dispatches)} actions'
				f' still active.'
			)

		if pending_awaits or self._active_dispatches:
			if timeout is None or timeout > 0:
				self.logger.info(f'Waiting {timeout} seconds for AwaitDispatches and actions to finish')

				async def gather():
					await asyncio.gather(
						*itertools.chain(pending_awaits, self._active_dispatches),
						loop=self.loop_thread.event_loop,
						return_exceptions=True,
					)

				result = self.loop_thread.run_coroutine_threadsafe(gather())

				try:
					result.result(timeout)
				except ConcurrentTimeoutError:
					self.logger.debug('Took too long to finish, cancelling remaining AwaitDispatches')

				if result.done():
					self.logger.debug('All AwaitDispatches finished')
					pending_awaits.clear()

					if self._active_dispatches:
						self.logger.warning(f'Waited for all actions but {len(self._active_dispatches)} remain')
					else:
						self.logger.info('All active dispatch actions finished')
		else:
			self.logger.info('All AwaitDispatches and actions are already completed')

		self._stopped = True

		if pending_awaits:
			self.logger.info(f'Cancelling {len(pending_awaits)} pending AwaitDispatches')
			for future in pending_awaits:
				future.cancel()

		if self._active_dispatches:
			self.logger.info(f'Cancelling {len(self._active_dispatches)} actions')
			for future in self._active_dispatches:
				future.cancel()

		self.logger.debug('Shutting down loop thread, and joining thread')
		self.loop_thread.shutdown_loop()
		self.loop_thread.join()

	def _build_actions(self):
		self.actions.clear()

		# Go through each action-having method and clone it to this ResponseDispatcher
		for name, attr in inspect.getmembers(self.instance, lambda m: hasattr(m, Action.func_attr_tag)):
			orig_action = getattr(attr, Action.func_attr_tag)  # type: Action

			if orig_action.name in self.actions:
				raise Exception(f'Dispatched function {orig_action.name!r} already exists')

			# Combine both exposed and intrinsic
			param_set = self.async_params if orig_action.is_coro else self.non_async_params

			# Verify that action params do not conflict with ResponseDispatcher's common params

			combined_action_params = param_set.all()

			conflicting = list(set(combined_action_params) & set(orig_action.params))

			if conflicting:
				raise Exception(f'Action defined params {conflicting} conflict with ResponseDispatcher common params')

			combined_params = {
				**combined_action_params,
				**orig_action.params,
			}

			missing_params = list(set(combined_params.keys()) - set(orig_action.get_func_params()))

			if missing_params:
				raise Exception(
					f'Could not init ResponseDispatcher due to missing arguments {missing_params}'
					f' in action {action.name!r}'
				)

			action = orig_action.clone()

			action.instance = self.instance
			action.params = combined_params
			# Combine common intrinsics to specific action's intrinsics'
			action.intrinsic_params = set(action.intrinsic_params) | set(param_set.intrinsic.keys())

			self.actions[action.name] = action

	def _set_await_dispatch(self, source: object, key: Tuple[str, uuid.UUID], provider: 'AbstractAwaitDispatch'):
		with self._lock:
			self._active_ads[key] = provider

			action, uuid_ = key

			action_key = (action, source)

			self._active_ads_per_source[action_key] = provider

	# This is public because AwaitDispatch uses it
	def delete_await_dispatch(self, key: Tuple[str, uuid.UUID]):
		with self._lock:
			provider = self._active_ads[key]

			del self._active_ads[key]

			action, uuid_ = key

			action_key = (action, provider.get_await_dispatch().source)

			del self._active_ads_per_source[action_key]

			del provider

	def _get_await_dispatch(self, key: Tuple[str, uuid.UUID]):
		with self._lock:
			return self._active_ads.get(key)

	@classmethod
	def _validate_param_set(cls, param_set: ParameterSet):
		conflicting = list(set(param_set.intrinsic.keys()) & set(param_set.exposed.keys()))

		if conflicting:
			raise Exception(f'Param set {param_set!r} has conflicting params {conflicting}')

		disallowed = (set(param_set.exposed.keys()) | set(param_set.intrinsic.keys())) & cls.reserved_params

		if disallowed:
			raise Exception(f'Reserved parameters {disallowed!r} are not allowed to be defined parameters')

	@classmethod
	def _verify_exposed_arguments(cls, action: 'Action', args: Dict):
		"""
		Verifies that the given arguments are valid for the action without taking into account any intrinsics
		and type checking.
		"""
		invalid_args = list(set(action.intrinsic_params or []) & set(args.keys()))

		if invalid_args:
			raise DispatchArgumentError(f'Specified intrinsic parameters: {invalid_args}')

		exposed_param_names = set(action.get_exposed_params().keys())

		missing_params = list(exposed_param_names - set(args.keys()))

		if missing_params:
			raise DispatchMissingArgumentError(f'Missing arguments for parameter {missing_params}')

	@classmethod
	def _verify_full_arguments(cls, action_name: str, params: Dict[str, Type], args: Dict):
		"""
		Verifies that the given arguments are ready to be dispatched and valid for the given parameters and types.
		"""

		missing_params = list(set(params.keys()) - set(args.keys()))

		if missing_params:
			raise DispatchMissingArgumentError(f'Missing arguments for parameter {missing_params}')

		for param, param_type in params.items():
			value = args.get(param)

			if not isinstance(value, param_type):
				raise DispatchArgumentError(
					f'Invalid type provided for action {action_name!r}: '
					f'{value!r} is not a {param_type}'
				)

	def await_dispatch(
			self,
			await_dispatch: 'AwaitDispatch',
			params: Dict[str, Type],
			timeout: float = None,
	) -> asyncio.Future:
		"""
		Prepares AwaitDispatch object and create a future to await on
		"""
		if self._stopping:
			raise DispatchStopping()

		future = self.loop_thread.event_loop.create_future()  # type: asyncio.Future
		await_dispatch.set(future, params)

		self.logger.debug(f'Awaiting {await_dispatch!r}')

		if timeout is not None:
			# Creating the timeout should be relatively safe. The only places where it can be cancelled is next dispatch
			# or cancel requests. Next dispatch must occur next event since this await_dispatch itself is an event. Cancel
			async def async_timeout():
				await asyncio.sleep(timeout, loop=self.loop_thread.event_loop)
				future.set_exception(DispatchAwaitTimeout())
				self.logger.warning(f'Awaiting response {await_dispatch} timed out in {timeout}')

			# TODO: Maybe we can possibly be sure that we can be safe if the dispatch class stops before
			# TODO: create_timeout_callback() is called by using the result of run_coroutine_threadsafe to cancel
			# TODO: in stop(). And if no stop is issued, the concurrent future can be swapped with asyncio's. They
			# TODO: both have .handle() methods, we could type that attribute as a Cancellabled or something.
			def create_timeout_callback():
				self.logger.debug(f'Also awaiting response with timeout of {timeout}')
				asyncio.sleep(timeout)
				timeout_future = asyncio.ensure_future(async_timeout(), loop=self.loop_thread.event_loop)
				await_dispatch.set_timeout_future(timeout_future)

			self.loop_thread.call_soon_threadsafe(create_timeout_callback)

		return future

	def _ensure_exclusive(self, action: 'Action', source: object):
		"""
		Given an action and a source, if the action is exclusive cancel any pending actions that are
		awaiting on an AwaitDispatch.
		:param action:
		:param source:
		:return:
		"""
		if action.exclusive_async:
			# If it is then we need to check for in-flight DispatchResponses for the current action and source
			# and cancel them

			source_key = (action.name, source)

			provider = self._active_ads_per_source.get(source_key)

			if provider is not None:
				pending_await = provider.get_await_dispatch()

				future = pending_await.get_current_future()

				if future:
					self.logger.info(f'Cancelling pending exclusive {action.name!r} action')
					# Mark as removed so done callback doesn't try to remove it again
					pending_await.mark_removed()
					# Immediately remove it now
					self.delete_await_dispatch((action.name, pending_await.guid))

					def cancel_callback():
						# Cancel future and timeout if any
						future.cancel()
						pending_await.cancel_timeout()

					self.loop_thread.call_soon_threadsafe(cancel_callback)

	def dispatch(self, source: object, action_name: str, args: Dict, response_id: Union[str, uuid.UUID] = None):
		"""
		Dispatches an action onto an instance given the name of the action and the arguments
		associated. And optionally provide a response ID for coroutine awaiting actions.
		:param source: An object representing the source of dispatch, can be None for no source. A source is required
		for actions to cancel existing await dispatches of async_exclusive actions.
		:param action_name: Action name
		:param args: Action arguments
		:param response_id: Guid of response session to reply to, if any
		:return:
		"""
		# Allow if we're _stopping in case stop() is waiting
		if self._stopped:
			raise DispatchStopping()

		action = self.actions.get(action_name)

		if action is None:
			raise DispatchNotFound()

		self.logger.debug(f'Dispatching for {action}')

		# If no response ID is provided then generate one.
		if response_id is None:
			response_guid = uuid.uuid4()

			# Since we had to generate one, then this definitely a new dispatch
			existing_ad = None
		else:
			# If one was given, attempt to parse it
			try:
				response_guid = uuid.UUID(str(response_id))
			except ValueError:
				raise DispatchError('Invalid response id')

			dispatch_key = (action_name, response_guid)
			existing_ad = self._get_await_dispatch(dispatch_key)

		# If there is no existing_ad, then this is a new action
		if not existing_ad:
			cleanup_callback = None  # type: Optional[Callable, None]
			async_dispatch_callback = None  # type: Optional[Callable[[Iterable], Any]]
			await_dispatch = None  # type: Optional['AwaitDispatch']

			# Verify parameters are correct, but not with type checking as there is a possibility
			# of argument hooks returning correct parameter types later
			self._verify_exposed_arguments(action, args)

			# Run as coroutine if action's func is a coroutine
			if action.is_coro:
				self._ensure_exclusive(action, source)

				await_dispatch = AwaitDispatch(
					source=source,
					dispatcher=self,
					action_name=action_name,
					default_params=action.get_exposed_params(),
					guid=response_guid,
				)

				args['await_dispatch'] = await_dispatch

				# Prepare the AwaitDispatch
				self._set_await_dispatch(source, (action_name, await_dispatch.guid), await_dispatch)

				self.logger.debug(f'Action is coroutine, created AwaitDispatch: {await_dispatch}')

				# Callback to destroy AwaitDispatch
				def remove_await_callback():
					self.logger.debug(f'Action coroutine completed, removing {await_dispatch}')
					await_dispatch.remove_and_cancel_timeout()

				cleanup_callback = remove_await_callback

				async def coro_dispatch(dispatch_args) -> Any:
					return await action.func(action.instance, **dispatch_args)

				async_dispatch_callback = coro_dispatch
			else:
				async def call_dispatch(dispatch_args) -> Any:
					return action.func(action.instance, **dispatch_args)

				async_dispatch_callback = call_dispatch

			# Co-routine to run regardless of synchronocity
			async def dispatch_async():
				dispatch_future = None

				try:
					# If a hook is defined, run it and get new arguments
					if self._arg_hook:
						passed_args = self._arg_hook(args, source, action)
					else:
						passed_args = args

					# Verify new arguments with type checking
					self._verify_full_arguments(action.name, action.params, passed_args)

					# Strip out only what isn't needed if action has no kwargs
					if not action.has_kwargs:
						passed_args = {k: passed_args[k] for k in passed_args.keys() & action.params.keys()}

					dispatch_future = asyncio.ensure_future(async_dispatch_callback(passed_args), loop=self.loop_thread.event_loop)
					self._active_dispatches.add(dispatch_future)

					return await dispatch_future

				except Exception as exc:
					# Call exception handler if something happened
					try:
						if asyncio.iscoroutinefunction(self._exc_handler):
							await self._exc_handler(source, action_name, exc, response_guid)
						else:
							self._exc_handler(source, action_name, exc, response_guid)

					except Exception as exc_exc:
						self.logger.error(f'Unexpected error while running exception handler: {exc_exc}')

					# Raise so future does not invoke complete handler
					raise

				finally:
					if dispatch_future:
						self._active_dispatches.remove(dispatch_future)

			def done_callback(done_future: asyncio.Future):
				# Call cleanup handler if any
				if cleanup_callback:
					cleanup_callback()

				# Call done handler if done
				if self._complete_handler and done_future.done() and not done_future.cancelled():
					# Make sure handler is called in our event loop
					# TODO: Do we need to do this event loop deferring elsewhere?
					self.loop_thread.call_soon_threadsafe(
						self._complete_handler,
						source,
						action_name,
						done_future.result(),
						response_guid
					)

			fut = self.loop_thread.run_coroutine_threadsafe(dispatch_async())

			if cleanup_callback or self._complete_handler:
				fut.add_done_callback(done_callback)

		# If this is a coroutine in progress, then continue its await_dispatch
		elif action.is_coro:
			# Make sure that a future was even set
			future = existing_ad.get_current_future()
			params = existing_ad.get_current_params()

			if not future or params is None:
				raise DispatchError('No response has been awaited')

			# TODO: When should the timeout cancel occur? Or better yet should the timeout be cancelled if
			# TODO: an exception rose in the dispatch?

			# Verify parameters and issue callback

			self.logger.debug(f'Dispatching continuation of {existing_ad}')

			# TODO: A continuation dispatch will default to
			self._verify_full_arguments(
				action_name=action_name,
				params=existing_ad.get_await_dispatch().get_current_params(),
				args=args,
			)

			def set_future_result_callback():
				self.logger.debug(f'Fulfilled {existing_ad}')
				# Cancel timeout since no exceptions arose
				existing_ad.cancel_timeout()
				future.set_result(args)

			self.loop_thread.call_soon_threadsafe(set_future_result_callback)

		else:
			self.logger.error(f'UUID collision for {action!r}')
			raise DispatchError(
				'Somehow we may have UUID collision-ed with another action of same name but of differing'
				' synchronicity.'
			)


def add_action(
	action_name: str = None,
	exclusive_async: bool = True,
	params: Dict[str, Type] = None,
	intrinsic_params: Iterable[str] = (),
	timeout: float = None,
) -> Callable:
	"""
	Decorator to add actions from methods. This decorator MUST be called with
	an argument list, even if empty.
	:param action_name: Specify the name of the action that the client can invoke. If not specified
	then the action_name is extracted from the method name as 'X_action'
	:param exclusive_async: Whether or not this action is exclusively asynchronous per source. Otherwise
	a single client can dispatch an unlimited amount of this asynchronous action that may or may not complete.
	:param params: Dict of parameter name strings to types
	:param intrinsic_params: Iterable of parameter name strings specified in `params` that are intrinsic. Meaning
	these parameters are given by the user of the ResponseDispatcher instance and not directly through `dispatch()`.
	:param timeout:
	:return:
	"""

	def decorator(func: Callable):
		nonlocal params, action_name

		params = params or {}

		missing_intrinsics = list(set(intrinsic_params) - set(params.keys()))

		if missing_intrinsics:
			raise Exception(
				f"Intrinsic parameter names {missing_intrinsics} do not refer to any params of action {action_name}'s"
				f" decorator"
			)

		params = params or {}

		if action_name is None:
			match = re.match('^action_(?P<name>.+)$', func.__name__)

			if not match:
				raise Exception(
					"Could not automatically discern action name, or at least"
					"no the form of 'action_XXX'"
				)

			match = match.groupdict()

			action_name = match['name']

		func_parameters = inspect.signature(func).parameters
		provided = list(func_parameters.keys())
		has_kwargs = len(list(filter(lambda param: param.kind.name == 'VAR_KEYWORD', func_parameters.values()))) > 0

		is_coro = asyncio.iscoroutinefunction(func)

		# kwargs is a catch all so don't be strict about parameters if they have it
		if not has_kwargs:
			required = set(params.keys())
			missing_params = list(required - set(provided))

			if missing_params:
				raise Exception('Callable {func!r} does not provide required params: {missing_params!r}')

		action = Action(
			instance=None,
			name=action_name,
			func=func,
			func_params=provided,
			params=params,
			intrinsic_params=list(intrinsic_params),
			is_coro=is_coro,
			has_kwargs=has_kwargs,
			exclusive_async=exclusive_async,
			timeout=timeout,
		)

		setattr(func, Action.func_attr_tag, action)

		return func

	return decorator


class Action(object):
	"""
	Metadata generated by `add_action` generator. This metadata is eventually cloned and common params
	from ResponseDispatcher are added to the clone.
	"""
	func_attr_tag = '__dispatch_action'

	def __init__(
		self,
		instance: object = None,
		name: str = None,
		func: Callable = None,
		func_params: List[str] = None,
		params: Dict[str, Type] = None,
		intrinsic_params: List[str] = None,
		is_coro: bool = None,
		has_kwargs: bool = None,
		exclusive_async: bool = None,
		timeout: float = None,
	):
		self.instance = instance
		self.name = name
		self.func = func
		self.params = params
		self.intrinsic_params = intrinsic_params
		self.is_coro = is_coro
		self.has_kwargs = has_kwargs
		self.exclusive_async = exclusive_async
		self.timeout = timeout
		self.func_params = func_params

	def get_exposed_params(self) -> Dict[str, Type]:
		return {param: self.params[param] for param in (set(self.params.keys()) - set(self.intrinsic_params))}

	def clone(self) -> 'Action':
		copy = Action()
		copy.__dict__ = deepcopy(self.__dict__)

		return copy

	def get_func_params(self) -> List[str]:
		return self.func_params

	def __repr__(self):
		return f'Action(func: {self.instance.__class__.__name__}.{self.func.__name__})'

	def __str__(self):
		return repr(self)


class AbstractAwaitDispatch(object):
	def get_await_dispatch(self) -> 'AwaitDispatch':
		raise NotImplemented()

	def __call__(self, params: Dict = None, timeout: float = None) -> asyncio.Future:
		return self.get_await_dispatch()(params=params, timeout=timeout)


class AwaitDispatch(AbstractAwaitDispatch):
	"""
	A callable object that returns a future for awaiting for responses in a single action.
	"""
	def __init__(
		self,
		dispatcher: 'ResponseDispatcher',
		source: object,
		action_name: str,
		default_params: Dict[str, Type],
		guid: uuid.UUID,
	):
		self.dispatcher = dispatcher
		self.source = source
		self.action_name = action_name
		self.default_params = default_params
		self._current_future = None  # type: asyncio.Future
		self._current_params = None  # type: Dict[str, Type]
		self._timeout_future = None  # type: asyncio.Future
		self.guid = guid

		self.removed = False

	def mark_removed(self):
		self.removed = True

	def remove_and_cancel_timeout(self):
		if self.removed:
			self.dispatcher.logger.info('AwaitDispatch already removed')
			return

		self.dispatcher.delete_await_dispatch((self.action_name, self.guid))
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

	def __call__(self, params: Dict =None, timeout: float = None) -> asyncio.Future:
		params = self.default_params if params is None else params
		return self.dispatcher.await_dispatch(self, params=params, timeout=timeout)

	def __repr__(self):
		return f'AwaitDispatch(action: {self.action_name!r}, guid: {self.guid}, param: {self._current_params!r})'

	def __str__(self):
		return repr(self)

	def get_await_dispatch(self) -> 'AwaitDispatch':
		return self


class DispatchError(Exception):
	def __str__(self):
		return super(DispatchError, self).__str__() if self.args else self.__class__.__name__


class DispatchAwaitTimeout(DispatchError):
	pass


class DispatchNotFound(DispatchError):
	pass


class DispatchStopping(DispatchError):
	pass


class DispatchArgumentError(DispatchError):
	def __init__(self, argument_name: str, *args, **kwargs):
		super(DispatchArgumentError, self).__init__(argument_name, *args, **kwargs)

		self.argument_name = argument_name


class DispatchMissingArgumentError(DispatchArgumentError):
	def __init__(self, argument_name: str, *args, **kwargs):
		super(DispatchMissingArgumentError, self).__init__(argument_name=argument_name, *args, **kwargs)
