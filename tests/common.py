import fnmatch
import socket
import time
from types import TracebackType
from contextlib import closing
from types import TracebackType
from typing import *

import pytest

from webswitch.client import Client
from webswitch.channel_server import ChannelServer

HOSTNAME: str = '127.0.0.1'
PORT: Optional[int] = None  # None for auto-select


class ChannelServerBase(ChannelServer):
	def __init__(self, port: int) -> None:
		super(ChannelServerBase, self).__init__('localhost', port)

	def __enter__(self) -> 'ChannelServerBase':
		self.serve(daemon=True)
		return self

	def __exit__(self, exc_type: Optional[BaseException], exc_val: Any, exc_tb: TracebackType) -> None:
		self.stop_serve()

# TODO: raise NotImplement() version of get_server should be defined here?


@pytest.fixture(name='get_server', scope='function')
def get_server_fixture(free_port: int) -> Callable[[], ChannelServerBase]:
	raise NotImplemented('Must override this fixture in module')


@pytest.fixture(scope='function')
async def client_with_server(
	get_client: Callable[[], Client],
	get_server: Callable[[], ChannelServerBase]
) -> AsyncIterable[Client]:
	with get_server():
		async with get_client() as client:
			yield client


def filter_records(
		records: Iterable[Tuple[str, str, str]],
		name_pattern: Optional[str] = None,
		msg_pattern: Optional[str] = None,
) -> List[Tuple[str, str, str]]:
	filtered = []
	for name, level, msg in records:
		if name_pattern and not fnmatch.fnmatch(name, name_pattern):
			continue

		if msg_pattern and not fnmatch.fnmatch(msg, msg_pattern):
			continue

		filtered.append((name, level, msg))

	return filtered


def find_free_port() -> int:
	global PORT

	if PORT:
		return PORT

	with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
		s.bind(('', 0))
		return cast(Tuple[Any, int], s.getsockname())[1]


@pytest.fixture(scope='session')
def free_port() -> int:
	return find_free_port()


@pytest.fixture(scope='function')
def get_client(free_port: int) -> Callable[[], Client]:
	def func() -> Client:
		return Client(f'ws://{HOSTNAME}:{free_port}/foo/bar')

	return func


class TimeBox(object):
	def __init__(self, window: float, slack: float = 0.01) -> None:
		self._timelimit = window
		self._slack = slack
		self._start: Optional[float] = None
		self._elapsed: Optional[float] = None

	@property
	def timelimit(self) -> float:
		return self._timelimit

	@property
	def elapsed(self) -> Optional[float]:
		return self._elapsed

	@property
	def within_timelimit(self) -> bool:
		assert self._elapsed is not None
		return self._elapsed < self._timelimit - self._slack

	def __enter__(self) -> 'TimeBox':
		self._start = time.monotonic()
		return self

	def __exit__(self, exc_type: Optional[BaseException], exc_val: Any, exc_tb: TracebackType) -> None:
		assert self._start is not None
		self._elapsed = time.monotonic() - self._start

		assert self.within_timelimit, f'Operation did not complete with timebox of {self._timelimit} seconds'



__all__ = [
	'get_client',
	'free_port',
	'find_free_port',
	'filter_records',
	'client_with_server',
	'ChannelServerBase',
	'TimeBox',
	'HOSTNAME',
	'PORT',
]
