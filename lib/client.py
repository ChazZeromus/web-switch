import asyncio
import json
import logging
import uuid
from typing import Dict

import websockets


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
	def _extract_guid(data: Dict) -> uuid.UUID:
		response_id = data.get('response_id')

		if not response_id:
			error_data = data.get('error_data')

			if error_data:
				response_id = error_data.get('response_id')

		return uuid.UUID(response_id)

	async def _recv_loop(self):
		async for response in self.ctx:
			try:
				data = json.loads(response)
				error = data.get('error')

				guid = self._extract_guid(data)
				convo = self.active_convos.get(guid)

				if error:
					error_data = error.get('error_data', {})

					exc = ResponseException(error, **error_data)

					if convo:
						await convo.queue.put(exc)

					raise Exception(f'Response error: {exc}')

				if convo:
					convo.queue.put(data)

				elif guid is not None:
					self.logger.warning(f'Got response for non-existent conversation {guid}')

				else:
					self.logger.warning(f'No response ID provided in body: {data!r}')

			except Exception as e:
				self.logger.error(f'Error handling response: {e!r}')

	def convo(self):
		if self.ctx is None:
			raise Exception('No context is available to creating convo')

		new_convo = Convo(self)

		self.active_convos[new_convo.guid] = new_convo

		return new_convo


class Convo:
	def __init__(self, client: Client):
		self.client = client
		self.ctx = client.ctx
		self.guid = uuid.uuid4()

		self.queue = asyncio.Queue()

	async def send_and_wait(self, data: dict, timeout=3):
		copy = data.copy()

		copy.update(response_id=str(self.guid))

		await self.ctx.send(json.dumps(copy))

		data_fut = asyncio.get_event_loop().create_future()  # type: asyncio.Future
		timeout_fut = None

		async def timeout_callback():
			await asyncio.sleep(timeout)
			data_fut.set_exception(Exception(f'send_and_wait timeout out after {timeout}'))

		async def await_data():
			new_data = await self.queue.get()
			timeout_fut.cancel()
			return new_data

		timeout_fut = asyncio.ensure_future(timeout_callback())
		asyncio.ensure_future(await_data())

		data = await data_fut

		if isinstance(data, BaseException):
			raise data

		return data


class ResponseException(Exception):
	def __init__(self, message, *, error_type, **data):
		super(ResponseException, self).__init__(message)
		self.error_type = error_type
		self.data = data

	def __repr__(self):
		data = ','.join(f'{key}={value!r}' for key, value in self.data.items())
		return f'ResponseException({self.error_type!r},{self.args},{data})'

	def __str__(self):
		return self.error_type