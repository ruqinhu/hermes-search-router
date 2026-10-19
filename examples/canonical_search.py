#!/usr/bin/env python3
"""Example canonical script for the search router.

Contract:
  stdin:  {"capability": "search", "query": "...", "limit": 5}
  stdout: {"results": [{"title": "...", "url": "...", "description": "..."}]}

This template returns deterministic local results so it can be used to test
routing without a network. Replace ``search()`` with your own data source.
"""

from __future__ import annotations

import json
import sys


def search(query: str, limit: int) -> list[dict[str, str]]:
    return [
        {
            "title": f"Local example {index + 1}: {query}",
            "url": f"https://example.com/local/{index + 1}",
            "description": "Replace this template with your local search logic.",
        }
        for index in range(max(1, min(limit, 5)))
    ]


def main() -> int:
    try:
        text = sys.stdin.read().lstrip("\ufeff")
        request = json.loads(text)
    except json.JSONDecodeError as exc:
        print(f"invalid request JSON: {exc}", file=sys.stderr)
        return 2

    query = str(request.get("query") or "").strip()
    if not query:
        print("query must not be blank", file=sys.stderr)
        return 2
    try:
        limit = max(1, min(int(request.get("limit") or 5), 50))
    except (TypeError, ValueError):
        limit = 5

    json.dump({"results": search(query, limit)}, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
