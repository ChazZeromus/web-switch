import json
import uuid
from copy import deepcopy
from typing import Dict

from lib.router.errors import RouterError


class MessageJSONEncoder(json.JSONEncoder):
	def default(self, obj):
		if isinstance(obj, uuid.UUID):
			return str(obj)

		return super(MessageJSONEncoder, self).default(obj)


class Message(object):
	def __init__(
			self,
			data: dict = None,
			success: bool = None,
			error: str = None,
			error_data: Dict = None
		):
		self.data = deepcopy(data) or {}
		self.success = success
		self.error = error
		self.error_data = error_data

	def load(self, json_data):
		self.data = json_data.copy()

		self.success = json_data.get('success')
		self.error = json_data.get('error')
		self.error_data = json_data.get('error_data')

		for key in ('success', 'error', 'error_data'):
			if key in self.data:
				del self.data[key]

		return self

	@classmethod
	def error(cls, message, **error_data):
		return Message(success=False, error=message, error_data=error_data)

	@classmethod
	def error_from_exc(cls, exception: BaseException):
		if isinstance(exception, RouterError):
			error_data = exception.error_data.copy()

			# Try to decode error data, if successful then we can serialize it
			# if not then turn it into a repr'd string and send that instead.
			for key, value in error_data.items():
				try:
					json.dumps(value, cls=MessageJSONEncoder)
				except TypeError:
					error_data[key] = repr(value)

			return cls.error(
				message=exception.message,
				error_types=exception.error_types,
				**error_data,
			)

		return cls.error(str(exception), **{'data': repr(exception)})

	def _render_tags(self):
		return []

	def __str__(self):
		tags = ' '.join(self._render_tags())
		return f'Message({tags}): {self.data!r}'

	def __repr__(self):
		return str(self)

	def extend(self, **kwargs):
		self.data.update(**kwargs)
		return self

	def clone(self) -> 'Message':
		copy = Message()
		copy.__dict__ = deepcopy(self.__dict__)

		return copy

	def json(self, **extra) -> str:
		payload = {
			**self.data,
			**extra,
		}

		if self.success is not None:
			payload['success'] = self.success

		if self.error or (self.success is not None and not self.success):
			payload['error'] = self.error

		if self.error_data:
			payload['error_data'] = self.error_data

		return json.dumps(payload, cls=MessageJSONEncoder)

