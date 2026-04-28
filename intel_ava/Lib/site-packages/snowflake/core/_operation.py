from collections.abc import Iterable, Iterator
from concurrent.futures import CancelledError, Future
from typing import Any, Callable, TypeVar


T = TypeVar("T")
U = TypeVar("U")

class PollingOperation(Future[U]):
    """Encapsulates the asynchronous execution of a callable.

    Supports all methods provided by the :class:`concurrent.futures.Future` class. :class:`PollingOperation` instances
    are not intended to be created directly.

    :param: transformer - a callable that is executed with the result of the delegated future as an argument.
        The result of the callable is accessible via the :func:`result` method.

    Examples
    ________
    >>> operation = root.databases.iter_async()

    Getting a value returned by the call, waiting indefinitely if the call hasn't completed yet:

    >>> result = operation.result()

    Getting a value returned by the call, waiting up to 60 seconds if the call hasn't completed yet:

    >>> result = operation.result(timeout=60)

    Getting whether the call is currently being executed:

    >>> is_running = operation.running()

    Cancelling the call if it has not started yet:

    >>> cancelled = operation.cancel()

    Getting an exception raised by the call, waiting indefinitely if the call hasn't completed yet:

    >>> exception = operation.exception()

    Getting an exception raised by the call, waiting up to 60 seconds if the call hasn't completed yet:

    >>> exception = operation.exception(timeout=60)

    Scheduling 100 task creation jobs and waiting for all of them to complete:

    >>> operations = [task_collection.create_async(Task(name=f"task_{n}", definition="select 1")) for n in range(100)]
    >>> concurrent.futures.wait(operations)
    """

    def __init__(self, delegate: Future[T], transformer: Callable[[T], U]) -> None:
        self._delegate = delegate
        self._transformer = transformer
        super().__init__()
        delegate.add_done_callback(self._callback)

    def cancel(self) -> bool:
        if not self._delegate.cancel():
            return False
        return super().cancel()

    def _callback(self, _: Any) -> None:
        try:
            result = self._delegate.result()
            self.set_result(self._transformer(result))
        except CancelledError:
            return  # do nothing
        except Exception as e:
            self.set_exception(e)


class PollingOperations:
    @staticmethod
    def empty(future: Future[T]) -> PollingOperation[None]:
        return PollingOperation(future, lambda _: None)

    @staticmethod
    def identity(future: Future[T]) -> PollingOperation[T]:
        return PollingOperation(future, lambda x: x)

    @staticmethod
    def iterator(future: Future[Iterable[T]]) -> PollingOperation[Iterator[T]]:
        return PollingOperation(future, lambda x: iter(x))
