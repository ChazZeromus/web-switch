from asyncio import sleep as async_sleep
import asyncio
import pytest
import uuid
import itertools
import time
from typing import *

from lib.router.errors import RouterError
from lib.client import Client, ResponseException, ResponseTimeoutException, MessageQueues
from lib.channel_server import Conversation, add_action, ChannelClient
from lib.message import Message

from .common import *


class ClientTestingServer(ChannelServerBase):
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

# @pytest.mark.asyncio
# async def test_send(get_client: Callable[[], Client], get_server: Callable[[], ClientTestingServer]):
# 	with get_server():
# 		async with get_client() as client1, get_client() as client2:
# 			await client1.convo('send').send({
# 				'targets': [client2.client_id],
# 				'data': {'msg': 'yo'}
# 			})


def test_message_queue():
	mq = MessageQueues(10)

	uuids = [
		uuid.UUID('2e29fa61-13c9-4583-b09b-9247eff7e55f'),
		uuid.UUID('65d999cc-db56-46e0-b78f-332b3d3d7106'),
		uuid.UUID('c39aea80-ac5d-4486-80fe-3aa9a199d54c'),
	]

	messages = [Message(data={'id': _}) for _ in range(11)]

	msg_iter = iter(messages)


	mq.add(uuids[0], next(msg_iter))
	time.sleep(0.5)  # Ensure there is a discernable gap in time

	old_msg = next(msg_iter)
	mq.add(uuids[0], old_msg)
	time.sleep(0.5)
	mq.add(uuids[0], next(msg_iter))
	time.sleep(0.1)

	for msg in itertools.islice(msg_iter, 3):
		time.sleep(0.01)
		mq.add(uuids[1], msg)

	for msg in itertools.islice(msg_iter, 4):
		time.sleep(0.01)
		mq.add(uuids[2], msg)

	assert set(uuids) == set(mq.get_guids())
	assert mq.get_messages(uuids[0]) == messages[:3]
	assert mq.get_messages(uuids[1]) == messages[3:6]
	assert mq.get_messages(uuids[2]) == messages[6:10]
	assert mq.count == 10

	mq.add(uuids[1], next(msg_iter))

	assert mq.count == 10

	assert mq.get_messages(uuids[0]) == messages[1:3]
	assert mq.get_messages(uuids[1]) == messages[3:6] + [messages[10]]

	mq.remove_oldest(0.49)

	assert set(mq.get_messages(uuids[0])) == set(messages[1:3]) - {old_msg}



# TODO: Test active source cancelling
# TODO: On both Client- and Channel-side, should register all timeouts so that upon server close
# TODO: all pending timeouts are cancelled
