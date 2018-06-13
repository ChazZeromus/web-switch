import pytest
import socket
from contextlib import closing

from lib.channel import Channel, ChannelClient, add_action, Conversation
from lib.router.errors import RouterError
from lib.client import Client, ResponseException


class ChannelFixture(Channel):
	def __init__(self, port: int):
		super(ChannelFixture, self).__init__('localhost', port)

	@add_action(params={'arg': str})
	async def action_test_conversation(self, arg: str, client: 'ChannelClient', convo: Conversation):

		response = await convo.send_and_recv(dict(data=f'You said {arg}', whatoyousaid=f'is {arg}'))

		arg = response.get('arg')

		assert arg, 'arg was not in response'
		assert arg != 'ok', 'response was not "ok"'

		response = await convo.send_and_recv(dict(data=f'What is 2+2?'))

		arg = response.get('arg')

		assert arg == 4, 'Incorrect response'

	@add_action()
	async def action_async_raise(self, client: 'ChannelClient', convo: Conversation):
		raise RouterError(error_types='foo', message='something happened!')

	def __enter__(self):
		self.serve(daemon=True)
		return self

	def __exit__(self, exc_type, exc_val, exc_tb):
		self.stop_serve()


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
	convo = client_with_server.convo()

	reply = await convo.send_and_wait({'action': 'whoami'})

	my_id = reply.data.get('id')

	assert my_id, 'Did not receive an ID from whoami'
	assert isinstance(my_id, int), 'Is not int'


@pytest.mark.asyncio
async def test_async_raise(client_with_server: Client):
	convo = client_with_server.convo()

	with pytest.raises(ResponseException) as excinfo:
		await convo.send_and_wait({'action': 'async_raise'})

	assert 'foo' in excinfo.value.error_types

# TODO: Test active source cancelling

