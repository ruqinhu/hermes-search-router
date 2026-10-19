#!/usr/bin/env python3
"""Concurrent multi-query search for Hermes reports.

Uses the search-router provider (DeepSeek primary, AnySearch fallback) instead
of the legacy AnySearch-only ``concurrent_search.py``.

Examples:
    python3 batch_search.py --profile news --workers 6 \
        "澜起科技 公告" "存储 涨价" "美股 隔夜 科技股"

    python3 batch_search.py --file queries.txt --dedupe-urls --json --out /tmp/res.json

    python3 batch_search.py --stdin --extract-top 1 < queries.txt
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
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

from hsr.batch import (  # noqa: E402
    BatchConfig,
    PROFILES,
    resolve_profile,
    run_batch,
)
from hsr.provider import SearchRouterProvider  # noqa: E402


def _read_queries(args: argparse.Namespace) -> list[str]:
    queries = list(args.queries)
    if args.file:
        if args.file == "-":
            text = sys.stdin.read()
        else:
            text = Path(args.file).read_text(encoding="utf-8")
        queries.extend(text.splitlines())
    if args.stdin:
        queries.extend(sys.stdin.read().splitlines())
    cleaned: list[str] = []
    for query in queries:
        value = str(query).strip()
        if value and not value.startswith("#"):
            cleaned.append(value)
    return cleaned


def _print_human(result: dict, *, profile: str) -> None:
    summary = result.get("summary", {})
    print(
        "batch: "
        f"profile={profile} queries={summary.get('queries')} "
        f"workers={summary.get('workers')} count={summary.get('count')} "
        f"search={summary.get('search_wall_ms')}ms "
        f"wall={summary.get('wall_ms')}ms spread={summary.get('spread_ms')}ms"
    )
    print(
        "status: "
        f"succeeded={summary.get('succeeded')} failed={summary.get('failed')} "
        f"stale={summary.get('stale')} date_unknown={summary.get('date_unknown')} "
        f"duplicates_removed={summary.get('duplicates_removed')}"
    )
    for query, item in result.get("results", {}).items():
        if item.get("ok"):
            print(
                f"[ok] {query}  backend={item.get('backend')} "
                f"hits={len(item.get('hits', []))} "
                f"stale={item.get('stale_count', 0)} "
                f"tries={item.get('tries')} {item.get('elapsed_ms')}ms"
            )
            for index, hit in enumerate(item.get("hits", [])[:5], 1):
                flags = []
                if hit.get("date"):
                    flags.append(f"date={hit['date']}")
                if hit.get("age_days") is not None:
                    flags.append(f"age={hit['age_days']}d")
                if hit.get("stale"):
                    flags.append("STALE")
                if hit.get("date_unknown"):
                    flags.append("date?")
                if hit.get("extract_error"):
                    flags.append(f"extract_error={hit['extract_error']}")
                print(f"  {index}. {hit.get('title') or ''}")
                print(f"     {hit.get('url') or ''}")
                if flags:
                    print(f"     {' | '.join(flags)}")
        else:
            print(
                f"[FAIL] {query}  tries={item.get('tries')} "
                f"{item.get('elapsed_ms')}ms  error={item.get('error')}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("queries", nargs="*", help="queries (or use --file/--stdin)")
    parser.add_argument("--file", help="read queries from a file (one per line; - = stdin)")
    parser.add_argument("--stdin", action="store_true", help="read queries from stdin")
    parser.add_argument(
        "--profile", choices=sorted(PROFILES), default="general", help="preset"
    )
    parser.add_argument("--workers", type=int, default=6, help="1-12")
    parser.add_argument("--count", type=int, help="results per query (profile default)")
    parser.add_argument("--retries", type=int, default=1, help="retries per query (0-3)")
    parser.add_argument(
        "--max-age-days", type=float, help="mark hits older than this as stale"
    )
    parser.add_argument("--dedupe-urls", action="store_true", help="dedupe across queries")
    parser.add_argument("--extract-top", type=int, help="extract top N hits per query")
    parser.add_argument(
        "--extract-max-chars", type=int, default=4000, help="content chars kept"
    )
    parser.add_argument("--out", help="write full JSON to this file")
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.add_argument(
        "--fail-on-error", action="store_true", help="exit 1 if any query failed"
    )
    parser.add_argument("--config", help="routes.yaml path")
    args = parser.parse_args(argv)

    queries = _read_queries(args)
    if not queries:
        print("no queries; pass arguments, --file, or --stdin", file=sys.stderr)
        return 2

    profile = resolve_profile(args.profile)
    count = args.count if args.count is not None else int(profile["count"])
    max_age = (
        args.max_age_days
        if args.max_age_days is not None
        else float(profile["max_age_days"])
    )
    extract_top = (
        args.extract_top
        if args.extract_top is not None
        else int(profile["extract_top"])
    )

    config = BatchConfig(
        workers=args.workers,
        count=count,
        retries=args.retries,
        max_age_days=max_age,
        dedupe_urls=args.dedupe_urls,
        extract_top=extract_top,
        extract_max_chars=args.extract_max_chars,
    )
    provider = SearchRouterProvider(config_path=args.config)
    result = run_batch(provider, queries, config)

    if args.out:
        Path(args.out).write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_human(result, profile=args.profile)
        if args.out:
            print(f"json written to {args.out}")

    if not result.get("ok"):
        return 1
    if args.fail_on_error and result.get("summary", {}).get("failed"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
