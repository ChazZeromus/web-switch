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


class ChannelError(RouterError):
	def __init__(self, message: str):
		super(ChannelError, self).__init__(error_type='channel_error', message=message)


class ChannelResponseError(RouterResponseError):
	def __init__(self, message: str, response: Union[uuid.UUID, 'AwaitDispatch'], **data):
		super(ChannelResponseError, self).__init__(message=message, error_type='channel_response', **data)
		self.set_guid(self, response)

	@staticmethod
	def set_guid(exception: RouterError, response: Union[uuid.UUID, 'AwaitDispatch']):
		exception.error_data['response_id'] = response


class ClientACL:
	def __init__(self, **kwargs):
		self.is_admin = False

		for key, val in kwargs:
			if not hasattr(self, key):
				raise Exception(f'Unknown ACL entry {key!r}')

			setattr(self, key, val)


# TODO: Create a ChannelClient class that can have multiple Connections
class ChannelClient(object):
	def __init__(self, channel: 'Channel', conn: Optional[Connection], **kwargs):
		self.channel = channel
		self.conn = conn
		self.acl = ClientACL(**kwargs)
		self.name = None  # type: str

	def send(self, message: Message, response_id: Optional[uuid.UUID]):
		if response_id is not None:
			message = message.clone()
			message.data.update(response_id=str(response_id))

		self.channel.send_messages([self.conn], message)


class Conversation(AbstractAwaitDispatch):
	def __init__(self, client: ChannelClient, original: AwaitDispatch):
		self.await_dispatch = original
		self.client = client

	def get_await_dispatch(self):
		return self.await_dispatch

	def send_and_recv(self, data: Dict, params: Dict[str, Type] = None, timeout: float = None):
		self.client.send(Message(data=data), self.await_dispatch.guid)
		return self(params=params, timeout=timeout)


class Channel(Router):
	_common_intrinsic_params = {
		'client': ChannelClient
	}
	_common_exposed_params = {}

	non_async_params = ParameterSet(exposed=_common_exposed_params, intrinsic=_common_intrinsic_params)
	async_params = ParameterSet(
		exposed=_common_exposed_params,
		intrinsic={
			**_common_intrinsic_params,
			'convo': Conversation,
		}
	)

	def __init__(self, host: str, port: int, max_queue_size: int = 100):
		super(Channel, self).__init__(host, port, max_queue_size)
		self.channels = {}  # type: Dict[Tuple[str, str], Set[ChannelClient]]
		self.conn_to_client = {}  # type: Dict[Connection, ChannelClient]

		self.host, self.port = host, port

		self.broadcast_client = ChannelClient(self, None)

		self.dispatcher = ResponseDispatcher(
			common_params=self.non_async_params,
			common_async_params=self.async_params,
			instance=self,
			exception_handler=self.action_exception_handler,
			complete_handler=self.action_complete_handler,
			argument_hook=self.argument_hook,
		)

		self.logger = logging.getLogger(f'ChannelRouter:{self.id}')
		self.logger.setLevel(logging.DEBUG)
		self.logger.info('Creating channel server')

	def argument_hook(self, args: Dict, source: object, action: Action) -> Dict:
		await_dispatch = args.get('await_dispatch')

		if await_dispatch:
			assert isinstance(source, ChannelClient)
			assert isinstance(await_dispatch, AwaitDispatch)

			new_conversation = Conversation(client=source, original=await_dispatch)

			# TODO: Having to use a class specific copy method for subclasses seems awkward, instead
			# TODO: why not provide an interface for
			new_args = args.copy()
			new_args.update(convo=new_conversation)

			return new_args

		return args

	def on_start(self):
		self.dispatcher.start()

	def on_stop(self):
		self.dispatcher.stop()

	def on_new(self, connection: Connection, path: str) -> None:
		groups = {'channel': None, 'room': None}

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

		if action_name is None:
			raise RouterResponseError(
				'No action provided' if not action_name else f'Unknown action {action_name!r}'
			)

		action = self.dispatcher.actions.get(action_name)

		if not action:
			raise RouterResponseError(f'Invalid action {action_name!r}')

		data = message.data.get('data') or {}

		# Since we already provide a default argument 'connection', remove it
		params = action.params.copy()
		del params['client']

		if data is None:
			if params:
				raise RouterResponseError('No data body provided')

		elif not isinstance(data, dict):
			raise RouterResponseError('Data body must be an object')

		response_id = message.data.get('response_id')

		data = {**data, 'client': client}

		# Dispatch our action

		try:
			self.dispatcher.dispatch(source=client, action_name=action_name, args=data, response_id=response_id)

		except RouterError as e:
			self.logger.warning(f'Caught error while performing action: {e!r}')
			ChannelResponseError.set_guid(e, response_id)
			client.send(Message.error_from_exc(e))

		except Exception as e:
			self.logger.error(f'{traceback.format_exc()}\ndispatch error: {e!r}')
			raise ChannelError(f'Unexpected error performing action: {e!r}')

	def on_remove(self, connection: Connection):
		# TODO: Remove and cleanup ChannelClients from whatever lists, dicts
		pass

	def _get_client(self, connection: Connection):
		client = self.conn_to_client.get(connection)

		if not client:
			self.logger.error(f'Could not find client from connection {connection!r}')
			raise ChannelError(f'Could not find client with given connection')

		return client

	# Define our exception handler for actions
	def action_exception_handler(self, source: object, action_name: str, e: Exception, response_id: uuid.UUID = None):
		self.logger.warning(f'Exception while dispatching for action {action_name} with source {source}: {e!r}')

		assert isinstance(source, ChannelClient)

		if not isinstance(e, RouterError):
			e = ChannelResponseError(f'Unexpected exception: {e}', response=response_id)

		# Try to set response ID even if error isn't ChannelResponseError
		if response_id:
			ChannelResponseError.set_guid(e, response_id)

		source.send(Message.error_from_exc(e), response_id)

	# Define our completer when actions complete and return
	def action_complete_handler(self, source: object, action_name: str, result: Any, response_id: uuid.UUID = None):
		assert isinstance(source, ChannelClient)

		if isinstance(result, Dict):
			source.send(Message(data=result), response_id)
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

	router = Channel(args.host, args.port)
	router.serve(daemon=False)


if __name__ == '__main__':
	cli_main()
