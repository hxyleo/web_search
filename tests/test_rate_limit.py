import asyncio
import time

import pytest

from app.rate_limit import HostRateLimiter, RateLimitCapacityError


def test_same_host_concurrent_requests_are_spaced_and_other_hosts_do_not_wait():
    async def run():
        limiter = HostRateLimiter(interval=0.04)
        starts = []

        async def start(url):
            await limiter.wait(url)
            starts.append((url, time.monotonic()))

        await asyncio.gather(start("https://Example.com/a"), start("http://example.com/b"),
                             start("https://other.example/c"), start("https://example.com/d"))
        same = [started for url, started in starts if "example.com" in url.lower()]
        assert len(same) == 3
        assert all(b - a >= 0.035 for a, b in zip(same, same[1:]))
        other = next(started for url, started in starts if "other.example" in url)
        assert other < same[1]
    asyncio.run(run())


def test_cancelled_waiter_does_not_reserve_or_leak_host_state():
    async def run():
        limiter = HostRateLimiter(interval=0.06, max_hosts=1)
        await limiter.wait("https://example.com")
        waiter = asyncio.create_task(limiter.wait("https://example.com/queued"))
        await asyncio.sleep(0.005)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert limiter._hosts["example.com"].users == 0
        with pytest.raises(RateLimitCapacityError):
            await limiter.wait("https://other.example")
        await asyncio.sleep(0.07)
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
