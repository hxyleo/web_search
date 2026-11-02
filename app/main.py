"""A small search API backed by SearXNG and, optionally, Crawl4AI."""
import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import Depends, FastAPI, HTTPException
from app.cache import CacheBusy, SearchCache
from app.rate_limit import HostRateLimiter, RateLimitCapacityError
from app.search_params import SearchRequest, filter_domains

SEARXNG_URL = os.getenv("SEARXNG_URL", "http://searxng:8080").rstrip("/")
CRAWL4AI_URL = os.getenv("CRAWL4AI_URL", "http://crawl4ai:11235").rstrip("/")
CRAWL4AI_API_TOKEN = os.getenv("CRAWL4AI_API_TOKEN", "")
CRAWL_BUDGET_SECONDS = 25.0
CRAWL_WORK_BUDGET_SECONDS = 25.0
SEARCH_WORK_BUDGET_SECONDS = 15.0
MAX_RAW_CONTENT_CHARS = 200_000
MAX_CRAWL_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_SEARCH_RESPONSE_BYTES = 8 * 1024 * 1024
# Shared across all requests in this process to avoid an unbounded browser workload.
crawl_slots = asyncio.Semaphore(3)
search_slots = asyncio.Semaphore(8)
host_limiter = HostRateLimiter()
search_cache = SearchCache()
crawl_cache = SearchCache(max_entries=128, max_bytes=32 * 1024 * 1024, max_entry_bytes=1024 * 1024)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
        app.state.client = client
        yield


app = FastAPI(title="Local Web Search", lifespan=lifespan)


async def get_client() -> httpx.AsyncClient:
    return app.state.client


def usable_url(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        parts = urlsplit(value)
        if (parts.scheme.lower() not in ("http", "https") or not parts.hostname
                or parts.username is not None or parts.password is not None
                or any(c.isspace() or ord(c) < 32 for c in parts.netloc)):
            return None
        # Force parsing of malformed ports and IPv6 literals.
        _ = parts.port
        return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path, parts.query, ""))
    except ValueError:
        return None


def url_key(url: str) -> tuple:
    """Identify equivalent URLs without changing paths or meaningful query parameters."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    port = parts.port
    if (scheme, port) in (("http", 80), ("https", 443)):
        port = None
    return scheme, parts.hostname.lower(), port, parts.path or "/", parts.query


def select_results(items: list, limit: int) -> list[dict]:
    """Deduplicate first, then make only small, reversible source-order adjustments."""
    candidates = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        url = usable_url(item.get("url"))
        if url is None:
            continue
        key = url_key(url)
        if key in seen:
            continue
        seen.add(key)
        host = urlsplit(url).hostname.lower()
        # Treat the ordinary www alias as the same source; keep other subdomains distinct.
        source = host.removeprefix("www.")
        candidates.append((source, {
            "title": str(item.get("title") or ""),
            "url": url,
            "content": str(item.get("content") or ""),
            "raw_content": None,
        }))

    selected = []
    source_counts = {}
    while candidates and len(selected) < limit:
        source = candidates[0][0]
        if source_counts.get(source, 0) >= 2:
            # Only look a few positions ahead so a distant, less relevant result
            # cannot displace a much higher ranked one just for diversity.
            for index in range(1, min(4, len(candidates))):
                alternative = candidates[index][0]
                if source_counts.get(alternative, 0) < 2:
                    candidates.insert(0, candidates.pop(index))
                    break
        source, result = candidates.pop(0)
        source_counts[source] = source_counts.get(source, 0) + 1
        selected.append(result)
    return selected


async def crawl_result(client: httpx.AsyncClient, result: dict) -> None:
    async def extract() -> tuple[dict, bool]:
        page = {"url": result["url"], "raw_content": None}
        await crawl_page(client, page)
        return page, "crawl_error" not in page

    try:
        page = await crawl_cache.get_or_create((result["url"],), extract)
    except CacheBusy:
        result["crawl_error"] = "Crawl4AI queue is full"
        return
    result["raw_content"] = page["raw_content"]
    if "crawl_error" in page:
        result["crawl_error"] = page["crawl_error"]
    if page.get("raw_content_truncated"):
        result["raw_content_truncated"] = True


async def crawl_page(client: httpx.AsyncClient, result: dict) -> None:
    if not CRAWL4AI_API_TOKEN:
        result["crawl_error"] = "Crawl4AI token is not configured"
        return
    try:
        async with asyncio.timeout(CRAWL_WORK_BUDGET_SECONDS):
            await _crawl_page(client, result)
    except TimeoutError:
        result["crawl_error"] = "Crawl4AI extraction timed out"


async def _crawl_page(client: httpx.AsyncClient, result: dict) -> None:
    try:
        async with crawl_slots:
            await host_limiter.wait(result["url"])
            async with client.stream(
                "POST",
                f"{CRAWL4AI_URL}/crawl",
                headers={"Authorization": f"Bearer {CRAWL4AI_API_TOKEN}"},
                json={"urls": [result["url"]]},
                timeout=40.0,
            ) as response:
                if response.status_code != 400:
                    response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_CRAWL_RESPONSE_BYTES:
                        result["crawl_error"] = "Crawl4AI response exceeded the size limit"
                        return
                if response.status_code == 400:
                    try:
                        detail = json.loads(body).get("detail", "")
                    except (ValueError, AttributeError):
                        detail = ""
                    if isinstance(detail, str) and detail.startswith("URL blocked (SSRF protection)"):
                        result["crawl_error"] = "URL blocked by Crawl4AI SSRF protection"
                        return
                    response.raise_for_status()
                data = json.loads(body)
        entries = data.get("results") if isinstance(data, dict) else None
        entry = entries[0] if isinstance(entries, list) and entries and isinstance(entries[0], dict) else None
        if not entry or not entry.get("success"):
            # Do not return upstream details: errors can contain internal addresses.
            result["crawl_error"] = "Crawl4AI could not extract this page"
            return
        markdown = entry.get("markdown")
        if isinstance(markdown, dict):
            markdown = markdown.get("raw_markdown")
        if not isinstance(markdown, str) or not markdown.strip():
            result["crawl_error"] = "Crawl4AI returned no Markdown content"
            return
        if len(markdown) > MAX_RAW_CONTENT_CHARS:
            result["raw_content_truncated"] = True
        result["raw_content"] = markdown[:MAX_RAW_CONTENT_CHARS]
    except RateLimitCapacityError:
        result["crawl_error"] = "Crawl4AI rate limiter is full"
    except (httpx.HTTPError, ValueError, TypeError):
        result["crawl_error"] = "Crawl4AI unavailable or returned an invalid response"


@app.get("/health")
async def health() -> dict:
    # Liveness only; /search reports upstream failures separately.
    return {"status": "ok"}


@app.post("/search")
async def search(payload: SearchRequest, client: httpx.AsyncClient = Depends(get_client)) -> dict:
    started = time.monotonic()
    # Cache upstream candidates by the exact parameters sent to SearXNG.
    search_key = tuple(sorted(payload.searx_params().items()))
    try:
        data = await search_cache.get_or_create(search_key, lambda: search_upstream(payload, client))
    except CacheBusy as exc:
        raise HTTPException(status_code=503, detail="Search queue is full") from exc
    candidates = filter_domains(data["results"], payload.include_domains, payload.exclude_domains)
    results = select_results(candidates, payload.max_results)
    if payload.include_raw_content:
        try:
            await asyncio.wait_for(
                asyncio.gather(*(crawl_result(client, result) for result in results)),
                timeout=CRAWL_BUDGET_SECONDS,
            )
        except TimeoutError:
            for result in results:
                if result["raw_content"] is None and "crawl_error" not in result:
                    result["crawl_error"] = "Crawl4AI exceeded the overall extraction time limit"
    output = {
        "query": payload.query,
        "answer": None,
        "images": [],
        "results": results,
        "response_time": round(time.monotonic() - started, 3),
    }
    if data.get("unresponsive_engines"):
        if results:
            output["warning"] = "Some SearXNG search engines were unavailable; results may be incomplete."
        else:
            output["warning"] = "No results returned and some SearXNG search engines were unavailable; check upstream connectivity."
    return output


async def search_upstream(payload: SearchRequest, client: httpx.AsyncClient) -> tuple[dict, bool]:
    try:
        async with asyncio.timeout(SEARCH_WORK_BUDGET_SECONDS):
            async with search_slots:
                async with client.stream(
                    "GET", f"{SEARXNG_URL}/search", params=payload.searx_params(), timeout=12.0
                ) as response:
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_SEARCH_RESPONSE_BYTES:
                            raise ValueError("SearXNG response too large")
                data = json.loads(body)
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise ValueError("invalid SearXNG response")
    except (httpx.TimeoutException, TimeoutError) as exc:
        raise HTTPException(status_code=504, detail="SearXNG timed out") from exc
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=502, detail="SearXNG is unavailable or returned an invalid response") from exc

    return data, not bool(data.get("unresponsive_engines"))
