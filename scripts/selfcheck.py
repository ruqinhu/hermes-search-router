#!/usr/bin/env python3
"""Offline self-check and optional live smoke test for the search router.

Dry run (default): config, backend availability, env names, executable paths.
No network, no API cost.

Live: ``--live`` performs one search per available backend and optionally one
extract. This may consume DeepSeek/AnySearch quota.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_package():
    if "hsr" in sys.modules:
        return sys.modules["hsr"]
    spec = importlib.util.spec_from_file_location(
        "hsr",
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["hsr"] = module
    spec.loader.exec_module(module)
    return module


_load_package()

from hsr.config import ConfigError, ConfigStore  # noqa: E402
from hsr.factory import build_backends, env_value  # noqa: E402


def _presence(name: str) -> str:
    return "set" if env_value(name) else "missing"


def _summary(config, backends) -> dict:
    rows = []
    for name, backend_config in config.backends.items():
        backend = backends.get(name)
        rows.append(
            {
                "name": name,
                "type": backend_config.type,
                "available": bool(backend and backend.available()),
                "capabilities": (
                    sorted(backend.capabilities) if backend is not None else []
                ),
                "timeout": backend_config.timeout,
                "command": (
                    " ".join(backend_config.command)
                    if backend_config.type == "script"
                    else None
                ),
            }
        )
    return {
        "config_path": None,
        "active": config.active,
        "on_empty": config.on_empty,
        "search_fallback": list(config.search_fallback),
        "extract_fallback": list(config.extract_fallback),
        "env": {
            "DEEPSEEK_API_KEY": _presence("DEEPSEEK_API_KEY"),
            "ANYSEARCH_API_KEY": _presence("ANYSEARCH_API_KEY"),
            "DEEPSEEK_SEARCH_NEED_CONTENT": _presence(
                "DEEPSEEK_SEARCH_NEED_CONTENT"
            ),
        },
        "backends": rows,
    }


def _print_summary(summary: dict) -> None:
    print(f"config: {summary['config_path']}")
    print(
        f"active: {summary['active']}  on_empty: {summary['on_empty']}  "
        f"search_fallback: {summary['search_fallback']}  "
        f"extract_fallback: {summary['extract_fallback']}"
    )
    print("env:")
    for name, state in summary["env"].items():
        print(f"  {name}: {state}")
    print("backends:")
    for row in summary["backends"]:
        caps = ",".join(row["capabilities"]) or "-"
        print(
            f"  {row['name']:<16} type={row['type']:<9} "
            f"available={str(row['available']).lower():<5} caps={caps}"
        )
        if row["command"]:
            print(f"    command: {row['command']}")


def _live_search(config, backends, query: str, limit: int) -> list[dict]:
    results = []
    for name, backend_config in config.backends.items():
        backend = backends.get(name)
        if backend is None or not backend.available():
            continue
        if "search" not in backend.capabilities:
            continue
        started = time.monotonic()
        try:
            outcome = backend.search(
                query,
                limit,
                timeout=min(20.0, config.search_total_timeout),
            )
        except Exception as exc:  # noqa: BLE001
            results.append(
                {
                    "backend": name,
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                }
            )
            continue
        first = None
        if outcome.hits:
            first = {
                "title": outcome.hits[0].title,
                "url": outcome.hits[0].url,
            }
        results.append(
            {
                "backend": name,
                "ok": outcome.ok,
                "hits": len(outcome.hits),
                "first": first,
                "error": outcome.error,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            }
        )
    return results


def _live_extract(config, backends, url: str) -> list[dict]:
    results = []
    for name in config.backends:
        backend = backends.get(name)
        if backend is None or not backend.available():
            continue
        if "extract" not in backend.capabilities:
            continue
        started = time.monotonic()
        try:
            entries = backend.extract(
                [url], timeout=min(30.0, config.extract_total_timeout)
            )
        except Exception as exc:  # noqa: BLE001
            results.append(
                {
                    "backend": name,
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                }
            )
            continue
        entry = entries[0] if entries else {}
        results.append(
            {
                "backend": name,
                "ok": bool(entry.get("content")) and not entry.get("error"),
                "url": entry.get("url", url),
                "title": entry.get("title", ""),
                "content_chars": len(entry.get("content") or ""),
                "error": entry.get("error"),
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            }
        )
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="routes.yaml path")
    parser.add_argument("--live", action="store_true", help="run real API calls")
    parser.add_argument(
        "--query", default="DeepSeek V4 发布", help="live search query"
    )
    parser.add_argument("--limit", type=int, default=3, help="live result limit")
    parser.add_argument("--extract", metavar="URL", help="live extract URL")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args(argv)

    store = ConfigStore(args.config)
    try:
        config = store.get()
    except ConfigError as exc:
        if args.json:
            print(json.dumps({"ok": False, "config_error": str(exc)}))
        else:
            print(f"config error: {exc}", file=sys.stderr)
        return 1

    backends = build_backends(config)
    summary = _summary(config, backends)
    summary["config_path"] = str(store.path)

    report: dict = {"ok": True, "summary": summary}
    if not args.live:
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            _print_summary(summary)
            print("\ndry run: no network calls. Add --live to test real backends.")
        return 0 if any(row["available"] for row in summary["backends"]) else 1

    report["live"] = {
        "search": _live_search(config, backends, args.query, args.limit)
    }
    if args.extract:
        report["live"]["extract"] = _live_extract(config, backends, args.extract)
    report["ok"] = any(
        item.get("ok") for item in report["live"]["search"]
    )

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_summary(summary)
        print("\nlive search:")
        for item in report["live"]["search"]:
            print(
                f"  {item['backend']:<16} ok={str(item['ok']).lower():<5} "
                f"hits={item.get('hits', '-')} elapsed_ms={item['elapsed_ms']}"
            )
            if item.get("first"):
                print(
                    f"    first: {item['first']['title']} "
                    f"<{item['first']['url']}>"
                )
            if item.get("error"):
                print(f"    error: {item['error']}")
        if "extract" in report["live"]:
            print("\nlive extract:")
            for item in report["live"]["extract"]:
                print(
                    f"  {item['backend']:<16} ok={str(item['ok']).lower():<5} "
                    f"chars={item.get('content_chars', 0)} "
                    f"elapsed_ms={item['elapsed_ms']}"
                )
                if item.get("error"):
                    print(f"    error: {item['error']}")
        print(
            "\nnote: DeepSeek Search/Fetch is free until 2026-11-01 00:00 "
            "Beijing time, then billed per successful request."
        )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
