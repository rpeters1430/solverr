import unittest

from app.models.flaresolverr import CookieModel
from app.solver.browser.cookies import build_playwright_cookies, extract_captured_cookies
from app.solver.browser.navigation import _build_post_form_html


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


if __name__ == "__main__":
    unittest.main()
