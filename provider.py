"""Hermes WebSearchProvider implementation for the search router."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Sequence

from agent.web_search_provider import WebSearchProvider

from .backends.base import (
    CAP_EXTRACT,
    CAP_SEARCH,
    Hit,
    SearchOutcome,
    clamp_limit,
    extract_entry,
    truncate,
)
from .config import ConfigError, ConfigStore, RouteConfig
from .factory import build_backend

logger = logging.getLogger(__name__)


class SearchRouterProvider(WebSearchProvider):
    """One Hermes backend that routes to DeepSeek, AnySearch, or local scripts."""

    def __init__(
        self,
        config_path: str | None = None,
        *,
        backend_factory=None,
        client: Any = None,
        clock=time.monotonic,
        sleep=time.sleep,
    ) -> None:
        self._store = ConfigStore(config_path)
        self._factory = backend_factory
        self._client = client
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._cached_config: RouteConfig | None = None
        self._cached_backends: dict[str, Any] = {}
        self._cooldowns: dict[str, float] = {}

    # -- identity -------------------------------------------------------

    @property
    def name(self) -> str:
        return "search-router"

    @property
    def display_name(self) -> str:
        return "Search Router"

    def is_keyless_available(self) -> bool:
        return False

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        try:
            config = self._store.get()
            backends = self._backends(config)
        except Exception:  # noqa: BLE001 - capability probe must not crash paint
            return True
        return any(CAP_EXTRACT in backend.capabilities for backend in backends.values())

    def is_available(self) -> bool:
        # Cheap environment/config check only; never touch the network.
        try:
            config = self._store.get()
            backends = self._backends(config)
        except ConfigError:
            # Let a real call surface the config error instead of silently
            # routing to a different provider.
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("search-router availability check failed: %s", exc)
            return False
        return any(backend.available() for backend in backends.values())

    def get_setup_schema(self) -> dict[str, Any]:
        return {
            "name": "Search Router",
            "badge": "router",
            "tag": (
                "DeepSeek / AnySearch / local scripts; manual switch + "
                "ordered fallback."
            ),
            "env_vars": [
                {
                    "key": "DEEPSEEK_API_KEY",
                    "prompt": "DeepSeek platform API key (Search/Fetch, not Ark)",
                    "url": "https://platform.deepseek.com/api_keys",
                },
                {
                    "key": "ANYSEARCH_API_KEY",
                    "prompt": "AnySearch API key (optional; anonymous works)",
                    "url": "https://anysearch.com/console/api-keys",
                },
            ],
        }

    # -- public API -----------------------------------------------------

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        limit_value = clamp_limit(limit, minimum=1, maximum=50, default=5)
        try:
            config = self._store.get()
        except ConfigError as exc:
            return {
                "success": False,
                "error": f"search-router config error: {truncate(exc)}",
            }

        try:
            backends = self._backends(config)
        except Exception as exc:  # noqa: BLE001
            logger.warning("search-router backend construction failed: %s", exc)
            return {
                "success": False,
                "error": f"search-router backend construction failed: {truncate(exc)}",
            }

        chain = self._chain(config, backends, CAP_SEARCH)
        if not chain:
            return {
                "success": False,
                "error": (
                    "search-router: no available search backend; check "
                    f"{self._store.path} and credentials"
                ),
            }

        deadline = self._clock() + config.search_total_timeout
        attempts: list[str] = []

        for backend in chain:
            remaining = deadline - self._clock()
            if remaining <= 0:
                attempts.append(f"{backend.name}: total timeout")
                break
            timeout = max(
                0.1,
                min(
                    float(getattr(backend, "timeout", config.search_total_timeout)),
                    remaining,
                ),
            )
            try:
                outcome = backend.search(query, limit_value, timeout=timeout)
            except Exception as exc:  # noqa: BLE001 - adapters should not raise
                outcome = SearchOutcome.failure(
                    f"{type(exc).__name__}: {exc}",
                    cooldown=config.cooldown_seconds,
                )

            if outcome.ok:
                hits = self._normalize(outcome.hits, limit_value, backend.name)
                if hits:
                    logger.info(
                        "search-router: query_chars=%d limit=%d backend=%s hits=%d "
                        "attempts=%d",
                        len(query),
                        limit_value,
                        backend.name,
                        len(hits),
                        len(attempts) + 1,
                    )
                    return {"success": True, "data": {"web": hits}}
                if config.on_empty == "accept":
                    logger.info(
                        "search-router: query_chars=%d backend=%s empty (accepted)",
                        len(query),
                        backend.name,
                    )
                    return {"success": True, "data": {"web": []}}
                attempts.append(f"{backend.name}: empty")
                continue

            attempts.append(
                f"{backend.name}: {truncate(outcome.error or 'failed', 160)}"
            )
            self._mark_failure(backend.name, outcome)

        error = "search-router: all backends failed"
        if attempts:
            error += ": " + "; ".join(attempts)
        logger.warning("search-router: %s", truncate(error, 600))
        return {"success": False, "error": truncate(error, 600)}

    def extract(
        self, urls: Sequence[str], **kwargs: Any
    ) -> list[dict[str, Any]]:
        if isinstance(urls, str):
            url_list = [urls]
        elif urls is None:
            url_list = []
        else:
            url_list = [str(url) for url in urls]

        if not url_list:
            return []

        try:
            config = self._store.get()
        except ConfigError as exc:
            return [
                extract_entry(
                    url,
                    error=f"search-router config error: {truncate(exc)}",
                    backend=self.name,
                )
                for url in url_list
            ]

        try:
            backends = self._backends(config)
        except Exception as exc:  # noqa: BLE001
            return [
                extract_entry(
                    url,
                    error=f"search-router backend construction failed: {truncate(exc)}",
                    backend=self.name,
                )
                for url in url_list
            ]

        chain = self._chain(config, backends, CAP_EXTRACT)
        if not chain:
            return [
                extract_entry(
                    url,
                    error="search-router: no available extract backend",
                    backend=self.name,
                )
                for url in url_list
            ]

        deadline = self._clock() + config.extract_total_timeout
        attempts: list[str] = []
        last_entries: list[dict[str, Any]] = []

        for backend in chain:
            remaining = deadline - self._clock()
            if remaining <= 0:
                attempts.append(f"{backend.name}: total timeout")
                break
            timeout = max(
                0.1,
                min(
                    float(getattr(backend, "timeout", config.extract_total_timeout)),
                    remaining,
                ),
            )
            try:
                raw_entries = backend.extract(url_list, timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                attempts.append(
                    f"{backend.name}: {truncate(type(exc).__name__ + ': ' + str(exc), 160)}"
                )
                continue

            if isinstance(raw_entries, dict):
                raw_entries = raw_entries.get("data")
            if not isinstance(raw_entries, list):
                attempts.append(f"{backend.name}: invalid extract result")
                continue

            entries = self._ensure_entries(url_list, raw_entries, backend.name)
            content_count = sum(
                1
                for entry in entries
                if entry.get("content") and not entry.get("error")
            )
            if content_count:
                logger.info(
                    "search-router extract: urls=%d backend=%s content=%d attempts=%d",
                    len(url_list),
                    backend.name,
                    content_count,
                    len(attempts) + 1,
                )
                return entries

            attempts.append(f"{backend.name}: no content")
            last_entries = entries

        if last_entries:
            return last_entries

        return [
            extract_entry(
                url,
                error=(
                    "search-router: all extract backends failed"
                    + (": " + "; ".join(attempts) if attempts else "")
                ),
                backend=self.name,
            )
            for url in url_list
        ]

    # -- internals ------------------------------------------------------

    def _backends(self, config: RouteConfig) -> dict[str, Any]:
        with self._lock:
            if config is self._cached_config:
                return self._cached_backends
            built: dict[str, Any] = {}
            for name, backend_config in config.backends.items():
                if self._factory is not None:
                    built[name] = self._factory(backend_config)
                else:
                    built[name] = build_backend(
                        backend_config,
                        client=self._client,
                        sleep=self._sleep,
                    )
            self._cached_config = config
            self._cached_backends = built
            return built

    def _chain(
        self,
        config: RouteConfig,
        backends: dict[str, Any],
        capability: str,
    ) -> list[Any]:
        names: list[str] = []
        if config.active != "auto":
            names.append(config.active)
        if capability == CAP_SEARCH:
            names.extend(config.search_fallback)
        else:
            names.extend(config.extract_fallback)
        if not names:
            names = list(config.backends)

        chain: list[Any] = []
        seen: set[str] = set()
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            backend = backends.get(name)
            if backend is None:
                continue
            if capability not in backend.capabilities:
                continue
            try:
                if not backend.available():
                    continue
            except Exception:  # noqa: BLE001
                continue
            if self._is_cooling(name):
                logger.info("search-router: skipping cooling backend %s", name)
                continue
            chain.append(backend)
        return chain

    def _normalize(
        self, hits: Sequence[Any], limit: int, backend_name: str
    ) -> list[dict[str, Any]]:
        cap = clamp_limit(limit, minimum=1, maximum=50, default=5)
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for hit in hits:
            if isinstance(hit, Hit):
                title = hit.title
                url = hit.url
                description = hit.description
                extra = hit.extra
            elif isinstance(hit, dict):
                title = str(hit.get("title") or "")
                url = str(hit.get("url") or "")
                description = str(hit.get("description") or "")
                extra = {
                    key: value
                    for key, value in hit.items()
                    if key not in {"title", "url", "description", "position"}
                }
            else:
                continue

            url = (url or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)

            item: dict[str, Any] = {
                "title": (title or "").strip() or url,
                "url": url,
                "description": (description or "").strip(),
                "position": len(normalized) + 1,
            }
            for key, value in (extra or {}).items():
                if key not in item and value is not None:
                    item[key] = value
            item["backend"] = (extra or {}).get("backend") or backend_name
            normalized.append(item)
            if len(normalized) >= cap:
                break
        return normalized

    @staticmethod
    def _ensure_entries(
        urls: Sequence[str], entries: list[Any], backend_name: str
    ) -> list[dict[str, Any]]:
        by_url: dict[str, dict[str, Any]] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            url = str(entry.get("url") or "").strip()
            if url and url not in by_url:
                by_url[url] = entry

        ordered: list[dict[str, Any]] = []
        for url in urls:
            entry = by_url.get(url)
            if entry is None:
                ordered.append(
                    extract_entry(
                        url,
                        error=f"{backend_name}: no entry returned",
                        backend=backend_name,
                    )
                )
                continue
            metadata = entry.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
            metadata.setdefault("sourceURL", url)
            metadata.setdefault("backend", backend_name)
            entry["metadata"] = metadata
            entry.setdefault("raw_content", entry.get("content", ""))
            ordered.append(entry)
        return ordered

    def _mark_failure(self, name: str, outcome: SearchOutcome) -> None:
        # Only health failures carry a cooldown. Validation errors (for example
        # a query that is too long) must not sideline a healthy backend.
        seconds = outcome.cooldown
        if seconds is None:
            return
        if seconds > 0:
            with self._lock:
                self._cooldowns[name] = self._clock() + float(seconds)

    def _is_cooling(self, name: str) -> bool:
        with self._lock:
            until = self._cooldowns.get(name)
        return until is not None and self._clock() < until
