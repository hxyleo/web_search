import asyncio

import httpx
import pytest

from app import main


@pytest.fixture(autouse=True)
def disable_host_delay_for_api_tests(monkeypatch):
    # Host pacing is covered separately; keep API tests deterministic across loops.
    monkeypatch.setattr(main, "host_limiter", main.HostRateLimiter(interval=0))


def request(payload, upstream_handler):
    async def run():
        main.search_cache = main.SearchCache()
        main.crawl_cache = main.SearchCache()
        upstream = httpx.AsyncClient(transport=httpx.MockTransport(upstream_handler))
        async def override_client():
            return upstream
        main.app.dependency_overrides[main.get_client] = override_client
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
                return await client.post("/search", json=payload)
        finally:
            main.app.dependency_overrides.clear()
            await upstream.aclose()
    return asyncio.run(run())


def test_search_removes_duplicate_and_invalid_urls():
    def handler(req):
        assert req.url.path == "/search"
        assert req.url.params["format"] == "json"
        assert req.url.params["q"] == "test"
        return httpx.Response(200, json={"results": [
            {"title": "One", "url": "https://example.com/a#top", "content": "summary"},
            {"title": "Duplicate", "url": "https://example.com/a#bottom"},
            {"url": "file:///etc/passwd"},
            {"title": "Two", "url": "https://example.org/"},
        ]})
    response = request({"query": " test ", "max_results": 2}, handler)
    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "test"
    assert [r["url"] for r in body["results"]] == ["https://example.com/a", "https://example.org/"]
    assert body["results"][0]["raw_content"] is None
    assert isinstance(body["response_time"], (float, int))


def test_dedup_preserves_meaningful_url_differences():
    def handler(req):
        return httpx.Response(200, json={"results": [
            {"title": "original", "url": "https://EXAMPLE.com:443"},
            {"title": "same homepage", "url": "https://example.com/#fragment"},
            {"title": "first query", "url": "https://example.com/?id=1"},
            {"title": "second query", "url": "https://example.com/?id=2"},
            {"title": "different case path", "url": "https://example.com/Article"},
            {"title": "different case path 2", "url": "https://example.com/article"},
            {"url": "https://@example.com/"},
            {"url": "https://bad host.example/"},
        ]})
    results = request({"query": "test", "max_results": 10}, handler).json()["results"]
    assert [r["title"] for r in results] == [
        "original", "first query", "second query", "different case path", "different case path 2"
    ]
    assert results[0]["url"] == "https://EXAMPLE.com:443"


def test_diversity_uses_nearby_candidates_without_losing_results():
    def handler(req):
        return httpx.Response(200, json={"results": [
            {"title": "a1", "url": "https://a.example/1"},
            {"title": "a2", "url": "https://www.a.example/2"},
            {"title": "a3", "url": "https://a.example/3"},
            {"title": "a4", "url": "https://a.example/4"},
            {"title": "b1", "url": "https://b.example/1"},
            {"title": "a5", "url": "https://a.example/5"},
        ]})
    results = request({"query": "test", "max_results": 5}, handler).json()["results"]
    assert [r["title"] for r in results] == ["a1", "a2", "b1", "a3", "a4"]


def test_diversity_does_not_pull_distant_or_discard_single_source_results():
    items = [{"title": str(i), "url": f"https://a.example/{i}"} for i in range(6)]
    items.append({"title": "distant", "url": "https://b.example/"})
    assert [r["title"] for r in main.select_results(items, 3)] == ["0", "1", "2"]
    assert len(main.select_results(items[:5], 5)) == 5


def test_raw_content_and_failure_are_distinguished(monkeypatch):
    monkeypatch.setattr(main, "CRAWL4AI_API_TOKEN", "test-token")
    def handler(req):
        if req.url.path == "/search":
            return httpx.Response(200, json={"results": [{"url": "https://example.com"}, {"url": "https://example.org"}]})
        assert req.url.path == "/crawl"
        assert req.headers["authorization"] == "Bearer test-token"
        if b"example.com" in req.content:
            return httpx.Response(200, json={"results": [{"success": True, "markdown": {"raw_markdown": "# Page"}}]})
        return httpx.Response(200, json={"results": [{"success": False}]})
    response = request({"query": "test", "include_raw_content": True}, handler)
    assert response.status_code == 200
    assert response.json()["results"][0]["raw_content"] == "# Page"
    assert response.json()["results"][1]["raw_content"] is None
    assert "crawl_error" in response.json()["results"][1]


def test_validation_and_unsupported_fields():
    def handler(req):
        raise AssertionError("invalid requests must not reach upstream")
    for payload in ({"query": "  "}, {"query": "ok", "max_results": 11},
                    {"query": "ok", "include_raw_content": "true"},
                    {"query": "ok", "include_answer": True}):
        assert request(payload, handler).status_code == 422


def test_raw_content_reports_ssrf_rejection(monkeypatch):
    monkeypatch.setattr(main, "CRAWL4AI_API_TOKEN", "test-token")
    def handler(req):
        if req.url.path == "/search":
            return httpx.Response(200, json={"results": [{"url": "https://example.com"}]})
        return httpx.Response(400, json={"detail": "URL blocked (SSRF protection): URL blocked"})
    result = request({"query": "test", "include_raw_content": True}, handler).json()["results"][0]
    assert result["raw_content"] is None
    assert result["crawl_error"] == "URL blocked by Crawl4AI SSRF protection"


def test_search_failure_and_partial_engine_warning():
    def failure(req):
        raise httpx.ConnectError("unavailable", request=req)
    assert request({"query": "ok"}, failure).status_code == 502
    def timeout(req):
        raise httpx.ReadTimeout("timed out", request=req)
    assert request({"query": "ok"}, timeout).status_code == 504
    def partial(req):
        return httpx.Response(200, json={"results": [], "unresponsive_engines": [["x", "failed"]]})
    response = request({"query": "ok"}, partial)
    assert response.status_code == 200
    assert "No results returned" in response.json()["warning"]
    def no_results(req):
        return httpx.Response(200, json={"results": [], "unresponsive_engines": []})
    assert "warning" not in request({"query": "ok"}, no_results).json()
    def partial_with_results(req):
        return httpx.Response(200, json={"results": [{"url": "https://example.com"}], "unresponsive_engines": [["x", "failed"]]})
    assert "Some SearXNG" in request({"query": "ok"}, partial_with_results).json()["warning"]


def test_cache_expiry_and_request_parameters(monkeypatch):
    async def run():
        now = [100.0]
        monkeypatch.setattr("app.cache.time.monotonic", lambda: now[0])
        main.search_cache = main.SearchCache(ttl=600)
        main.crawl_cache = main.SearchCache(ttl=600)
        calls = []
        def handler(req):
            calls.append(req.url.path)
            if req.url.path == "/crawl":
                return httpx.Response(200, json={"results": [{"success": True, "markdown": "# text"}]})
            return httpx.Response(200, json={"results": [{"url": "https://example.com"}, {"url": "https://other.example.com"}]})
        monkeypatch.setattr(main, "CRAWL4AI_API_TOKEN", "test-token")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
            payload = main.SearchRequest(query="one")
            first = await main.search(payload, upstream)
            first["results"][0]["title"] = "mutated"
            assert (await main.search(payload, upstream))["results"][0]["title"] == ""
            assert calls == ["/search"]
            assert len((await main.search(main.SearchRequest(query="one", max_results=2), upstream))["results"]) == 2
            await main.search(main.SearchRequest(query="one", include_raw_content=True), upstream)
            assert calls == ["/search", "/crawl", "/crawl"]
            await main.search(main.SearchRequest(query="different", max_results=1, include_raw_content=True), upstream)
            assert calls == ["/search", "/crawl", "/crawl", "/search"]
            now[0] += 601
            await main.search(payload, upstream)
            assert calls.count("/search") == 3
    asyncio.run(run())


def test_cache_coalesces_concurrent_requests():
    async def run():
        main.search_cache = main.SearchCache()
        main.crawl_cache = main.SearchCache()
        calls = []
        async def handler(req):
            calls.append(req.url.path)
            await asyncio.sleep(0.02)
            if req.url.path == "/crawl":
                return httpx.Response(200, json={"results": [{"success": True, "markdown": "# text"}]})
            return httpx.Response(200, json={"results": [{"url": "https://example.com"}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
            responses = await asyncio.gather(*(main.search(main.SearchRequest(query="same"), upstream) for _ in range(5)))
            assert calls == ["/search"]
            assert all(r["results"] for r in responses)
            main.CRAWL4AI_API_TOKEN, old_token = "test-token", main.CRAWL4AI_API_TOKEN
            try:
                await asyncio.gather(*(main.search(main.SearchRequest(query=f"q{i}", include_raw_content=True), upstream) for i in range(5)))
                assert calls.count("/crawl") == 1
            finally:
                main.CRAWL4AI_API_TOKEN = old_token
    asyncio.run(run())


def test_cache_does_not_store_upstream_or_crawl_failures(monkeypatch):
    async def run():
        main.search_cache = main.SearchCache()
        main.crawl_cache = main.SearchCache()
        calls = []
        monkeypatch.setattr(main, "CRAWL4AI_API_TOKEN", "test-token")
        def handler(req):
            calls.append(req.url.path)
            if req.url.path == "/crawl":
                return httpx.Response(500)
            if req.url.params["q"] == "failure":
                return httpx.Response(200, json={"results": [], "unresponsive_engines": [["x", "failed"]]})
            return httpx.Response(200, json={"results": [{"url": "https://example.com"}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
            for _ in range(2):
                await main.search(main.SearchRequest(query="failure"), upstream)
                await main.search(main.SearchRequest(query="crawl", include_raw_content=True), upstream)
            assert calls.count("/search") == 3
            assert calls.count("/crawl") == 2
    asyncio.run(run())


def test_search_passes_filters_and_filters_before_limit():
    def handler(req):
        assert req.url.params["language"] == "zh-CN"
        assert req.url.params["time_range"] == "month"
        return httpx.Response(200, json={"results": [
            {"url": "https://unrelated.example/a"},
            {"url": "https://private.example.com/b"},
            {"url": "https://docs.example.com/c"},
        ]})
    response = request({"query": "test", "max_results": 1, "language": "zh-CN",
                        "time_range": "month", "include_domains": ["example.com"],
                        "exclude_domains": ["private.example.com"]}, handler)
    assert response.status_code == 200
    assert [r["url"] for r in response.json()["results"]] == ["https://docs.example.com/c"]


def test_search_cache_distinguishes_upstream_filters():
    async def run():
        main.search_cache = main.SearchCache()
        calls = []
        def handler(req):
            calls.append(dict(req.url.params))
            return httpx.Response(200, json={"results": [{"url": "https://example.com"}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
            for options in ({}, {"language": "en"}, {"time_range": "day"}, {"language": "en"}):
                await main.search(main.SearchRequest(query="test", **options), upstream)
        assert len(calls) == 3
    asyncio.run(run())


def test_crawl_budget_keeps_partial_results(monkeypatch):
    async def run():
        main.search_cache = main.SearchCache()
        main.crawl_cache = main.SearchCache()
        monkeypatch.setattr(main, "CRAWL4AI_API_TOKEN", "test-token")
        monkeypatch.setattr(main, "CRAWL_BUDGET_SECONDS", 0.03)
        async def handler(req):
            if req.url.path == "/search":
                return httpx.Response(200, json={"results": [
                    {"url": "https://fast.example"}, {"url": "https://slow.example"}]})
            if b"slow.example" in req.content:
                await asyncio.sleep(0.15)
            return httpx.Response(200, json={"results": [{"success": True, "markdown": "# body"}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
            response = await main.search(main.SearchRequest(query="test", include_raw_content=True), upstream)
            assert response["results"][0]["raw_content"] == "# body"
            assert response["results"][1]["raw_content"] is None
            assert "time limit" in response["results"][1]["crawl_error"]
            await asyncio.sleep(0.16)  # let the shared in-flight task finish cleanly
    asyncio.run(run())


def test_crawl_queue_wait_has_own_deadline(monkeypatch):
    async def run():
        main.crawl_cache = main.SearchCache()
        monkeypatch.setattr(main, "crawl_slots", asyncio.Semaphore(0))
        monkeypatch.setattr(main, "CRAWL4AI_API_TOKEN", "test-token")
        monkeypatch.setattr(main, "CRAWL_WORK_BUDGET_SECONDS", 0.02)
        result = {"url": "https://example.com", "raw_content": None}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(500))) as upstream:
            await main.crawl_result(upstream, result)
        assert "timed out" in result["crawl_error"]
        assert not main.crawl_cache.inflight
        assert not main.crawl_cache.entries
    asyncio.run(run())


def test_crawl_content_limits_are_explicit(monkeypatch):
    async def run():
        main.crawl_cache = main.SearchCache()
        monkeypatch.setattr(main, "crawl_slots", asyncio.Semaphore(3))
        monkeypatch.setattr(main, "CRAWL4AI_API_TOKEN", "test-token")
        monkeypatch.setattr(main, "MAX_RAW_CONTENT_CHARS", 4)
        def handler(req):
            return httpx.Response(200, json={"results": [{"success": True, "markdown": "abcdefgh"}]})
        result = {"url": "https://example.com", "raw_content": None}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
            await main.crawl_result(upstream, result)
        assert result["raw_content"] == "abcd"
        assert result["raw_content_truncated"] is True
        main.crawl_cache = main.SearchCache()
        monkeypatch.setattr(main, "MAX_CRAWL_RESPONSE_BYTES", 10)
        result = {"url": "https://example.com", "raw_content": None}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
            await main.crawl_result(upstream, result)
        assert "size limit" in result["crawl_error"]
        assert not main.crawl_cache.entries
    asyncio.run(run())


def test_cache_inflight_is_bounded_but_shares_existing_work():
    async def run():
        from app.cache import CacheBusy
        cache = main.SearchCache(max_inflight=1)
        ready = asyncio.Event()
        async def create():
            await ready.wait()
            return {"value": 1}, True
        first = asyncio.create_task(cache.get_or_create(("same",), create))
        await asyncio.sleep(0)
        shared = asyncio.create_task(cache.get_or_create(("same",), create))
        await asyncio.sleep(0)
        try:
            await cache.get_or_create(("other",), create)
        except CacheBusy:
            pass
        else:
            raise AssertionError("a distinct request should be rejected when the queue is full")
        ready.set()
        assert await first == await shared == {"value": 1}
    asyncio.run(run())
