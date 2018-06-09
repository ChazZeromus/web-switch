import re
from typing import *
import traceback
import uuid
import argparse

from lib.dispatch import ResponseDispatcher, AwaitResponse, add_action, Action
from lib.webswitch import Client, Router, Message, WebswitchError, WebswitchConnectionError, WebswitchResponseError

import logging


class ChannelResponseError(WebswitchResponseError):
	def __init__(self, message: str, response: Union[uuid.UUID, 'AwaitResponse'], **data):
		super(ChannelResponseError, self).__init__(message=message, **data)
		self.set_guid(response)

	@staticmethod
	def set_guid(exception: WebswitchError, response: Union[uuid.UUID, 'AwaitResponse']):
		exception.error_data['response_id'] = response


class ClientACL:
	def __init__(self, **kwargs):
		self.is_admin = False

		for key, val in kwargs:
			if not hasattr(self, key):
				raise Exception(f'Unknown ACL entry {key!r}')

			setattr(self, key, val)


class ChannelClient(Client):
	def __init__(self, router: 'Channel', **kwargs):
		super(ChannelClient, self).__init__(router=router)
		self.acl = ClientACL(**kwargs)
		self.name = None  # type: str

class ChannelAwait(AwaitResponse):
	def __init__(self, client: ChannelClient, original: AwaitResponse):
		super(ChannelAwait, self).__init__(
			dispatcher=original.dispatcher,
			source=original.source,
			action_name=original.action_name,
			default_params=original.default_params,
			guid=original.guid
		)

		self.client = client

	async def send_and_recv(self, data: Dict, params: Dict[str, Type] = None, timeout: float = None):
		data.update(response_id=str(self.guid))

		self.client.send(Message(data=data))

		return await self(params=params, timeout=timeout)

class Channel(Router):
	def __init__(self, host: str, port: int, max_queue_size: int = 100):
		super(Channel, self).__init__(host, port, max_queue_size)
		self.channels = {}  # type: Dict[Tuple[str, str], Set[Client]]

		self.host, self.port = host, port

		self.broadcast_client = ChannelClient(self)


		self.action_handlers = {
			'whoami': (self.action_whoami, {}),
		}

		self.dispatcher = ResponseDispatcher(
			instance=self,
			exception_handler=self.action_exception_handler,
			argument_hook=self.argument_hook,
			common_params={'client': ChannelClient}
		)

		self.logger = logging.getLogger(f'ChannelRouter:{self.id}')
		self.logger.setLevel(logging.DEBUG)
		self.logger.info('Creating channel server')

	def handle_start(self):
		self.dispatcher.start()

	def handle_stop(self):
		self.dispatcher.stop()

	def argument_hook(self, args: Dict, source: object, action: Action) -> Dict:
		await_response = args.get('await_response')

		if await_response:
			assert isinstance(source, ChannelClient)

			new_ar = ChannelAwait(client=source, original=await_response)

			new_args = args.copy()
			new_args.update(await_response=new_ar)

			return new_args

		return args


	def handle_new(self, client: Client, path: str) -> Optional[Client]:
		groups = {'channel': None, 'room': None}

		try:
			# Check if new connection provided a channel and room
			matches = re.match(r"^/(?P<channel>[^/]+)(/(?P<room>[^/]*)(/(?P<other>.*))?)?$", path)

			if matches:
				groups.update(matches.groupdict())

			# They absolutely must be provided, no excuses!
			if not groups['channel'] or not groups['room']:
				raise WebswitchError("Path must be /<channel>/<room>/")

			self.logger.debug(f'New client connection {client} with path {path!r}')

			key = (groups['channel'], groups['room'])

			channel = self.channels.get(key)

			# For now if the room doesn't exist, create it.
			# TODO: Impl auth room master auth of some kind
			if channel is None:
				self.logger.info(f'Channel {key!r} does not exist, creating')

				self.channels[key] = channel = set()

			# Create our channel client instance and return it
			new_client = ChannelClient(self)
			channel.add(new_client)

			self.logger.debug(f'Added web-switch client {client!r} as Channel client to {key!r}')

			return new_client

		except Exception as e:
			client.close(reason=str(e))
			return None

	def handle_message(self, client: Client, message: Message):
		action_name = message.data.get('action')

		if action_name is None:
			raise WebswitchResponseError(
				'No action provided' if not action_name else f'Unknown action {action_name!r}'
			)

		action = self.dispatcher.actions.get(action_name)

		if not action:
			raise WebswitchResponseError(f'Invalid action {action!r}')

		data = message.data.get('data') or {}

		# Since we already provide a default argument 'client', remove it
		params = action.params.copy()
		del params['client']

		if data is None:
			if params:
				raise WebswitchResponseError('No data body provided')

		elif not isinstance(data, dict):
			raise WebswitchResponseError('Data body must be an object')

		response_id = message.data.get('response_id')

		data = {**data, 'client': client}

		# Dispatch our action

		try:
			self.dispatcher.dispatch(source=client, action_name=action_name, args=data, response_id=response_id)

		except WebswitchError as e:
			self.logger.warning(f'Caught error while performing action: {e!r}')
			ChannelResponseError.set_guid(e, response_id)
			client.send(Message.error_from_exc(e))

		except Exception as e:
			self.logger.error(f'{traceback.format_exc()}\ndispatch error: {e!r}')
			raise WebswitchError(f'Error performing action: {e!r}')

	# Define our exception handler for actions
	def action_exception_handler(self, source: object, action: str, e: Exception, response_id: uuid.UUID = None):
		self.logger.warning(f'Exception while dispatching for action {action} with source {source}: {e!r}')

		assert isinstance(source, ChannelClient)

		if not isinstance(e, WebswitchError):
			e = ChannelResponseError('Unexpected exception: {e}', response=response_id)

		# Try to set response ID even if error isn't ChannelResponseError
		if response_id:
			ChannelResponseError.set_guid(e, response_id)

		source.send(Message.error_from_exc(e))

	@add_action()
	def action_whoami(self, client: ChannelClient):
		new_message = Message(
			data={'id': client.client_id},
		)

		self.send_messages(recipients=[client], message=new_message)

	@add_action()
	def action_message(self, client: ChannelClient):
		pass

if __name__ == '__main__':
	parser = argparse.ArgumentParser(
		description='Websocket channel-style server',
		formatter_class=argparse.ArgumentDefaultsHelpFormatter
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
