"""Admin/status helpers for the search router.

The payload functions are pure enough to test with fake backends/providers.
The CLI entry points are registered into Hermes and also exposed as
``scripts/router_admin.py`` for standalone use.
"""

from __future__ import annotations

import argparse
import json
import time
from typing import Any, Sequence

from .config import ConfigError, ConfigStore, RouteConfig
from .factory import build_backends, env_value

__all__ = [
    "add_subparsers",
    "bench_payload",
    "handle_cli",
    "main",
    "register_cli",
    "status_payload",
    "test_payload",
]


def _chain_names(config: RouteConfig) -> list[str]:
    names: list[str] = []
    if config.active != "auto":
        names.append(config.active)
    names.extend(config.search_fallback)
    result: list[str] = []
    for name in names:
        if name not in result:
            result.append(name)
    return result


def _capabilities(backend: Any) -> list[str]:
    try:
        return sorted(str(cap) for cap in backend.capabilities)
    except Exception:  # noqa: BLE001
        return []


def _available(backend: Any) -> bool:
    try:
        return bool(backend.available())
    except Exception:  # noqa: BLE001
        return False


def _backend_row(name: str, backend_config: Any, backend: Any) -> dict[str, Any]:
    return {
        "name": name,
        "type": backend_config.type,
        "available": _available(backend) if backend is not None else False,
        "capabilities": _capabilities(backend) if backend is not None else [],
        "timeout": backend_config.timeout,
        "command": (
            " ".join(backend_config.command)
            if backend_config.type == "script"
            else None
        ),
    }


def status_payload(
    store: ConfigStore,
    config: RouteConfig,
    backends: dict[str, Any],
) -> dict[str, Any]:
    rows = [
        _backend_row(name, backend_config, backends.get(name))
        for name, backend_config in config.backends.items()
    ]
    chain = []
    for name in _chain_names(config):
        backend = backends.get(name)
        backend_config = config.backends.get(name)
        if backend is None or backend_config is None:
            continue
        chain.append(_backend_row(name, backend_config, backend))
    return {
        "config_path": str(store.path),
        "active": config.active,
        "on_empty": config.on_empty,
        "search_fallback": list(config.search_fallback),
        "extract_fallback": list(config.extract_fallback),
        "last_error": store.last_error,
        "env": {
            "DEEPSEEK_API_KEY": "set" if env_value("DEEPSEEK_API_KEY") else "missing",
            "ANYSEARCH_API_KEY": "set" if env_value("ANYSEARCH_API_KEY") else "missing",
            "DEEPSEEK_SEARCH_NEED_CONTENT": (
                "set" if env_value("DEEPSEEK_SEARCH_NEED_CONTENT") else "missing"
            ),
        },
        "chain": chain,
        "backends": rows,
    }


def test_payload(
    provider: Any,
    query: str,
    *,
    limit: int = 5,
    extract_url: str | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        raw = provider.search(query, limit)
    except Exception as exc:  # noqa: BLE001 - show the failure, do not crash
        return {
            "success": False,
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_ms": round((time.monotonic() - started) * 1000.0, 1),
            "attempts": [],
            "hits": [],
        }
    elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
    if not isinstance(raw, dict):
        return {
            "success": False,
            "error": "provider returned a non-object",
            "elapsed_ms": elapsed_ms,
            "attempts": [],
            "hits": [],
        }
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    attempts = raw.get("attempts")
    if not isinstance(attempts, list):
        attempts = data.get("attempts") if isinstance(data.get("attempts"), list) else []
    web = data.get("web") if isinstance(data.get("web"), list) else []
    result: dict[str, Any] = {
        "success": bool(raw.get("success")),
        "error": raw.get("error"),
        "backend": data.get("backend"),
        "attempts": [item for item in attempts if isinstance(item, dict)],
        "hits": web,
        "elapsed_ms": elapsed_ms,
    }
    if extract_url:
        extract_started = time.monotonic()
        try:
            entries = provider.extract([extract_url])
        except Exception as exc:  # noqa: BLE001
            result["extract"] = {
                "url": extract_url,
                "error": f"{type(exc).__name__}: {exc}",
            }
        else:
            entry = entries[0] if isinstance(entries, list) and entries else {}
            content = str(entry.get("content") or "")
            result["extract"] = {
                "url": entry.get("url", extract_url),
                "title": entry.get("title", ""),
                "content_chars": len(content),
                "content_head": content[:500],
                "error": entry.get("error"),
            }
        result["extract_elapsed_ms"] = round(
            (time.monotonic() - extract_started) * 1000.0, 1
        )
    return result


def bench_payload(
    config: RouteConfig,
    backends: dict[str, Any],
    query: str,
    *,
    limit: int = 5,
    timeout: float = 20.0,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for name, backend_config in config.backends.items():
        backend = backends.get(name)
        if backend is None or "search" not in _capabilities(backend):
            continue
        if not _available(backend):
            continue
        started = time.monotonic()
        try:
            outcome = backend.search(
                query,
                limit,
                timeout=min(float(timeout), config.search_total_timeout),
            )
        except Exception as exc:  # noqa: BLE001
            rows.append(
                {
                    "backend": name,
                    "ok": False,
                    "hits": 0,
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_ms": round((time.monotonic() - started) * 1000.0, 1),
                }
            )
            continue
        rows.append(
            {
                "backend": name,
                "ok": bool(outcome.ok),
                "hits": len(outcome.hits),
                "error": outcome.error,
                "elapsed_ms": round((time.monotonic() - started) * 1000.0, 1),
            }
        )
    return {
        "query": query,
        "limit": limit,
        "ok": any(row["ok"] for row in rows),
        "results": rows,
    }


def _format_status(payload: dict[str, Any], *, title: str) -> str:
    lines = [
        title,
        f"config: {payload['config_path']}",
        (
            f"active: {payload['active']}  on_empty: {payload['on_empty']}  "
            f"search_fallback: {payload['search_fallback']}  "
            f"extract_fallback: {payload['extract_fallback']}"
        ),
    ]
    if payload.get("last_error"):
        lines.append(f"last_error: {payload['last_error']}")
    lines.append("env:")
    for name, state in payload["env"].items():
        lines.append(f"  {name}: {state}")
    lines.append("chain:")
    for row in payload.get("chain", []):
        lines.append(
            f"  {row['name']:<16} type={row['type']:<9} "
            f"available={str(row['available']).lower():<5} "
            f"caps={','.join(row['capabilities']) or '-'}"
        )
    lines.append("backends:")
    for row in payload.get("backends", []):
        lines.append(
            f"  {row['name']:<16} type={row['type']:<9} "
            f"available={str(row['available']).lower():<5} "
            f"caps={','.join(row['capabilities']) or '-'}"
        )
        if row.get("command"):
            lines.append(f"    command: {row['command']}")
    return "\n".join(lines)


def _format_test(payload: dict[str, Any]) -> str:
    lines = [
        f"success={payload.get('success')} backend={payload.get('backend')} "
        f"elapsed_ms={payload.get('elapsed_ms')}",
    ]
    if payload.get("error"):
        lines.append(f"error: {payload['error']}")
    attempts = payload.get("attempts") or []
    if attempts:
        lines.append("attempts:")
        for attempt in attempts:
            lines.append(
                f"  {attempt.get('backend')} ok={attempt.get('ok')} "
                f"hits={attempt.get('hits', '-')} "
                f"error={attempt.get('error', '-')} "
                f"elapsed_ms={attempt.get('elapsed_ms', '-')}"
            )
    for index, hit in enumerate(payload.get("hits", [])[:5], 1):
        lines.append(f"  {index}. {hit.get('title') or ''}")
        lines.append(f"     {hit.get('url') or ''}")
    if "extract" in payload:
        extract = payload["extract"]
        lines.append("extract:")
        lines.append(
            f"  url={extract.get('url')} chars={extract.get('content_chars', 0)} "
            f"error={extract.get('error', '-')}"
        )
    return "\n".join(lines)


def _format_bench(payload: dict[str, Any]) -> str:
    lines = [f"bench query={payload.get('query')!r} limit={payload.get('limit')}"]
    for row in payload.get("results", []):
        lines.append(
            f"  {row['backend']:<16} ok={str(row['ok']).lower():<5} "
            f"hits={row['hits']} elapsed_ms={row['elapsed_ms']}"
            + (f" error={row['error']}" if row.get("error") else "")
        )
    return "\n".join(lines)


def add_subparsers(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(dest="search_router_action", required=True)

    status = subparsers.add_parser("status", help="show routes and backend availability")
    status.add_argument("--config", help="routes.yaml path")
    status.add_argument("--json", action="store_true")

    describe = subparsers.add_parser(
        "describe", help="show the active chain (status + chain detail)"
    )
    describe.add_argument("--config", help="routes.yaml path")
    describe.add_argument("--json", action="store_true")

    test = subparsers.add_parser("test", help="run one search through the router")
    test.add_argument("query")
    test.add_argument("--limit", type=int, default=5)
    test.add_argument("--config", help="routes.yaml path")
    test.add_argument("--extract", metavar="URL", help="also extract one URL")
    test.add_argument("--json", action="store_true")

    bench = subparsers.add_parser(
        "bench", help="time each available backend directly (live)"
    )
    bench.add_argument("query")
    bench.add_argument("--limit", type=int, default=5)
    bench.add_argument("--timeout", type=float, default=20.0)
    bench.add_argument("--config", help="routes.yaml path")
    bench.add_argument("--json", action="store_true")


def _load_config(store: ConfigStore) -> RouteConfig | None:
    try:
        return store.get()
    except ConfigError as exc:
        print(f"search-router config error: {exc}")
        return None


def handle_cli(args: argparse.Namespace) -> int:
    action = args.search_router_action
    store = ConfigStore(args.config)
    config = _load_config(store)
    if config is None:
        return 1

    if action in {"status", "describe"}:
        try:
            backends = build_backends(config)
        except Exception as exc:  # noqa: BLE001
            print(f"search-router backend construction failed: {exc}")
            return 1
        payload = status_payload(store, config, backends)
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(
                _format_status(
                    payload,
                    title="Search Router status" if action == "status" else "Search Router describe",
                )
            )
        return 0

    if action == "test":
        try:
            from .provider import SearchRouterProvider
        except Exception as exc:  # noqa: BLE001
            print(f"search-router provider import failed: {exc}")
            return 1
        provider = SearchRouterProvider(config_path=args.config)
        payload = test_payload(
            provider,
            args.query,
            limit=args.limit,
            extract_url=args.extract,
        )
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(_format_test(payload))
        return 0 if payload.get("success") else 1

    if action == "bench":
        try:
            backends = build_backends(config)
        except Exception as exc:  # noqa: BLE001
            print(f"search-router backend construction failed: {exc}")
            return 1
        payload = bench_payload(
            config,
            backends,
            args.query,
            limit=args.limit,
            timeout=args.timeout,
        )
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(_format_bench(payload))
        return 0 if payload.get("ok") else 1

    print(f"unknown action: {action}")
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hermes search-router",
        description="Inspect and test the Hermes search router.",
    )
    add_subparsers(parser)
    args = parser.parse_args(argv)
    return handle_cli(args)


def register_cli(ctx: Any) -> None:
    """Register ``hermes search-router`` with a Hermes PluginContext."""

    register = getattr(ctx, "register_cli_command", None)
    if not callable(register):
        return
    register(
        "search-router",
        "Inspect and test the search router",
        add_subparsers,
        handle_cli,
        description="status / describe / test / bench for search-router",
    )
