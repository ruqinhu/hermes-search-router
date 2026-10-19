from __future__ import annotations

import sys
from pathlib import Path

import yaml

from hsr.provider import SearchRouterProvider

ROOT = Path(__file__).resolve().parents[1]


def test_router_with_real_script_backend(tmp_path):
    config = {
        "version": 1,
        "active": "script:example",
        "fallback": {"search": ["example"], "extract": ["example"]},
        "backends": {
            "example": {
                "type": "script",
                "mode": "canonical",
                "command": [
                    sys.executable,
                    str(ROOT / "examples" / "canonical_search.py"),
                ],
                "timeout": 10,
            }
        },
    }
    path = tmp_path / "routes.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")

    provider = SearchRouterProvider(path)
    result = provider.search("hello", 2)
    assert result["success"] is True
    assert len(result["data"]["web"]) == 2
    assert result["data"]["web"][0]["backend"] == "example"
    assert "hello" in result["data"]["web"][0]["title"]

    # The script is search-only, so the router reports no extract backend.
    entries = provider.extract(["https://example.com/"])
    assert len(entries) == 1
    assert "no available extract backend" in entries[0]["error"]
