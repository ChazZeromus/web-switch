import pytest
import socket
from contextlib import closing
import websockets
import json
import asyncio

from lib.channel import Channel, ChannelClient, add_action, AwaitResponse, ChannelAwait

class ResponseException(Exception):
	def __init__(self, message, *, exc_type):
		super(ResponseException, self).__init__(message)
		self.exc_type = exc_type

	def __repr__(self):
		return f'ResponseException({self.exc_type},{self.args})'

	def __str__(self):
		return self.exc_type

class ChannelFixture(Channel):
	def __init__(self, port: int):
		super(ChannelFixture, self).__init__('localhost', port)

	@add_action(params={'arg': str})
	async def action_test_conversation(self, arg: str, client: 'ChannelClient',  await_response: ChannelAwait):

		response = await await_response.send_and_recv(dict(data=f'You said {arg}', whatoyousaid=f'is {arg}'))

		arg = response.get('arg')

		assert arg, 'arg was not in response'
		assert arg != 'ok', 'response was not "ok"'

		response = await await_response.send_and_recv(dict(data=f'What is 2+2?'))

		arg = response.get('arg')

		assert arg == 4, 'Incorrect response'

	@add_action()
	async def action_async_raise(self, client: 'ChannelClient', await_response: ChannelAwait):
		raise Exception('something happened!')

	def __enter__(self):
		self.serve(daemon=True)
		return self

	def __exit__(self, exc_type, exc_val, exc_tb):
		self.stop_serve()


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
async def client_with_server(free_port):
	with ChannelFixture(free_port):
		async with Client(f'ws://localhost:{free_port}/foo/bar') as client:
			yield client

@pytest.mark.asyncio
async def test_whoami(client_with_server):
	client = client_with_server

	reply = await client.send_and_wait({'action': 'whoami'})

	my_id = reply.get('id')

	assert my_id, 'Did not receive an ID from whoami'
	assert isinstance(my_id, int), 'Is not int'

@pytest.mark.asyncio
async def test_async_raise(client_with_server):
	client = client_with_server

	with pytest.raises(TestException) as excinfo:
		await client.send_and_wait({'action': 'async_raise'})

