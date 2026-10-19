from __future__ import annotations

import json

import httpx

from hsr.backends.base import RateLimiter
from hsr.backends.deepseek import DeepSeekBackend


def make_backend(handler, **kwargs):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return DeepSeekBackend(
        api_key="sk-test",
        client=client,
        limiter=RateLimiter(0),
        sleep=lambda _seconds: None,
        **kwargs,
    )


def test_search_mapping_footer_and_dedup():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/tools/search"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "T1",
                        "url": "https://example.com/1",
                        "summary": "S1",
                        "snippet": "snip",
                        "site_name": "Example",
                        "date": "2026-06-01T00:00:00+00:00",
                        "authority": 3,
                        "rerank_score": 2.4,
                    },
                    {
                        "title": "T1 duplicate",
                        "url": "https://example.com/1",
                        "summary": "dup",
                    },
                    {"title": "No URL", "url": "", "summary": "x"},
                    {
                        "title": "T2",
                        "url": "https://example.com/2",
                        "snippet": "snip2",
                        "authority": -1,
                    },
                ]
            },
        )

    outcome = make_backend(handler).search("hello", 5)
    assert outcome.ok
    assert len(outcome.hits) == 2
    first = outcome.hits[0]
    assert first.title == "T1"
    assert first.description.startswith("S1")
    assert "来源: Example" in first.description
    assert "发布: 2026-06-01" in first.description
    assert "权威度: 3/4" in first.description
    assert first.extra["rerank_score"] == 2.4
    second = outcome.hits[1]
    assert second.description == "snip2"
    assert "权威度" not in second.description


def test_search_count_clamping():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"results": []})

    backend = make_backend(handler)
    assert backend.search("q", 100).ok
    assert seen["count"] == 50
    assert backend.search("q", 0).ok
    assert seen["count"] == 1
    assert backend.search("q", "bad").ok
    assert seen["count"] == 5


def test_query_too_long_is_rejected_without_request():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"results": []})

    outcome = make_backend(handler).search("x" * 129, 5)
    assert not outcome.ok
    assert "too long" in outcome.error
    assert calls == []


def test_401_is_not_retried_and_sets_long_cooldown():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            401, json={"error": {"message": "bad key", "type": "auth"}}
        )

    outcome = make_backend(handler).search("q", 5)
    assert not outcome.ok
    assert "401" in outcome.error
    assert outcome.cooldown == 3600.0
    assert len(calls) == 1


def test_429_is_retried_once():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                429,
                headers={"retry-after": "0"},
                json={"error": {"message": "slow down"}},
            )
        return httpx.Response(
            200,
            json={
                "results": [
                    {"title": "T", "url": "https://example.com/1", "summary": "S"}
                ]
            },
        )

    outcome = make_backend(handler).search("q", 5)
    assert outcome.ok
    assert len(calls) == 2


def test_empty_results_is_success():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    outcome = make_backend(handler).search("q", 5)
    assert outcome.ok
    assert outcome.hits == []


def test_malformed_search_response_is_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"foo": 1})

    outcome = make_backend(handler).search("q", 5)
    assert not outcome.ok
    assert "results list" in outcome.error


def test_need_content_is_forwarded_and_kept_as_extra():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "T",
                        "url": "https://example.com/1",
                        "summary": "S",
                        "content": "# page",
                    }
                ]
            },
        )

    outcome = make_backend(handler, need_content=True).search("q", 5)
    assert outcome.ok
    assert seen["need_content"] is True
    assert outcome.hits[0].extra["content"] == "# page"


def test_extract_unescapes_markdown_and_preserves_order():
    content = (
        r"\# Heading" "\n"
        r"\[text\]\(https\:\/\/x\)" "\n"
        r"\| a \| b \|" "\n"
        r"C:\path"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200, json={"title": f"title for {body['url']}", "content": content}
        )

    urls = ["https://example.com/a", "https://example.com/b"]
    entries = make_backend(handler).extract(urls)
    assert [entry["url"] for entry in entries] == urls
    expected = "# Heading\n[text](https://x)\n| a | b |\nC:\\path"
    for entry in entries:
        assert entry["content"] == expected
        assert entry["raw_content"] == expected
        assert entry["metadata"]["sourceURL"] == entry["url"]


def test_extract_403_is_per_url_and_continues():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        body = json.loads(request.content)
        if body["url"].endswith("/bad"):
            return httpx.Response(403, json={"error": {"message": "blocked"}})
        return httpx.Response(200, json={"title": "T", "content": "ok"})

    entries = make_backend(handler).extract(
        ["https://example.com/bad", "https://example.com/good"]
    )
    assert len(calls) == 2
    assert "error" in entries[0]
    assert "403" in entries[0]["error"]
    assert entries[1]["content"] == "ok"


def test_extract_402_short_circuits_remaining_urls():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(402, json={"error": {"message": "no balance"}})

    entries = make_backend(handler).extract(
        ["https://example.com/a", "https://example.com/b"]
    )
    assert len(calls) == 1
    assert all("error" in entry for entry in entries)
    assert all("402" in entry["error"] for entry in entries)


def test_extract_invalid_urls_do_not_hit_network():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"title": "T", "content": "ok"})

    entries = make_backend(handler).extract(
        ["ftp://example.com/a", "https://user:pass@example.com/b"]
    )
    assert calls == []
    assert all("error" in entry for entry in entries)


def test_missing_api_key_is_unavailable():
    backend = DeepSeekBackend(api_key="")
    assert backend.available() is False
