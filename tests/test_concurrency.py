from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from hsr.backends.base import RateLimiter
from hsr.config import ConfigStore


def test_rate_limiter_enforces_interval():
    now = [0.0]
    sleeps = []

    def clock():
        return now[0]

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    limiter = RateLimiter(0.2, clock=clock, sleep=sleep)
    limiter.wait()
    limiter.wait()
    limiter.wait()
    assert len(sleeps) == 2
    assert all(abs(value - 0.2) < 1e-6 for value in sleeps)


def test_config_store_concurrent_reads(tmp_path):
    path = tmp_path / "routes.yaml"
    path.write_text(
        "version: 1\nactive: anysearch\nbackends:\n"
        "  deepseek: {type: deepseek}\n  anysearch: {type: anysearch}\n",
        encoding="utf-8",
    )
    store = ConfigStore(path)

    def read(_):
        return store.get().active

    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(read, range(80)))
    assert values == ["anysearch"] * 80
    assert store.last_error is None
