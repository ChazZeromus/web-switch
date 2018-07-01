from typing import List, Union


class RouterError(Exception):
	def __init__(self, error_types: Union[List[str], str], message: str, **data) -> None:
		super(RouterError, self).__init__()
		self.message = message
		self.error_types = [error_types] if isinstance(error_types, str) else error_types
		self.error_data = data

	def __repr__(self):
		errors = ','.join(self.error_types)
		return (
			f'RouterError('
			f'message={self.message!r},'
			f'error_type={errors},'
			f'data={self.error_data!r})'
		)

	def __str__(self):
		errors = ','.join(self.error_types)
		return f"({errors}): {self.message}"


class RouterResponseError(RouterError):
	def __init__(self, message: str, **data) -> None:
		super(RouterResponseError, self).__init__(message=message, **{'error_types': 'response', **data})


class RouterConnectionError(RouterError):
	def __init__(self, message: str, **data) -> None:
		super(RouterConnectionError, self).__init__(message=message, **{'error_types': 'connection', **data})


class RouterServerError(RouterError):
	def __init__(self, message: str, **data) -> None:
		super(RouterServerError, self).__init__(message=message, **{'error_types': 'server', **data})
