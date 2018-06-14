import asyncio
import json
import logging
import uuid
import traceback
from typing import Dict, Optional, Union, Set, Tuple

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
			convo.cancel_expects()

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

	def convo(self, action: str):
		if self.ctx is None:
			raise Exception('No context is available to creating convo')

		new_convo = Convo(action, self)

		self.active_convos[new_convo.guid] = new_convo

		return new_convo


class Convo:
	last_convo_id = 0

	def __init__(self, action: str, client: Client):
		self.client = client
		self.action = action

		self.ctx = client.ctx
		self.guid = uuid.uuid4()

		self.queue = asyncio.Queue()

		Convo.last_convo_id += 1
		self.id = Convo.last_convo_id
		self.logger = self.client.logger.getChild(f'convo:{self.action}:{self.id}')

		self._active_expects = set()  # type: Set[Tuple[asyncio.Future, asyncio.Future]]

	def cancel_expects(self):
		for item in list(self._active_expects):
			timeout_fut, data_fut = item

			timeout_fut.cancel() # TODO: threadsafe?
			data_fut.set_exception(ClientShutdownException())

			self._active_expects.remove(item)

	async def send(self, data: Union[dict, Message]):
		if isinstance(data, dict):
			message = Message(data=data)
		elif isinstance(data, Message):
			message = data
		else:
			raise TypeError('data must be dict or Message')

		self.logger.debug(f'Sending {message!r}')

		await self.ctx.send(message.json(action=self.action, response_id=str(self.guid)))

	async def expect(self, timeout: float):
		self.logger.debug(f'Waiting {timeout} seconds for response')

		data_fut = asyncio.get_event_loop().create_future()  # type: asyncio.Future
		timeout_fut = None
		active_item = None

		async def timeout_callback():
			await asyncio.sleep(timeout)
			self.logger.warning('Timed out waiting for response')
			data_fut.set_exception(ResponseTimeoutException(f'send_and_expect timeout out after {timeout}'))
			self._active_expects.remove(active_item)

		async def await_data():
			new_data = await self.queue.get()
			timeout_fut.cancel()
			data_fut.set_result(new_data)
			self._active_expects.remove(active_item)

		timeout_fut = asyncio.ensure_future(timeout_callback())

		active_item = (timeout_fut, data_fut)
		self._active_expects.add(active_item)

		asyncio.ensure_future(await_data())

		response_data = await data_fut

		if isinstance(response_data, BaseException):
			raise response_data

		return response_data

	async def send_and_expect(self, data: Union[dict, Message], timeout: float=3.0) -> Message:
		await self.send(data)
		return await self.expect(timeout)


class ReceiveException(Exception):
	pass


class ClientShutdownException(Exception):
	pass


class ResponseTimeoutException(ReceiveException):
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
