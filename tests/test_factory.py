from __future__ import annotations

from hsr.backends.anysearch import DEFAULT_BASE_URL as ANYSEARCH_DEFAULT
from hsr.backends.deepseek import DEFAULT_BASE_URL as DEEPSEEK_DEFAULT
from hsr.factory import anysearch_base_url, deepseek_base_url


def test_anysearch_official_and_loopback_are_allowed(monkeypatch):
    monkeypatch.setenv("ANYSEARCH_URL", "https://api.anysearch.com/")
    assert anysearch_base_url() == ANYSEARCH_DEFAULT

    monkeypatch.setenv("ANYSEARCH_URL", "http://127.0.0.1:8765")
    assert anysearch_base_url() == ANYSEARCH_DEFAULT
    monkeypatch.setenv("SEARCH_ROUTER_ALLOW_LOOPBACK", "1")
    assert anysearch_base_url() == "http://127.0.0.1:8765"


def test_loopback_requires_explicit_gate(monkeypatch):
    monkeypatch.delenv("SEARCH_ROUTER_ALLOW_LOOPBACK", raising=False)
    monkeypatch.delenv("HERMES_DEV", raising=False)
    monkeypatch.setenv("ANYSEARCH_URL", "http://127.0.0.1:8765")
    assert anysearch_base_url() == ANYSEARCH_DEFAULT


def test_anysearch_unsafe_override_is_rejected(monkeypatch):
    monkeypatch.setenv("ANYSEARCH_URL", "http://evil.example")
    assert anysearch_base_url() == ANYSEARCH_DEFAULT

    monkeypatch.setenv("ANYSEARCH_URL", "https://api.anysearch.com.evil.example")
    assert anysearch_base_url() == ANYSEARCH_DEFAULT


def test_deepseek_official_and_loopback_are_allowed(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/")
    assert deepseek_base_url() == DEEPSEEK_DEFAULT

    monkeypatch.setenv("DEEPSEEK_BASE_URL", "http://localhost:8765")
    assert deepseek_base_url() == DEEPSEEK_DEFAULT
    monkeypatch.setenv("SEARCH_ROUTER_ALLOW_LOOPBACK", "1")
    assert deepseek_base_url() == "http://localhost:8765"


def test_deepseek_unsafe_override_is_rejected(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "http://api.deepseek.com")
    assert deepseek_base_url() == DEEPSEEK_DEFAULT

    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com:8443")
    assert deepseek_base_url() == DEEPSEEK_DEFAULT

    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://user:pass@api.deepseek.com")
    assert deepseek_base_url() == DEEPSEEK_DEFAULT

    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    assert deepseek_base_url() == DEEPSEEK_DEFAULT
