import fnmatch
import socket
from contextlib import closing
from typing import Iterable, Tuple, List

import pytest

from lib.client import Client
from lib.channel import Channel, ChannelClient, add_action, Conversation

HOSTNAME = '127.0.0.1'


class ChannelServerBase(Channel):
	def __init__(self, port: int):
		super(ChannelServerBase, self).__init__('localhost', port)

	def __enter__(self):
		self.serve(daemon=True)
		return self

	def __exit__(self, exc_type, exc_val, exc_tb):
		self.stop_serve()

# TODO: raise NotImplement() version of get_server should be defined here?

@pytest.fixture(scope='function')
async def client_with_server(get_client, get_server):
	with get_server():
		async with get_client() as client:
			yield client


def filter_records(
		records: Iterable[Tuple[str, str, str]],
		name_pattern: str = None,
		msg_pattern: str = None,
) -> List[Tuple[str, str, str]]:
	filtered = []
	for name, level, msg in records:
		if name_pattern and not fnmatch.fnmatch(name, name_pattern):
			continue

		if msg_pattern and not fnmatch.fnmatch(msg, msg_pattern):
			continue

		filtered.append((name, level, msg))

	return filtered


def find_free_port():
	with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
		s.bind(('', 0))
		return s.getsockname()[1]


@pytest.fixture(scope='session')
def free_port():
	return find_free_port()


@pytest.fixture(scope='function')
def get_client(free_port):
	def func():
		return Client(f'ws://{HOSTNAME}:{free_port}/foo/bar')

	return func