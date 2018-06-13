import asyncio
import json
import logging
import uuid
import traceback
from typing import Dict, Optional, Union

import websockets

from lib.message import Message

class Client:
	def __init__(self, ws_url):
		self.connection = websockets.connect(ws_url)
		self.ctx = None
		self.response_id = None
		self.active_convos = {}  # type: Dict[uuid.UUID, Convo]
		self.logger = logging.getLogger('Client')
		self._loop_fut = None

	async def __aenter__(self, *args, **kwargs) -> 'Client':
		self.ctx = await self.connection.__aenter__(*args, **kwargs)
		self._loop_fut = asyncio.ensure_future(self._recv_loop())
		return self

	async def __aexit__(self, *args, **kwargs) -> None:
		for convo in self.active_convos.values():
			await convo.queue.put(Exception('Client terminating!'))

		await self.connection.__aexit__(*args, **kwargs)
		await self._loop_fut

	@staticmethod
	def _extract_guid(message: Message) -> Optional[uuid.UUID]:
		response_id = message.data.get('response_id')

		if not response_id and message.error_data:
			error_data = message.error_data.get('error_data')

			if error_data:
				response_id = error_data.get('response_id')

		if not response_id:
			return None

		return uuid.UUID(response_id)

	async def _recv_loop(self):
		async for response in self.ctx:
			try:
				data = json.loads(response)  # type: dict

				message = Message()
				message.load(data)

				error = data.get('error')

				guid = self._extract_guid(message)
				convo = self.active_convos.get(guid)

				if error:
					error_data = message.error_data

					exc = ResponseException(error, **error_data)

					if convo:
						await convo.queue.put(exc)

					raise ReceiveException(f'Error from server: {exc}')

				if convo:
					await convo.queue.put(message)
					self.logger.debug(f'Posted {message!r}')

				elif guid is not None:
					self.logger.warning(f'Got response for non-existent conversation {guid}')

				else:
					self.logger.warning(f'No response ID provided in body: {data!r}')

			except ReceiveException as e:
				self.logger.warning(f'Received error: {e!r}')
			except Exception as e:
				self.logger.error(f'{traceback.format_exc()}\nError handling response: {e!r}')

	def convo(self):
		if self.ctx is None:
			raise Exception('No context is available to creating convo')

		new_convo = Convo(self)

		self.active_convos[new_convo.guid] = new_convo

		return new_convo


class Convo:
	last_convo_id = 0
	def __init__(self, client: Client):
		self.client = client
		self.ctx = client.ctx
		self.guid = uuid.uuid4()

		self.queue = asyncio.Queue()

		Convo.last_convo_id += 1
		self.id = Convo.last_convo_id
		self.logger = self.client.logger.getChild(f'convo:{self.id}')

	async def send_and_wait(self, data: Union[dict, Message], timeout=3) -> Message:
		if isinstance(data, dict):
			message = Message(data=data)
		elif isinstance(data, Message):
			message = data
		else:
			raise TypeError('data must be dict or Message')

		await self.ctx.send(message.json(response_id=str(self.guid)))

		self.logger.debug(f'Sending data {message} and waiting {timeout} seconds for response')

		data_fut = asyncio.get_event_loop().create_future()  # type: asyncio.Future
		timeout_fut = None

		async def timeout_callback():
			await asyncio.sleep(timeout)
			self.logger.warning('Timed out waiting for response')
			data_fut.set_exception(Exception(f'send_and_wait timeout out after {timeout}'))

		async def await_data():
			new_data = await self.queue.get()
			timeout_fut.cancel()
			data_fut.set_result(new_data)

		timeout_fut = asyncio.ensure_future(timeout_callback())
		asyncio.ensure_future(await_data())

		data = await data_fut

		if isinstance(data, BaseException):
			raise data

		return data


class ReceiveException(Exception):
	pass


class ResponseException(Exception):
	def __init__(self, message, *, error_types=None, **data):
		super(ResponseException, self).__init__(message)
		self.message = message
		self.error_types = error_types or []
		self.data = data

	def __repr__(self):
		data = ','.join(f'{key}={value!r}' for key, value in self.data.items())
		return f'ResponseException({self.error_types!r},{self.args},{data})'

	def __str__(self):
		return f'ResponseException: {self.message!r} {self.error_types}'
