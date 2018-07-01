import re
from typing import *
import traceback
import uuid
import argparse

from lib.dispatch import ResponseDispatcher, AwaitDispatch, AbstractAwaitDispatch, add_action, Action, ParameterSet
from lib.router.errors import RouterError, RouterResponseError, RouterConnectionError
from lib.router.connection import Connection
from lib.router.router import Router
from lib.message import Message

import logging

# TODO: What if instead of an error_type for RouterErrors, have an array of error_types?


class ChannelServerError(RouterError):
	def __init__(self, message: str, **data) -> None:
		super(ChannelServerError, self).__init__('channel_error', message=message, **data)


class ChannelServerActionError(ChannelServerError):
	def __init__(self, message: str, **data) -> None:
		super(ChannelServerActionError, self).__init__(message, **data)
		self.error_types.append('channel_action_error')


class ChannelServerResponseError(RouterResponseError):
	def __init__(self, message: str, response: Optional[Union[uuid.UUID, 'AwaitDispatch']], orig_exc: Exception = None, **data) -> None:
		new_data = dict(data)

		if orig_exc:
			new_data['exc_class'] = orig_exc.__class__.__name__

		super(ChannelServerResponseError, self).__init__(message=message, **new_data)
		self.error_types.append('channel_response')

		if response:
			self.set_guid(self, response)

	@staticmethod
	def set_guid(exception: RouterError, response: Union[uuid.UUID, 'AwaitDispatch']):
		exception.error_data['response_id'] = response


class ClientACL:
	def __init__(self, **kwargs) -> None:
		self.is_admin = False

		for key, val in kwargs:
			if not hasattr(self, key):
				raise Exception(f'Unknown ACL entry {key!r}')

			setattr(self, key, val)


# TODO: Create a ChannelClient class that can have multiple Connections
class ChannelClient(object):
	def __init__(self, channel: 'ChannelServer', conn: Connection, **kwargs) -> None:
		self.channel = channel
		self.conn = conn
		self.acl = ClientACL(**kwargs)
		self.name: Optional[str] = None

	async def send(self, message: Message, response_id: Optional[uuid.UUID]):
		if response_id is not None:
			message = message.clone()
			message.data.update(response_id=str(response_id))

		await self.channel.send_messages([self.conn], message)

	def try_send(self, message: Message, response_id: Optional[uuid.UUID]):
		if response_id is not None:
			message = message.clone()
			message.data.update(response_id=str(response_id))

		self.channel.try_send_messages([self.conn], message)


class Conversation(AbstractAwaitDispatch):
	def __init__(self, client: ChannelClient, original: AwaitDispatch) -> None:
		self.await_dispatch = original
		self.client = client

	def get_await_dispatch(self):
		return self.await_dispatch

	async def send(self, data: Dict):
		await self.client.send(Message(data=data), self.await_dispatch.guid)

	async def send_and_recv(self, data: Dict, params: Dict[str, Type] = None, timeout: float = None):
		await self.send(data)
		return await self(params=params, timeout=timeout)


class ChannelServer(Router):
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
		self.channels: Dict[Tuple[str, str], Set[ChannelClient]] = {}
		self.conn_to_client: Dict[Connection, ChannelClient] = {}

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

		self.logger = self.get_logger().getChild(f'ChannelRouter:{self.id}')
		self.logger.info('Creating channel server')

	def argument_hook(self, args: Dict, source: object, action: Action) -> Dict:
		await_dispatch = args.get('await_dispatch')

		new_args = args.copy()

		if await_dispatch:
			assert isinstance(source, ChannelClient)
			assert isinstance(await_dispatch, AwaitDispatch)

			new_conversation = Conversation(client=source, original=await_dispatch)

			# TODO: Having to use a class specific copy method for subclasses seems awkward, instead
			# TODO: why not provide an interface for
			new_args.update(convo=new_conversation)

		assert isinstance(source, ChannelClient)

		new_args['client'] = source

		return new_args

	def on_start(self):
		self.dispatcher.start()

	def on_stop(self):
		self.dispatcher.stop(self.stop_timeout)

	def stop_serve(self, timeout: float = None):
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
			if not groups['channel'] or not groups['room']:
				raise RouterConnectionError("Path must be /<channel>/<room>/")

			self.logger.debug(f'New connection {connection} with path {path!r}')

			key = (groups['channel'], groups['room'])

			channel = self.channels.get(key)

			# For now if the room doesn't exist, create it.
			# TODO: Impl auth room master auth of some kind
			if channel is None:
				self.logger.info(f'Channel {key!r} does not exist, creating')

				self.channels[key] = channel = set()

			# Create our channel connection instance
			new_client = ChannelClient(self, connection)
			self.conn_to_client[connection] = new_client
			channel.add(new_client)

			self.logger.debug(f'Added web-switch connection {connection!r} as Channel connection to {key!r}')

		except Exception as e:
			connection.close(reason=str(e))

	def on_message(self, connection: Connection, message: Message):
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

	def on_remove(self, connection: Connection):
		# TODO: Remove and cleanup ChannelClients from whatever lists, dicts
		pass

	def _get_client(self, connection: Connection):
		client = self.conn_to_client.get(connection)

		if not client:
			self.logger.error(f'Could not find client from connection {connection!r}')
			raise ChannelServerError(f'Could not find client with given connection')

		return client

	# Define our exception handler for actions so we can send back a
	async def action_exception_handler(self, source: object, action_name: str, e: Exception, response_id: uuid.UUID):
		self.logger.warning(f'Exception while dispatching for action {action_name} with source {source}: {e!r}')

		assert isinstance(source, ChannelClient)

		if not isinstance(e, RouterError):
			e = ChannelServerResponseError(f'Unexpected non-RouterError exception: {e}', orig_exc=e, response=response_id)

		# Try to set response ID even if error isn't ChannelServerResponseError
		if response_id:
			ChannelServerResponseError.set_guid(e, response_id)

		source.try_send(Message.error_from_exc(e), response_id)

	# Define our completer when actions complete and return
	def action_complete_handler(self, source: object, action_name: str, result: Any, response_id: uuid.UUID):
		assert isinstance(source, ChannelClient)

		if isinstance(result, Dict):
			source.try_send(Message(data=result), response_id)
		else:
			self.logger.warning(f'Action {action_name} returned a non-dict, so nothing to do: {result!r}')

	@add_action()
	def action_whoami(self, client: ChannelClient):
		return {'id': client.conn.conn_id}

	@add_action()
	def action_message(self, client: ChannelClient):
		pass


def cli_main():
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

	args = parser.parse_args()

	logging.basicConfig(format='[%(name)s] [%(levelname)s] %(message)s')

	router = ChannelServer(args.host, args.port)
	router.serve(daemon=False)


if __name__ == '__main__':
	cli_main()
