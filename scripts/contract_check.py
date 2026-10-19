#!/usr/bin/env python3
"""Verify the plugin against the real Hermes WebSearchProvider ABC.

Run this on the Hermes host after installing the plugin:

    python3 scripts/contract_check.py

It checks inheritance, abstract methods, signatures, capabilities, and the
setup schema. It performs no network calls.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    try:
        from agent.web_search_provider import WebSearchProvider
    except Exception as exc:  # noqa: BLE001
        print(f"cannot import Hermes WebSearchProvider: {exc}", file=sys.stderr)
        return 2

    spec = importlib.util.spec_from_file_location(
        "hsr",
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["hsr"] = module
    spec.loader.exec_module(module)

    from hsr.provider import SearchRouterProvider

    provider = SearchRouterProvider(config_path=str(ROOT / "missing-routes.yaml"))
    checks = {
        "isinstance": isinstance(provider, WebSearchProvider),
        "abstract_methods": sorted(
            getattr(provider.__class__, "__abstractmethods__", frozenset())
        ),
        "name": provider.name,
        "display_name": provider.display_name,
        "supports_search": provider.supports_search(),
        "supports_extract": provider.supports_extract(),
        "is_available": provider.is_available(),
        "search_signature": str(inspect.signature(provider.search)),
        "extract_signature": str(inspect.signature(provider.extract)),
        "setup_schema_keys": sorted(provider.get_setup_schema()),
    }
    failures = []
    if not checks["isinstance"]:
        failures.append("provider is not a WebSearchProvider")
    if checks["abstract_methods"]:
        failures.append(f"unimplemented abstract methods: {checks['abstract_methods']}")
    if checks["name"] != "search-router":
        failures.append("provider name is not search-router")
    if not checks["supports_search"]:
        failures.append("supports_search() is false")
    if not checks["supports_extract"]:
        failures.append("supports_extract() is false with DS + AnySearch defaults")
    if not checks["setup_schema_keys"]:
        failures.append("get_setup_schema() is empty")

    payload = {"ok": not failures, "checks": checks, "failures": failures}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
