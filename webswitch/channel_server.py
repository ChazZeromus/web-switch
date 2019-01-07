import re
from typing import Any, Optional, Union, Tuple, Dict, Type, Set, List
import traceback
import uuid
import argparse
import asyncio

from .logger import g_logger
from .dispatch import ResponseDispatcher, AwaitDispatch, AbstractAwaitDispatch, add_action, Action, ParameterSet
from .router.errors import RouterError, RouterResponseError, RouterConnectionError
from .router.connection import Connection
from .router.router import Router
from .message import Message

import logging

# TODO: What if instead of an error_type for RouterErrors, have an array of error_types?


class ChannelServerError(RouterError):
	def __init__(self, message: str, **data: Any) -> None:
		super(ChannelServerError, self).__init__('channel_error', message=message, **data)


class ChannelServerActionError(ChannelServerError):
	def __init__(self, message: str, **data: Any) -> None:
		super(ChannelServerActionError, self).__init__(message, **data)
		self.error_types.append('channel_action_error')


RoomChannelKey = Tuple[str, str]


class ChannelServerResponseError(RouterResponseError):
	def __init__(
		self,
		message: str,
		response: Optional[Union[uuid.UUID, 'AwaitDispatch']],
		orig_exc: Optional[Exception] = None,
		**data: str
	) -> None:
		new_data = dict(data)

		if orig_exc:
			new_data['exc_class'] = orig_exc.__class__.__name__

		super(ChannelServerResponseError, self).__init__(message=message, **new_data)
		self.error_types.append('channel_response')

		if response:
			self.set_guid(self, response)

	@staticmethod
	def set_guid(exception: RouterError, response: Union[uuid.UUID, 'AwaitDispatch']) -> None:
		exception.error_data['response_id'] = response


# TODO: Create a ChannelClient class that can have multiple Connections
class ChannelClient(object):
	"""
	Object representing a single connected client. Also allows clients to
	have multiple connections.
	"""
	def __init__(self, channel_server: 'ChannelServer', conn: Connection) -> None:
		self.channel_server = channel_server
		self.connections = [conn]
		self.name: Optional[str] = None
		self._room_key: Optional[RoomChannelKey] = None

		self.id: int = channel_server.get_next_client_id()

		self.logger = channel_server.get_logger().getChild(f'ChannelClient:{self.id}')
		self.logger.debug(f'Created channel client {self!r}')

	def get_room_key(self) -> RoomChannelKey:
		if not self._room_key:
			raise ChannelServerError('No room key to retrieve')

		return self._room_key

	def set_room_key(self, key: RoomChannelKey) -> None:
		self._room_key = key

	async def send(self, message: Union[Message, Dict], response_id: Optional[uuid.UUID]) -> None:
		"""
		Sends a message to this client and all of its connections.
		:param message: Message payload if any
		:param response_id: AwaitDispatch response_id if required
		:return:
		"""
		if isinstance(message, dict):
			message = Message(data=message)

		if response_id is not None:
			message = message.clone()
			message.data.update(response_id=str(response_id))

		await self.channel_server.send_messages(self.connections, message)

	def try_send(self, message: Message, response_id: Optional[uuid.UUID]) -> None:
		"""
		Same as send() but passively handles errors and logs them.
		:param message: Payload to send
		:param response_id: AwaitDispatch response_id if required
		:return:
		"""
		if response_id is not None:
			message = message.clone()
			message.data.update(response_id=str(response_id))

		self.channel_server.try_send_messages(self.connections, message)

	def __repr__(self) -> str:
		return f'ChannelClient(id:{self.id},conns:{len(self.connections)})'

	def __str__(self) -> str:
		return repr(self)


class Conversation(AbstractAwaitDispatch):
	"""
	A wrapper around Router's AwaitDispatch so dispatch-awaiting ChannelServer methods
	have access to the current ChannelClient making the request.
	"""
	def __init__(self, client: ChannelClient, original: AwaitDispatch) -> None:
		self.await_dispatch = original
		self.client = client

	def get_await_dispatch(self) -> AwaitDispatch:
		return self.await_dispatch

	async def send(self, data: Union[Message, Dict]) -> None:
		await self.client.send(data, self.await_dispatch.guid)

	async def send_and_recv(
		self,
		data: Union[Message, Dict],
		expect_params: Optional[Dict[str, Type]] = None,
		timeout: Optional[float] = None
	) -> Any:
		await self.send(data)
		return await self(params=expect_params, timeout=timeout)


class ChannelServer(Router):
	"""
	A sub-class of a Router that allows clients (of one or many connections) to connect
	a rooms to perform actions served by ChannelServer methods.
	And also utilizing a ResponseDispatcher, these methods can be co-routines to allow
	program-defined asynchronous back-and-forth request and responses.
	"""
	_common_intrinsic_params: Dict[str, Type] = {
		'client': ChannelClient
	}
	_common_exposed_params: Dict[str, Type] = {}

	non_async_params = ParameterSet(exposed=_common_exposed_params, intrinsic=_common_intrinsic_params)
	async_params = ParameterSet(
		exposed=_common_exposed_params,
		intrinsic={
			**_common_intrinsic_params,
			'convo': Conversation,
		}
	)

	def __init__(self, host: str, port: int, max_queue_size: int = 100) -> None:
		super(ChannelServer, self).__init__(host, port, max_queue_size)
		self.rooms: Dict[RoomChannelKey, Set[ChannelClient]] = {}
		self.conn_to_client: Dict[Connection, ChannelClient] = {}
		# TODO: Make ID-to-client mapping local to room
		self.id_to_client: Dict[int, ChannelClient] = {}

		self.host, self.port = host, port

		self.dispatcher = ResponseDispatcher(
			common_params=self.non_async_params,
			common_async_params=self.async_params,
			instance=self,
			exception_handler=self.action_exception_handler,
			complete_handler=self.action_complete_handler,
			argument_hook=self.argument_hook,
		)

		self.stop_timeout: Optional[float] = None

		self.logger = super(ChannelServer, self).get_logger().getChild(f'ChannelRouter:{self.id}')
		self.logger.info('Creating channel server')

		self.last_client_id = 0

	def get_logger(self) -> logging.Logger:
		return self.logger

	def get_next_client_id(self) -> int:
		self.last_client_id += 1
		return self.last_client_id

	def _add_connection(
		self,
		key: RoomChannelKey,
		connection: Connection,
		room: Set[ChannelClient],
		other_data: Optional[str],
	) -> None:
		"""
		Called when a new connection is established to determine if the connection is
		a new ChannelClient or add the connection to a list of it is an existing
		ChannelClient.
		:param key:
		:param connection:
		:param room:
		:param other_data:
		:return:
		"""
		if other_data:
			client_id = int(other_data)
			client = self.id_to_client[client_id]
			client.connections.append(connection)
		else:
			client = ChannelClient(self, connection)
			self.id_to_client[client.id] = client
			room.add(client)
			client.set_room_key(key)

		self.conn_to_client[connection] = client

		self.logger.info(f'Added web-switch connection {connection!r} to {client!r} as Channel connection to room {key!r}')

	def _remove_connection(self, connection: Connection) -> None:
		"""
		Cleanup version of _add_connection when disconnect occurs. Destroy ChannelClient
		if it has no more connections.
		:param connection:
		:return:
		"""
		client = self.conn_to_client[connection]

		del self.conn_to_client[connection]

		client.connections.remove(connection)

		self.logger.info(f'Removed connection {connection!r} from {client!r}')

		if not client.connections:
			room = self.rooms[client.get_room_key()]
			room.remove(client)
			del self.id_to_client[client.id]

			self.logger.info(f'Removed last connection of {client!r}, removed client and cancelling actions')
			self.dispatcher.cancel_action_by_source(client)

	def _get_client(self, connection: Connection) -> ChannelClient:
		"""
		Helper method to retrieve a ChannelClient from a connection
		:param connection:
		:return:
		"""
		client = self.conn_to_client.get(connection)

		if not client:
			self.logger.error(f'Could not find client from connection {connection!r}')
			raise ChannelServerError(f'Could not find client with given connection')

		return client

	async def send_to(self,
		data: dict,
		targets: Optional[List[int]],
		key: RoomChannelKey,
		sender_id: Optional[int],
	) -> None:
		# TODO: Do we want a way to broadcast to all rooms eventually?
		target_clients: List[ChannelClient] = []

		if key not in self.rooms:
			raise ChannelServerActionError(f'Room {key!r} does not exist')

		# Find invalid IDs
		for target_id in targets or self.rooms[key]:
			client_ = self.id_to_client.get(target_id)

			if not client_:
				raise ChannelServerActionError(f'No client with id {target_id} exists')

			if key and key != client_.get_room_key():
				raise ChannelServerActionError(f'Client {client_!r} is not in room {key!r}')

			target_clients.append(client_)

		data_to_send = dict(data)

		if sender_id is not None:
			data_to_send['sender_id'] = sender_id

		message = Message(data_to_send)

		for client_ in target_clients:
			client_.try_send(message, response_id=None)

	############
	# Handlers #
	############
	def argument_hook(self, args: Dict[str, Any], source: object, action: Action) -> Dict[str, Any]:
		"""
		Before dispatching any methods to serve a client's request, provide an intrinsic `client` argument
		to be mainly used by synchronous ChannelServer actions. For asynchronous ChannelServer coroutine
		actions, provide a `convo` argument that wraps AwaitDispatch and forwards the current ChannelClient's
		send() methods.
		:param args:
		:param source:
		:param action:
		:return:
		"""
		assert isinstance(source, ChannelClient)

		await_dispatch: Optional[AwaitDispatch] = args.get('await_dispatch')

		new_args = args.copy()

		self.logger.debug(f'Processing argument hook for {action!r}')

		if await_dispatch:
			assert isinstance(await_dispatch, AwaitDispatch)
			new_conversation = Conversation(client=source, original=await_dispatch)
			new_args.update(convo=new_conversation)

		new_args['client'] = source

		return new_args

	# Define our exception handler for actions so we can notify client of the error for the particular response_id.
	async def action_exception_handler(
			self,
			source: object,
			action_name: str,
			e: Exception,
			response_id: uuid.UUID
	) -> None:
		"""
		If something bad happened during an action in response to a client. Send the client an error-type message, and
		try to provide a response_id if action was a co-routine.
		:param source:
		:param action_name:
		:param e:
		:param response_id:
		:return:
		"""
		self.logger.warning(f'Exception while dispatching for action {action_name} with source {source}: {e!r}')

		assert isinstance(source, ChannelClient)

		if not isinstance(e, RouterError):
			e = ChannelServerResponseError(f'Unexpected non-RouterError exception: {str(e) or repr(e)}', orig_exc=e, response=response_id)

		# Try to set response ID even if error isn't ChannelServerResponseError
		if response_id:
			ChannelServerResponseError.set_guid(e, response_id)

		source.try_send(Message.error_from_exc(e), response_id)

	# Define our completer when actions complete and return
	def action_complete_handler(self, source: object, action_name: str, result: Any, response_id: uuid.UUID) -> None:
		assert isinstance(source, ChannelClient)

		if isinstance(result, Dict):
			source.try_send(Message(data=result, is_final=True), response_id)
		else:
			self.logger.warning(f'Action {action_name} returned a non-dict, so nothing to do: {result!r}')

	####################
	# Router overrides #
	####################

	def on_start(self) -> None:
		self.dispatcher.start()

	def on_stop(self) -> None:
		self.dispatcher.stop(self.stop_timeout)

	def stop_serve(self, timeout: Optional[float] = None) -> None:
		self.stop_timeout = timeout
		super(ChannelServer, self).stop_serve()

	def on_new(self, connection: Connection, path: str) -> None:
		groups: Dict[str, str] = {}

		try:
			# Check if new connection provided a channel and room
			matches = re.match(r"^/(?P<channel>[^/]+)(/(?P<room>[^/]*)(/(?P<other>.*))?)?$", path)

			if matches:
				groups.update(matches.groupdict())

			# They absolutely must be provided, no excuses!
			if not groups.get('channel') or not groups.get('room'):
				raise RouterConnectionError("Path must be /<channel>/<room>/")

			self.logger.debug(f'New connection {connection} with path {path!r}')

			key = (groups['channel'], groups['room'])

			room = self.rooms.get(key)

			# For now if the room doesn't exist, create it.
			# TODO: Impl auth room master auth of some kind
			if room is None:
				self.logger.info(f'Channel {key!r} does not exist, creating')

				self.rooms[key] = room = set()

			self._add_connection(key, connection, room, groups.get('other'))

		# Forward these errors so their details are provided to client
		except RouterConnectionError as e:
			raise

		except Exception as e:
			connection.close(reason=str(e))

	def on_remove(self, connection: Connection) -> None:
		self._remove_connection(connection)

	def on_message(self, connection: Connection, message: Message) -> None:
		action_name = message.data.get('action')
		client = self._get_client(connection)

		response_id = message.data.get('response_id')

		try:
			if action_name is None:
				raise ChannelServerActionError('No action provided')

			action = self.dispatcher.actions.get(action_name)

			if not action:
				raise ChannelServerError(f'Invalid action {action_name!r}')

			# Dispatch our action
			# TODO: Maybe make dispatch() async so we can utilize return values and deprecate completion handler
			self.dispatcher.dispatch(source=client, action_name=action_name, args=message.data, response_id=response_id)

		except RouterError as e:
			self.logger.warning(f'Caught error while performing action: {e!r}')
			if response_id:
				ChannelServerResponseError.set_guid(e, response_id)
			raise

		except Exception as e:
			self.logger.error(f'dispatch error: {e!r}\n{traceback.format_exc()}')
			raise ChannelServerError(f'Unexpected error performing action: {e!r}')

	#########################
	# ChannelServer Actions #
	#########################

	@add_action()
	def action_whoami(self, client: ChannelClient) -> Dict:
		return {'id': client.id}

	@add_action(params={'targets': list, 'data': dict})
	async def action_send(self, convo: Conversation, targets: List[int], data: dict, client: ChannelClient) -> None:
		key = client.get_room_key()

		await self.send_to(
			data,
			targets,
			key,
			client.id,
		)

	@add_action()
	def action_enum_clients(self, client: ChannelClient) -> Dict:
		clients = self.rooms[client.get_room_key()]
		return {'client_ids': list(map(lambda c: c.id, clients))}


def cli_main() -> None:
	parser = argparse.ArgumentParser(
		description='Websocket channel-style server',
		formatter_class=argparse.ArgumentDefaultsHelpFormatter,
	)

	parser.add_argument(
		'--host',
		type=str,
		metavar='hostname',
		default='127.0.0.1',
		help='Host to bind on',
	)

	parser.add_argument(
		'--port',
		type=int,
		metavar='port',
		default=8765,
		help='Port to listen on',
	)

	parser.add_argument(
		'-l', '--log-level',
		type=str,
		choices=('DEBUG', 'WARN', 'WARNING', 'INFO', 'ERROR'),
		default='DEBUG',
	)

	args = parser.parse_args()

	logging.basicConfig(format='[%(name)s] [%(levelname)s] %(message)s')
	g_logger.setLevel(args.log_level.upper())

	router = ChannelServer(args.host, args.port)
	router.serve(daemon=False)


if __name__ == '__main__':
	cli_main()

__all__ = [
	'ChannelServerError',
	'ChannelServerActionError',
	'ChannelServerResponseError',
	'ChannelClient',
	'Conversation',
	'ChannelServer',
]
