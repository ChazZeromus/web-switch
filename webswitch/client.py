import asyncio
import json
import uuid
import traceback
import time
import bisect
from logging import Logger
from types import TracebackType
from typing import *

import websockets
from websockets.client import Connect as WebSocketConnection
from websockets.client import WebSocketClientProtocol

from .message import Message
from .logger import g_logger

WHOAMI_WAIT = 2.0


T = TypeVar('T')


class TimestampedList(Generic[T]):
	def __init__(self) -> None:
		self._list: List[T] = []
		self._ts_list: List[float] = []

	def __delslice__(self, i: int, j: int) -> None:
		del self._list[i:j]
		del self._ts_list[i:j]

	def __delitem__(self, index: Union[int, slice]) -> None:
		self._list.__delitem__(index)
		self._ts_list.__delitem__(index)

	def __iter__(self) -> Iterator:
		return iter(self._list)

	def append(self, obj: T) -> None:
		self._list.append(obj)
		self._ts_list.append(time.monotonic())

	def remove_old(self, max_age: float) -> int:
		now = time.monotonic()
		lookup = [now - x for x in reversed(self._ts_list)]

		reverse_i = bisect.bisect_right(lookup, max_age)
		i = len(self._list) - reverse_i

		del self[:i]

		return i

	def get(self) -> T:
		self._ts_list.pop(0)
		return self._list.pop(0)

	@property
	def oldest_ts(self) -> Optional[float]:
		if not self:
			return None

		return self._ts_list[0]

	def __getitem__(self, item: int) -> T:
		return self._list[item]

	def __len__(self) -> int:
		return len(self._list)

	def __bool__(self) -> bool:
		return bool(self._list)

	def get_list_copy(self) -> List[T]:
		return list(self._list)


class MessageQueues(object):
	def __init__(self, max_count: int, max_message_age: float = 10.0) -> None:
		self._max: int = max_count
		self._count: int = 0
		self._queues: Dict[Optional[uuid.UUID], TimestampedList] = {}
		self._max_age = max_message_age

	@property
	def count(self) -> int:
		return self._count

	def _remove_oldest_by_count(self, count: int) -> None:
		left = count
		while left > 0:
			candidates = []

			for guid, queue in self._queues.items():
				if queue:
					candidates.append((guid, queue))

			candidates.sort(key=lambda x: x[1].oldest_ts)

			slice_count = min(left, len(candidates))
			left -= slice_count
			self._count -= slice_count

			for guid, queue in candidates[:slice_count]:
				queue.get()
				if not queue:
					del self._queues[guid]

	def remove_oldest(self, max_age: float) -> None:
		for guid, queue in list(self._queues.items()):
			self._count -= queue.remove_old(max_age)

			if not queue:
				del self._queues[guid]

	def add(self, guid: Optional[uuid.UUID], message: Message) -> None:
		if self._count + 1 > self._max:
			self._remove_oldest_by_count(self._count - self._max + 1)

		queue = self._queues.get(guid)

		if queue is None:
			self._queues[guid] = queue = TimestampedList()

		queue.append(message)

		self._count += 1

	def get(self, guid: Optional[uuid.UUID]) -> Optional[Message]:
		queue = self._queues.get(guid)

		if queue is None:
			return None

		msg: Message = queue.get()

		self._count -= 1

		if not queue:
			del self._queues[guid]

		return msg

	def get_messages(self, guid: Optional[uuid.UUID]) -> Optional[List]:
		if guid not in self._queues:
			return None

		return self._queues[guid].get_list_copy()

	def get_guids(self) -> List[Optional[uuid.UUID]]:
		return list(self._queues.keys())


class Client:
	last_instance_id = 0

	def __init__(self, ws_url: str, max_queued_messages: int = 1000) -> None:
		self._url = ws_url
		self._connection: WebSocketConnection = websockets.connect(ws_url)
		# self._ctx: Optional[WebSocketClientProtocol] = None
		self._ctx = None

		Client.last_instance_id += 1
		self._id: int = Client.last_instance_id

		self._client_id: Optional[int] = None

		self._active_convos: Dict[uuid.UUID, Convo] = {}
		self._logger: Logger = g_logger.getChild('Client')
		self._loop_fut: Optional[asyncio.Future] = None

		self._queues: MessageQueues = MessageQueues(max_queued_messages)

	@property
	def id(self) -> int:
		return self._id

	@property
	def client_id(self) -> Optional[int]:
		return self._client_id

	@property
	def url(self) -> str:
		return self._url

	async def __aenter__(self, *args: Any, **kwargs: Any) -> 'Client':
		self._ctx = await self._connection.__aenter__(*args, **kwargs)
		self._loop_fut = asyncio.ensure_future(self._recv_loop())
		self._logger.debug(f'Async enter from event loop id {id(asyncio.get_event_loop()):x}')

		message = await self.convo('whoami').send_and_expect({}, WHOAMI_WAIT)

		self._client_id = int(message.data['id'])

		return self

	async def __aexit__(self, exc_type: Optional[BaseException], exc_val: Any, exc_tb: TracebackType) -> None:
		if not self._loop_fut:
			raise Exception('Cannot async-exit when client never entered')

		for convo in self._active_convos.values():
			await convo.queue.put(Exception('Client terminating!'))
			convo.cancel_expects()

		await self._connection.__aexit__(exc_type, exc_val, traceback)
		await self._loop_fut

		self._logger.debug('Exiting')

	@staticmethod
	def _extract_guid(message: Message) -> Optional[uuid.UUID]:
		response_id = message.data.get('response_id')

		if not response_id and message.error_data:
			response_id = message.error_data.get('response_id')

		if not response_id:
			return None

		return uuid.UUID(response_id)

	async def _recv_loop(self) -> None:
		if self._ctx is None:
			raise Exception('No context is available to start receive loop')

		async for response in self._ctx:
			try:
				data: Dict = json.loads(response)

				message = Message()
				message.load(data)

				error = data.get('error')

				guid = self._extract_guid(message)
				convo = self._active_convos.get(guid)

				if error:
					error_data = message.error_data or {}

					exc = ResponseException(error, **error_data)

					if convo:
						await convo.queue.put(exc)

					raise ReceiveException(f'Error from server: {exc}')

				if convo:
					await convo.queue.put(message)
					self._logger.debug(f'Posted {message!r}')

				elif guid is not None:
					self._logger.info(f'Got response for non-existent conversation {guid!r}, queuing')
					await self._queues.add(guid, message)
				else:
					self._logger.warning(f'No response ID provided in body: {data!r}')

			except ReceiveException as e:
				self._logger.warning(f'Received error: {e!r}')
			except Exception as e:
				self._logger.error(f'{traceback.format_exc()}\nError handling response: {e!r}')

	def get_message(self, guid: Optional[uuid.UUID]) -> Optional[Message]:
		return self._queues.get(guid)

	# TODO: Create a status method that returns a Convo as an async context-manager as a shorthand
	def convo(self, action: Optional[str]) -> 'Convo':
		if self._ctx is None:
			raise Exception('No context is available for creating convo')

		new_convo = Convo(action, self)

		self._active_convos[new_convo._guid] = new_convo

		return new_convo


class _ActiveItem(NamedTuple):
	data_future: asyncio.Future
	timeout_future: Optional[asyncio.Future]
	retrieve_future: asyncio.Future


class Convo:
	last_convo_id: int = 0

	def __init__(self, action: Optional[str], client: Client) -> None:
		if not client._ctx:
			raise NoContextException()

		self._client: Client = client
		self._action: Optional[str] = action

		self._ctx: WebSocketClientProtocol = client._ctx

		self._guid: Optional[uuid.UUID] = uuid.uuid4() if self._action else None

		self.client_id: int = client._id

		self.queue: asyncio.Queue = asyncio.Queue()

		Convo.last_convo_id += 1
		self._id: int = Convo.last_convo_id
		self._logger: Logger = self._client._logger.getChild(f'convo:{self._action!r}:{self._id}')

		self._active_expects: Set[_ActiveItem] = set()

	@property
	def id(self) -> int:
		return self._id

	@property
	def guid(self) -> Optional[uuid.UUID]:
		return self._guid

	@property
	def action(self) -> Optional[str]:
		return self._action

	def cancel_expects(self) -> None:
		for active_item in list(self._active_expects):
			if active_item.timeout_future:
				active_item.timeout_future.cancel()  # TODO: threadsafe?
			active_item.retrieve_future.cancel()
			active_item.data_future.set_exception(ClientShutdownException())

			self._active_expects.remove(active_item)

	async def send(self, data: Union[dict, Message]) -> None:
		if not self._ctx:
			raise NoContextException()

		if not self._action or not self._guid:
			raise UnrequitedException('Cannot call send()')

		if isinstance(data, dict):
			message = Message(data=data)
		elif isinstance(data, Message):
			message = data
		else:
			raise TypeError('data must be dict or Message')

		self._logger.debug(f'Sending {message!r}')

		await self._ctx.send(message.json(action=self._action, response_id=str(self._guid)))

	async def expect_forever(self) -> Message:
		return await self.expect(None)

	async def expect(self, timeout: Optional[float]) -> Message:
		self._logger.debug(f'Waiting {timeout if timeout else "indefinitely"} seconds for response')

		# First check client for new messages

		response_data = self._client.get_message(self._guid)

		if response_data is None:
			active_item: Optional[_ActiveItem] = None

			async def timeout_callback() -> None:
				assert active_item is not None
				assert timeout is not None
				await asyncio.sleep(timeout)
				self._logger.warning('Timed out waiting for response')
				active_item.retrieve_future.cancel()
				active_item.data_future.set_exception(ResponseTimeoutException(f'send_and_expect timeout out after {timeout}'))
				self._active_expects.remove(active_item)

			async def await_data() -> None:
				assert active_item is not None
				new_data = await self.queue.get()

				if active_item.timeout_future:
					active_item.timeout_future.cancel()

				self._active_expects.remove(active_item)
				active_item.data_future.set_result(new_data)

			active_item = _ActiveItem(
				data_future=asyncio.get_event_loop().create_future(),
				timeout_future=asyncio.ensure_future(timeout_callback()) if timeout is not None else None,
				retrieve_future=asyncio.ensure_future(await_data()),
			)

			self._active_expects.add(active_item)

			response_data = await active_item.data_future

		assert response_data is not None

		if isinstance(response_data, BaseException):
			raise response_data

		return response_data

	# TODO: Implement __aiter__ for continuous messages

	async def send_and_expect(self, data: Union[dict, Message], timeout: float = 10.0) -> Message:
		await self.send(data)
		return await self.expect(timeout)


class ClientException(Exception):
	pass


class ReceiveException(ClientException):
	pass


class NoContextException(ClientException):
	pass


class UnrequitedException(ClientException):
	def __init__(self, msg: str) -> None:
		super(UnrequitedException, self).__init__(f'Cannot send a message to server on an actionless conversation: {msg}')


class ResponseTimeoutException(ReceiveException):
	pass


class ClientShutdownException(ClientException):
	pass


class ResponseException(Exception):
	def __init__(self, message: str, *, error_types: Optional[List[str]] = None, **data: Any) -> None:
		super(ResponseException, self).__init__(message)
		self.message = message
		self.error_types = error_types or []
		self.data = data

	def __repr__(self) -> str:
		data = ','.join(f'{key}={value!r}' for key, value in self.data.items())
		return f'ResponseException({self.error_types!r},{self.args},{data})'

	def __str__(self) -> str:
		return f'ResponseException: {self.message} {self.error_types}'


__all__ = [
	'Client',
	'Convo',
	'ReceiveException',
	'ClientShutdownException',
	'ResponseTimeoutException',
	'ResponseException',
]
