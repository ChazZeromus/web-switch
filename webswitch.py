#!/usr/bin/env python3

import asyncio
import websockets
from websockets.server import WebSocketServerProtocol
import json
from queue import Queue
import signal
from threading import Thread
from collections import AsyncIterable
import time
import logging

client_count = 0

logger = logging.getLogger()

class Message(dict):
	@classmethod
	def error(cls, message):
		return Message(error=message)

	@classmethod
	def payload(cls, data):
		return Message(data=data)

	def toJSON(self):
		return json.dumps(self)

class Client(object):
	def __init__(self, router, ws: WebSocketServerProtocol, **extra_kwargs):
		self.extra = extra_kwargs
		self.ws = ws
		self.router = router

		router.connection_index += 1

		self.id = router.connection_index

	def __repr__(self):
		return 'Client(id: {}, {!r},{!r})'.format(self.id, self.ws.remote_address, self.ws.port)

	def __str__(self):
		return repr(self)

	def send(self, data: [Message, str]):
		if isinstance(data, Message):
			data = data.toJSON()

		def callback():
			async def func():
				await self.ws.send(data)
				print('Sent payload to {!r}: {!r}'.format(self, data))

			asyncio.ensure_future(func(), loop=self.router.event_loop)

		self.router.event_loop.call_soon_threadsafe(callback)
		# asyncio.run_coroutine_threadsafe(func(data), self.router.event_loop).result()

	def close(self):

		def callback():
			async def func():
				await self.ws.close()
				print('Closed {!r}'.format(self))

			asyncio.ensure_future(func(), loop=self.router.event_loop)


		self.router.event_loop.call_soon_threadsafe(callback)
		# self.router.event_loop.call_soon_threadsafe(func)
		# asyncio.run_coroutine_threadsafe(func(), self.router.event_loop).result()


def _route_thread(router):
	print('Main thread started')

	while True:
		client, data = router.receive_queue.get()

		if client is None:
			if router.connections:
				raise Exception('Connections still exist!')
			break

		assert isinstance(client, Client)

		if data is None:
			router.connections.remove(client)
			client.close()

			# future = asyncio.run_coroutine_threadsafe(client.ws.close(), router.event_loop)
			# future.result()
			continue

		print('got: {!r}'.format(data))

		router.handle_message(client, data)


class Router(object):
	def __init__(self, host, port, max_queue_size=100):
		self.host, self.port = host, port

		self.connections = []

		self.receive_queue = Queue(100)

		self.closed = False

		self.event_loop = asyncio.new_event_loop()
		self.event_loop.set_debug(True)

		self.connection_index = 0

	def serve(self):
		asyncio.set_event_loop(self.event_loop)
		serve_task = websockets.serve(self.on_connect, 'localhost', 8765)
		server = serve_task.ws_server

		def loop_thread():
			asyncio.set_event_loop(self.event_loop)
			self.event_loop.run_until_complete(serve_task)
			self.event_loop.run_forever()

			self.event_loop.run_until_complete(self.event_loop.shutdown_asyncgens())

			print('Shutting down socket server')
			server.close()

			print('Waiting for websocket server to die')
			self.event_loop.run_until_complete(server.wait_closed())
			self.event_loop.close()

		loop_thread = Thread(target=loop_thread)
		loop_thread.start()

		# prev = signal.signal(signal.SIGINT, signal.SIG_IGN)
		main_thread = Thread(target=_route_thread, args=(self,))
		main_thread.start()
		# signal.signal(signal.SIGINT, prev)

		print('Serving {}:{}'.format(self.host, self.port))

		try:
			while True:
				time.sleep(1)

		except KeyboardInterrupt:
			print('Canceling')

		self.closed = True

		print('Closing {} connections'.format(len(self.connections)))

		for client in self.connections[:]:
			self.receive_queue.put((client, None))

		print('Waiting for main thread to finish')
		self.receive_queue.put((None, None))

		main_thread.join()

		# Get all uncompleted tasks to wait on, noteworthy that we're calling this
		# before calling func() and creating a task as to not wait for itself
		# and thus causing .result() to wait indefinitely.
		pending = asyncio.Task.all_tasks()

		async def func():
			# Note that we will get a CancelledError upon calling .result() if
			# returns_exception is not set, this is due to the list of pending
			# tasks containing the async-for in on_connect being already cancelled
			# because of the route_thread issuing close() calls, and you can't
			# await a cancelled Task.
			ret = await asyncio.gather(*pending, return_exceptions=True)

			# print('Finished results ({})'.format(len(ret)), ret)
			# ret = [r for r in ret if r]
			# print('Finished results truthy ({})'.format(len(ret)), ret)

		print('Finishing remaining tasks')

		asyncio.run_coroutine_threadsafe(func(), loop=self.event_loop).result()

		print('Shutting down event loop thread')
		self.event_loop.call_soon_threadsafe(self.event_loop.stop)

		loop_thread.join()

		print('Socket server shutdown.')

	async def on_connect(self, websocket: [WebSocketServerProtocol, AsyncIterable], path):
		if self.closed:
			print('Rejecting connection', websocket)
			websocket.close()
			return

		client = Client(router=self, ws=websocket, path=path)

		print('connected: {!r} {}'.format(path, client))

		self.connections.append(client)

		async for message in websocket:
			self.receive_queue.put((client, message))

		print('{} closed'.format(client))

	def handle_message(self, client: Client, data: str):
		try:
			json_obj = json.loads(data)
		except Exception as e:
			logger.error('Could not decode json {!r} from {!r}: {!r}'.format(data, client, e))
			client.send(Message.error('Decode error'))
			return

		client.send('That a nice message')


if __name__ == '__main__':
	router = Router('localhost', 8765)
	router.serve()

# loop = asyncio.get_event_loop()
#
# thread = Thread(target=loop.run_forever)
# thread.start()
#
# async def foo():
# 	await asyncio.sleep(1)
#
# k = [None]
#
# def ya():
# 	k[0] = asyncio.ensure_future(foo())
# 	k[0].cancel()
#
# loop.call_soon_threadsafe(ya)
#
# time.sleep(2)
#
# l = asyncio.Task.all_tasks()
#
# async def foo():
# 	fut = asyncio.gather(*k, return_exceptions=True)
# 	print(await fut)
# asyncio.run_coroutine_threadsafe(foo(), loop).result()
#

