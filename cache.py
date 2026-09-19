from __future__ import annotations

import time
from typing import Any, Callable, TypeVar

T = TypeVar("T")


class TtlCache:
    def __init__(self) -> None:
        self._store: dict[str, tuple[float, Any]] = {}

    def get_or_set(self, key: str, ttl_seconds: float, factory: Callable[[], T]) -> T:
        now = time.time()
        hit = self._store.get(key)
        if hit and hit[0] > now:
            return hit[1]
        value = factory()
        self._store[key] = (now + ttl_seconds, value)
        return value


cache = TtlCache()
