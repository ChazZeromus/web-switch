import re
from typing import *

from lib.webswitch import Client, Router

import logging

logger = logging.getLogger('Channel')
logger.setLevel(logging.DEBUG)

class ClientACL:
	def __init__(self, **kwargs):
		self.is_admin = False

		for key, val in kwargs:
			if not hasattr(self, key):
				raise Exception('Unknown ACL entry {!r}'.format(key))

			setattr(self, key, val)


class ChannelClient(Client):
	def __init__(self, router: Router, **kwargs):
		super(ChannelClient, self).__init__(router=router)
		self.acl = ClientACL(**kwargs)


class Channel(Router):
	MASTER_AUTH_TOKEN = 'hunter1'

	def __init__(self, host: str, port: int, max_queue_size: int = 100):
		super(Channel, self).__init__(host, port, max_queue_size)
		self.channels = {}  # type: Dict[Tuple[str, str], Set[Client]]

		self.broadcast_client = ChannelClient(self)

		logger.info('Creating channel server')

	def handle_new(self, client: Client, path: str) -> Optional[Client]:
		try:
			# TODO: Filter on printable chars only
			matches = re.match(r"^\\/(?P<channel>[^/])(?P<room>[^/])(?P<other_path>.+)$", path)

			if not matches:
				raise Exception("Path must be /<channel>/<room>/")

			groups = matches.groupdict()

			logger.debug('New client connection {!r} with path {!r}'.format(client, path))

			key = (groups['channel'], groups['room'])

			is_admin = False

			channel = self.channels.get(key)

			# If the room doesn't exist, create it if they know our secret password
			# TODO: Some master auth shit later
			if channel is None:
				logger.info('Channel {!r} does not exist, creating'.format(key))
				is_admin = True

				self.channels[key] = channel = set()

			new_client = ChannelClient(self)
			client.copy_to_subclass(new_client)

			channel.add(new_client)

			logger.debug('Added channel client {!r} to {!r}'.format(new_client, key))

			return new_client

		except Exception as e:
			client.close(reason=str(e))
			return None

if __name__ == '__main__':
	logging.basicConfig(format='[%(name)s] [%(levelname)s] %(message)s')

	router = Channel('localhost', 8765)
	router.serve()
