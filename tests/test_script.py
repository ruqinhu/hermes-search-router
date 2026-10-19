from __future__ import annotations

import sys
import textwrap
import time
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
        cwd=kwargs.pop("cwd", None),
        max_output_bytes=kwargs.pop("max_output_bytes", 1_000_000),
        env_passthrough=kwargs.pop("env_passthrough", ()),
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


def test_query_containing_limit_placeholder_is_not_rewritten(tmp_path):
    out = tmp_path / "arg.txt"
    script = write_script(
        tmp_path,
        "argv_capture2.py",
        """
        import pathlib
        import sys
        pathlib.Path(sys.argv[2]).write_text(sys.argv[1], encoding="utf-8")
        print('{"results": []}')
        """,
    )
    config = make_config(
        script,
        mode="argv",
        command=(sys.executable, str(script), "{query}", str(out)),
    )
    query = "x {limit} y"
    outcome = ScriptBackend(config).search(query, 5)
    assert outcome.ok
    assert out.read_text(encoding="utf-8") == query


def test_timeout_kills_grandchild_and_returns(tmp_path):
    marker = tmp_path / "grandchild_alive.txt"
    script = write_script(
        tmp_path,
        "tree.py",
        """
        import subprocess
        import sys
        import time
        marker = sys.argv[1]
        code = (
            "import pathlib, time; "
            "time.sleep(2); "
            "pathlib.Path(" + repr(marker) + ").write_text('alive')"
        )
        subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(5)
        """,
    )
    started = time.monotonic()
    outcome = ScriptBackend(
        make_config(script, timeout=0.3, command=(sys.executable, str(script), str(marker)))
    ).search("q", 5)
    elapsed = time.monotonic() - started
    assert not outcome.ok
    assert "timed out" in outcome.error
    assert elapsed < 4.0
    time.sleep(2.5)
    assert not marker.exists(), "grandchild survived the timeout kill"


def test_cwd_relative_script_is_checked_for_availability(tmp_path):
    script = write_script(tmp_path, "cwd_ok.py", "print('{}')")
    config = make_config(
        script,
        command=(sys.executable, "cwd_ok.py"),
        cwd=str(tmp_path),
    )
    assert ScriptBackend(config).available() is True

    missing = make_config(
        script,
        command=(sys.executable, "missing.py"),
        cwd=str(tmp_path),
    )
    assert ScriptBackend(missing).available() is False


def test_query_and_limit_placeholders_are_single_pass(tmp_path):
    out = tmp_path / "both.txt"
    script = write_script(
        tmp_path,
        "argv_both.py",
        """
        import pathlib
        import sys
        pathlib.Path(sys.argv[3]).write_text(
            sys.argv[1] + "|" + sys.argv[2], encoding="utf-8"
        )
        print('{"results": []}')
        """,
    )
    config = make_config(
        script,
        mode="argv",
        command=(sys.executable, str(script), "{query}", "{limit}", str(out)),
    )
    query = "{query} and {limit}"
    outcome = ScriptBackend(config).search(query, 7)
    assert outcome.ok
    assert out.read_text(encoding="utf-8") == "{query} and {limit}|7"


def test_bom_prefixed_json_is_accepted(tmp_path):
    script = write_script(
        tmp_path,
        "bom.py",
        """
        import sys
        sys.stdout.buffer.write(
            b"\\xef\\xbb\\xbf" + b'{"results": [{"title": "T", "url": "https://e/1"}]}'
        )
        """,
    )
    outcome = ScriptBackend(make_config(script)).search("q", 5)
    assert outcome.ok
    assert len(outcome.hits) == 1


def test_stderr_is_capped(tmp_path):
    script = write_script(
        tmp_path,
        "stderr_huge.py",
        """
        import sys
        sys.stderr.write("e" * 5000)
        sys.exit(1)
        """,
    )
    outcome = ScriptBackend(
        make_config(script, max_output_bytes=100)
    ).search("q", 5)
    assert not outcome.ok
    assert "too large" in outcome.error


def test_scripts_do_not_inherit_secrets_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_TEST_SECRET", "s3cret")
    out = tmp_path / "env.txt"
    script = write_script(
        tmp_path,
        "env_capture.py",
        """
        import json
        import os
        import pathlib
        import sys
        pathlib.Path(sys.argv[1]).write_text(
            os.environ.get("HERMES_TEST_SECRET", "<missing>"), encoding="utf-8"
        )
        print(json.dumps({"results": []}))
        """,
    )
    config = make_config(script, command=(sys.executable, str(script), str(out)))
    assert ScriptBackend(config).search("q", 5).ok
    assert out.read_text(encoding="utf-8") == "<missing>"

    passthrough = make_config(
        script,
        command=(sys.executable, str(script), str(out)),
        env_passthrough=("HERMES_TEST_SECRET",),
    )
    assert ScriptBackend(passthrough).search("q", 5).ok
    assert out.read_text(encoding="utf-8") == "s3cret"


def test_query_with_control_char_is_rejected(tmp_path):
    script = write_script(tmp_path, "never.py", "print('{}')")
    outcome = ScriptBackend(make_config(script)).search("a\x00b", 5)
    assert not outcome.ok
    assert "control characters" in outcome.error


def test_child_exits_with_pipe_holding_grandchild_does_not_hang(tmp_path):
    # Regression: close() on a BufferedReader whose drain thread is blocked in
    # read() can wait forever. The parent exits immediately, the grandchild
    # inherits the pipe and sleeps.
    script = write_script(
        tmp_path,
        "pipe_holder.py",
        """
        import subprocess
        import sys
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        """,
    )
    started = time.monotonic()
    outcome = ScriptBackend(make_config(script, timeout=0.5)).search("q", 5)
    elapsed = time.monotonic() - started
    assert not outcome.ok
    assert elapsed < 3.0


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
