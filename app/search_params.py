"""Validated subset of Tavily search options supported by the local service."""

import re
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StringConstraints, field_validator


_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def normalize_domain(domain: str) -> str:
    """Validate a bare domain and normalize it to lowercase ASCII IDNA."""
    if not isinstance(domain, str) or not domain or domain != domain.strip():
        raise ValueError("domain must be a bare hostname")
    try:
        name = domain.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("invalid domain") from exc
    if not name or len(name) > 253 or not all(_LABEL.fullmatch(label) for label in name.split(".")):
        raise ValueError("domain must be a bare hostname")
    return name


def domain_matches(url: str, domain: str) -> bool:
    """Match a host or its subdomain on a label boundary, never a substring."""
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in ("http", "https") or parsed.username is not None or parsed.password is not None:
            return False
        host = normalize_domain(parsed.hostname or "")
        suffix = normalize_domain(domain)
        return host == suffix or host.endswith("." + suffix)
    except (ValueError, TypeError):
        return False


def filter_domains(items: list, include: list[str], exclude: list[str]) -> list:
    """Filter upstream candidates before ranking or crawling them."""
    filtered = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            continue
        url = item["url"]
        if include and not any(domain_matches(url, domain) for domain in include):
            continue
        if exclude and any(domain_matches(url, domain) for domain in exclude):
            continue
        filtered.append(item)
    return filtered


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
    max_results: Annotated[StrictInt, Field(ge=1, le=10)] = 5
    include_raw_content: StrictBool | Literal["markdown"] = False
    search_depth: Literal["basic"] = "basic"
    topic: Literal["general"] = "general"
    language: Annotated[str, StringConstraints(pattern=r"^[a-z]{2,3}(?:-[A-Z]{2})?$")] | None = None
    time_range: Literal["day", "month", "year"] | None = None
    include_domains: list[str] = Field(default_factory=list, max_length=100)
    exclude_domains: list[str] = Field(default_factory=list, max_length=100)
    include_domains_mode: Literal["restrict"] = "restrict"
    include_answer: StrictBool = False
    include_images: StrictBool = False
    include_image_descriptions: StrictBool = False
    include_favicon: StrictBool = False
    include_usage: StrictBool = False
    include_published_date: StrictBool = False
    filter_by_published_date: StrictBool = False
    filter_by_language: StrictBool = False
    auto_parameters: StrictBool = False
    exact_match: StrictBool = False

    @field_validator("include_answer", "include_images", "include_image_descriptions", "include_favicon", "include_usage", "include_published_date", "filter_by_published_date", "filter_by_language", "auto_parameters", "exact_match")
    @classmethod
    def reject_unsupported_features(cls, value: bool) -> bool:
        if value:
            raise ValueError("this option is not supported")
        return value

    @field_validator("include_domains", "exclude_domains")
    @classmethod
    def normalize_domains(cls, values: list[str]) -> list[str]:
        return sorted({normalize_domain(value) for value in values})

    def searx_params(self) -> dict[str, str]:
        params = {"q": self.query, "format": "json"}
        if self.language is not None:
            params["language"] = self.language
        if self.time_range is not None:
            params["time_range"] = self.time_range
        return params

    def cache_key(self) -> tuple:
        """Include every supported option that changes the returned response."""
        return (self.query, self.max_results, bool(self.include_raw_content), self.language,
                self.time_range, tuple(self.include_domains), tuple(self.exclude_domains))
