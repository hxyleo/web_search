import asyncio
from types import SimpleNamespace

import pytest

from app import rate_limit
from app.rate_limit import HostRateLimiter, RateLimitCapacityError


def test_same_host_is_spaced_without_blocking_other_hosts(monkeypatch):
    async def run():
        now = [100.0]
        delays = []
        waiting = asyncio.Event()
        release = asyncio.Event()

        async def sleep(delay):
            delays.append(delay)
            waiting.set()
            await release.wait()
            now[0] += delay

        # Keep the event loop's own clock intact while controlling the limiter.
        monkeypatch.setattr(rate_limit, "time", SimpleNamespace(monotonic=lambda: now[0]))
        monkeypatch.setattr(rate_limit, "asyncio", SimpleNamespace(Lock=asyncio.Lock, sleep=sleep))

        limiter = HostRateLimiter(interval=0.5)
        await limiter.wait("https://Example.com/a")
        second = asyncio.create_task(limiter.wait("http://example.com/b"))
        await asyncio.wait_for(waiting.wait(), timeout=1)

        # A different host can start while the first host's next request waits.
        await asyncio.wait_for(limiter.wait("https://other.example/c"), timeout=1)
        assert not second.done()
        release.set()
        await second
        await limiter.wait("https://example.com/d")
        assert delays == [0.5, 0.5]

    asyncio.run(run())


def test_cancelled_waiter_does_not_reserve_or_leak_host_state(monkeypatch):
    async def run():
        now = [100.0]
        waiting = asyncio.Event()
        release = asyncio.Event()

        async def sleep(delay):
            waiting.set()
            await release.wait()

        monkeypatch.setattr(rate_limit, "time", SimpleNamespace(monotonic=lambda: now[0]))
        monkeypatch.setattr(rate_limit, "asyncio", SimpleNamespace(Lock=asyncio.Lock, sleep=sleep))

        limiter = HostRateLimiter(interval=0.5, max_hosts=1)
        await limiter.wait("https://example.com")
        waiter = asyncio.create_task(limiter.wait("https://example.com/queued"))
        await asyncio.wait_for(waiting.wait(), timeout=1)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        assert limiter._hosts["example.com"].users == 0
        with pytest.raises(RateLimitCapacityError):
            await limiter.wait("https://other.example")
        now[0] += 0.5
        await limiter.wait("https://other.example")
        assert list(limiter._hosts) == ["other.example"]

    asyncio.run(run())


def test_limiter_rejects_invalid_configuration_and_urls():
    with pytest.raises(ValueError):
        HostRateLimiter(max_hosts=0)
    with pytest.raises(ValueError):
        HostRateLimiter(interval=-1)

    async def run():
        with pytest.raises(ValueError):
            await HostRateLimiter().wait("not-a-url")

    asyncio.run(run())
