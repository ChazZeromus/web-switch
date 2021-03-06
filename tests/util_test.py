import itertools
import time
import uuid
from typing import NamedTuple, List

from webswitch.client import MessageQueues
from webswitch.index_map import IndexMap
from webswitch.message import Message


def test_message_queue() -> None:
	mq = MessageQueues(10)

	uuids: List[uuid.UUID] = [
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

	first = mq.get_messages(uuids[0])

	assert first
	assert set(first) == set(messages[1:3]) - {old_msg}


class Item(NamedTuple):
	a: int
	b: str


def test_index_map() -> None:
	im: IndexMap[Item] = IndexMap('a', 'b', 'c')

	items: List[Item] = [
		Item(a=1, b='foo'),
		Item(a=2, b='bar'),
		Item(a=3, b='bar'),
		Item(a=3, b='foo'),
		Item(a=4, b='baz'),
	]

	for item in items:
		im.add(item, a=item.a, b=item.b, c='df')

	assert set(im.lookup(a=4, b='baz')) == {items[4]}

	im.remove(items[4])

	assert not im.lookup(a=4)
	assert not im.lookup(b='baz')
	assert not im.lookup(a=4, b='baz')

	items.remove(items[4])

	assert set(im.lookup(c='df')) == set(items)
	assert set(im.lookup(b='bar')) == set(items[1:3])
	assert set(im.lookup(a=3)) == set(items[2:4])
	assert set(im.lookup(a=3, b='foo')) == {items[3]}
	assert set(im._indexes.keys()) == {'a', 'b', 'c'}

	for item in im:
		im.remove(item)

	assert not im._items
	assert not im._indexes['a']
	assert not im._indexes['b']
	assert not im._indexes['c']
	assert not im._inverse_indexes

