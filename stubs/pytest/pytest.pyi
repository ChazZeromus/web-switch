from typing import Optional, List, Callable, TypeVar, Type, Any
from types import TracebackType

__all__ = ['fixture', 'raises']

T = TypeVar('T', bound=Callable)

def fixture(
	scope: str = "function",
	params: Optional[List] = None,
	autouse: Optional[bool] = False,
	ids: Optional[List]=None,
	name: Optional[str]=None,
) -> Callable[[T], T]:
	...

class ExceptionInfo(object):
	def __init__(self) -> None:
		#: the exception class
		self.type: Type[BaseException]
		#: the exception instance
		self.value: Any
		#: the exception raw traceback
		self.tb: TracebackType
		#: the exception type name
		self.typename: str
		#: the exception traceback (_pytest._code.Traceback instance)
		self.traceback: Any

	def match(self, regexp: str) -> bool:
		...


class RaisesContext(object):
	def __enter__(self) -> ExceptionInfo: ...
	def __exit__(self, *args: Any, **kwargs: Any) -> None: ...

def raises(expected_exception: Type[BaseException], *args: Any, **kwargs: Any) -> RaisesContext:
	...
