"""DeepSeek Search / Fetch backend."""

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
    unescape_markdown,
    validate_query,
    validate_url,
)
from .http import cooldown_for_status, error_message, post_json_retry

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.deepseek.com"


class DeepSeekBackend:
    name = "deepseek"
    capabilities = frozenset({CAP_SEARCH, CAP_EXTRACT})

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        need_content: bool = False,
        client: Any = None,
        limiter: RateLimiter | None = None,
        sleep=time.sleep,
    ) -> None:
        self._api_key = (api_key or "").strip()
        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = float(timeout)
        self._need_content = bool(need_content)
        self._client = client if client is not None else httpx
        self._limiter = limiter or RateLimiter(0.0)
        self._sleep = sleep

    def available(self) -> bool:
        return bool(self._api_key)

    # -- search ---------------------------------------------------------

    def search(
        self, query: str, limit: int, timeout: float | None = None
    ) -> SearchOutcome:
        clean, error = validate_query(query)
        if error:
            return SearchOutcome.failure(f"deepseek: {error}")

        count = clamp_limit(limit, minimum=1, maximum=50, default=5)
        request_timeout = float(timeout or self.timeout)
        body = {
            "query": clean,
            "count": count,
            "need_content": self._need_content,
        }
        started = time.monotonic()
        self._limiter.wait()
        response = post_json_retry(
            self._client,
            self._url("/v1/tools/search"),
            headers=self._headers(),
            json_body=body,
            timeout=request_timeout,
            retries=1,
            sleep=self._sleep,
        )
        elapsed_ms = (time.monotonic() - started) * 1000.0

        if response.status is None:
            logger.warning(
                "deepseek search: transport failure after %.0fms: %s",
                elapsed_ms,
                response.error,
            )
            return SearchOutcome.failure(
                f"deepseek: {response.error or 'request failed'}", cooldown=60.0
            )
        if response.status != 200:
            logger.warning(
                "deepseek search: HTTP %s after %.0fms", response.status, elapsed_ms
            )
            return SearchOutcome.failure(
                self._error(response), cooldown=cooldown_for_status(response.status)
            )

        data = response.data
        if not isinstance(data, dict):
            return SearchOutcome.failure(
                "deepseek: unexpected response (not a JSON object)"
            )
        results = data.get("results")
        if not isinstance(results, list):
            return SearchOutcome.failure(
                "deepseek: response is missing a results list"
            )

        hits = self._map_results(results, count)
        logger.info(
            "deepseek search: query_chars=%d count=%d hits=%d elapsed_ms=%.0f",
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
            description = str(raw.get("summary") or raw.get("snippet") or "").strip()
            description = self._with_footer(description, raw)
            extra: dict[str, Any] = {}
            for key in ("rerank_score", "authority", "date", "site_name", "site_icon"):
                value = raw.get(key)
                if value is not None:
                    extra[key] = value
            if self._need_content and raw.get("content"):
                extra["content"] = str(raw["content"])
            hits.append(
                Hit(title=title, url=url, description=description, extra=extra)
            )
            if len(hits) >= count:
                break
        return hits

    @staticmethod
    def _with_footer(description: str, raw: dict[str, Any]) -> str:
        parts: list[str] = []
        site = raw.get("site_name")
        if site:
            parts.append(f"来源: {site}")
        date = raw.get("date")
        if date:
            text = str(date).strip()
            if len(text) > 10:
                text = text[:10]
            parts.append(f"发布: {text}")
        try:
            authority = int(raw.get("authority"))
        except (TypeError, ValueError):
            authority = -1
        if authority >= 0:
            parts.append(f"权威度: {authority}/4")
        if not parts:
            return description
        footer = "｜".join(parts)
        return f"{description}\n\n{footer}" if description else footer

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
                        error="deepseek: extract time budget exceeded",
                        backend=self.name,
                    )
                )
                continue

            self._limiter.wait()
            response = post_json_retry(
                self._client,
                self._url("/v1/tools/fetch"),
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
                        error=f"deepseek: {response.error or 'request failed'}",
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
                        error="deepseek: unexpected response (not a JSON object)",
                        backend=self.name,
                    )
                )
                continue

            content = data.get("content")
            content = unescape_markdown(content) if isinstance(content, str) else ""
            title = str(data.get("title") or "")
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
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "hermes-search-router/0.1",
        }

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def _error(self, response: Any) -> str:
        status = response.status
        detail = error_message(response.data, "")
        if status == 401:
            base = (
                "DeepSeek API key invalid (401); use an official "
                "platform.deepseek.com key, not an Ark key"
            )
        elif status == 402:
            base = (
                "DeepSeek account balance insufficient (402); "
                "top up at platform.deepseek.com"
            )
        elif status == 403:
            base = "DeepSeek request forbidden (403)"
        elif status == 429:
            base = "DeepSeek rate limit (429); try again shortly"
        elif status == 400:
            base = "DeepSeek rejected the request (400)"
        elif status == 413:
            base = "DeepSeek request too large (413)"
        elif status is not None and 500 <= status <= 599:
            base = f"DeepSeek server error ({status})"
        else:
            base = f"DeepSeek HTTP {status}"
        if detail:
            base = f"{base}: {detail}"
        return truncate(f"deepseek: {base}")
