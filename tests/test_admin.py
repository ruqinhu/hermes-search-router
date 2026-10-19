from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from hsr.admin import (
    add_subparsers,
    bench_payload,
    register_cli,
    status_payload,
    test_payload as run_test_payload,
)
from hsr.backends.base import Hit, SearchOutcome
from hsr.config import ConfigStore, load_config

ROOT = Path(__file__).resolve().parents[1]


class FakeBackend:
    capabilities = frozenset({"search", "extract"})

    def __init__(self, name="a", *, available=True, outcome=None):
        self.name = name
        self.timeout = 30.0
        self._available = available
        self._outcome = outcome or SearchOutcome.success(
            [Hit("T", "https://e/1", "S")]
        )
        self.calls = []

    def available(self):
        return self._available

    def search(self, query, limit, timeout=None):
        self.calls.append((query, limit, timeout))
        return self._outcome


class FakeProvider:
    def __init__(self, *, search_result=None, extract_result=None):
        self.search_result = search_result
        self.extract_result = extract_result
        self.search_calls = []
        self.extract_calls = []

    def search(self, query, limit):
        self.search_calls.append((query, limit))
        if isinstance(self.search_result, Exception):
            raise self.search_result
        return self.search_result

    def extract(self, urls):
        self.extract_calls.append(list(urls))
        if isinstance(self.extract_result, Exception):
            raise self.extract_result
        return self.extract_result or []


def test_status_payload_chain_and_rows(tmp_path):
    path = tmp_path / "routes.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "active": "auto",
                "fallback": {"search": ["a", "b"], "extract": ["b"]},
                "backends": {
                    "a": {"type": "deepseek"},
                    "b": {"type": "anysearch"},
                },
            }
        ),
        encoding="utf-8",
    )
    store = ConfigStore(path)
    config = store.get()
    payload = status_payload(
        store,
        config,
        {"a": FakeBackend("a"), "b": FakeBackend("b", available=False)},
    )
    assert payload["active"] == "auto"
    assert [row["name"] for row in payload["chain"]] == ["a", "b"]
    assert payload["chain"][1]["available"] is False
    assert payload["backends"][0]["capabilities"] == ["extract", "search"]
    assert payload["last_error"] is None


def test_test_payload_search_and_extract():
    provider = FakeProvider(
        search_result={
            "success": True,
            "data": {
                "web": [
                    {
                        "title": "T",
                        "url": "https://e/1",
                        "backend": "deepseek",
                    }
                ],
                "backend": "deepseek",
                "attempts": [
                    {"backend": "a", "ok": False, "error": "boom"},
                    {"backend": "deepseek", "ok": True, "hits": 1},
                ],
            },
        },
        extract_result=[
            {
                "url": "https://e/1",
                "title": "T",
                "content": "x" * 600,
            }
        ],
    )
    payload = run_test_payload(
        provider, "q", limit=3, extract_url="https://e/1"
    )
    assert payload["success"] is True
    assert payload["backend"] == "deepseek"
    assert len(payload["attempts"]) == 2
    assert payload["extract"]["content_chars"] == 600
    assert len(payload["extract"]["content_head"]) == 500
    assert provider.search_calls == [("q", 3)]


def test_test_payload_failure_is_reported():
    payload = run_test_payload(FakeProvider(search_result=RuntimeError("boom")), "q")
    assert payload["success"] is False
    assert "boom" in payload["error"]


def test_bench_payload_marks_ok_and_failure(tmp_path):
    path = tmp_path / "routes.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "backends": {
                    "a": {"type": "deepseek"},
                    "b": {"type": "anysearch"},
                },
            }
        ),
        encoding="utf-8",
    )
    config = load_config(path)
    ok = FakeBackend("a")
    bad = FakeBackend(
        "b", outcome=SearchOutcome.failure("boom")
    )
    payload = bench_payload(config, {"a": ok, "b": bad}, "q", limit=2)
    assert payload["ok"] is True
    assert [row["backend"] for row in payload["results"]] == ["a", "b"]
    assert payload["results"][0]["ok"] is True
    assert payload["results"][1]["ok"] is False


def test_subparsers():
    parser = argparse.ArgumentParser()
    add_subparsers(parser)
    args = parser.parse_args(["status", "--json"])
    assert args.search_router_action == "status"
    assert args.json is True
    args = parser.parse_args(["test", "q", "--limit", "3"])
    assert args.search_router_action == "test"
    assert args.query == "q"
    assert args.limit == 3


def test_register_cli_uses_context():
    class Context:
        def __init__(self):
            self.calls = []

        def register_cli_command(self, *args, **kwargs):
            self.calls.append((args, kwargs))

    context = Context()
    register_cli(context)
    assert context.calls
    assert context.calls[0][0][0] == "search-router"


def test_main_status_offline(tmp_path, capsys):
    path = tmp_path / "routes.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "active": "auto",
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
        ),
        encoding="utf-8",
    )
    from hsr.admin import main

    code = main(["status", "--config", str(path), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["active"] == "auto"
    assert payload["chain"][0]["name"] == "example"
    assert payload["chain"][0]["available"] is True
