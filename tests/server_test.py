import logging
import pytest
from threading import Thread, Event
from asyncio import sleep as async_sleep, new_event_loop

from .common import *
from webswitch.channel_server import Conversation, add_action, ChannelClient
from webswitch.client import Client, ResponseException
from webswitch.message import Message


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

	@add_action()
	async def action_send_error_and_continue(self, convo: Conversation, client: 'ChannelClient'):
		response = await convo.send_and_recv(Message({'msg': 'give me nothing'}, error='some error'))
		raise Exception('Not suppose to receive a response!')


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
	# We can't assert these anymore because client d/cs will actually cancel active actions. Which is good. But
	# in order to actually not stop an active action upon disconnect, you could have multiple connections to one client.
	# TODO: Make another test like this one whenever multiple connections-per-client is implemented so we can actually
	# TODO: Test the timeout properly
	# assert filter_records(caplog.record_tuples, name_pattern=name_pattern, msg_pattern='Took too long to finish*')
	# assert filter_records(caplog.record_tuples, name_pattern=name_pattern, msg_pattern='Cancelling * active dispatches *')


@pytest.mark.asyncio
async def test_response_dispatch_no_cancel(get_server, get_client, caplog):
	server = get_server()
	server.serve(daemon=True)

	async with get_client() as client:
		convo = client.convo('timeout')
		response = await convo.send_and_expect(data={'timeout': 0.1})
		assert 'confirmed' in response.data

	# Make sure stop timeout does not expire
	wait_time = 0.2
	caplog.clear()
	with caplog.at_level(logging.DEBUG):
		server.stop_serve(wait_time)

	name_pattern = 'web-switch.ResponseDispatch:*'

	assert filter_records(caplog.record_tuples, name_pattern=name_pattern, msg_pattern='Stop requested with *')
	assert filter_records(caplog.record_tuples, name_pattern=name_pattern, msg_pattern=f'Waiting {wait_time} seconds *')
	assert not filter_records(caplog.record_tuples, name_pattern=name_pattern, msg_pattern='Took too long to finish*')
	assert not filter_records(caplog.record_tuples, name_pattern=name_pattern, msg_pattern='Cancelling * active dispatches *')
	assert filter_records(caplog.record_tuples, name_pattern=name_pattern, msg_pattern='All AwaitDispatches finished')
	assert filter_records(caplog.record_tuples, name_pattern=name_pattern, msg_pattern='All active dispatch actions finished')

@pytest.mark.asyncio
async def test_response_dispatch_do_cancel(get_server, get_client, caplog):
	server = get_server()
	server.serve(daemon=True)

	# Note: Because of Note 1, we have to create a client on a new event loop in a separate thread as to make sure the
	# client can handle disconnects from the server.

	client_send_event = Event()
	server_stop_event = Event()

	def thread_main():
		async def async_main():
			async with get_client() as client:
				convo = client.convo('timeout')
				# Make sure stop_serve() timeout expires while server sleeps for 10 seconds
				response = await convo.send_and_expect(data={'timeout': 10.0})
				assert 'confirmed' in response.data
				client_send_event.set()

				while not server_stop_event.is_set():
					await async_sleep(0.001)

		event_loop = new_event_loop()
		event_loop.run_until_complete(async_main())

	thread = Thread(name='Client event loop thread', target=thread_main)
	thread.start()
	client_send_event.wait()

	wait_time = 0.3

	caplog.clear()
	with caplog.at_level(logging.DEBUG):
		server.stop_serve(wait_time)

	server_stop_event.set()
	thread.join()

	name_pattern = 'web-switch.ResponseDispatch:*'

	assert filter_records(caplog.record_tuples, name_pattern=name_pattern, msg_pattern='Stop requested with *')
	assert filter_records(caplog.record_tuples, name_pattern=name_pattern, msg_pattern=f'Waiting {wait_time} seconds *')
	assert filter_records(caplog.record_tuples, name_pattern=name_pattern, msg_pattern='Took too long to finish*')
	assert filter_records(caplog.record_tuples, name_pattern=name_pattern, msg_pattern='Cancelling * active dispatches *')

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

@pytest.mark.asyncio
async def test_client_error(get_server, get_client):
	# When client disconnects due to an exception like an error sent to it from the server and the in-flight action
	# is expecting a response, that action needs to immediately cancel.
	server: ServerTestingServer = get_server()

	server.serve(daemon=True)

	with pytest.raises(ResponseException) as excinfo:
		async with get_client() as client:
			response = await client.convo('send_error_and_continue').send_and_expect({}, 2)

	assert excinfo.value.message == 'some error'

	with TimeBox(2) as window:
		server.stop_serve(window.timelimit)


# Note 1:
# It's important to note that we don't want to stop_serve() inside of client context because
# stop_serve() will try to close connections but only if they connections ever terminate or
# receive a close websocket frame. But since stop_serve() is being called within the client context,
# disconnect does not occur because stop_serve() is blocking client from exiting context and disconnecting.

