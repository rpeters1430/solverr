"""Tier 2 cookie cache and the session store.

`get_cookies` runs on the hot path of every single request (before Fast TLS
even fires) and walks the whole local store, so its cost scales with how many
domains an instance has seen. `export_netscape` / `get_all_entries` back the
dashboard and `/api/cookies/export` endpoints.
"""

import pytest

from app.solver.cache import CookieCache
from app.solver.sessions import Session, SessionManager

from sample_data import make_cookies

DOMAIN_COUNT = 120
COOKIES_PER_DOMAIN = 12
TARGET_URL = "https://sub.indexer60.example.com/api?t=search&q=ubuntu"


def _populated_cache(tmp_path) -> CookieCache:
    cache = CookieCache(cache_file=str(tmp_path / "cookies_cache.json"))
    # Disk persistence is debounced in production and is not what these
    # benchmarks measure - keep the store purely in memory.
    cache._save_to_disk = lambda: None
    for i in range(DOMAIN_COUNT):
        domain = f"indexer{i}.example.com"
        cache.set_cookies(f"https://{domain}/", make_cookies(COOKIES_PER_DOMAIN, domain=domain))
    return cache


@pytest.fixture
def populated_cache(tmp_path):
    return _populated_cache(tmp_path)


def test_cache_get_cookies(benchmark, populated_cache):
    result = benchmark(populated_cache.get_cookies, TARGET_URL)
    assert len(result) == COOKIES_PER_DOMAIN


def test_cache_get_cookie_dict(benchmark, populated_cache):
    result = benchmark(populated_cache.get_cookie_dict, TARGET_URL)
    assert "cf_clearance" in result


def test_cache_get_cookies_miss(benchmark, populated_cache):
    """Full scan of the store with no matching domain - the miss path every
    first-time target pays for."""
    result = benchmark(populated_cache.get_cookies, "https://unknown-target.example.org/")
    assert result == []


def test_cache_set_cookies(benchmark, populated_cache):
    cookies = make_cookies(COOKIES_PER_DOMAIN, domain="fresh-indexer.example.com")
    benchmark(populated_cache.set_cookies, "https://fresh-indexer.example.com/", cookies)
    assert populated_cache.get_cookies("https://fresh-indexer.example.com/")


def test_cache_get_all_entries(benchmark, populated_cache):
    result = benchmark(populated_cache.get_all_entries)
    assert len(result) == DOMAIN_COUNT


def test_cache_export_netscape(benchmark, populated_cache):
    result = benchmark(populated_cache.export_netscape)
    assert result.startswith("# Netscape HTTP Cookie File")


def test_session_update_cookies(benchmark):
    """Session cookie merge keyed by (domain, path, name) - runs after every
    solve made with a pinned session."""
    session = Session("bench-session", proxy=None, ttl=7200)
    session.cookies = make_cookies(30)
    fresh = make_cookies(30)

    def merge():
        session.update_cookies(fresh)
        return session.cookies

    result = benchmark(merge)
    assert len(result) == 30


def test_session_roundtrip(benchmark):
    """to_dict/from_dict is what Redis-backed sessions pay on every access."""
    session = Session("bench-session", proxy="http://user:pass@proxy.example.com:8888", ttl=7200)
    session.cookies = make_cookies(30)

    def roundtrip():
        return Session.from_dict(session.to_dict())

    result = benchmark(roundtrip)
    assert len(result.cookies) == 30


def test_session_manager_lookup(benchmark):
    manager = SessionManager(redis_url=None)
    for i in range(200):
        manager.create_session(session_id=f"session-{i}", proxy=None, ttl=7200)

    def lookup():
        manager.get_session("session-150")
        return manager.list_sessions()

    result = benchmark(lookup)
    assert len(result) == 200
