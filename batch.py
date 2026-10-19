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

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MAX_WORKERS = 12
MAX_COUNT = 50
MAX_RETRIES = 3
MAX_EXTRACT_TOP = 10
DEFAULT_EXTRACT_CHARS = 4000

_TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "spm",
    "scm",
    "from",
    "share",
    "share_token",
    "ref",
    "referrer",
    "fbclid",
    "gclid",
    "yclid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "_ga",
    "_gl",
}
_URL_DATE_PATTERNS = (
    re.compile(r"/(20\d{2})-(\d{2})-(\d{2})(?:/|$)"),
    re.compile(r"/(20\d{2})-(\d{2})/(\d{2})(?:/|$)"),
    re.compile(r"/(20\d{2})/(\d{2})/(\d{2})(?:/|$)"),
    re.compile(r"/a/(20\d{6})"),
    re.compile(r"/p(20\d{6})"),
    re.compile(r"/(20\d{6})(?:\D|$)"),
)
_SORT_MODES = {"relevance", "date", "authority"}

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
    min_authority: int | None = None
    require_authority: bool = False
    sort: str = "relevance"
    query_suffix: str = ""
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], datetime] = _utcnow


def resolve_profile(name: str) -> dict[str, Any]:
    profile = PROFILES.get(name)
    if profile is None:
        raise ValueError(f"unknown profile {name!r}; choose one of {sorted(PROFILES)}")
    return dict(profile)


def normalize_url(url: Any) -> str:
    """Canonical key for cross-query dedupe; the original URL is kept in output."""

    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw
    if not parts.scheme or not parts.hostname:
        return raw
    scheme = parts.scheme.lower()
    if scheme in {"http", "https"}:
        # Dedupe key only: report the same page once across http/https variants.
        scheme = "https"
    try:
        host = parts.hostname.lower()
    except ValueError:
        return raw
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return raw
    try:
        port = parts.port
    except ValueError:
        return raw
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host
    if port and port not in (80, 443):
        netloc = f"{host}:{port}"
    pairs = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_PARAMS
        and not key.lower().startswith("utm_")
    ]
    query = urlencode(sorted(pairs))
    path = parts.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return urlunsplit((scheme, netloc, path, query, ""))


def date_from_url(url: Any) -> datetime | None:
    text = str(url or "")
    for pattern in _URL_DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        groups = match.groups()
        if len(groups) == 3:
            year, month, day = groups
        else:
            compact = groups[0]
            year, month, day = compact[:4], compact[4:6], compact[6:8]
        try:
            return datetime(int(year), int(month), int(day), tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def build_query_suffix(
    sites: Iterable[str] | str | None = None,
    lang: str | None = None,
    after: str | None = None,
) -> str:
    """Build a DeepSeek search-operator suffix.

    Multiple sites are joined with ``OR`` (DeepSeek syntax). AnySearch fallback
    may treat these operators literally, so use them when DeepSeek quality
    matters.
    """

    if isinstance(sites, str):
        sites = [sites]
    site_list: list[str] = []
    for raw_site in sites or []:
        value = str(raw_site).strip()
        if not value:
            continue
        value = re.sub(r"^https?://", "", value, flags=re.IGNORECASE)
        value = value.split("/", 1)[0].split("?", 1)[0].strip()
        if not re.fullmatch(r"[A-Za-z0-9.-]+", value):
            raise ValueError(f"invalid --site value: {raw_site!r}")
        site_list.append(value)
    parts: list[str] = []
    if site_list:
        parts.append(" OR ".join(f"site:{site}" for site in site_list))
    if lang and str(lang).strip():
        value = str(lang).strip()
        if not re.fullmatch(r"[A-Za-z-]+(?:,[A-Za-z-]+)*", value):
            raise ValueError(f"invalid --lang value: {lang!r}")
        parts.append(f"lang:{value}")
    if after and str(after).strip():
        value = str(after).strip()
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("after must be YYYY-MM-DD") from exc
        parts.append(f"after:{value}")
    return (" " + " ".join(parts)) if parts else ""


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


def _age_for_datetime(
    parsed: datetime | None, now: Callable[[], datetime]
) -> tuple[float | None, bool]:
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
    field_dt = _parse_datetime(item.get("date"))
    url_dt = date_from_url(item.get("url"))
    if field_dt is not None:
        parsed = field_dt
        source = "field"
    elif url_dt is not None:
        parsed = url_dt
        source = "url"
        item["date"] = url_dt.isoformat()
    else:
        parsed = None
        source = None
    age, unknown = _age_for_datetime(parsed, now)
    item["date_source"] = source
    item["date_unknown"] = unknown
    item["age_days"] = round(age, 2) if age is not None else None
    item["stale"] = bool(
        age is not None
        and max_age_days is not None
        and age > float(max_age_days)
    )
    return item


def _attempts_from(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, dict):
        return []
    attempts = raw.get("attempts")
    if isinstance(attempts, list):
        return [item for item in attempts if isinstance(item, dict)]
    data = raw.get("data")
    if isinstance(data, dict):
        attempts = data.get("attempts")
        if isinstance(attempts, list):
            return [item for item in attempts if isinstance(item, dict)]
    return []


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
    attempts = _attempts_from(raw)
    if not isinstance(raw, dict):
        return {
            "query": query,
            "ok": False,
            "tries": tries,
            "elapsed_ms": elapsed_ms,
            "backend": None,
            "hits": [],
            "stale_count": 0,
            "attempts": attempts,
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
            "attempts": attempts,
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
            "attempts": attempts,
            "error": "response is missing data.web",
        }

    hits = [
        _annotate_hit(hit, now=config.now, max_age_days=max_age_days)
        for hit in web
        if isinstance(hit, dict) and hit.get("url")
    ]
    backend = (
        data.get("backend")
        if isinstance(data, dict) and isinstance(data.get("backend"), str)
        else next(
            (hit.get("backend") for hit in hits if hit.get("backend")),
            None,
        )
    )
    return {
        "query": query,
        "ok": True,
        "tries": tries,
        "elapsed_ms": elapsed_ms,
        "backend": backend,
        "hits": hits,
        "stale_count": sum(1 for hit in hits if hit.get("stale")),
        "attempts": attempts,
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
    last_attempts: list[dict[str, Any]] = []
    search_query = f"{query}{config.query_suffix}" if config.query_suffix else query
    for attempt in range(retries + 1):
        tries = attempt + 1
        try:
            raw = provider.search(search_query, count)
        except Exception as exc:  # noqa: BLE001 - isolate per-query failures
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            last_attempts = _attempts_from(raw)
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
        "attempts": last_attempts,
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
            key = normalize_url(hit.get("url"))
            if key and key in seen:
                removed += 1
                per_query_removed += 1
                continue
            if key:
                seen.add(key)
            kept.append(hit)
        result["hits"] = kept
        result["duplicates_removed"] = per_query_removed
        result["stale_count"] = sum(1 for hit in kept if hit.get("stale"))
    return removed


def _authority_value(hit: dict[str, Any]) -> int | None:
    value = hit.get("authority")
    if isinstance(value, bool):
        return int(value)
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None


def _set_positions(hits: Sequence[dict[str, Any]]) -> None:
    for index, hit in enumerate(hits, 1):
        hit["position"] = index


def _filter_authority(
    ordered: Sequence[dict[str, Any]],
    *,
    min_authority: int | None,
    require_authority: bool,
) -> int:
    removed = 0
    for result in ordered:
        kept: list[dict[str, Any]] = []
        per_query_removed = 0
        for hit in result.get("hits", []):
            authority = _authority_value(hit)
            if authority is None:
                if require_authority:
                    removed += 1
                    per_query_removed += 1
                    continue
                kept.append(hit)
                continue
            if min_authority is not None and authority < min_authority:
                removed += 1
                per_query_removed += 1
                continue
            kept.append(hit)
        result["hits"] = kept
        result["filtered_authority"] = per_query_removed
        result["stale_count"] = sum(1 for hit in kept if hit.get("stale"))
        _set_positions(kept)
    return removed


def _date_key(hit: dict[str, Any]) -> float:
    parsed = _parse_datetime(hit.get("date")) or date_from_url(hit.get("url"))
    return parsed.timestamp() if parsed else float("-inf")


def _sort_hits(ordered: Sequence[dict[str, Any]], mode: str) -> None:
    if mode not in _SORT_MODES:
        return
    for result in ordered:
        hits = result.get("hits", [])
        if mode == "authority":
            hits.sort(
                key=lambda hit: (
                    _authority_value(hit) is not None,
                    _authority_value(hit) if _authority_value(hit) is not None else -1,
                ),
                reverse=True,
            )
        elif mode == "date":
            hits.sort(key=_date_key, reverse=True)
        _set_positions(hits)


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
    min_authority = cfg.min_authority
    if min_authority is not None:
        try:
            min_authority = int(min_authority)
        except (TypeError, ValueError) as exc:
            raise ValueError("min_authority must be an integer 0-4") from exc
        min_authority = max(0, min(4, min_authority))
    sort_mode = cfg.sort if cfg.sort in _SORT_MODES else "relevance"
    query_suffix = cfg.query_suffix if isinstance(cfg.query_suffix, str) else ""
    query_suffix = query_suffix.strip()
    if query_suffix:
        query_suffix = " " + query_suffix
    effective = replace(
        cfg,
        workers=workers,
        count=count,
        retries=retries,
        max_age_days=max_age_days,
        extract_top=extract_top,
        extract_max_chars=max_chars,
        min_authority=min_authority,
        sort=sort_mode,
        query_suffix=query_suffix,
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
                    "attempts": [],
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
    filtered_authority = 0
    if min_authority is not None or cfg.require_authority:
        filtered_authority = _filter_authority(
            ordered,
            min_authority=min_authority,
            require_authority=bool(cfg.require_authority),
        )
    # Sort before dedupe so a duplicate inside one query keeps the best hit
    # under the requested order, not merely the provider's first row.
    _sort_hits(ordered, sort_mode)
    if cfg.dedupe_urls:
        duplicates_removed = _dedupe_results(ordered)
    for result in ordered:
        _set_positions(result.get("hits", []))
        result["stale_count"] = sum(
            1 for hit in result.get("hits", []) if hit.get("stale")
        )

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
        "min_authority": min_authority,
        "require_authority": bool(cfg.require_authority),
        "sort": sort_mode,
        "query_suffix": query_suffix,
        "dedupe_urls": bool(cfg.dedupe_urls),
        "extract_top": extract_top,
        "succeeded": succeeded,
        "failed": failed,
        "stale": stale,
        "date_unknown": date_unknown,
        "duplicates_removed": duplicates_removed,
        "filtered_authority": filtered_authority,
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
