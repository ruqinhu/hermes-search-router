"""Minimal JSON-over-HTTP helpers shared by the HTTP adapters."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx


@dataclass
class JsonResponse:
    status: int | None
    data: Any | None
    error: str | None = None
    retry_after: float | None = None


def error_message(data: Any, default: str = "") -> str:
    """Extract a human-readable message from common API error shapes."""

    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            message = err.get("message")
            if message:
                return str(message)
        if isinstance(err, str) and err.strip():
            return err.strip()
        for key in ("message", "msg", "detail"):
            value = data.get(key)
            if value:
                return str(value)
    return default


def retry_after_seconds(response: Any) -> float | None:
    raw = response.headers.get("retry-after") if response is not None else None
    if not raw:
        return None
    try:
        return max(0.0, min(5.0, float(raw)))
    except (TypeError, ValueError):
        return None


def post_json(
    client: Any,
    url: str,
    *,
    headers: dict[str, str],
    json_body: dict[str, Any],
    timeout: float,
) -> JsonResponse:
    """POST JSON without raising. ``client`` may be ``httpx`` or ``httpx.Client``."""

    try:
        response = client.post(url, headers=headers, json=json_body, timeout=timeout)
    except httpx.TimeoutException:
        return JsonResponse(None, None, f"timeout after {timeout:g}s")
    except Exception as exc:  # noqa: BLE001 - network errors are data here
        return JsonResponse(None, None, f"request failed: {type(exc).__name__}: {exc}")

    data: Any = None
    try:
        data = response.json()
    except Exception:  # noqa: BLE001 - non-JSON bodies are still HTTP results
        data = None
    return JsonResponse(
        response.status_code,
        data,
        None,
        retry_after_seconds(response),
    )


def post_json_retry(
    client: Any,
    url: str,
    *,
    headers: dict[str, str],
    json_body: dict[str, Any],
    timeout: float,
    retries: int = 1,
    sleep: Callable[[float], None] = time.sleep,
) -> JsonResponse:
    """Retry 408/429/5xx and transport errors once by default."""

    last: JsonResponse | None = None
    for attempt in range(retries + 1):
        result = post_json(
            client,
            url,
            headers=headers,
            json_body=json_body,
            timeout=timeout,
        )
        last = result
        retryable = (
            result.status is None
            or result.status in (408, 429)
            or 500 <= result.status <= 599
        )
        if not retryable or attempt >= retries:
            return result
        delay = (
            result.retry_after
            if result.retry_after is not None
            else 0.5 * (attempt + 1)
        )
        sleep(min(max(0.0, delay), 5.0))
    return last or JsonResponse(None, None, "request failed")


def cooldown_for_status(status: int | None, *, default: float = 60.0) -> float:
    # Client mistakes (bad query, bad URL, too large) are not backend outages.
    if status in (400, 404, 413):
        return 0.0
    if status in (401, 402, 403):
        return 3600.0
    if status == 429:
        return 120.0
    return default
