from typing import *

T = TypeVar('T')


class IndexMap(Generic[T]):
	def __init__(self, *index_names: str) -> None:
		self._items: Set[T] = set()
		self._indexes: Dict[str, Dict[Any, Set[T]]] = {
			name: {} for name in index_names
		}
		self._inverse_indexes: Dict[T, Dict[str, Any]] = {}

	def add(self, item: T, **kwargs: Any):
		if item in self._items:
			raise Exception(f'Active {item!r} already exists')

		for index_name, key in kwargs.items():
			index = self._indexes.get(index_name)

			if index is None:
				raise Exception(f'Specified index {index_name!r} does not exist')

			item_set = index.get(key)

			if item_set is None:
				index[key] = item_set = set()

			item_set.add(item)

		self._inverse_indexes[item] = {
			index_name: key for index_name, key in kwargs.items()
		}

		self._items.add(item)

	def __len__(self):
		return len(self._items)

	def __bool__(self):
		return bool(self._items)

	def remove(self, item: T):
		if item not in self._items:
			raise Exception(f'Active {item!r} does not exist')

		for index_name, key in self._inverse_indexes[item].items():
			index_set = self._indexes[index_name][key]
			index_set.remove(item)

			if not index_set:
				del self._indexes[index_name][key]

		self._items.remove(item)

		del self._inverse_indexes[item]

	def lookup(self, **kwargs: Any) -> Set[T]:
		result: Optional[Set[T]] = None

		if not kwargs:
			return set()

		for index_name, key in kwargs.items():
			index = self._indexes.get(index_name)

			if index is None:
				raise Exception(f'Specified index {index_name!r} does not exist')

			item_set = index.get(key)

			if not item_set:
				return set()

			if result is None:
				result = item_set.copy()
			else:
				result &= item_set

		assert result is not None

		return result

	def lookup_one(self, **kwargs: Any) -> T:
		results = self.lookup(**kwargs)

		if len(results) == 0:
			raise Exception(f'No such item of {kwargs!r} exists')
		elif len(results) > 1:
			raise Exception(f'More than one item of {kwargs!r} exists')

		return list(results)[0]

	def try_lookup_one(self, **kwargs: Any) -> Optional[T]:
		results = self.lookup(**kwargs)

		if len(results) == 0:
			return None
		elif len(results) > 1:
			raise Exception(f'More than one item of {kwargs!r} exists')

		return list(results)[0]

	def __iter__(self) -> Iterator[T]:
		return iter(list(self._items))


__all__ = [
	'IndexMap'
]