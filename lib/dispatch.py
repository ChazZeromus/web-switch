from typing import Callable, Dict, Type, Tuple


class ResponseDispatcher(object):
	def __init__(self, common_params: Dict[str, Type]):
		self.actions = {}  # type: Dict[str, Tuple[Callable, Dict[str, Type]]]
		self.common_params = common_params

	def add(self, action: str, func: Callable, params: Dict[str, Type]):
		if action in self.actions:
			raise Exception(f'Action {action!r} already exists in dispatch')

		self.actions[action] = (func, {**self.common_params, **params})

	def dispatch(self, instance: object, action: str, args: Dict):
		entry = self.actions.get(action)

		if entry is None:
			raise DispatchNotFound()

		func, params = entry

		for param, param_type in params.items():
			if param not in args:
				raise DispatchMissingArgumentError(f'Missing {param!r}')

			value = args.get(param)

			if not isinstance(value, param_type):
				raise DispatchArgumentError(
					f'Invalid type provided for action {action!r}: '
					f'{value!r} is not a {param_type}'
				)

		return func(instance, **args)


class DispatchError(Exception):
	pass


class DispatchNotFound(DispatchError):
	pass


class DispatchArgumentError(DispatchError):
	def __init__(self, argument_name: str, *args, **kwargs):
		super(DispatchArgumentError, self).__init__(*args, **kwargs)

		self.argument_name = argument_name


class DispatchMissingArgumentError(DispatchArgumentError):
	def __init__(self, argument_name: str, *args, **kwargs):
		super(DispatchArgumentError, self).__init__(argument_name=argument_name, *args, **kwargs)


def add_dispatch(action: str, dispatcher: ResponseDispatcher, params: Dict[str, Type] =None) -> Callable:
	def decorate(func: Callable):
		if action in dispatcher.actions:
			raise Exception(f'Dispatched function {func!r} already exists')

		dispatcher.add(action, func, params or {})

		return func

	return decorate
