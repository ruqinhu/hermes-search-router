from __future__ import annotations

import time

import httpx

from hsr.backends.http import (
    cooldown_for_status,
    post_json,
    post_json_retry,
    retry_after_seconds,
)


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_retry_after_seconds_number_and_http_date():
    numeric = httpx.Response(429, headers={"retry-after": "3"})
    assert retry_after_seconds(numeric) == 3.0

    past = httpx.Response(
        429, headers={"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"}
    )
    assert retry_after_seconds(past) == 0.0

    assert retry_after_seconds(None) is None


def test_cooldown_for_status():
    assert cooldown_for_status(400) == 0.0
    assert cooldown_for_status(404) == 0.0
    assert cooldown_for_status(413) == 0.0
    assert cooldown_for_status(401) == 3600.0
    assert cooldown_for_status(403) == 60.0
    assert cooldown_for_status(429) == 120.0
    assert cooldown_for_status(503) == 60.0


def test_expired_deadline_returns_error_without_request():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={})

    result = post_json_retry(
        _client(handler),
        "https://example.com",
        headers={},
        json_body={},
        timeout=1.0,
        deadline=time.monotonic() - 1.0,
        sleep=lambda _seconds: None,
    )
    assert result.status is None
    assert "deadline" in result.error
    assert calls == []


def test_5xx_is_retried_once():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503, json={"error": {"message": "down"}})
        return httpx.Response(200, json={"ok": True})

    result = post_json_retry(
        _client(handler),
        "https://example.com",
        headers={},
        json_body={},
        timeout=1.0,
        sleep=lambda _seconds: None,
    )
    assert result.status == 200
    assert result.data == {"ok": True}
    assert len(calls) == 2


def test_non_json_error_body_is_reported_as_http_error():
    result = post_json_retry(
        _client(lambda request: httpx.Response(500, text="oops")),
        "https://example.com",
        headers={},
        json_body={},
        timeout=1.0,
        retries=0,
        sleep=lambda _seconds: None,
    )
    assert result.status == 500
    assert result.data is None


def test_transport_timeout_is_reported():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    result = post_json(
        _client(handler),
        "https://example.com",
        headers={},
        json_body={},
        timeout=0.2,
    )
    assert result.status is None
    assert "timeout" in result.error
