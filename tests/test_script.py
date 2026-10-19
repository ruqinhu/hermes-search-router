from __future__ import annotations

import sys
import textwrap
from pathlib import Path

from hsr.backends.script import ScriptBackend
from hsr.config import BackendConfig


def write_script(tmp_path: Path, name: str, code: str) -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(code), encoding="utf-8")
    return path


def make_config(script: Path, *, mode="canonical", command=None, **kwargs) -> BackendConfig:
    if command is None:
        command = (sys.executable, str(script))
    return BackendConfig(
        name="local",
        type="script",
        mode=mode,
        command=tuple(command),
        timeout=kwargs.pop("timeout", 5.0),
        results_path=kwargs.pop("results_path", "results"),
        fields=kwargs.pop("fields", {}),
        max_output_bytes=kwargs.pop("max_output_bytes", 1_000_000),
    )


def test_canonical_success(tmp_path):
    script = write_script(
        tmp_path,
        "canonical_ok.py",
        """
        import json
        import sys
        request = json.load(sys.stdin)
        print(json.dumps({
            "results": [{
                "title": "T",
                "url": "https://example.com/1",
                "description": request["query"],
            }]
        }))
        """,
    )
    outcome = ScriptBackend(make_config(script)).search("hello", 5)
    assert outcome.ok
    assert outcome.hits[0].title == "T"
    assert outcome.hits[0].description == "hello"


def test_canonical_accepts_bare_list(tmp_path):
    script = write_script(
        tmp_path,
        "canonical_list.py",
        """
        import json
        print(json.dumps([{"title": "T", "url": "https://example.com/1"}]))
        """,
    )
    outcome = ScriptBackend(make_config(script)).search("q", 5)
    assert outcome.ok
    assert len(outcome.hits) == 1


def test_nonzero_exit_is_failure(tmp_path):
    script = write_script(
        tmp_path,
        "fail.py",
        """
        import sys
        print("boom", file=sys.stderr)
        sys.exit(2)
        """,
    )
    outcome = ScriptBackend(make_config(script)).search("q", 5)
    assert not outcome.ok
    assert "exit 2" in outcome.error
    assert "boom" in outcome.error


def test_invalid_json_is_failure(tmp_path):
    script = write_script(
        tmp_path,
        "bad_json.py",
        """
        print("not json")
        """,
    )
    outcome = ScriptBackend(make_config(script)).search("q", 5)
    assert not outcome.ok
    assert "not JSON" in outcome.error


def test_timeout_is_failure(tmp_path):
    script = write_script(
        tmp_path,
        "slow.py",
        """
        import time
        time.sleep(2)
        """,
    )
    outcome = ScriptBackend(make_config(script, timeout=0.2)).search("q", 5)
    assert not outcome.ok
    assert "timed out" in outcome.error


def test_argv_mode_mapping_and_placeholders(tmp_path):
    script = write_script(
        tmp_path,
        "argv_ok.py",
        """
        import json
        import sys
        query = sys.argv[1]
        count = sys.argv[3]
        print(json.dumps({"data": {"results": [{
            "t": "T", "u": "https://example.com/1", "s": query + "|" + count,
        }]}}))
        """,
    )
    config = make_config(
        script,
        mode="argv",
        command=(sys.executable, str(script), "{query}", "--count", "{limit}"),
        results_path="data.results",
        fields={"title": "t", "url": "u", "description": "s"},
    )
    outcome = ScriptBackend(config).search("hi", 3)
    assert outcome.ok
    assert outcome.hits[0].description == "hi|3"


def test_query_is_not_shell_interpreted(tmp_path):
    out = tmp_path / "arg.txt"
    script = write_script(
        tmp_path,
        "argv_capture.py",
        """
        import json
        import pathlib
        import sys
        pathlib.Path(sys.argv[2]).write_text(sys.argv[1], encoding="utf-8")
        print(json.dumps({"results": []}))
        """,
    )
    config = make_config(
        script,
        mode="argv",
        command=(sys.executable, str(script), "{query}", str(out)),
    )
    query = '; echo PWNED; $(echo PWNED)'
    outcome = ScriptBackend(config).search(query, 5)
    assert outcome.ok
    assert outcome.error is None
    assert out.read_text(encoding="utf-8") == query


def test_output_cap(tmp_path):
    script = write_script(
        tmp_path,
        "huge.py",
        """
        print("x" * 5000)
        """,
    )
    outcome = ScriptBackend(
        make_config(script, max_output_bytes=100)
    ).search("q", 5)
    assert not outcome.ok
    assert "too large" in outcome.error


def test_available_checks_executable(tmp_path):
    assert ScriptBackend(
        make_config(tmp_path / "missing.py")
    ).available() is False
    script = write_script(tmp_path, "ok.py", "print('{}')")
    assert ScriptBackend(make_config(script)).available() is True


def test_script_is_search_only(tmp_path):
    script = write_script(tmp_path, "ok.py", "print('{}')")
    backend = ScriptBackend(make_config(script))
    assert backend.capabilities == frozenset({"search"})
    entries = backend.extract(["https://example.com/a"])
    assert len(entries) == 1
    assert "search only" in entries[0]["error"]


def test_duplicate_and_empty_urls_are_dropped(tmp_path):
    script = write_script(
        tmp_path,
        "dupes.py",
        """
        import json
        print(json.dumps({"results": [
            {"title": "A", "url": "https://example.com/1"},
            {"title": "B", "url": "https://example.com/1"},
            {"title": "C", "url": ""},
        ]}))
        """,
    )
    outcome = ScriptBackend(make_config(script)).search("q", 5)
    assert outcome.ok
    assert len(outcome.hits) == 1
    assert outcome.hits[0].title == "A"
