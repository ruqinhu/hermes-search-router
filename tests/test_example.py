from __future__ import annotations

import sys
from pathlib import Path

from hsr.backends.script import ScriptBackend
from hsr.config import BackendConfig

ROOT = Path(__file__).resolve().parents[1]


def test_example_canonical_script_runs():
    script = ROOT / "examples" / "canonical_search.py"
    config = BackendConfig(
        name="example",
        type="script",
        mode="canonical",
        command=(sys.executable, str(script)),
        timeout=10,
    )
    outcome = ScriptBackend(config).search("hello", 2)
    assert outcome.ok
    assert len(outcome.hits) == 2
    assert "hello" in outcome.hits[0].title
    assert outcome.hits[0].url.startswith("https://example.com/")
