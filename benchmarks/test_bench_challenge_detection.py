"""Challenge detection and Playwright cookie conversion.

`detect_challenge` runs on every Fast-TLS response and on every iteration of
the Tier 3 solve loop, against the full page body - it is the most frequently
executed pure function in the pipeline. The cookie converters run once per
solve, over the whole jar.
"""

from app.solver.browser import detect_challenge, has_age_gate_marker, is_challenge_title
from app.solver.browser.cookies import build_playwright_cookies, extract_captured_cookies

from sample_data import (
    CHALLENGE_PAGE_HTML_LOWER,
    CHALLENGE_PAGE_TITLE,
    CLEAN_PAGE_HTML_LOWER,
    CLEAN_PAGE_TITLE,
    make_cookies,
)


def test_detect_challenge_title_hit(benchmark):
    """Cheapest path: the title alone identifies the challenge."""
    result = benchmark(detect_challenge, CHALLENGE_PAGE_TITLE, "", False)
    assert result == "cloudflare_turnstile"


def test_detect_challenge_body_scan_hit(benchmark):
    """Full body scan on a Cloudflare interstitial (check_content=True)."""
    result = benchmark(detect_challenge, CHALLENGE_PAGE_TITLE, CHALLENGE_PAGE_HTML_LOWER, True)
    assert result == "cloudflare_turnstile"


def test_detect_challenge_body_scan_miss(benchmark):
    """Worst case: a large clean page, every marker of every WAF checked."""
    result = benchmark(detect_challenge, CLEAN_PAGE_TITLE, CLEAN_PAGE_HTML_LOWER, True)
    assert result is None


def test_is_challenge_title(benchmark):
    result = benchmark(is_challenge_title, CLEAN_PAGE_TITLE)
    assert result is False


def test_has_age_gate_marker(benchmark):
    result = benchmark(has_age_gate_marker, CLEAN_PAGE_HTML_LOWER, True)
    assert result is False


def test_build_playwright_cookies(benchmark):
    cookies = make_cookies(40)
    result = benchmark(build_playwright_cookies, "https://indexer.example.com/search", cookies)
    assert len(result) == 40


def test_extract_captured_cookies(benchmark):
    raw = [
        {
            "name": f"cookie_{i}",
            "value": f"value-{i}-" + "y" * 32,
            "domain": ".indexer.example.com",
            "path": "/",
            "expires": 4102444800.0 if i % 2 else -1,
            "httpOnly": bool(i % 2),
            "secure": True,
            "sameSite": "Lax",
        }
        for i in range(40)
    ]
    result = benchmark(extract_captured_cookies, raw)
    assert len(result) == 40
