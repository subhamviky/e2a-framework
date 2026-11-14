"""A client built on first use, so from_config() never opens a connection. A profile can then be
bound and verified (bindings.verify()) without cloud credentials or network, and the client is
created where the adapter first talks to its service."""
import threading
from typing import Any, Callable


class Lazy:
    def __init__(self, factory: Callable[[], Any]) -> None:
        object.__setattr__(self, '_factory', factory)
        object.__setattr__(self, '_target', None)
        object.__setattr__(self, '_lock', threading.Lock())

    def _get(self) -> Any:
        if self._target is None:
            with self._lock:
                if self._target is None:
                    object.__setattr__(self, '_target', self._factory())
        return self._target

    def __getattr__(self, name: str) -> Any:
        return getattr(self._get(), name)
