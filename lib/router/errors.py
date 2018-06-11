class RouterError(BaseException):
	def __init__(self, error_type: str, message: str, **data):
		super(RouterError, self).__init__()
		self.message = message
		self.error_type = error_type
		self.error_data = data

	def __repr__(self):
		return (
			f'RouterError('
			f'message={self.message!r},'
			f'error_type={self.error_type!r},'
			f'data={self.error_data!r})'
		)

	def __str__(self):
		return f"{self.error_type}: {self.message}"


class RouterResponseError(RouterError):
	def __init__(self, message: str, **data):
		super(RouterResponseError, self).__init__(error_type='response', message=message, **data)


class RouterConnectionError(RouterError):
	def __init__(self, message: str, **data):
		super(RouterConnectionError, self).__init__(error_type='connection', message=message, **data)


class RouterServerError(RouterError):
	def __init__(self, message: str, **data):
		super(RouterServerError, self).__init__(error_type='server', message=message, **data)
