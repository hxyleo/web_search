"""Bounded, process-local async cache with coalescing for identical requests."""

import asyncio
import copy
import json
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable


class CacheBusy(Exception):
    """The bounded work queue is full."""


class AsyncTTLCache:
    def __init__(
        self,
        ttl: float = 600,
        max_entries: int = 256,
        max_bytes: int = 16 * 1024 * 1024,
        max_entry_bytes: int = 1024 * 1024,
        max_inflight: int = 64,
    ) -> None:
        self.ttl = ttl
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.max_entry_bytes = max_entry_bytes
        self.max_inflight = max_inflight
        self.entries: OrderedDict[tuple, tuple[float, int, dict]] = OrderedDict()
        self.size = 0
        self.inflight: dict[tuple, asyncio.Task] = {}

    def _remove(self, key: tuple) -> None:
        _, size, _ = self.entries.pop(key)
        self.size -= size

    async def get_or_create(
        self, key: tuple, create: Callable[[], Awaitable[tuple[dict, bool]]]
    ) -> dict:
        # The section before the first await runs atomically on the event loop.
        entry = self.entries.get(key)
        if entry:
            if entry[0] > time.monotonic():
                self.entries.move_to_end(key)
                return copy.deepcopy(entry[2])
            self._remove(key)
        task = self.inflight.get(key)
        if task is None:
            if len(self.inflight) >= self.max_inflight:
                raise CacheBusy()
            task = asyncio.create_task(self._create(key, create))
            self.inflight[key] = task
            # Consume exceptions if all waiting clients disconnect.
            task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        # A client disconnect must not cancel work shared by other callers.
        return copy.deepcopy(await asyncio.shield(task))

    async def _create(self, key: tuple, create: Callable[[], Awaitable[tuple[dict, bool]]]) -> dict:
        try:
            value, cacheable = await create()
            if cacheable:
                size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
                if self.ttl > 0 and size <= self.max_entry_bytes and size <= self.max_bytes:
                    while self.entries and (
                        len(self.entries) >= self.max_entries or self.size + size > self.max_bytes
                    ):
                        self._remove(next(iter(self.entries)))
                    if self.max_entries > 0:
                        self.entries[key] = (
                            time.monotonic() + self.ttl,
                            size,
                            copy.deepcopy(value),
                        )
                        self.size += size
            return value
        finally:
            self.inflight.pop(key, None)
