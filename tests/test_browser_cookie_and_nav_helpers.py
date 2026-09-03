import unittest
from unittest.mock import AsyncMock, MagicMock

from app.models.flaresolverr import CookieModel
from app.solver.browser.cookies import build_playwright_cookies, extract_captured_cookies
from app.solver.browser.navigation import _build_post_form_html, navigate_to_target


class TestBuildPlaywrightCookies(unittest.TestCase):
    def test_no_cookies_returns_empty_list(self):
        self.assertEqual(build_playwright_cookies("https://example.com", None), [])
        self.assertEqual(build_playwright_cookies("https://example.com", []), [])

    def test_defaults_domain_to_target_host(self):
        cookies = [CookieModel(name="a", value="1", domain=None, path=None)]
        out = build_playwright_cookies("https://sub.example.com:8443/path", cookies)
        self.assertEqual(out, [{"name": "a", "value": "1", "domain": "sub.example.com", "path": "/"}])

    def test_leading_dot_domain_is_stripped(self):
        cookies = [CookieModel(name="a", value="1", domain=".example.com", path="/x")]
        out = build_playwright_cookies("https://example.com", cookies)
        self.assertEqual(out[0]["domain"], "example.com")
        self.assertEqual(out[0]["path"], "/x")


class TestExtractCapturedCookies(unittest.TestCase):
    def test_normalizes_missing_fields(self):
        raw = [{"name": "a", "value": "1"}]
        out = extract_captured_cookies(raw)
        self.assertEqual(len(out), 1)
        c = out[0]
        self.assertEqual(c.expires, -1)
        self.assertEqual(c.size, len("a") + len("1"))
        self.assertEqual(c.domain, "")
        self.assertEqual(c.path, "/")
        self.assertEqual(c.sameSite, "Lax")

    def test_preserves_explicit_fields(self):
        raw = [{
            "name": "sess", "value": "xyz", "domain": "example.com", "path": "/api",
            "expires": 123.0, "size": 42, "httpOnly": True, "secure": True,
            "session": False, "sameSite": "Strict"
        }]
        out = extract_captured_cookies(raw)
        c = out[0]
        self.assertEqual(c.expires, 123.0)
        self.assertEqual(c.size, 42)
        self.assertTrue(c.httpOnly)
        self.assertTrue(c.secure)
        self.assertEqual(c.sameSite, "Strict")

    def test_negative_expires_normalized_to_minus_one(self):
        raw = [{"name": "a", "value": "1", "expires": -5}]
        out = extract_captured_cookies(raw)
        self.assertEqual(out[0].expires, -1)


class TestBuildPostFormHtml(unittest.TestCase):
    def test_json_body_becomes_hidden_inputs(self):
        html_out = _build_post_form_html("https://example.com/submit", '{"a": "1", "b": "x&y"}')
        self.assertIn('name="a" value="1"', html_out)
        self.assertIn('name="b" value="x&amp;y"', html_out)
        self.assertIn('action="https://example.com/submit"', html_out)

    def test_urlencoded_body_becomes_hidden_inputs(self):
        html_out = _build_post_form_html("https://example.com/submit", "a=1&b=2")
        self.assertIn('name="a" value="1"', html_out)
        self.assertIn('name="b" value="2"', html_out)

    def test_opaque_body_falls_back_to_single_data_field(self):
        html_out = _build_post_form_html("https://example.com/submit", "just-some-raw-text")
        self.assertIn('name="data" value="just-some-raw-text"', html_out)


class FakeNavigationContext:
    """Mimics Playwright's AsyncEventContextManager returned by
    page.expect_navigation(): an async context manager whose `.value`
    attribute is itself awaitable to the captured Response (or None, e.g.
    for a same-page anchor navigation)."""

    def __init__(self, response):
        self.value = self._make_value(response)

    @staticmethod
    async def _make_value(response):
        return response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class TestNavigateToTarget(unittest.IsolatedAsyncioTestCase):
    async def test_post_navigation_captures_response_status(self):
        # Regression test for the bug Copilot flagged on PR #29:
        # page.wait_for_load_state() always returns None, so the POST path
        # never actually captured a status. expect_navigation() should.
        fake_response = MagicMock(status=201)
        page = MagicMock()
        page.set_content = AsyncMock()
        page.expect_navigation = MagicMock(return_value=FakeNavigationContext(fake_response))

        response, status = await navigate_to_target(page, "https://example.com/submit", "POST", "a=1", 5000)

        page.set_content.assert_awaited_once()
        self.assertEqual(status, 201)
        self.assertIs(response, fake_response)

    async def test_post_navigation_no_response_falls_back_to_zero(self):
        page = MagicMock()
        page.set_content = AsyncMock()
        page.expect_navigation = MagicMock(return_value=FakeNavigationContext(None))

        response, status = await navigate_to_target(page, "https://example.com/submit", "POST", "a=1", 5000)

        self.assertEqual(status, 0)
        self.assertIsNone(response)

    async def test_get_navigation_uses_goto(self):
        fake_response = MagicMock(status=200)
        page = MagicMock()
        page.goto = AsyncMock(return_value=fake_response)

        response, status = await navigate_to_target(page, "https://example.com", "GET", None, 5000)

        page.goto.assert_awaited_once_with("https://example.com", wait_until="domcontentloaded", timeout=5000)
        self.assertEqual(status, 200)
        self.assertIs(response, fake_response)

    async def test_navigation_error_falls_back_to_zero_status(self):
        page = MagicMock()
        page.goto = AsyncMock(side_effect=RuntimeError("boom"))

        response, status = await navigate_to_target(page, "https://example.com", "GET", None, 5000)

        self.assertEqual(status, 0)
        self.assertIsNone(response)


if __name__ == "__main__":
    unittest.main()
