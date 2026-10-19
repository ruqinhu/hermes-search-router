from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from hsr.batch import BatchConfig, PROFILES, resolve_profile, run_batch

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)


class FakeProvider:
    def __init__(self, responses, *, extract_content=None):
        self.responses = responses
        self.extract_content = extract_content
        self.calls = []
        self.extract_calls = []
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0

    def search(self, query, limit):
        with self._lock:
            self.calls.append((query, limit))
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            time.sleep(0.02)
            response = self.responses[query]
            if isinstance(response, list):
                return response.pop(0)
            return response
        finally:
            with self._lock:
                self._active -= 1

    def extract(self, urls):
        self.extract_calls.append(list(urls))
        return [
            {
                "url": url,
                "title": "T",
                "content": self.extract_content or "content",
                "raw_content": self.extract_content or "content",
            }
            for url in urls
        ]


def hit(title, url, *, date=None):
    value = {
        "title": title,
        "url": url,
        "description": "desc",
        "backend": "deepseek",
    }
    if date is not None:
        value["date"] = date
    return value


def ok(*hits):
    return {"success": True, "data": {"web": list(hits)}}


def failed(message="boom"):
    return {"success": False, "error": message}


def config(**kwargs):
    defaults = {
        "workers": 3,
        "count": 3,
        "retries": 1,
        "max_age_days": 3.0,
        "sleep": lambda _seconds: None,
        "now": lambda: NOW,
    }
    defaults.update(kwargs)
    return BatchConfig(**defaults)


def test_concurrent_results_and_order():
    queries = ["q1", "q2", "q3", "q4", "q5"]
    fake = FakeProvider(
        {query: ok(hit(query, f"https://example.com/{query}")) for query in queries}
    )
    result = run_batch(fake, queries, config(workers=3))
    assert result["ok"] is True
    assert list(result["results"]) == queries
    assert result["summary"]["succeeded"] == 5
    assert result["summary"]["workers"] == 3
    assert fake.max_active <= 3
    assert result["summary"]["spread_ms"] >= 0


def test_spread_measures_first_wave_only():
    queries = [f"q{i}" for i in range(10)]
    fake = FakeProvider(
        {query: ok(hit(query, f"https://example.com/{query}")) for query in queries}
    )
    result = run_batch(fake, queries, config(workers=3))
    assert result["summary"]["succeeded"] == 10
    assert result["summary"]["spread_ms"] < 100.0


def test_retry_then_success():
    fake = FakeProvider({"q": [failed("first"), ok(hit("T", "https://e/1"))]})
    result = run_batch(fake, ["q"], config(retries=1))
    item = result["results"]["q"]
    assert item["ok"] is True
    assert item["tries"] == 2
    assert len(fake.calls) == 2


def test_failure_isolation():
    fake = FakeProvider(
        {
            "bad": [failed("always"), failed("always")],
            "good": ok(hit("T", "https://e/1")),
        }
    )
    result = run_batch(fake, ["bad", "good"], config(retries=1))
    assert result["ok"] is True
    assert result["summary"]["succeeded"] == 1
    assert result["summary"]["failed"] == 1
    assert result["results"]["bad"]["ok"] is False
    assert "always" in result["results"]["bad"]["error"]


def test_freshness_and_unknown_date():
    fresh = (NOW - timedelta(days=1)).isoformat()
    old = (NOW - timedelta(days=10)).isoformat()
    fake = FakeProvider(
        {
            "q": ok(
                hit("fresh", "https://e/1", date=fresh),
                hit("old", "https://e/2", date=old),
                hit("unknown", "https://e/3"),
            )
        }
    )
    result = run_batch(fake, ["q"], config(max_age_days=3))
    hits = {item["title"]: item for item in result["results"]["q"]["hits"]}
    assert hits["fresh"]["stale"] is False
    assert hits["old"]["stale"] is True
    assert hits["unknown"]["date_unknown"] is True
    assert result["summary"]["stale"] == 1
    assert result["summary"]["date_unknown"] == 1


def test_duplicate_queries_are_removed():
    fake = FakeProvider({"q": ok(hit("T", "https://e/1"))})
    result = run_batch(fake, ["q", "q", " q "], config())
    assert result["summary"]["queries"] == 1
    assert result["summary"]["duplicate_queries_removed"] == 2
    assert list(result["results"]) == ["q"]


def test_cross_query_url_dedupe():
    fake = FakeProvider(
        {
            "q1": ok(hit("A", "https://e/same")),
            "q2": ok(
                hit("A dup", "https://e/same"),
                hit("B", "https://e/other"),
            ),
        }
    )
    result = run_batch(fake, ["q1", "q2"], config(dedupe_urls=True))
    assert len(result["results"]["q1"]["hits"]) == 1
    assert len(result["results"]["q2"]["hits"]) == 1
    assert result["results"]["q2"]["hits"][0]["title"] == "B"
    assert result["summary"]["duplicates_removed"] == 1


def test_extract_top_and_truncation():
    fake = FakeProvider(
        {"q": ok(hit("T", "https://e/1"), hit("T2", "https://e/2"))},
        extract_content="x" * 5000,
    )
    result = run_batch(
        fake,
        ["q"],
        config(extract_top=1, extract_max_chars=100),
    )
    first = result["results"]["q"]["hits"][0]
    assert first["content"] == "x" * 100
    assert first["content_chars"] == 5000
    assert first["content_truncated"] is True
    assert fake.extract_calls == [["https://e/1"]]
    assert result["summary"]["extract_queries"] == 1


def test_workers_are_clamped():
    fake = FakeProvider({"q": ok(hit("T", "https://e/1"))})
    result = run_batch(fake, ["q"], config(workers=50))
    assert result["summary"]["workers"] == 12


def test_no_queries():
    result = run_batch(FakeProvider({}), ["", "  "], config())
    assert result["ok"] is False
    assert result["error"] == "no queries"


def test_profiles():
    assert resolve_profile("news")["count"] == 8
    assert resolve_profile("news")["max_age_days"] == 3.0
    assert resolve_profile("research")["extract_top"] == 2
    assert set(PROFILES) == {"general", "news", "research"}
