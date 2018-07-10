import json
import uuid
from copy import deepcopy
from typing import *

from .router.errors import RouterError


class MessageError(Exception):
	pass


class ReservedKeyError(MessageError):
	pass


class MessageJSONEncoder(json.JSONEncoder):
	def default(self, obj):
		if isinstance(obj, uuid.UUID):
			return str(obj)

		return super(MessageJSONEncoder, self).default(obj)


class Message(object):
	_RESERVED_KEYS = {'success', 'error', 'error_data', '__final'}

	def __init__(
		self,
		data: dict = None,
		success: bool = None,
		error: str = None,
		error_data: Dict = None,
		*, is_final: bool = False,
	) -> None:
		self.data: Dict = deepcopy(data) if data is not None else {}

		Message.verify_reserved_use(self.data)

		self.success: Optional[bool] = success
		self.error: Optional[str] = error
		self.error_data: Optional[Dict] = error_data
		self.is_final = is_final

	def load(self, json_data) -> 'Message':
		self.data = deepcopy(json_data)

		self.success = json_data.get('success')
		self.error = json_data.get('error')
		self.error_data = json_data.get('error_data')
		self.is_final = bool(json_data.get('__final'))

		for key in ('success', 'error', 'error_data'):
			if key in self.data:
				del self.data[key]

		return self

	@classmethod
	def verify_reserved_use(cls, data: dict):
		if set(data.keys()) & cls._RESERVED_KEYS:
			raise ReservedKeyError()

	@classmethod
	def error_from_exc(cls, exc: BaseException):
		if isinstance(exc, RouterError):
			error_data = exc.error_data.copy()

			# Try to decode error data, if successful then we can serialize it
			# if not then turn it into a repr'd string and send that instead.
			for key, value in error_data.items():
				try:
					json.dumps(value, cls=MessageJSONEncoder)
				except TypeError:
					error_data[key] = repr(value)

			if not error_data.get('exc_class'):
				error_data['exc_class'] = exc.__class__.__name__

			error_data['error_types'] = exc.error_types

			return Message(success=False, error=exc.message, error_data=error_data)

		return Message(success=False, error=str(exc), error_data={'data': repr(exc)})

	def _render_tags(self):
		tags = []

		if self.success is not None:
			tags.append(f'success={self.success}')

		if self.error:
			tags.append(f'error={self.error}')

		return tags

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

		if self.is_final:
			payload['__final'] = True

		return json.dumps(payload, cls=MessageJSONEncoder)


__all__ = ['Message']