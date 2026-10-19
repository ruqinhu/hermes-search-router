"""Shared backend contract and small helpers.

This module deliberately has no Hermes imports so it can be used by the
self-check script and by offline tests without a Hermes installation.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence
from urllib.parse import urlsplit

CAP_SEARCH = "search"
CAP_EXTRACT = "extract"

MAX_QUERY_CHARS = 128
MAX_URL_CHARS = 8192


@dataclass
class Hit:
    """One normalized search hit."""

    title: str
    url: str
    description: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SearchOutcome:
    """Result of one backend search attempt."""

    ok: bool
    hits: list[Hit] = field(default_factory=list)
    error: str | None = None
    cooldown: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(
        cls,
        hits: Sequence[Hit] | None = None,
        *,
        cooldown: float | None = None,
        **meta: Any,
    ) -> "SearchOutcome":
        return cls(True, list(hits or []), None, cooldown, dict(meta))

    @classmethod
    def failure(
        cls,
        error: Any,
        *,
        cooldown: float | None = None,
        **meta: Any,
    ) -> "SearchOutcome":
        return cls(False, [], str(error or "unknown error"), cooldown, dict(meta))


class Backend(Protocol):
    """Internal seam: every routing target implements this."""

    name: str
    capabilities: frozenset[str]
    timeout: float

    def available(self) -> bool:
        ...

    def search(
        self, query: str, limit: int, timeout: float | None = None
    ) -> SearchOutcome:
        ...

    def extract(
        self, urls: Sequence[str], timeout: float | None = None, **kwargs: Any
    ) -> list[dict[str, Any]]:
        ...


class RateLimiter:
    """Thread-safe minimum-interval limiter."""

    def __init__(
        self,
        min_interval: float = 0.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._min_interval = max(0.0, float(min_interval))
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            now = self._clock()
            delay = self._next_at - now
            if delay > 0:
                self._sleep(delay)
                now = self._clock()
            self._next_at = max(now, self._next_at) + self._min_interval


def clamp_limit(
    value: Any, *, minimum: int = 1, maximum: int = 50, default: int = 5
) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def validate_query(
    query: Any, *, max_chars: int = MAX_QUERY_CHARS
) -> tuple[str | None, str | None]:
    if not isinstance(query, str):
        return None, "query must be a string"
    clean = query.strip()
    if not clean:
        return None, "query must not be blank"
    if len(clean) > max_chars:
        return (
            None,
            f"query is too long ({len(clean)} > {max_chars} characters); shorten it",
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in clean):
        return None, "query contains control characters"
    return clean, None


def validate_url(url: Any) -> str | None:
    if not isinstance(url, str):
        return "url must be a string"
    value = url.strip()
    if not value:
        return "url must not be blank"
    if len(value) > MAX_URL_CHARS:
        return f"url is too long (> {MAX_URL_CHARS} characters)"
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return "url contains control characters"
    try:
        parts = urlsplit(value)
    except ValueError as exc:
        return f"invalid url: {exc}"
    if parts.scheme not in ("http", "https"):
        return "url scheme must be http or https"
    if not parts.hostname:
        return "url must include a host"
    if parts.username or parts.password:
        return "url must not include credentials"
    return None


_ESCAPED_PUNCT = re.compile(r"\\([!-/:-@\[-`{-~])")


def unescape_markdown(text: str) -> str:
    """Reverse DeepSeek Fetch's backslash escaping of ASCII punctuation.

    Only ``\\`` + ASCII punctuation is touched. ``\\n``, ``\\d`` and
    ``C:\\path`` are left alone.
    """

    return _ESCAPED_PUNCT.sub(r"\1", text or "")


def truncate(value: Any, limit: int = 300) -> str:
    text = "" if value is None else str(value)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def extract_entry(
    url: str,
    *,
    title: str = "",
    content: str = "",
    error: str | None = None,
    backend: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one Hermes-compatible extract entry."""

    entry: dict[str, Any] = {
        "url": url,
        "title": title or "",
        "content": content or "",
        "raw_content": content or "",
    }
    extra = dict(metadata or {})
    extra.setdefault("sourceURL", url)
    if backend:
        extra["backend"] = backend
    if extra:
        entry["metadata"] = extra
    if error:
        entry["error"] = truncate(error)
    return entry
