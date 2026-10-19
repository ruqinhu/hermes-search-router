#!/usr/bin/env python3
"""Standalone admin CLI for the search router.

Equivalent to ``hermes search-router status|describe|test|bench`` when the
Hermes CLI command is unavailable.

Examples:
    python3 router_admin.py status --json
    python3 router_admin.py describe
    python3 router_admin.py test "澜起科技 公告" --limit 5
    python3 router_admin.py bench "存储 涨价" --limit 3
"""

from __future__ import annotations

import importlib.util
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

from hsr.admin import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
