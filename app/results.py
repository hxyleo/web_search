"""Normalize, deduplicate, and select search results."""

from urllib.parse import urlsplit, urlunsplit


def usable_url(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        parts = urlsplit(value)
        if (
            parts.scheme.lower() not in ("http", "https")
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or any(c.isspace() or ord(c) < 32 for c in parts.netloc)
        ):
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
        candidates.append(
            (
                source,
                {
                    "title": str(item.get("title") or ""),
                    "url": url,
                    "content": str(item.get("content") or ""),
                    "raw_content": None,
                },
            )
        )

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
