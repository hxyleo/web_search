import pytest
from pydantic import ValidationError

from app.search_params import SearchRequest, domain_matches, filter_domains


def test_supported_parameters_are_forwarded():
    request = SearchRequest(
        query="  python  ",
        language="zh-CN",
        time_range="month",
        include_raw_content="markdown",
        search_depth="basic",
        topic="general",
    )
    assert request.searx_params() == {
        "q": "python",
        "format": "json",
        "language": "zh-CN",
        "time_range": "month",
    }
    assert request.include_raw_content == "markdown"


@pytest.mark.parametrize(
    "options",
    [
        {"search_depth": "advanced"},
        {"topic": "news"},
        {"time_range": "week"},
        {"include_raw_content": "text"},
        {"include_raw_content": "true"},
        {"include_answer": True},
        {"include_images": True},
        {"include_usage": True},
        {"unknown": False},
        {"max_results": 11},
        {"max_results": True},
        {"language": "invalid language"},
    ],
)
def test_unsupported_parameters_rejected(options):
    with pytest.raises(ValidationError):
        SearchRequest(query="x", **options)


def test_false_feature_flags_are_accepted():
    request = SearchRequest(
        query="x",
        include_answer=False,
        include_images=False,
        include_favicon=False,
        include_usage=False,
    )
    assert request.searx_params() == {"q": "x", "format": "json"}


@pytest.mark.parametrize(
    "domain",
    [
        "https://example.com",
        "user@example.com",
        "example.com:80",
        ".example.com",
        "bad..com",
        "bad_host.com",
        " example.com",
        "example.com/path",
    ],
)
def test_only_bare_domains_accepted(domain):
    with pytest.raises(ValidationError):
        SearchRequest(query="x", include_domains=[domain])


def test_domain_boundaries_and_exclusions():
    assert domain_matches("https://docs.Example.com./x", "example.com")
    assert not domain_matches("https://notexample.com", "example.com")
    assert not domain_matches("https://example.com.attacker.com", "example.com")
    assert domain_matches("https://www.例子.中国/", "例子.中国")
    items = [
        {"url": "https://example.com/a"},
        {"url": "https://private.example.com/a"},
        {"url": "https://evil-example.com/a"},
    ]
    assert filter_domains(items, ["example.com"], ["private.example.com"]) == items[:1]
