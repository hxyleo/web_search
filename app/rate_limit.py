"""Limit the rate of starting top-level crawl requests, independently per host.

This does not govern redirects or subresources loaded by the browser.
"""
import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from urllib.parse import urlsplit


class RateLimitCapacityError(Exception):
    """The host tracker is full and no inactive host can be safely evicted."""


@dataclass
class _HostState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_started: float | None = None
    users: int = 0


class HostRateLimiter:
    def __init__(self, interval: float = 0.5, max_hosts: int = 1024):
        if interval < 0 or max_hosts < 1:
            raise ValueError("interval must be non-negative and max_hosts must be positive")
        self.interval = interval
        self.max_hosts = max_hosts
        self._hosts: OrderedDict[str, _HostState] = OrderedDict()
        self._lock = asyncio.Lock()

    async def wait(self, url: str) -> None:
        """Wait until a top-level request for this URL's host can start.

        Call immediately before sending the request. If the tracker is full of
        active/recent hosts, raise RateLimitCapacityError instead of evicting a
        still-relevant timestamp and accidentally allowing an early request.
        """
        try:
            host = (urlsplit(url).hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
        except (ValueError, UnicodeError) as exc:
            raise ValueError("invalid crawl URL host") from exc
        if not host:
            raise ValueError("crawl URL requires a host")

        async with self._lock:
            state = self._hosts.get(host)
            if state is None:
                now = time.monotonic()
                if len(self._hosts) >= self.max_hosts:
                    for old_host, old_state in self._hosts.items():
                        if not old_state.users and (old_state.last_started is None or
                                                    now - old_state.last_started >= self.interval):
                            del self._hosts[old_host]
                            break
                    else:
                        raise RateLimitCapacityError("too many active crawl hosts")
                state = _HostState()
                self._hosts[host] = state
            self._hosts.move_to_end(host)
            state.users += 1

        try:
            async with state.lock:
                if state.last_started is not None:
                    delay = state.last_started + self.interval - time.monotonic()
                    if delay > 0:
                        await asyncio.sleep(delay)
                state.last_started = time.monotonic()
        finally:
            # There is no await between decrement and function exit; an idle state
            # cannot be evicted while any waiter still holds a reference to it.
            state.users -= 1
