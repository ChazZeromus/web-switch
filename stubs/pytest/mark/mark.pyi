from typing import Callable, TypeVar, Sequence, Tuple, Optional, List, Union, Any

__all__ = ['asyncio', 'parametrize']

T = TypeVar('T', bound=Callable)

def asyncio(func: T) -> T:
	...

def parametrize(
	argnames: str,
	argvalues: Sequence[Any],
	indirect: Optional[bool] = False,
	ids: Optional[Union[List[int], Callable[[Any], Optional[str]]]] = None,
	scope: Optional[str] = None,
) -> Callable[[T], T]:
	...
