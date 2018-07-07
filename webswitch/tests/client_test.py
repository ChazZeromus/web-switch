from asyncio import sleep as async_sleep
import asyncio
import pytest
from typing import *

from lib.router.errors import RouterError
from lib.client import Client, ResponseException, ResponseTimeoutException
from lib.channel_server import Conversation, add_action, ChannelClient

from .common import *


class ClientTestingServer(ChannelServerBase):
	@add_action(params={'arg': str})
	async def action_test_conversation(self, arg: str, client: 'ChannelClient', convo: Conversation):

		response = await convo.send_and_recv({'foo1': f'You said {arg}', 'foo2': f'is {arg}'})

		arg = response.get('arg')

		assert arg, 'arg was not in response'
		assert arg == 'ok', 'response was not "ok"'

		response = await convo.send_and_recv({'data': 'What is 2+2?'}, expect_params={'arg': int})

		arg = response.get('arg')

		assert arg == 4, 'Incorrect response'

	@add_action()
	async def action_async_raise(self, client: 'ChannelClient', convo: Conversation):
		raise RouterError(error_types='foo', message='something happened!')

	@add_action(params={'timeout': float})
	async def action_client_timeout_test(self, timeout: float, client: 'ChannelClient', convo: Conversation):
		await async_sleep(timeout)
		await convo.send({'data': 'all done!'})


@pytest.fixture(name='get_server', scope='function')
def get_server_fixture(free_port) -> Callable[[], ClientTestingServer]:
	def func():
		return ClientTestingServer(free_port)

	return func


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
		await convo.send_and_expect({'timeout': 0.2}, timeout=0.1)

@pytest.mark.asyncio
async def test_unknown_action(client_with_server: Client):
	convo = client_with_server.convo('this_action_is_fake')

	with pytest.raises(ResponseException) as excinfo:
		await convo.send_and_expect({})

	assert excinfo.match('Invalid action')


@pytest.mark.parametrize("count", [10, 5])
@pytest.mark.asyncio
async def test_enum(count: int, get_client: Callable[[], Client], get_server: Callable[[], ClientTestingServer]):
	with get_server() as server:
		clients = []

		for i in range(count):
			clients.append(get_client())

		await asyncio.gather(*(c.__aenter__() for c in clients))

		collected_ids = set()

		for client in clients:
			message = await client.convo('whoami').send_and_expect({})
			assert 'id' in message.data
			collected_ids.add(message.data['id'])

		client1 = clients[0]

		message = await client1.convo('enum_clients').send_and_expect({})

		await asyncio.gather(*(c.__aexit__(None, None, None) for c in clients))

		data = message.data

		assert isinstance(data, dict)
		assert 'client_ids' in data
		assert isinstance(data['client_ids'], list)

		enum_ids = set(data['client_ids'])

		assert enum_ids == collected_ids


@pytest.mark.asyncio
async def test_send(get_client: Callable[[], Client], get_server: Callable[[], ClientTestingServer]):
	with get_server():
		async with get_client() as client1, get_client() as client2:
			await client1.convo('send').send({
				'targets': [client2.client_id],
				'data': {'msg': 'yo'}
			})

			response = await client2.convo(None).expect(2.0)

			assert response.data.get('msg') == 'yo'
			assert response.data.get('sender_id') == client1.client_id

# TODO: Test active source cancelling
# TODO: On both Client- and Channel-side, should register all timeouts so that upon server close
# TODO: all pending timeouts are cancelled
