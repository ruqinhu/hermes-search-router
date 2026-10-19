"""AnySearch backend (anonymous or keyed)."""

from __future__ import annotations

import logging
import time
from typing import Any, Sequence

import httpx

from .base import (
    CAP_EXTRACT,
    CAP_SEARCH,
    Hit,
    RateLimiter,
    SearchOutcome,
    clamp_limit,
    extract_entry,
    truncate,
    validate_query,
    validate_url,
)
from .http import cooldown_for_status, error_message, post_json_retry

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.anysearch.com"


class AnySearchBackend:
    name = "anysearch"
    capabilities = frozenset({CAP_SEARCH, CAP_EXTRACT})

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        client: Any = None,
        limiter: RateLimiter | None = None,
        sleep=time.sleep,
    ) -> None:
        self._api_key = (api_key or "").strip()
        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = float(timeout)
        self._client = client if client is not None else httpx
        self._limiter = limiter or RateLimiter(0.0)
        self._sleep = sleep

    def available(self) -> bool:
        # The anonymous tier works without a key.
        return True

    # -- search ---------------------------------------------------------

    def search(
        self, query: str, limit: int, timeout: float | None = None
    ) -> SearchOutcome:
        clean, error = validate_query(query)
        if error:
            return SearchOutcome.failure(f"anysearch: {error}")

        count = clamp_limit(limit, minimum=1, maximum=10, default=5)
        request_timeout = float(timeout or self.timeout)
        started = time.monotonic()
        self._limiter.wait()
        response = post_json_retry(
            self._client,
            self._url("/v1/search"),
            headers=self._headers(),
            json_body={"query": clean, "max_results": count},
            timeout=request_timeout,
            retries=1,
            sleep=self._sleep,
        )
        elapsed_ms = (time.monotonic() - started) * 1000.0

        if response.status is None:
            logger.warning(
                "anysearch search: transport failure after %.0fms: %s",
                elapsed_ms,
                response.error,
            )
            return SearchOutcome.failure(
                f"anysearch: {response.error or 'request failed'}", cooldown=60.0
            )
        if response.status != 200:
            logger.warning(
                "anysearch search: HTTP %s after %.0fms", response.status, elapsed_ms
            )
            return SearchOutcome.failure(
                self._error(response), cooldown=cooldown_for_status(response.status)
            )

        data = response.data
        if not isinstance(data, dict):
            return SearchOutcome.failure(
                "anysearch: unexpected response (not a JSON object)"
            )
        code = data.get("code")
        if code not in (0, None):
            return SearchOutcome.failure(
                truncate(
                    f"anysearch error {code}: {data.get('message', '')}"
                )
            )

        payload = data.get("data")
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            return SearchOutcome.failure(
                "anysearch: response is missing data.results"
            )

        hits = self._map_results(results, count)
        logger.info(
            "anysearch search: query_chars=%d count=%d hits=%d elapsed_ms=%.0f",
            len(clean),
            count,
            len(hits),
            elapsed_ms,
        )
        return SearchOutcome.success(hits, backend=self.name, count=len(hits))

    def _map_results(self, results: list[Any], count: int) -> list[Hit]:
        hits: list[Hit] = []
        seen: set[str] = set()
        for raw in results:
            if not isinstance(raw, dict):
                continue
            url = str(raw.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            title = str(raw.get("title") or "").strip() or url
            description = str(
                raw.get("snippet") or raw.get("content") or ""
            ).strip()
            hits.append(Hit(title=title, url=url, description=description))
            if len(hits) >= count:
                break
        return hits

    # -- extract --------------------------------------------------------

    def extract(
        self,
        urls: Sequence[str],
        timeout: float | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        if isinstance(urls, str):
            urls = [urls]
        url_list = [str(url) for url in urls]
        budget = float(timeout or self.timeout)
        deadline = time.monotonic() + budget
        entries: list[dict[str, Any]] = []
        account_error: str | None = None

        for url in url_list:
            if account_error:
                entries.append(
                    extract_entry(url, error=account_error, backend=self.name)
                )
                continue

            validation = validate_url(url)
            if validation:
                entries.append(
                    extract_entry(url, error=validation, backend=self.name)
                )
                continue

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                entries.append(
                    extract_entry(
                        url,
                        error="anysearch: extract time budget exceeded",
                        backend=self.name,
                    )
                )
                continue

            self._limiter.wait()
            response = post_json_retry(
                self._client,
                self._url("/v1/extract"),
                headers=self._headers(),
                json_body={"url": url},
                timeout=min(self.timeout, remaining),
                retries=1,
                sleep=self._sleep,
            )

            if response.status is None:
                entries.append(
                    extract_entry(
                        url,
                        error=f"anysearch: {response.error or 'request failed'}",
                        backend=self.name,
                    )
                )
                continue

            if response.status in (401, 402):
                account_error = self._error(response)
                entries.append(
                    extract_entry(url, error=account_error, backend=self.name)
                )
                continue

            if response.status != 200:
                entries.append(
                    extract_entry(
                        url, error=self._error(response), backend=self.name
                    )
                )
                continue

            data = response.data
            if not isinstance(data, dict):
                entries.append(
                    extract_entry(
                        url,
                        error="anysearch: unexpected response (not a JSON object)",
                        backend=self.name,
                    )
                )
                continue
            code = data.get("code")
            if code not in (0, None):
                entries.append(
                    extract_entry(
                        url,
                        error=truncate(
                            f"anysearch error {code}: {data.get('message', '')}"
                        ),
                        backend=self.name,
                    )
                )
                continue

            payload = data.get("data") if isinstance(data.get("data"), dict) else {}
            content = payload.get("content")
            content = content if isinstance(content, str) else ""
            title = str(payload.get("title") or "")
            entries.append(
                extract_entry(
                    url,
                    title=title,
                    content=content,
                    backend=self.name,
                )
            )

        return entries

    # -- helpers --------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Anysearch-Client": "hermes-search-router/0.1",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def _error(self, response: Any) -> str:
        status = response.status
        detail = error_message(response.data, "")
        if status == 401:
            base = "AnySearch API key invalid (401)"
        elif status == 402:
            base = "AnySearch quota/balance issue (402)"
        elif status == 429:
            base = "AnySearch rate limit (429)"
        elif status is not None and 500 <= status <= 599:
            base = f"AnySearch server error ({status})"
        else:
            base = f"AnySearch HTTP {status}"
        if detail:
            base = f"{base}: {detail}"
        return truncate(f"anysearch: {base}")
