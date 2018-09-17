
from typing import Dict, Any, Optional, Callable, List, Type, Iterable, Union, Coroutine, NamedTuple, TypeVar
from threading import Lock
import asyncio
import uuid
import inspect
import re
import itertools
import functools
from copy import deepcopy
from concurrent.futures import TimeoutError as ConcurrentTimeoutError

from .event_loop import EventLoopManager
from .index_map import IndexMap
from .logger import g_logger


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

	def all(self) -> Dict[str, Type]:
		return {**self.exposed, **self.intrinsic}

	# def __add__(self, other: Tuple):
	# 	if isinstance(other, ParameterSet):
	# 		return ParameterSet(
	# 			exposed={**self.exposed, **other.exposed},
	# 			intrinsic={**self.intrinsic, **other.intrinsic},
	# 		)
	# 	return super(other)


class ActiveAction(object):
	def __init__(
		self,
		action: 'Action',
		provider: Optional['AbstractAwaitDispatch'] = None,
		action_future: Optional[asyncio.Future] = None,
	) -> None:
		self.action: 'Action' = action
		self.provider: Optional['AbstractAwaitDispatch'] = provider
		self.action_future: Optional[asyncio.Future] = action_future

	def get_ad_future(self) -> Optional[asyncio.Future]:
		if not self.provider:
			return None

		return self.provider.get_await_dispatch().get_current_future()

	def cancel_all(self) -> None:
		future = self.get_ad_future()
		if future:
			future.cancel()

		if self.action_future:
			self.action_future.cancel()


# TODO: For coroutine actions, perhaps implement a sort of session heartbeat for possible
# TODO: long periods of waiting?


class ResponseDispatcher(object):
	"""
	Asyncio based message dispatcher. Classes can use the `add_action()` decorator to
	define actions to be automatically be dispatched when calling `dispatch()`

	Classes typically use ResponseDispatcher like so:

		class Foo:
			def __init__(self):
				self.dispatcher = ResponseDispatcher(self)
				self.dispatcher.start()

			def dispatching_method(self, data: Dict):
				self.dispatcher.dispatch(data['action'], data['data'])

			@add_action('status', params={'device': str})
			def status_action(self, device: str):
				do_something

	If the method of the action is a coroutine, the coroutine can await on responses
	that were dispatched for that particular action by using the callable that is always
	provided

	Since all dispatching occurs through one method `dispatch()`, routing which messages to what instance of an
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
		common_params: ParameterSet = ParameterSet(),
		exception_handler: Optional[Callable[[object, str, Exception, uuid.UUID], Coroutine]] = None,
		complete_handler: Optional[Callable[[object, str, Any, uuid.UUID], None]] = None,
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

		self.event_manager: Optional[EventLoopManager] = None

		self._actives: IndexMap[ActiveAction] = IndexMap('guid', 'source', 'action')

		self._lock = Lock()

		self._stopping = False
		self._stopped = False

		self._exc_handler = exception_handler
		self._complete_handler = complete_handler

		self._arg_hook = argument_hook

		self.actions: Dict[str, 'Action'] = {}

		self.logger = g_logger.getChild(f'ResponseDispatch:{self.id}')
		self.logger.debug(f'Creating {self!r}')

		self._build_actions()

	def __repr__(self) -> str:
		params = ','.join([str(self.non_async_params), str(self.async_params)])
		return f'ResponseDispatch(id:{self.id}, params:({params}))'

	def __str__(self) -> str:
		return repr(self)

	def start(self) -> None:
		"""
		Start dispatch event loop. Will block until background event loop thread initializes.
		This method must be called before ResponseDispatch can perform its duties.
		"""
		if self.event_manager is None:
			self.event_manager = EventLoopManager()

		self.logger.debug('Spinning up loop thread')
		self.event_manager.start()
		self.event_manager.wait_result()

	def stop(self, timeout: Optional[float] = None) -> None:
		"""
		Stop dispatch event loop and wait for pending actions and their AwaitResponses for a specified timeout.
		If those actions and AwaitResponses are still waiting or executing when stop() times out, their futures
		will be cancelled and stop() will return.
		:param timeout:
		:return:
		"""
		if not self.event_manager:
			raise DispatchNotStarted()

		if self._stopping:
			self.logger.warning('Stop dispatcher already requested')
			return

		# TODO: This might be needed because of the done callbacks being called by scheduling onto the event loop
		# TODO: Maybe try to test for cases where stop() could be called sooner than done callbacks can finish?
		# timeout = timeout or 0.1

		self.logger.debug(f'Stop dispatcher requested with timeout of {timeout}')
		self._stopping = True

		# Cancel any awaiting dispatches
		pending_awaits: List[asyncio.Future] = []
		active_dispatches: List[asyncio.Future] = []

		for active in self._actives:
			future = active.get_ad_future()

			if future:
				pending_awaits.append(future)

			action_future = active.action_future
			if action_future:
				active_dispatches.append(action_future)

		self.logger.info(
			f'Stop requested with {len(pending_awaits)} awaits and {len(active_dispatches)} actions'
			f' still active.'
		)

		if pending_awaits or active_dispatches:
			if timeout is None or timeout > 0:
				self.logger.info(f'Waiting {timeout} seconds for AwaitDispatches and actions to finish')

				async def gather() -> None:
					assert self.event_manager is not None

					await asyncio.gather(
						*itertools.chain(pending_awaits, active_dispatches),
						loop=self.event_manager.event_loop,
						return_exceptions=True,
					)

				result = self.event_manager.run_coroutine_threadsafe(gather())

				try:
					result.result(timeout)
				except ConcurrentTimeoutError:
					self.logger.debug('Took too long to finish, cancelling remaining AwaitDispatches')

				if result.done():
					self.logger.debug('All AwaitDispatches finished')

					if self._actives:
						self.logger.warning(f'Waited for all actions but {len(self._actives)} remain')
					else:
						self.logger.info('All active dispatch actions finished')
		else:
			self.logger.info('All AwaitDispatches and actions are already completed')

		self._stopped = True

		if self._actives:
			self.logger.warning(f'Cancelling {len(self._actives)} active dispatches and their AwaitDispatches')

			for active in self._actives:
				active.cancel_all()

		self.logger.debug('Shutting down loop thread, and joining thread')
		self.event_manager.shutdown_loop()
		self.event_manager.join()
		self.event_manager = None

	def cancel_action_by_source(self, source: object) -> None:
		with self._lock:
			if self._stopped:
				self.logger.debug('Not cancelling action because dispatcher has already stopped')
				return

			if not self.event_manager:
				raise DispatchNotStarted()

			active_actions = self._actives.lookup(source=source)
			self.logger.info(f'Request for cancelling {len(active_actions)} active actions for source {source!r}')

			if not active_actions:
				return

			def callback() -> None:
				for active in active_actions:
					active.cancel_all()

			self.event_manager.call_soon_threadsafe(callback)

	def _build_actions(self) -> None:
		"""
		Called when ResponseDispatcher is initialized: Enumerate every attribute of `self.instance` that
		was decorated with add_action() and build the instance-local dispatch table for this ResponseDispatcher
		instance.
		:return:
		"""
		self.actions.clear()

		# Go through each action-having method and clone it to this ResponseDispatcher
		for name, attr in inspect.getmembers(self.instance, lambda m: hasattr(m, Action.func_attr_tag)):
			orig_action: Action = getattr(attr, Action.func_attr_tag)

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
					f' in action {orig_action.name!r}'
				)

			action: Action = orig_action.clone()

			action.instance = self.instance
			action.params = combined_params
			# Combine common intrinsics to specific action's intrinsics'
			action.intrinsic_params = list(set(action.intrinsic_params) | set(param_set.intrinsic.keys()))

			self.actions[action.name] = action

	def _add_active_action(
		self,
		source: object,
		action: 'Action',
		provider: Optional['AbstractAwaitDispatch'],
	) -> ActiveAction:
		"""
		Prepares an AwaitDispatch object so that the action that created it can `await` its results. The AwaitDispatch's
		future's result is set when a dispatch() occurs for that particular action response-id.
		:param source: The source object of the AwaitResponse object.
		:param action:  Action to create an AwaitDispatch for
		:param provider: The provider of the AwaitDispatch object
		"""
		with self._lock:
			active = ActiveAction(action=action, provider=provider)

			self._actives.add(
				active,
				guid=provider.get_await_dispatch().guid if provider else None,
				source=source,
				action=action.name,
			)

			return active

	# This is public because AwaitDispatch uses it
	def remove_active_by_action(self, action: str, guid: Optional[uuid.UUID]) -> None:
		with self._lock:
			active_action = self._actives.lookup_one(action=action, guid=guid)
			self._actives.remove(active_action)

	def remove_active_action(self, active_action: ActiveAction) -> None:
		with self._lock:
			self._actives.remove(active_action)

	def _try_get_await_dispatch(self, action: str, guid: Optional[uuid.UUID]) -> Optional['AwaitDispatch']:
		with self._lock:
			active_action = self._actives.try_lookup_one(action=action, guid=guid)

			if not active_action or not active_action.provider:
				return None

			return active_action.provider.get_await_dispatch()

	@classmethod
	def _validate_param_set(cls, param_set: ParameterSet) -> None:
		conflicting = list(set(param_set.intrinsic.keys()) & set(param_set.exposed.keys()))

		if conflicting:
			raise Exception(f'Param set {param_set!r} has conflicting params {conflicting}')

		disallowed = (set(param_set.exposed.keys()) | set(param_set.intrinsic.keys())) & cls.reserved_params

		if disallowed:
			raise Exception(f'Reserved parameters {disallowed!r} are not allowed to be defined parameters')

	@classmethod
	def _verify_exposed_arguments(cls, action: 'Action', args: Dict[str, Any]) -> None:
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
	def _verify_full_arguments(cls, action_name: str, params: Dict[str, Type], args: Dict[str, Any]) -> None:
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
			timeout: Optional[float] = None,
	) -> asyncio.Future:
		"""
		Prepares AwaitDispatch object and create a future to await on
		"""
		if self._stopping:
			raise DispatchStopping()

		if not self.event_manager:
			raise DispatchNotStarted

		future: asyncio.Future = self.event_manager.event_loop.create_future()
		await_dispatch.set(future, params)

		self.logger.debug(f'Awaiting {await_dispatch!r}')

		if timeout is not None:
			# Creating the timeout should be relatively safe. The only places where it can be cancelled is next dispatch
			# or cancel requests. Next dispatch must occur next event since this await_dispatch itself is an event. Cancel
			async def async_timeout() -> None:
				assert timeout is not None
				assert self.event_manager is not None
				await asyncio.sleep(timeout, loop=self.event_manager.event_loop)
				future.set_exception(DispatchAwaitTimeout())
				self.logger.warning(f'Awaiting response {await_dispatch} timed out in {timeout}')

			# TODO: Maybe we can possibly be sure that we can be safe if the dispatch class stops before
			# TODO: create_timeout_callback() is called by using the result of run_coroutine_threadsafe to cancel
			# TODO: in stop(). And if no stop is issued, the concurrent future can be swapped with asyncio's. They
			# TODO: both have .handle() methods, we could type that attribute as a Cancellabled or something.
			def create_timeout_callback() -> None:
				assert self.event_manager is not None
				self.logger.debug(f'Also awaiting response with timeout of {timeout}')
				timeout_future = asyncio.ensure_future(async_timeout(), loop=self.event_manager.event_loop)
				await_dispatch.set_timeout_future(timeout_future)

			self.event_manager.call_soon_threadsafe(create_timeout_callback)

		return future

	def _ensure_exclusive(self, action: 'Action', source: object) -> None:
		"""
		Given an action and a source, if the action is exclusive cancel any pending actions that are
		awaiting on an AwaitDispatch.
		:param action:
		:param source:
		:return:
		"""

		if not self.event_manager:
			raise DispatchNotStarted()

		if action.exclusive_async:
			# If it is then we need to check for in-flight DispatchResponses for the current action and source
			# and cancel them

			active_action = self._actives.try_lookup_one(action=action.name, source=source)

			if not active_action:
				return

			guid: Optional[uuid.UUID] = None

			if active_action.provider is not None:
				pending_await = active_action.provider.get_await_dispatch()

				future = pending_await.get_current_future()
				guid = pending_await.guid

				if future:
					self.logger.info(f'Cancelling pending exclusive {action.name!r} action')
					# Mark as removed so done callback in dispatch() doesn't try to remove it again
					pending_await.mark_removed()

					def cancel_callback() -> None:
						# Cancel future and timeout if any
						if future:
							future.cancel()
						pending_await.cancel_timeout()

					self.event_manager.call_soon_threadsafe(cancel_callback)

			if active_action.action_future:
				active_action.action_future.cancel()

			# Immediately remove it now
			self.remove_active_by_action(action.name, guid)

	def dispatch(
		self,
		source: object,
		action_name: str,
		args: Dict[str, Any],
		response_id: Optional[Union[str, uuid.UUID]] = None
	) -> None:
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

		if not self.event_manager:
			raise DispatchNotStarted()

		_action: Optional[Action] = self.actions.get(action_name)

		if not _action:
			raise DispatchNotFound()

		action: Action = _action

		self.logger.debug(f'Dispatching for {action}')

		existing_ad: Optional[AwaitDispatch] = None

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

			existing_ad = self._try_get_await_dispatch(action_name, response_guid)

		# If there is no existing_ad, then this is a new action
		if not existing_ad:
			cleanup_callback: Optional[Callable] = None
			async_dispatch_callback: Optional[Callable[['Action', Dict[str, Any]], Any]] = None
			await_dispatch: Optional['AwaitDispatch'] = None

			active_action: ActiveAction

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
				active_action = self._add_active_action(source, action, await_dispatch)

				self.logger.debug(f'Action is coroutine, created AwaitDispatch: {await_dispatch}')

				# Callback to destroy AwaitDispatch
				def remove_await_callback() -> None:
					assert await_dispatch is not None
					self.logger.debug(f'Action coroutine completed, removing {await_dispatch}')
					await_dispatch.remove_and_cancel_timeout()

				cleanup_callback = remove_await_callback

				async def coro_dispatch(dispatch_action: Action, dispatch_args: Dict[str, Any]) -> Any:
					return await dispatch_action.func(dispatch_action.instance, **dispatch_args)

				# Set async callback used to call async function
				async_dispatch_callback = coro_dispatch
			else:
				async def call_dispatch(dispatch_action: Action, dispatch_args: Dict[str, Any]) -> Any:
					return dispatch_action.func(dispatch_action.instance, **dispatch_args)

				active_action = self._add_active_action(source, action, None)

				def remove_active_callback() -> None:
					self.remove_active_action(active_action)

				cleanup_callback = remove_active_callback

				# Set async callback used to call non-async function
				async_dispatch_callback = call_dispatch

			# Co-routine to run regardless of synchronocity
			async def dispatch_async() -> Any:
				assert self.event_manager is not None
				assert async_dispatch_callback is not None
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

					dispatch_future = asyncio.ensure_future(async_dispatch_callback(action, passed_args), loop=self.event_manager.event_loop)

					active_action.action_future = dispatch_future

					return await dispatch_future

				except Exception as exc:
					if self._exc_handler:
						self.logger.debug(f'Calling exception handler for {await_dispatch or action!r}')
						# Call exception handler if something happened
						try:
							if asyncio.iscoroutinefunction(self._exc_handler):
								await self._exc_handler(source, action_name, exc, response_guid)
							else:
								self._exc_handler(source, action_name, exc, response_guid)

						except Exception as exc_exc:
							self.logger.error(f'Unexpected error while running exception handler: {exc_exc}')
					else:
						self.logger.error(f'Error occurred in dispatch in {await_dispatch or action!r}: {exc!r}')

					# Raise so future does not invoke complete handler
					raise

				finally:
					if dispatch_future:
						active_action.action_future = None

			def done_callback(event_manager: EventLoopManager, done_future: asyncio.Future) -> None:
				# Call cleanup handler if any
				if cleanup_callback:
					cleanup_callback()

				# Call done handler if done
				if self._complete_handler and done_future.done() and not done_future.cancelled():
					# Make sure handler is called in our event loop
					# TODO: Do we need to do this event loop deferring elsewhere?
					event_manager.call_soon_threadsafe(
						self._complete_handler,
						source,
						action_name,
						done_future.result(),
						response_guid
					)

			fut = self.event_manager.run_coroutine_threadsafe(dispatch_async())

			if cleanup_callback or self._complete_handler:
				fut.add_done_callback(functools.partial(done_callback, self.event_manager))

		# If this is a coroutine in progress, then continue its await_dispatch
		elif action.is_coro:
			# Make sure that a future was even set
			_future = existing_ad.get_current_future()
			_params = existing_ad.get_current_params()

			if not _future or _params is None:
				raise DispatchError('No response has been awaited')

			future: asyncio.Future = _future

			# TODO: When should the timeout cancel occur? Or better yet should the timeout be cancelled if
			# TODO: an exception rose in the dispatch?

			# Verify parameters and issue callback

			self.logger.debug(f'Dispatching continuation of {existing_ad}')

			# TODO: A continuation dispatch will default to
			self._verify_full_arguments(
				action_name=action_name,
				params=existing_ad.get_await_dispatch().get_current_params() or {},
				args=args,
			)

			def set_future_result_callback() -> None:
				assert existing_ad is not None

				self.logger.debug(f'Fulfilled {existing_ad}')
				# Cancel timeout since no exceptions arose
				existing_ad.cancel_timeout()
				future.set_result(args)

			self.event_manager.call_soon_threadsafe(set_future_result_callback)

		else:
			self.logger.error(f'UUID collision for {action!r}')
			raise DispatchError(
				'Somehow we may have UUID collision-ed with another action of same name but of differing'
				' synchronicity.'
			)


T = TypeVar('T', bound=Callable)


def add_action(
	action_name: Optional[str] = None,
	exclusive_async: bool = True,
	params: Optional[Dict[str, Type]] = None,
	intrinsic_params: Iterable[str] = (),
	timeout: Optional[float] = None,
) -> Callable[[T], T]:
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

	def decorator(func: T) -> T:
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

			matches = match.groupdict()

			action_name = matches['name']

		func_parameters = inspect.signature(func).parameters
		provided = list(func_parameters.keys())

		has_kwargs = len(list(filter(lambda p: p.kind == inspect.Parameter.VAR_KEYWORD, func_parameters.values()))) > 0

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
		instance: object,
		name: str,
		func: Callable,
		func_params: List[str],
		params: Dict[str, Type],
		intrinsic_params: List[str],
		is_coro: bool,
		has_kwargs: bool,
		exclusive_async: bool,
		timeout: Optional[float],
	) -> None:
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
		return Action(**deepcopy(self.__dict__))

	def get_func_params(self) -> List[str]:
		return self.func_params

	def __repr__(self) -> str:
		return f'Action(func: {self.instance.__class__.__name__}.{self.func.__name__})'

	def __str__(self) -> str:
		return repr(self)


class AbstractAwaitDispatch(object):
	def get_await_dispatch(self) -> 'AwaitDispatch':
		raise NotImplemented()

	def __call__(self, params: Optional[Dict[str, Type]] = None, timeout: Optional[float] = None) -> asyncio.Future:
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
	) -> None:
		self.dispatcher = dispatcher
		self.source = source
		self.action_name = action_name
		self.default_params = default_params
		self._current_future: Optional[asyncio.Future] = None
		self._current_params: Optional[Dict[str, Type]] = None
		self._timeout_future: Optional[asyncio.Future] = None
		self.guid = guid

		self.removed = False

	def mark_removed(self) -> None:
		self.removed = True

	def remove_and_cancel_timeout(self) -> None:
		if self.removed:
			self.dispatcher.logger.info('AwaitDispatch already removed')
			return

		self.dispatcher.remove_active_by_action(self.action_name, self.guid)
		self.mark_removed()

		self.cancel_timeout()

	def cancel_timeout(self) -> None:
		if self._timeout_future:
			self._timeout_future.cancel()
			self._timeout_future = None

	def set_timeout_future(self, future: asyncio.Future) -> None:
		self._timeout_future = future

	def set(self, future: asyncio.Future, params: Dict[str, Type]) -> None:
		self._current_future = future
		self._current_params = params

	def get_current_future(self) -> Optional[asyncio.Future]:
		return self._current_future

	def get_current_params(self) -> Optional[Dict[str, Type]]:
		return self._current_params

	def __call__(self, params: Optional[Dict[str, Type]] = None, timeout: Optional[float] = None) -> asyncio.Future:
		params = self.default_params if params is None else params
		return self.dispatcher.await_dispatch(self, params=params, timeout=timeout)

	def __repr__(self) -> str:
		return f'AwaitDispatch(action: {self.action_name!r}, guid: {self.guid}, param: {self._current_params!r})'

	def __str__(self) -> str:
		return repr(self)

	def get_await_dispatch(self) -> 'AwaitDispatch':
		return self


class DispatchError(Exception):
	def __str__(self) -> str:
		return super(DispatchError, self).__str__() if self.args else self.__class__.__name__


class DispatchNotStarted(DispatchError):
	pass


class DispatchAwaitTimeout(DispatchError):
	pass


class DispatchNotFound(DispatchError):
	pass


class DispatchStopping(DispatchError):
	pass


class DispatchArgumentError(DispatchError):
	def __init__(self, argument_name: str, *args: Any, **kwargs: Any) -> None:
		super(DispatchArgumentError, self).__init__(argument_name, *args, **kwargs)

		self.argument_name = argument_name


class DispatchMissingArgumentError(DispatchArgumentError):
	def __init__(self, argument_name: str, *args: Any, **kwargs: Any) -> None:
		super(DispatchMissingArgumentError, self).__init__(*args, **{**kwargs, 'argument_name': argument_name})


__all__ = [
	'ParameterSet',
	'ResponseDispatcher',
	'add_action',
	'Action',
	'AbstractAwaitDispatch',
	'AwaitDispatch',
	'DispatchError',
	'DispatchNotStarted',
	'DispatchAwaitTimeout',
	'DispatchNotFound',
	'DispatchStopping',
	'DispatchArgumentError',
	'DispatchMissingArgumentError',
]
