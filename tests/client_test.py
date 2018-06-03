import pytest
import socket
from contextlib import closing
import websockets
import json
import asyncio

from lib import channel


class Client:
	def __init__(self, ws_url):
		self.connection = websockets.connect(ws_url)
		self.ctx = None
		self.response_id = None

	async def __aenter__(self, *args, **kwargs) -> 'Client':
		self.ctx = await self.connection.__aenter__(*args, **kwargs)
		return self

	async def __aexit__(self, *args, **kwargs) -> None:
		await self.connection.__aexit__(*args, **kwargs)

	async def send_and_wait(self, data: dict, timeout=3):
		copy = data.copy()

		if self.response_id:
			copy.update(response_id=self.response_id)

		await self.ctx.send(json.dumps(copy))

		fut = asyncio.get_event_loop().create_future()  # type: asyncio.Future

		async def timeout_callback():
			await asyncio.sleep(timeout)
			fut.set_exception(Exception(f'send_and_wait timeout out after {timeout}'))

		timeout_fut = asyncio.ensure_future(timeout_callback())

		response = await self.ctx.recv()
		timeout_fut.cancel()

		r_dict = json.loads(response)

		error = r_dict.get('error')

		if error:
			raise Exception(f'Response error: {error}')

		response_id = r_dict.get('response_id')

		if response_id:
			self.response_id = response_id

		return r_dict


def find_free_port():
	with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
		s.bind(('', 0))
		return s.getsockname()[1]

@pytest.fixture(scope='session')
def free_port():
	return find_free_port()

@pytest.fixture(scope='function')
def channel_server(free_port):
	server = channel.Channel('127.0.0.1', free_port)

	server.serve(daemon=True)

	yield server

	server.stop_serve()


@pytest.mark.asyncio
async def test_whoami(channel_server: channel.Channel):
	async with Client(f'ws://localhost:{channel_server.port}/foo/bar') as client:
		reply = await client.send_and_wait({'action': 'whoami'})

		my_id = reply.get('id')

		assert my_id, 'Did not receive an ID from whoami'
		assert isinstance(my_id, int), 'Is not int'

@pytest.mark.asyncio
async def test_whoami_again(channel_server: channel.Channel):
	async with Client(f'ws://localhost:{channel_server.port}/foo/bar') as client:
		reply = await client.send_and_wait({'action': 'whoami'})

		my_id = reply.get('id')

		assert my_id, 'Did not receive an ID from whoami'
		assert isinstance(my_id, int), 'Is not int'
