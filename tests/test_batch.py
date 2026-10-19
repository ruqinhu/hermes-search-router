from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from hsr.batch import (
    BatchConfig,
    PROFILES,
    build_query_suffix,
    date_from_url,
    normalize_url,
    resolve_profile,
    run_batch,
)

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


def test_normalize_url_for_dedupe():
    assert (
        normalize_url("https://Example.com:443/a/?utm_source=x&b=2&a=1#frag")
        == "https://example.com/a?a=1&b=2"
    )
    assert (
        normalize_url("http://example.com:80/path/?spm=1&scm=2")
        == "https://example.com/path"
    )
    assert normalize_url("https://www.example.com/a") == "https://example.com/a"
    assert normalize_url("https://example.com/") == "https://example.com/"
    assert normalize_url("not a url") == "not a url"
    assert (
        normalize_url("https://user:pass@example.com:8443/a/")
        == "https://example.com:8443/a"
    )
    assert normalize_url("http://[::1]:8080/a/") == "https://[::1]:8080/a"
    for value in (
        "https://Example.com:443/a/?utm_source=x&b=2&a=1#frag",
        "http://[::1]:8080/a/",
        "not a url",
    ):
        assert normalize_url(normalize_url(value)) == normalize_url(value)


def test_http_https_and_www_variants_dedupe():
    fake = FakeProvider(
        {
            "q1": ok(hit("A", "http://www.example.com/a/")),
            "q2": ok(hit("A dup", "https://example.com/a")),
        }
    )
    result = run_batch(fake, ["q1", "q2"], config(dedupe_urls=True))
    assert result["summary"]["duplicates_removed"] == 1
    assert result["results"]["q2"]["hits"] == []


def test_date_field_wins_over_url_date():
    fake = FakeProvider(
        {
            "q": ok(
                hit(
                    "field",
                    "https://example.com/2020-01-01/story.html",
                    date="2026-10-18T00:00:00+00:00",
                )
            )
        }
    )
    result = run_batch(fake, ["q"], config(max_age_days=3))
    item = result["results"]["q"]["hits"][0]
    assert item["date_source"] == "field"
    assert item["date"] == "2026-10-18T00:00:00+00:00"
    assert item["stale"] is False


def test_invalid_field_date_falls_back_to_url():
    fake = FakeProvider(
        {
            "q": ok(
                hit(
                    "x",
                    "https://example.com/2026-10-18/story.html",
                    date="3小时前",
                )
            )
        }
    )
    result = run_batch(fake, ["q"], config(max_age_days=3))
    item = result["results"]["q"]["hits"][0]
    assert item["date_source"] == "url"
    assert item["date"].startswith("2026-10-18")
    assert item["date_unknown"] is False


def test_sort_date_uses_url_date():
    fake = FakeProvider(
        {
            "q": ok(
                hit("old", "https://example.com/2026-10-10/story"),
                hit("new", "https://example.com/2026-10-18/story"),
            )
        }
    )
    result = run_batch(fake, ["q"], config(sort="date"))
    assert [item["title"] for item in result["results"]["q"]["hits"]] == [
        "new",
        "old",
    ]


def test_invalid_field_date_is_kept_and_marked_unknown():
    fake = FakeProvider(
        {"q": ok(hit("bad", "https://e/1", date="3小时前"))}
    )
    result = run_batch(fake, ["q"], config())
    item = result["results"]["q"]["hits"][0]
    assert item["date"] == "3小时前"
    assert item["date_unknown"] is True
    assert result["summary"]["date_unknown"] == 1


def test_require_authority_without_min():
    fake = FakeProvider(
        {
            "q": ok(
                hit("known", "https://e/1") | {"authority": 0},
                hit("missing", "https://e/2"),
            )
        }
    )
    result = run_batch(fake, ["q"], config(require_authority=True))
    titles = [item["title"] for item in result["results"]["q"]["hits"]]
    assert titles == ["known"]
    assert result["summary"]["filtered_authority"] == 1


def test_date_from_url_patterns():
    assert date_from_url("https://example.com/2026-10-18/story.html").date().isoformat() == "2026-10-18"
    assert date_from_url("https://paper.cnstock.com/html/2026-10/17/content_1.htm").date().isoformat() == "2026-10-17"
    assert date_from_url("https://example.com/2026/10/16/story").date().isoformat() == "2026-10-16"
    assert date_from_url("https://finance.eastmoney.com/a/202610173850395010.html").date().isoformat() == "2026-10-17"
    assert date_from_url("https://example.com/p20261016000181.html").date().isoformat() == "2026-10-16"
    assert date_from_url("https://example.com/20261015.html").date().isoformat() == "2026-10-15"
    assert date_from_url("https://example.com/20261015/story").date().isoformat() == "2026-10-15"
    assert date_from_url("https://example.com/no-date") is None
    assert date_from_url("https://example.com/2026-13-40/story") is None
    assert date_from_url("https://example.com/20260230/story") is None


def test_url_date_fallback_annotates_stale():
    fake = FakeProvider(
        {
            "q": ok(
                hit("fresh", "https://example.com/2026-10-18/story.html"),
                hit("old", "https://example.com/2020-01-01/story.html"),
            )
        }
    )
    result = run_batch(fake, ["q"], config(max_age_days=3))
    hits = {item["title"]: item for item in result["results"]["q"]["hits"]}
    assert hits["fresh"]["date_source"] == "url"
    assert hits["fresh"]["date"].startswith("2026-10-18")
    assert hits["fresh"]["date_unknown"] is False
    assert hits["fresh"]["stale"] is False
    assert hits["old"]["date_source"] == "url"
    assert hits["old"]["date"].startswith("2020-01-01")
    assert hits["old"]["stale"] is True
    assert result["summary"]["stale"] == 1


def test_min_authority_filter_and_require():
    response = ok(
        hit("a4", "https://e/4") | {"authority": 4},
        hit("a2", "https://e/2") | {"authority": 2},
        hit("missing", "https://e/m"),
    )
    fake = FakeProvider({"q": response})
    result = run_batch(fake, ["q"], config(min_authority=3))
    titles = [item["title"] for item in result["results"]["q"]["hits"]]
    assert titles == ["a4", "missing"]
    assert result["summary"]["filtered_authority"] == 1

    fake = FakeProvider({"q": ok(
        hit("a4", "https://e/4") | {"authority": 4},
        hit("a2", "https://e/2") | {"authority": 2},
        hit("missing", "https://e/m"),
    )})
    result = run_batch(
        fake, ["q"], config(min_authority=3, require_authority=True)
    )
    titles = [item["title"] for item in result["results"]["q"]["hits"]]
    assert titles == ["a4"]
    assert result["summary"]["filtered_authority"] == 2


def test_sort_by_authority_and_date():
    fake = FakeProvider(
        {
            "q": ok(
                hit("a2", "https://e/2") | {"authority": 2, "date": "2026-10-17"},
                hit("a4", "https://e/4") | {"authority": 4, "date": "2026-10-15"},
                hit("a3", "https://e/3") | {"authority": 3, "date": "2026-10-18"},
            )
        }
    )
    result = run_batch(fake, ["q"], config(sort="authority"))
    hits = result["results"]["q"]["hits"]
    assert [item["title"] for item in hits] == ["a4", "a3", "a2"]
    assert [item["position"] for item in hits] == [1, 2, 3]

    fake = FakeProvider(
        {
            "q": ok(
                hit("old", "https://e/1") | {"date": "2026-10-10"},
                hit("new", "https://e/2") | {"date": "2026-10-18"},
            )
        }
    )
    result = run_batch(fake, ["q"], config(sort="date"))
    assert [item["title"] for item in result["results"]["q"]["hits"]] == [
        "new",
        "old",
    ]


def test_query_suffix_is_sent_but_results_keep_original_key():
    fake = FakeProvider({"q site:example.com": ok(hit("T", "https://e/1"))})
    result = run_batch(
        fake, ["q"], config(query_suffix="site:example.com")
    )
    assert list(result["results"]) == ["q"]
    assert fake.calls[0][0] == "q site:example.com"
    assert result["summary"]["query_suffix"] == " site:example.com"


def test_min_authority_parses_float_and_string():
    fake = FakeProvider(
        {
            "q": ok(
                hit("a", "https://e/a") | {"authority": 3.0},
                hit("b", "https://e/b") | {"authority": "4"},
                hit("c", "https://e/c") | {"authority": 2.9},
            )
        }
    )
    result = run_batch(fake, ["q"], config(min_authority=3, sort="authority"))
    hits = result["results"]["q"]["hits"]
    assert [item["title"] for item in hits] == ["b", "a"]
    assert result["summary"]["filtered_authority"] == 1


def test_sort_before_dedupe_keeps_best_hit():
    fake = FakeProvider(
        {
            "q": ok(
                hit("low", "https://example.com/same") | {"authority": 2},
                hit("high", "https://example.com/same") | {"authority": 4},
            )
        }
    )
    result = run_batch(
        fake, ["q"], config(dedupe_urls=True, sort="authority")
    )
    hits = result["results"]["q"]["hits"]
    assert len(hits) == 1
    assert hits[0]["title"] == "high"


def test_min_authority_invalid_and_clamped():
    fake = FakeProvider({"q": ok(hit("T", "https://e/1") | {"authority": 4})})
    result = run_batch(fake, ["q"], config(min_authority=9))
    assert result["summary"]["min_authority"] == 4
    try:
        run_batch(fake, ["q"], config(min_authority="not-an-int"))
    except ValueError as exc:
        assert "min_authority" in str(exc)
    else:
        raise AssertionError("invalid min_authority was accepted")


def test_min_authority_all_filtered_updates_stale():
    fake = FakeProvider(
        {
            "q": ok(
                hit("low-old", "https://e/1", date="2020-01-01")
                | {"authority": 1},
                hit("low-fresh", "https://e/2", date="2026-10-18")
                | {"authority": 2},
            )
        }
    )
    result = run_batch(fake, ["q"], config(min_authority=3, max_age_days=3))
    item = result["results"]["q"]
    assert item["ok"] is True
    assert item["hits"] == []
    assert item["stale_count"] == 0
    assert result["summary"]["filtered_authority"] == 2
    assert result["summary"]["stale"] == 0


def test_sort_authority_missing_last_and_invalid_mode():
    fake = FakeProvider(
        {
            "q": ok(
                hit("a2", "https://e/2") | {"authority": 2},
                hit("missing", "https://e/m"),
                hit("a4", "https://e/4") | {"authority": 4},
            )
        }
    )
    result = run_batch(fake, ["q"], config(sort="authority"))
    assert [item["title"] for item in result["results"]["q"]["hits"]] == [
        "a4",
        "a2",
        "missing",
    ]
    assert [item["position"] for item in result["results"]["q"]["hits"]] == [
        1,
        2,
        3,
    ]

    result = run_batch(fake, ["q"], config(sort="bogus"))
    assert result["summary"]["sort"] == "relevance"
    assert [item["title"] for item in result["results"]["q"]["hits"]] == [
        "a2",
        "missing",
        "a4",
    ]


def test_query_suffix_survives_retry():
    fake = FakeProvider({"q site:x": [failed("first"), ok(hit("T", "https://e/1"))]})
    result = run_batch(
        fake, ["q"], config(retries=1, query_suffix="site:x")
    )
    assert result["results"]["q"]["ok"] is True
    assert result["results"]["q"]["tries"] == 2
    assert [call[0] for call in fake.calls] == ["q site:x", "q site:x"]


def test_dedupe_keeps_original_url():
    fake = FakeProvider(
        {
            "q1": ok(hit("A", "https://example.com/a?utm_source=x")),
            "q2": ok(hit("A dup", "https://example.com/a?utm_source=y")),
        }
    )
    result = run_batch(fake, ["q1", "q2"], config(dedupe_urls=True))
    assert result["results"]["q1"]["hits"][0]["url"].endswith("utm_source=x")
    assert result["summary"]["duplicates_removed"] == 1


def test_build_query_suffix():
    assert (
        build_query_suffix(["https://a.com/path", "b.com"], "zh", "2026-10-17")
        == " site:a.com OR site:b.com lang:zh after:2026-10-17"
    )
    assert build_query_suffix(["CNSTOCK.COM"], "zh-CN", None) == (
        " site:CNSTOCK.COM lang:zh-CN"
    )
    assert build_query_suffix(None, "zh,en", None) == " lang:zh,en"
    assert build_query_suffix([], None, None) == ""
    for invalid in ("a b", "a.com:bad"):
        try:
            build_query_suffix([invalid], None, None)
        except ValueError as exc:
            assert "invalid --site" in str(exc)
        else:
            raise AssertionError(f"invalid site accepted: {invalid}")
    try:
        build_query_suffix(None, "zh en", None)
    except ValueError as exc:
        assert "invalid --lang" in str(exc)
    else:
        raise AssertionError("invalid lang accepted")
    for invalid_date in ("10/17/2026", "2026-02-30"):
        try:
            build_query_suffix(None, None, invalid_date)
        except ValueError as exc:
            assert "YYYY-MM-DD" in str(exc)
        else:
            raise AssertionError(f"invalid after date accepted: {invalid_date}")


def test_dedupe_uses_normalized_urls():
    fake = FakeProvider(
        {
            "q1": ok(hit("A", "https://example.com/a?utm_source=x")),
            "q2": ok(
                hit("A dup", "https://EXAMPLE.com/a?spm=1&utm_source=y"),
                hit("B", "https://example.com/b"),
            ),
        }
    )
    result = run_batch(fake, ["q1", "q2"], config(dedupe_urls=True))
    assert [item["title"] for item in result["results"]["q2"]["hits"]] == ["B"]
    assert result["summary"]["duplicates_removed"] == 1


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
    assert fake.max_active >= 2
    assert result["summary"]["spread_ms"] >= 0


def test_spread_measures_first_wave_only():
    queries = [f"q{i}" for i in range(10)]
    fake = FakeProvider(
        {query: ok(hit(query, f"https://example.com/{query}")) for query in queries}
    )
    result = run_batch(fake, queries, config(workers=3))
    assert result["summary"]["succeeded"] == 10
    assert fake.max_active >= 2
    assert result["summary"]["spread_ms"] < 40.0


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
