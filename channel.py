import re
from typing import *

from lib.dispatch import ResponseDispatcher, add_dispatch
from lib.webswitch import Client, Router, Message, WebswitchResponseError

import logging

logger = logging.getLogger('Channel')
logger.setLevel(logging.DEBUG)


class ClientACL:
	def __init__(self, **kwargs):
		self.is_admin = False

		for key, val in kwargs:
			if not hasattr(self, key):
				raise Exception(f'Unknown ACL entry {key!r}')

			setattr(self, key, val)


class ChannelClient(Client):
	def __init__(self, router: Router, **kwargs):
		super(ChannelClient, self).__init__(router=router)
		self.acl = ClientACL(**kwargs)
		self.name = None  # type: str


class Channel(Router):
	MASTER_AUTH_TOKEN = 'hunter1'
	dispatch = ResponseDispatcher({'client': Client, 'message': Message})

	def __init__(self, host: str, port: int, max_queue_size: int = 100):
		super(Channel, self).__init__(host, port, max_queue_size)
		self.channels = {}  # type: Dict[Tuple[str, str], Set[Client]]

		self.broadcast_client = ChannelClient(self)

		logger.info('Creating channel server')

		self.action_handlers = {
			'whoami': (self.action_whoami, {}),
		}

	def handle_new(self, client: Client, path: str) -> Optional[Client]:
		groups = {'channel': None, 'room': None}

		try:
			# Check if new connection provided a channel and room
			matches = re.match(r"^/(?P<channel>[^/]+)(/(?P<room>[^/]*)(/(?P<other>.*))?)?$", path)

			if matches:
				groups.update(matches.groupdict())

			# They absolutely must be provided, no excuses!
			if not groups['channel'] or not groups['room']:
				raise Exception("Path must be /<channel>/<room>/")

			logger.debug(f'New client connection {client} with path {path!r}')

			key = (groups['channel'], groups['room'])

			channel = self.channels.get(key)

			# For now if the room doesn't exist, create it.
			# TODO: Impl auth room master auth of some kind
			if channel is None:
				logger.info(f'Channel {key!r} does not exist, creating')

				self.channels[key] = channel = set()

			# Create our channel client instance and return it
			new_client = ChannelClient(self)
			channel.add(new_client)

			logger.debug(f'Added web-switch client {client!r} as Channel client to {key!r}')

			return new_client

		except Exception as e:
			client.close(reason=str(e))
			return None

	def handle_message(self, client: Client, message: Message):
		action = message.data.get('action')

		if action is None:
			raise WebswitchResponseError('No action provided' if not action else f'Unknown action {action!r}')

		data = message.data.get('data')

		if data is None:
			raise WebswitchResponseError('No data body provided')

		if not isinstance(data, dict):
			raise WebswitchResponseError('Data body must be an object')

		data = {**data, 'client': client, 'message': message}

		try:
			self.dispatch.dispatch(instance=self, action=action, args=data)
		except Exception as e:
			raise WebswitchResponseError(f'Error performing action: {e}')

	@add_dispatch(action='whoami', dispatcher=dispatch, params={})
	def action_whoami(self, client: Client, message: Message):
		new_message = Message(
			sender=client,
			data={'id': client.client_id},
		)

		self.send_messages(sender=client, recipients=[client], message=new_message)

	@add_dispatch(action='message', dispatcher=dispatch, params={})
	def action_message(self, client: Client, message: Message):
		pass

if __name__ == '__main__':
	logging.basicConfig(format='[%(name)s] [%(levelname)s] %(message)s')

	router = Channel('localhost', 8765)
	router.serve()
