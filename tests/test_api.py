import asyncio

import httpx

from app import main


def request(payload, upstream_handler):
    async def run():
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
