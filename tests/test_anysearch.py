from __future__ import annotations

import json

import httpx

from hsr.backends.anysearch import AnySearchBackend
from hsr.backends.base import RateLimiter


def make_backend(handler, **kwargs):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return AnySearchBackend(
        client=client,
        limiter=RateLimiter(0),
        sleep=lambda _seconds: None,
        **kwargs,
    )


def test_search_mapping_and_dedup():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/search"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "results": [
                        {"title": "T1", "url": "https://e/1", "snippet": "S1"},
                        {"title": "T1 dup", "url": "https://e/1", "snippet": "D"},
                        {"title": "T2", "url": "https://e/2", "content": "C2"},
                        {"title": "No URL", "url": ""},
                    ]
                },
            },
        )

    outcome = make_backend(handler).search("q", 5)
    assert outcome.ok
    assert len(outcome.hits) == 2
    assert outcome.hits[0].description == "S1"
    assert outcome.hits[1].description == "C2"


def test_count_is_clamped_to_ten():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"code": 0, "data": {"results": []}})

    backend = make_backend(handler)
    assert backend.search("q", 100).ok
    assert seen["max_results"] == 10


def test_optional_api_key_is_sent():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"code": 0, "data": {"results": []}})

    make_backend(handler, api_key="key-1").search("q", 5)
    assert seen["auth"] == "Bearer key-1"


def test_code_error_is_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"code": 7, "message": "quota", "data": {"results": []}}
        )

    outcome = make_backend(handler).search("q", 5)
    assert not outcome.ok
    assert "anysearch error 7" in outcome.error


def test_429_is_retried_once():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, json={})
        return httpx.Response(200, json={"code": 0, "data": {"results": []}})

    outcome = make_backend(handler).search("q", 5)
    assert outcome.ok
    assert len(calls) == 2


def test_extract_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/extract"
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"title": "T", "content": "page for " + body["url"]},
            },
        )

    entries = make_backend(handler).extract(["https://example.com/a"])
    assert entries[0]["title"] == "T"
    assert entries[0]["content"] == "page for https://example.com/a"
    assert entries[0]["metadata"]["sourceURL"] == "https://example.com/a"


def test_extract_code_error_is_per_url():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 3, "message": "blocked"})

    entries = make_backend(handler).extract(["https://example.com/a"])
    assert "error" in entries[0]
    assert "anysearch error 3" in entries[0]["error"]


def test_extract_401_short_circuits():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(401, json={"message": "bad key"})

    entries = make_backend(handler, api_key="bad").extract(
        ["https://example.com/a", "https://example.com/b"]
    )
    assert len(calls) == 1
    assert all("error" in entry for entry in entries)


def test_available_without_key():
    assert AnySearchBackend().available() is True
