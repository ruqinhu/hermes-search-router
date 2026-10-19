from __future__ import annotations

import time

import yaml

from hsr.backends.base import Hit, SearchOutcome, extract_entry
from hsr.provider import SearchRouterProvider


class FakeBackend:
    def __init__(
        self,
        name,
        *,
        search_fn=None,
        extract_fn=None,
        caps=frozenset({"search", "extract"}),
        available=True,
        timeout=30.0,
    ):
        self.name = name
        self.capabilities = caps
        self.timeout = timeout
        self._search_fn = search_fn
        self._extract_fn = extract_fn
        self._available = available
        self.search_calls = []
        self.extract_calls = []

    def available(self):
        return self._available

    def search(self, query, limit, timeout=None):
        self.search_calls.append((query, limit, timeout))
        if self._search_fn is not None:
            return self._search_fn(query, limit)
        return SearchOutcome.success(
            [Hit(title="T", url="https://example.com/1", description="S")]
        )

    def extract(self, urls, timeout=None, **kwargs):
        self.extract_calls.append({"urls": list(urls), "timeout": timeout})
        if self._extract_fn is not None:
            return self._extract_fn(urls)
        return [
            extract_entry(str(url), title="T", content="C", backend=self.name)
            for url in urls
        ]


def make_provider(
    tmp_path,
    fakes,
    *,
    active="auto",
    fallbacks=None,
    on_empty="fallback",
    now=None,
):
    names = list(fakes)
    config = {
        "version": 1,
        "active": active,
        "fallback": {
            "search": fallbacks or names,
            "extract": fallbacks or names,
        },
        "on_empty": on_empty,
        "backends": {
            name: {"type": "deepseek", "timeout": 30} for name in names
        },
    }
    path = tmp_path / "routes.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    clock = (lambda: now[0]) if now is not None else time.monotonic
    return SearchRouterProvider(
        path,
        backend_factory=lambda backend_config: fakes[backend_config.name],
        clock=clock,
        sleep=lambda _seconds: None,
    )


def test_auto_falls_back_on_error(tmp_path):
    a = FakeBackend(
        "a", search_fn=lambda q, n: SearchOutcome.failure("boom", cooldown=60)
    )
    b = FakeBackend("b")
    provider = make_provider(tmp_path, {"a": a, "b": b})
    result = provider.search("q", 5)
    assert result["success"] is True
    assert result["data"]["web"][0]["backend"] == "b"
    assert result["data"]["backend"] == "b"
    assert len(result["data"]["attempts"]) == 2
    assert result["data"]["attempts"][0]["backend"] == "a"
    assert result["data"]["attempts"][0]["ok"] is False
    assert result["data"]["attempts"][1]["backend"] == "b"
    assert result["data"]["attempts"][1]["ok"] is True
    assert len(a.search_calls) == 1
    assert len(b.search_calls) == 1


def test_active_pins_first(tmp_path):
    a = FakeBackend("a")
    b = FakeBackend("b")
    provider = make_provider(tmp_path, {"a": a, "b": b}, active="b")
    result = provider.search("q", 5)
    assert result["success"] is True
    assert result["data"]["web"][0]["backend"] == "b"
    assert a.search_calls == []


def test_active_falls_back_when_pinned_backend_fails(tmp_path):
    a = FakeBackend("a")
    b = FakeBackend(
        "b", search_fn=lambda q, n: SearchOutcome.failure("boom", cooldown=60)
    )
    provider = make_provider(tmp_path, {"a": a, "b": b}, active="b")
    result = provider.search("q", 5)
    assert result["success"] is True
    assert result["data"]["web"][0]["backend"] == "a"
    assert len(b.search_calls) == 1
    assert len(a.search_calls) == 1


def test_empty_results_fall_back(tmp_path):
    a = FakeBackend("a", search_fn=lambda q, n: SearchOutcome.success([]))
    b = FakeBackend("b")
    provider = make_provider(tmp_path, {"a": a, "b": b}, on_empty="fallback")
    result = provider.search("q", 5)
    assert result["success"] is True
    assert result["data"]["web"][0]["backend"] == "b"
    assert len(a.search_calls) == 1
    assert len(b.search_calls) == 1


def test_empty_results_can_be_accepted(tmp_path):
    a = FakeBackend("a", search_fn=lambda q, n: SearchOutcome.success([]))
    b = FakeBackend("b")
    provider = make_provider(tmp_path, {"a": a, "b": b}, on_empty="accept")
    result = provider.search("q", 5)
    assert result["success"] is True
    assert result["data"]["web"] == []
    assert b.search_calls == []


def test_all_empty_results_return_success_not_failure(tmp_path):
    a = FakeBackend("a", search_fn=lambda q, n: SearchOutcome.success([]))
    b = FakeBackend("b", search_fn=lambda q, n: SearchOutcome.success([]))
    provider = make_provider(tmp_path, {"a": a, "b": b}, on_empty="fallback")
    result = provider.search("q", 5)
    assert result["success"] is True
    assert result["data"]["web"] == []
    assert len(a.search_calls) == 1
    assert len(b.search_calls) == 1


def test_alias_backend_key_is_used_for_hits_and_cooldown(tmp_path):
    now = [0.0]
    alias = FakeBackend(
        "deepseek", search_fn=lambda q, n: SearchOutcome.failure("boom", cooldown=60)
    )
    provider = make_provider(
        tmp_path, {"ds_alias": alias}, active="ds_alias", fallbacks=["ds_alias"], now=now
    )
    first = provider.search("q", 5)
    assert first["success"] is False
    second = provider.search("q", 5)
    assert second["success"] is False
    # The failure was recorded under the config key, so the alias is cooling.
    assert len(alias.search_calls) == 1

    now[0] = 61.0
    provider.search("q", 5)
    assert len(alias.search_calls) == 2


def test_hit_backend_uses_config_key_and_rejects_spoofed_extras(tmp_path):
    def search_fn(query, limit):
        return SearchOutcome.success(
            [
                Hit(
                    "T",
                    "https://example.com/1",
                    "S",
                    extra={"backend": "spoofed", "evil": "x", "date": "2026-01-01"},
                )
            ]
        )

    alias = FakeBackend("deepseek", search_fn=search_fn)
    provider = make_provider(tmp_path, {"ds_alias": alias}, active="ds_alias")
    result = provider.search("q", 5)
    hit = result["data"]["web"][0]
    assert hit["backend"] == "ds_alias"
    assert "evil" not in hit
    assert hit["date"] == "2026-01-01"


def test_extract_passes_total_budget_not_backend_timeout(tmp_path):
    fake = FakeBackend("a", timeout=5.0)
    provider = make_provider(tmp_path, {"a": fake})
    provider.extract(["https://example.com/a"])
    assert len(fake.extract_calls) == 1
    assert 95.0 <= fake.extract_calls[0]["timeout"] <= 100.0


def test_mixed_failure_and_empty_is_failure(tmp_path):
    a = FakeBackend("a", search_fn=lambda q, n: SearchOutcome.failure("boom"))
    b = FakeBackend("b", search_fn=lambda q, n: SearchOutcome.success([]))
    provider = make_provider(tmp_path, {"a": a, "b": b}, on_empty="fallback")
    result = provider.search("q", 5)
    assert result["success"] is False
    assert "boom" in result["error"]
    assert "empty" in result["error"]


def test_search_limit_is_clamped(tmp_path):
    hits = [
        Hit(f"T{i}", f"https://example.com/{i}") for i in range(60)
    ]
    fake = FakeBackend(
        "a", search_fn=lambda q, n: SearchOutcome.success(hits)
    )
    provider = make_provider(tmp_path, {"a": fake})
    assert len(provider.search("q", 100)["data"]["web"]) == 50
    assert len(provider.search("q", 0)["data"]["web"]) == 1


def test_backend_construction_failure_is_an_envelope(tmp_path):
    path = tmp_path / "routes.yaml"
    path.write_text(
        "version: 1\nactive: auto\nbackends:\n  a: {type: deepseek}\n",
        encoding="utf-8",
    )

    def boom(_backend_config):
        raise RuntimeError("construction boom")

    provider = SearchRouterProvider(path, backend_factory=boom)
    result = provider.search("q", 5)
    assert result["success"] is False
    assert "backend construction failed" in result["error"]


def test_empty_extract_urls_return_empty_list(tmp_path):
    provider = make_provider(tmp_path, {"a": FakeBackend("a")})
    assert provider.extract([]) == []


def test_invalid_hit_urls_are_dropped(tmp_path):
    def search_fn(query, limit):
        return SearchOutcome.success(
            [
                Hit("JS", "javascript:alert(1)"),
                Hit("File", "file:///etc/passwd"),
                Hit("OK", "https://example.com/1"),
            ]
        )

    fake = FakeBackend("a", search_fn=search_fn)
    provider = make_provider(tmp_path, {"a": fake})
    result = provider.search("q", 5)
    assert len(result["data"]["web"]) == 1
    assert result["data"]["web"][0]["url"] == "https://example.com/1"


def test_extract_metadata_cannot_be_spoofed(tmp_path):
    def extract_fn(urls):
        return [
            {
                "url": urls[0],
                "title": "T",
                "content": "C",
                "raw_content": "C",
                "metadata": {
                    "backend": "spoofed",
                    "sourceURL": "https://wrong.example/",
                },
            }
        ]

    alias = FakeBackend("deepseek", extract_fn=extract_fn)
    provider = make_provider(tmp_path, {"ds_alias": alias}, active="ds_alias")
    entries = provider.extract(["https://example.com/a"])
    assert entries[0]["metadata"]["backend"] == "ds_alias"
    assert entries[0]["metadata"]["sourceURL"] == "https://example.com/a"


def test_all_backends_failed_returns_error(tmp_path):
    a = FakeBackend("a", search_fn=lambda q, n: SearchOutcome.failure("a boom"))
    b = FakeBackend("b", search_fn=lambda q, n: SearchOutcome.failure("b boom"))
    provider = make_provider(tmp_path, {"a": a, "b": b})
    result = provider.search("q", 5)
    assert result["success"] is False
    assert "a: a boom" in result["error"]
    assert "b: b boom" in result["error"]


def test_dedup_cap_and_position(tmp_path):
    def search_fn(query, limit):
        return SearchOutcome.success(
            [
                Hit("T1", "https://e/1", "one"),
                Hit("T1 dup", "https://e/1", "dup"),
                Hit("T2", "https://e/2", "two"),
                Hit("T3", "https://e/3", "three"),
            ]
        )

    a = FakeBackend("a", search_fn=search_fn)
    provider = make_provider(tmp_path, {"a": a})
    result = provider.search("q", 2)
    web = result["data"]["web"]
    assert len(web) == 2
    assert [hit["url"] for hit in web] == ["https://e/1", "https://e/2"]
    assert [hit["position"] for hit in web] == [1, 2]
    assert all(hit["backend"] == "a" for hit in web)


def test_cooldown_skips_failing_backend_until_expiry(tmp_path):
    now = [0.0]
    a = FakeBackend(
        "a", search_fn=lambda q, n: SearchOutcome.failure("boom", cooldown=60)
    )
    b = FakeBackend("b")
    provider = make_provider(tmp_path, {"a": a, "b": b}, now=now)

    assert provider.search("q", 5)["success"] is True
    assert len(a.search_calls) == 1

    assert provider.search("q", 5)["success"] is True
    assert len(a.search_calls) == 1  # cooling

    now[0] = 61.0
    assert provider.search("q", 5)["success"] is True
    assert len(a.search_calls) == 2


def test_no_cooldown_for_generic_validation_failure(tmp_path):
    now = [0.0]
    a = FakeBackend(
        "a", search_fn=lambda q, n: SearchOutcome.failure("query too long")
    )
    b = FakeBackend("b")
    provider = make_provider(tmp_path, {"a": a, "b": b}, now=now)

    assert provider.search("q", 5)["success"] is True
    assert provider.search("q", 5)["success"] is True
    # A failure without a cooldown is retried instead of being skipped.
    assert len(a.search_calls) == 2


def test_no_available_backend_returns_error(tmp_path):
    a = FakeBackend("a", available=False)
    b = FakeBackend("b", available=False)
    provider = make_provider(tmp_path, {"a": a, "b": b})
    result = provider.search("q", 5)
    assert result["success"] is False
    assert "no available search backend" in result["error"]


def test_config_error_is_returned_as_envelope(tmp_path):
    path = tmp_path / "routes.yaml"
    path.write_text("version: 1\nactive: nope\n", encoding="utf-8")
    provider = SearchRouterProvider(path)
    result = provider.search("q", 5)
    assert result["success"] is False
    assert "config error" in result["error"]


def test_extract_falls_back_when_all_entries_error(tmp_path):
    a = FakeBackend(
        "a",
        extract_fn=lambda urls: [
            extract_entry(str(url), error="blocked", backend="a") for url in urls
        ],
    )
    b = FakeBackend("b")
    provider = make_provider(tmp_path, {"a": a, "b": b})
    entries = provider.extract(["https://example.com/a"])
    assert entries[0]["content"] == "C"
    assert entries[0]["metadata"]["backend"] == "b"
    assert len(a.extract_calls) == 1
    assert len(b.extract_calls) == 1


def test_extract_partial_success_retries_failed_url(tmp_path):
    def extract_fn(urls):
        return [
            extract_entry(urls[0], title="T", content="C", backend="a"),
            extract_entry(urls[1], error="blocked", backend="a"),
        ]

    a = FakeBackend("a", extract_fn=extract_fn)
    b = FakeBackend("b")
    provider = make_provider(tmp_path, {"a": a, "b": b})
    entries = provider.extract(
        ["https://example.com/a", "https://example.com/b"]
    )
    assert entries[0]["content"] == "C"
    assert entries[0]["metadata"]["backend"] == "a"
    assert entries[1]["content"] == "C"
    assert entries[1]["metadata"]["backend"] == "b"
    assert b.extract_calls[0]["urls"] == ["https://example.com/b"]


def test_extract_fills_missing_urls(tmp_path):
    a = FakeBackend(
        "a",
        extract_fn=lambda urls: [
            extract_entry(urls[0], title="T", content="C", backend="a")
        ],
    )
    provider = make_provider(tmp_path, {"a": a})
    entries = provider.extract(
        ["https://example.com/a", "https://example.com/b"]
    )
    assert len(entries) == 2
    assert entries[1]["url"] == "https://example.com/b"
    assert "no entry returned" in entries[1]["error"]


def test_extract_without_capable_backend_returns_errors(tmp_path):
    a = FakeBackend("a", caps=frozenset({"search"}))
    provider = make_provider(tmp_path, {"a": a})
    entries = provider.extract(["https://example.com/a"])
    assert len(entries) == 1
    assert "no available extract backend" in entries[0]["error"]


def test_contract_shapes(tmp_path):
    provider = make_provider(tmp_path, {"a": FakeBackend("a")})
    result = provider.search("q", 5)
    assert result["success"] is True
    assert isinstance(result["data"]["web"], list)
    for hit in result["data"]["web"]:
        for key in ("title", "url", "description", "position", "backend"):
            assert key in hit

    entries = provider.extract(["https://example.com/a"])
    assert isinstance(entries, list)
    for entry in entries:
        for key in ("url", "title", "content", "raw_content"):
            assert key in entry


def test_identity_and_capabilities(tmp_path):
    provider = make_provider(tmp_path, {"a": FakeBackend("a")})
    assert provider.name == "search-router"
    assert provider.display_name == "Search Router"
    assert provider.supports_search() is True
    assert provider.supports_extract() is True
    assert provider.is_keyless_available() is False


def test_is_available_reflects_backends(tmp_path):
    available = FakeBackend("a", available=True)
    provider = make_provider(tmp_path, {"a": available})
    assert provider.is_available() is True

    unavailable = FakeBackend("a", available=False)
    provider = make_provider(tmp_path, {"a": unavailable})
    assert provider.is_available() is False


def test_register_registers_provider():
    from hsr import register

    class Context:
        def __init__(self):
            self.providers = []

        def register_web_search_provider(self, provider):
            self.providers.append(provider)

    context = Context()
    register(context)
    assert len(context.providers) == 1
    assert context.providers[0].name == "search-router"
