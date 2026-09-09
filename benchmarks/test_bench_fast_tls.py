"""Tier 1 profile selection and the SSRF guard.

`_profile_for_domain` (adaptive JA3/UA picking) and `_normalize_domain` run
before every Fast TLS request; `check_target_url` runs at the top of
`process_request` for every request and again for a caller-supplied proxy.
Only IP-literal targets are used here so no DNS resolution happens.
"""

import pytest

from app.logging_config import sanitize_proxy_url
from app.security import _is_blocked_ip, check_target_url
from app.solver.fast_tls import FastTLSEngine

DOMAINS = [f"https://indexer{i}.example.com/api?t=search&q=ubuntu" for i in range(50)]


@pytest.fixture
def engine() -> FastTLSEngine:
    eng = FastTLSEngine()
    # Warm the adaptive scoreboard the way a running instance would: a mix of
    # successes and failures across domains and profiles.
    for i, url in enumerate(DOMAINS):
        for target, _ua in eng.profiles:
            eng.record_outcome(url, target, success=(i + len(target)) % 3 != 0)
    return eng


def test_normalize_domain(benchmark, engine):
    result = benchmark(engine._normalize_domain, "https://Sub.Indexer.Example.com:8443/api?t=search")
    assert result == "sub.indexer.example.com"


def test_profile_for_domain_scored(benchmark, engine):
    """Profile pick for a domain with success history recorded."""
    target, ua = benchmark(engine._profile_for_domain, DOMAINS[7])
    assert target.startswith("firefox")
    assert ua


def test_profile_for_domain_unseen(benchmark, engine):
    """Cold domain: falls through to the crc32 sticky assignment."""
    target, _ua = benchmark(engine._profile_for_domain, "https://brand-new-indexer.example.net/")
    assert target.startswith("firefox")


def test_record_outcome(benchmark, engine):
    benchmark(engine.record_outcome, DOMAINS[3], "firefox147", True)
    assert engine._domain_scores["indexer3.example.com"]["firefox147"] > 0


def test_check_target_url_public(benchmark):
    """The allow path: a public IP-literal target passes every policy check."""
    benchmark(check_target_url, "https://93.184.216.34/api?t=search")


def test_check_target_url_blocked(benchmark):
    """The block path, including the ipaddress classification."""

    def blocked():
        try:
            check_target_url("http://169.254.169.254/latest/meta-data/")
        except Exception as exc:
            return exc
        return None

    assert benchmark(blocked) is not None


def test_is_blocked_ip(benchmark):
    assert benchmark(_is_blocked_ip, "10.0.0.5") is True


def test_sanitize_proxy_url(benchmark):
    result = benchmark(sanitize_proxy_url, "http://user:sup3rsecret@residential.proxy.example.com:8888")
    assert result == "http://user:***@residential.proxy.example.com:8888"
