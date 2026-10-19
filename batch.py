"""Batch/concurrent search over the search-router provider.

This module is deliberately free of Hermes imports so it can be tested with a
fake provider. The CLI (`scripts/batch_search.py`) supplies the real
``SearchRouterProvider``.

It ports the report pipeline lessons from the live Hermes host:
- at most 12 concurrent searches
- one retry per query, failure isolation
- per-query timing, wall clock, and start spread (proof of real concurrency)
- date/age/stale annotation for the old-news trap
- optional cross-query URL dedupe and top-N content extraction
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence

MAX_WORKERS = 12
MAX_COUNT = 50
MAX_RETRIES = 3
MAX_EXTRACT_TOP = 10
DEFAULT_EXTRACT_CHARS = 4000

PROFILES: dict[str, dict[str, Any]] = {
    "general": {"count": 6, "max_age_days": 7.0, "extract_top": 0},
    "news": {"count": 8, "max_age_days": 3.0, "extract_top": 0},
    "research": {"count": 5, "max_age_days": 30.0, "extract_top": 2},
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class BatchConfig:
    workers: int = 6
    count: int = 6
    retries: int = 1
    max_age_days: float | None = 7.0
    dedupe_urls: bool = False
    extract_top: int = 0
    extract_max_chars: int = DEFAULT_EXTRACT_CHARS
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], datetime] = _utcnow


def resolve_profile(name: str) -> dict[str, Any]:
    profile = PROFILES.get(name)
    if profile is None:
        raise ValueError(f"unknown profile {name!r}; choose one of {sorted(PROFILES)}")
    return dict(profile)


def _unique_queries(queries: Iterable[Any]) -> tuple[list[str], int]:
    seen: set[str] = set()
    ordered: list[str] = []
    removed = 0
    for raw in queries:
        text = str(raw).strip()
        if not text:
            continue
        if text in seen:
            removed += 1
            continue
        seen.add(text)
        ordered.append(text)
    return ordered, removed


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_days(value: Any, now: Callable[[], datetime]) -> tuple[float | None, bool]:
    parsed = _parse_datetime(value)
    if parsed is None:
        return None, True
    reference = now()
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    age = (reference - parsed).total_seconds() / 86400.0
    return max(0.0, age), False


def _annotate_hit(
    hit: dict[str, Any],
    *,
    now: Callable[[], datetime],
    max_age_days: float | None,
) -> dict[str, Any]:
    item = dict(hit)
    age, unknown = _age_days(item.get("date"), now)
    item["date_unknown"] = unknown
    item["age_days"] = round(age, 2) if age is not None else None
    item["stale"] = bool(
        age is not None
        and max_age_days is not None
        and age > float(max_age_days)
    )
    return item


def _result_ok(
    query: str,
    raw: Any,
    *,
    tries: int,
    started: float,
    config: BatchConfig,
    max_age_days: float | None,
) -> dict[str, Any]:
    elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
    if not isinstance(raw, dict):
        return {
            "query": query,
            "ok": False,
            "tries": tries,
            "elapsed_ms": elapsed_ms,
            "backend": None,
            "hits": [],
            "stale_count": 0,
            "error": "provider returned a non-object",
        }
    if not raw.get("success"):
        return {
            "query": query,
            "ok": False,
            "tries": tries,
            "elapsed_ms": elapsed_ms,
            "backend": None,
            "hits": [],
            "stale_count": 0,
            "error": str(raw.get("error") or "search failed"),
        }

    data = raw.get("data")
    web = data.get("web") if isinstance(data, dict) else None
    if not isinstance(web, list):
        return {
            "query": query,
            "ok": False,
            "tries": tries,
            "elapsed_ms": elapsed_ms,
            "backend": None,
            "hits": [],
            "stale_count": 0,
            "error": "response is missing data.web",
        }

    hits = [
        _annotate_hit(hit, now=config.now, max_age_days=max_age_days)
        for hit in web
        if isinstance(hit, dict) and hit.get("url")
    ]
    backend = next(
        (hit.get("backend") for hit in hits if hit.get("backend")),
        None,
    )
    return {
        "query": query,
        "ok": True,
        "tries": tries,
        "elapsed_ms": elapsed_ms,
        "backend": backend,
        "hits": hits,
        "stale_count": sum(1 for hit in hits if hit.get("stale")),
        "error": None,
    }


def _search_one(
    provider: Any,
    query: str,
    *,
    config: BatchConfig,
    count: int,
    retries: int,
    starts: list[float],
    start_lock: Any,
) -> dict[str, Any]:
    started = time.monotonic()
    with start_lock:
        starts.append(started)
    last_error = "search failed"
    for attempt in range(retries + 1):
        tries = attempt + 1
        try:
            raw = provider.search(query, count)
        except Exception as exc:  # noqa: BLE001 - isolate per-query failures
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            result = _result_ok(
                query,
                raw,
                tries=tries,
                started=started,
                config=config,
                max_age_days=config.max_age_days,
            )
            if result["ok"]:
                return result
            last_error = str(result.get("error") or "search failed")
        if attempt < retries:
            config.sleep(0.6 * (tries))
    return {
        "query": query,
        "ok": False,
        "tries": retries + 1,
        "elapsed_ms": round((time.monotonic() - started) * 1000.0, 1),
        "backend": None,
        "hits": [],
        "stale_count": 0,
        "error": last_error,
    }


def _dedupe_results(
    ordered: Sequence[dict[str, Any]],
) -> int:
    seen: set[str] = set()
    removed = 0
    for result in ordered:
        kept: list[dict[str, Any]] = []
        per_query_removed = 0
        for hit in result.get("hits", []):
            url = str(hit.get("url") or "").strip()
            if url and url in seen:
                removed += 1
                per_query_removed += 1
                continue
            if url:
                seen.add(url)
            kept.append(hit)
        result["hits"] = kept
        result["duplicates_removed"] = per_query_removed
        result["stale_count"] = sum(1 for hit in kept if hit.get("stale"))
    return removed


def _extract_top_hits(
    provider: Any,
    ordered: Sequence[dict[str, Any]],
    *,
    extract_top: int,
    max_chars: int,
    workers: int,
) -> tuple[int, int, float]:
    requests: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for result in ordered:
        if not result.get("ok") or not result.get("hits") or extract_top <= 0:
            continue
        top = result["hits"][:extract_top]
        if any(hit.get("url") for hit in top):
            requests.append((result, top))
    if not requests:
        return 0, 0, 0.0

    started = time.monotonic()
    failures = 0
    extract_workers = max(1, min(4, workers))

    def do(item: tuple[dict[str, Any], list[dict[str, Any]]]):
        result, top = item
        urls = [str(hit["url"]) for hit in top if hit.get("url")]
        try:
            entries = provider.extract(urls)
        except Exception as exc:  # noqa: BLE001 - report per-hit failure
            return top, None, f"{type(exc).__name__}: {exc}"
        return top, entries, None

    with ThreadPoolExecutor(max_workers=extract_workers) as pool:
        futures = [pool.submit(do, item) for item in requests]
        for future in as_completed(futures):
            top, entries, error = future.result()
            if error:
                failures += 1
                for hit in top:
                    hit["extract_error"] = error
                continue
            by_url: dict[str, dict[str, Any]] = {}
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict) and entry.get("url"):
                        by_url[str(entry["url"])] = entry
            for hit in top:
                entry = by_url.get(str(hit.get("url")))
                if entry is None:
                    hit["extract_error"] = "no extract entry"
                    failures += 1
                    continue
                content = entry.get("content") or ""
                content = str(content)
                truncated = len(content) > max_chars
                hit["content"] = content[:max_chars] if truncated else content
                hit["content_chars"] = len(content)
                hit["content_truncated"] = truncated
                if entry.get("error"):
                    hit["extract_error"] = str(entry["error"])

    elapsed_ms = (time.monotonic() - started) * 1000.0
    return len(requests), failures, elapsed_ms


def run_batch(
    provider: Any,
    queries: Iterable[Any],
    config: BatchConfig | None = None,
) -> dict[str, Any]:
    """Run a list of queries through ``provider`` and return a JSON-ready dict.

    ``provider`` only needs ``search(query, limit)``; ``extract(urls)`` is used
    only when ``extract_top > 0``.
    """

    cfg = config or BatchConfig()
    if isinstance(queries, str):
        queries = [queries]
    ordered_queries, duplicate_queries_removed = _unique_queries(queries)
    if not ordered_queries:
        return {
            "ok": False,
            "error": "no queries",
            "summary": {},
            "results": {},
        }

    workers = max(1, min(MAX_WORKERS, int(cfg.workers)))
    count = max(1, min(MAX_COUNT, int(cfg.count)))
    retries = max(0, min(MAX_RETRIES, int(cfg.retries)))
    extract_top = max(0, min(MAX_EXTRACT_TOP, int(cfg.extract_top)))
    max_chars = max(100, min(50_000, int(cfg.extract_max_chars)))
    max_age_days = cfg.max_age_days
    if max_age_days is not None and float(max_age_days) < 0:
        max_age_days = 0.0
    effective = replace(
        cfg,
        workers=workers,
        count=count,
        retries=retries,
        max_age_days=max_age_days,
        extract_top=extract_top,
        extract_max_chars=max_chars,
    )

    starts: list[float] = []
    start_lock = threading.Lock()
    started = time.monotonic()
    results_by_query: dict[str, dict[str, Any]] = {}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _search_one,
                provider,
                query,
                config=effective,
                count=count,
                retries=retries,
                starts=starts,
                start_lock=start_lock,
            ): query
            for query in ordered_queries
        }
        for future in as_completed(futures):
            query = futures[future]
            try:
                results_by_query[query] = future.result()
            except Exception as exc:  # noqa: BLE001 - never lose the batch
                results_by_query[query] = {
                    "query": query,
                    "ok": False,
                    "tries": retries + 1,
                    "elapsed_ms": 0.0,
                    "backend": None,
                    "hits": [],
                    "stale_count": 0,
                    "error": f"{type(exc).__name__}: {exc}",
                }

    ordered = [results_by_query[query] for query in ordered_queries]
    search_wall_ms = (time.monotonic() - started) * 1000.0
    # Spread of the first wave only. With more queries than workers, later
    # waves necessarily start after a slot frees; including them would make a
    # perfectly concurrent batch look serial.
    first_wave = sorted(starts)[:workers]
    spread_ms = (
        (max(first_wave) - min(first_wave)) * 1000.0
        if len(first_wave) > 1
        else 0.0
    )
    duplicates_removed = 0
    if cfg.dedupe_urls:
        duplicates_removed = _dedupe_results(ordered)

    extract_queries = 0
    extract_failures = 0
    extract_wall_ms = 0.0
    if extract_top > 0:
        extract_queries, extract_failures, extract_wall_ms = _extract_top_hits(
            provider,
            ordered,
            extract_top=extract_top,
            max_chars=max_chars,
            workers=workers,
        )

    succeeded = sum(1 for result in ordered if result.get("ok"))
    failed = len(ordered) - succeeded
    stale = sum(int(result.get("stale_count") or 0) for result in ordered)
    date_unknown = sum(
        1
        for result in ordered
        for hit in result.get("hits", [])
        if hit.get("date_unknown")
    )
    total_wall_ms = (time.monotonic() - started) * 1000.0

    summary = {
        "queries": len(ordered_queries),
        "duplicate_queries_removed": duplicate_queries_removed,
        "workers": workers,
        "count": count,
        "retries": retries,
        "max_age_days": max_age_days,
        "dedupe_urls": bool(cfg.dedupe_urls),
        "extract_top": extract_top,
        "succeeded": succeeded,
        "failed": failed,
        "stale": stale,
        "date_unknown": date_unknown,
        "duplicates_removed": duplicates_removed,
        "extract_queries": extract_queries,
        "extract_failures": extract_failures,
        "wall_ms": round(total_wall_ms, 1),
        "search_wall_ms": round(search_wall_ms, 1),
        "extract_wall_ms": round(extract_wall_ms, 1),
        "spread_ms": round(spread_ms, 2),
    }
    return {
        "ok": succeeded > 0,
        "summary": summary,
        "results": {query: results_by_query[query] for query in ordered_queries},
    }
