from concurrent.futures.thread import ThreadPoolExecutor
from threading import Lock
from typing import Optional


class _SharedThreadPool:
    def __init__(self) -> None:
        self._pool: Optional[ThreadPoolExecutor] = None
        self._lock = Lock()

    def get(self, max_threads: Optional[int] = None) -> ThreadPoolExecutor:
        with self._lock:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=max_threads, thread_name_prefix="api-client-worker")
            return self._pool

    def reset(self) -> None:
        with self._lock:
            if self._pool is not None:
                self._pool.shutdown()
            self._pool = None


# Use a thread pool singleton for every resource
THREAD_POOL = _SharedThreadPool()


def get_thread_pool(max_threads: Optional[int] = None) -> ThreadPoolExecutor:
    return THREAD_POOL.get(max_threads=max_threads)
