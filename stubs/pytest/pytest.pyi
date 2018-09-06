from typing import Optional, List, Callable, TypeVar

__all__ = ['fixture']

T = TypeVar('T', bound=Callable)

def fixture(
	scope: str = "function",
	params: Optional[List] = None,
	autouse: Optional[bool] = False,
	ids: Optional[List]=None,
	name: Optional[str]=None,
) -> Callable[[T], T]:
	...

