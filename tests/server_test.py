import logging
import pytest
from asyncio import sleep as async_sleep

from .common import *
from lib.channel_server import Conversation, add_action, ChannelClient, AwaitDispatch
from lib.client import Client, ResponseException


class UniqueError(Exception):
	def __init__(self):
		super(UniqueError, self).__init__('unique', 'UniqueError')


class ServerTestingServer(ChannelServerBase):
	@add_action()
	async def action_server_timeout_test(self, client: 'ChannelClient', convo: Conversation):
		await convo.send_and_recv({'data': 'yo'}, timeout=0.1)

	@add_action(params={'timeout': float})
	async def action_timeout(self, timeout: float, client: 'ChannelClient', convo: Conversation):
		await convo.send({'confirmed': True})
		await async_sleep(timeout)

	@add_action()
	def action_raise_unique_error(self, client: 'ChannelClient'):
		raise UniqueError()

	@add_action()
	async def action_raise_unique_error_async(self, client: 'ChannelClient', convo: Conversation):
		raise UniqueError()

	@add_action()
	async def action_async_return(self, convo: Conversation, client: 'ChannelClient') -> dict:
		return {'data': 'hello a little later!'}

	@add_action()
	def action_nonasync_return(self, client: 'ChannelClient') -> dict:
		return {'data': 'hello right now!'}


@pytest.fixture(scope='function')
def get_server(free_port):
	def func():
		return ServerTestingServer(free_port)

	return func


@pytest.mark.asyncio
async def test_response_dispatch_timeout(get_server, get_client, caplog):
	server = get_server()
	server.serve(daemon=True)

	async with get_client() as client:
		convo = client.convo('timeout')
		response = await convo.send_and_expect(data={'timeout': 0.2})
		assert 'confirmed' in response.data

	wait_time = 0.1

	caplog.clear()
	# Make sure stop timeout expires
	with caplog.at_level(logging.DEBUG):
		server.stop_serve(wait_time)

	name_pattern = 'web-switch.ResponseDispatch:*'

	assert filter_records(caplog.record_tuples, name_pattern=name_pattern, msg_pattern='Stop requested with *')
	assert filter_records(caplog.record_tuples, name_pattern=name_pattern, msg_pattern=f'Waiting {wait_time} seconds *')
	assert filter_records(caplog.record_tuples, name_pattern=name_pattern, msg_pattern='Took too long to finish*')
	assert filter_records(caplog.record_tuples, name_pattern=name_pattern, msg_pattern='Cancelling * actions')


@pytest.mark.asyncio
async def test_response_dispatch_cancel(get_server, get_client, caplog):
	server = get_server()
	server.serve(daemon=True)

	async with get_client() as client:
		convo = client.convo('timeout')
		response = await convo.send_and_expect(data={'timeout': 0.1})
		assert 'confirmed' in response.data

	wait_time = 0.2

	caplog.clear()
	# Make sure stop timeout expires
	with caplog.at_level(logging.DEBUG):
		server.stop_serve(wait_time)

	name_pattern = 'web-switch.ResponseDispatch:*'

	assert filter_records(caplog.record_tuples, name_pattern=name_pattern, msg_pattern='Stop requested with *')
	assert filter_records(caplog.record_tuples, name_pattern=name_pattern, msg_pattern=f'Waiting {wait_time} seconds *')
	assert not filter_records(caplog.record_tuples, name_pattern=name_pattern, msg_pattern='Took too long to finish*')
	assert not filter_records(caplog.record_tuples, name_pattern=name_pattern, msg_pattern='Cancelling * actions')
	assert filter_records(caplog.record_tuples, name_pattern=name_pattern, msg_pattern='All AwaitDispatches finished')
	assert filter_records(caplog.record_tuples, name_pattern=name_pattern, msg_pattern='All active dispatch actions finished')


@pytest.mark.asyncio
async def test_unique_error_nonasync(client_with_server: Client):
	convo = client_with_server.convo('raise_unique_error')

	with pytest.raises(ResponseException) as excinfo:
		await convo.send_and_expect({})

	assert excinfo.value.data.get('exc_class') == 'UniqueError', 'Expecting UniqueError non-async'


@pytest.mark.asyncio
async def test_unique_error_async(client_with_server: Client):
	convo = client_with_server.convo('raise_unique_error_async')

	with pytest.raises(ResponseException) as excinfo:
		await convo.send_and_expect({})

	assert excinfo.value.data.get('exc_class') == 'UniqueError', 'Expecting UniqueError async'


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

	assert excinfo.value.data.get('exc_class') == 'DispatchAwaitTimeout', 'Timeout server exception class'

@pytest.mark.asyncio
async def test_async_return(client_with_server: Client):
	message = await client_with_server.convo('async_return').send_and_expect({})
	assert 'data' in message.data
	assert 'later' in message.data['data']

@pytest.mark.asyncio
async def test_nonasync_return(client_with_server: Client):
	message = await client_with_server.convo('nonasync_return').send_and_expect({})
	assert 'data' in message.data
	assert 'now' in message.data['data']
