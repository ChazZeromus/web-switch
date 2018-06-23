import pytest
import socket
from asyncio import sleep as async_sleep
from contextlib import closing

from lib.channel import Channel, ChannelClient, add_action, Conversation
from lib.router.errors import RouterError
from lib.client import Client, ResponseException, ResponseTimeoutException


class ChannelFixture(Channel):
	def __init__(self, port: int):
		super(ChannelFixture, self).__init__('localhost', port)

	def __enter__(self):
		self.serve(daemon=True)
		return self

	def __exit__(self, exc_type, exc_val, exc_tb):
		self.stop_serve()

	@add_action(params={'arg': str})
	async def action_test_conversation(self, arg: str, client: 'ChannelClient', convo: Conversation):

		response = await convo.send_and_recv({'foo1': f'You said {arg}', 'foo2': f'is {arg}'})

		arg = response.get('arg')

		assert arg, 'arg was not in response'
		assert arg == 'ok', 'response was not "ok"'

		response = await convo.send_and_recv({'data': 'What is 2+2?'}, params={'arg': int})

		arg = response.get('arg')

		assert arg == 4, 'Incorrect response'

	@add_action()
	async def action_async_raise(self, client: 'ChannelClient', convo: Conversation):
		raise RouterError(error_types='foo', message='something happened!')

	@add_action(params={'timeout': float})
	async def action_client_timeout_test(self, timeout: float, client: 'ChannelClient', convo: Conversation):
		await async_sleep(timeout)
		await convo.send({'data': 'all done!'})

	@add_action()
	async def action_server_timeout_test(self, client: 'ChannelClient', convo: Conversation):
		await convo.send_and_recv({'data': 'yo'}, timeout=0.1)

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
	convo = client_with_server.convo('whoami')

	reply = await convo.send_and_expect({})

	my_id = reply.data.get('id')

	assert my_id, 'Did not receive an ID from whoami'
	assert isinstance(my_id, int), 'Is not int'


@pytest.mark.asyncio
async def test_async_raise(client_with_server: Client):
	convo = client_with_server.convo('async_raise')

	with pytest.raises(ResponseException) as excinfo:
		await convo.send_and_expect({})

	assert 'foo' in excinfo.value.error_types


@pytest.mark.asyncio
async def test_convo(client_with_server: Client):
	convo = client_with_server.convo('test_conversation')
	response = await convo.send_and_expect({'arg': 'yo'})

	assert response.data.get('foo1') == 'You said yo'
	assert response.data.get('foo2') == 'is yo'

	response = await convo.send_and_expect({'arg': 'ok'})

	assert response.data.get('data') == 'What is 2+2?'

	await convo.send(data=dict(arg=4))


@pytest.mark.asyncio
async def test_client_timeout(client_with_server: Client):
	convo = client_with_server.convo('client_timeout_test')

	with pytest.raises(ResponseTimeoutException) as excinfo:
		await convo.send_and_expect({'timeout': 10.0}, timeout=0.1)


@pytest.mark.asyncio
async def test_server_timeout(client_with_server: Client):
	convo = client_with_server.convo('server_timeout_test')

	with pytest.raises(ResponseException) as excinfo:
		# Send nothing and we should get yo back
		response = await convo.send_and_expect({})

		assert response.data.get('data') == 'yo'

		# Server is now expecting a response back but it should timeout by itself
		# so wait until it timeouts and raise exception inside expect()

		await convo.expect(2.0)

	assert excinfo.value.data.get('exc_class') == 'DispatchAwaitTimeout', 'Incorrect server exception class'

# TODO: Test active source cancelling
# TODO: On both Client- and Channel-side, should register all timeouts so that upon server close
# TODO: all pending timeouts are cancelled
