from collections.abc import Callable
from threading import Lock
from typing import cast

_UNSET = object()


class LazyValueCache[**P, ValueT]:
    def __init__(self, loader: Callable[P, ValueT]) -> None:
        self._loader = loader
        self._lock = Lock()
        self._value: ValueT | object = _UNSET

    def invalidate(self) -> None:
        with self._lock:
            self._value = _UNSET

    def get(self, *args: P.args, **kwargs: P.kwargs) -> ValueT:
        value = self._value
        if value is not _UNSET:
            return cast(ValueT, value)

        with self._lock:
            value = self._value
            if value is not _UNSET:
                return cast(ValueT, value)

            value = self._loader(*args, **kwargs)
            self._value = value
            return value
