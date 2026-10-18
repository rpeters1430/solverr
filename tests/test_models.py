import unittest
from app.models.flaresolverr import V1Request, ProxyConfig, ScrapeRequest

class TestV1RequestModels(unittest.TestCase):
    def test_proxy_string_normalization(self):
        req = V1Request(cmd="request.get", url="http://example.com", proxy="http://user:pass@proxy.com:8080")
        self.assertEqual(req.get_proxy_url(), "http://user:pass@proxy.com:8080")

    def test_proxy_dict_normalization(self):
        req1 = V1Request(cmd="request.get", url="http://example.com", proxy={"url": "http://proxy.com:8080"})
        self.assertEqual(req1.get_proxy_url(), "http://proxy.com:8080")

        req2 = V1Request(cmd="request.get", url="http://example.com", proxy={"host": "1.2.3.4", "port": 8080, "username": "usr", "password": "pwd"})
        self.assertEqual(req2.get_proxy_url(), "http://usr:pwd@1.2.3.4:8080")

        req3 = V1Request(cmd="request.get", url="http://example.com", proxy={"url": "http://proxy.com:8080", "username": "usr", "password": "pwd"})
        self.assertEqual(req3.get_proxy_url(), "http://usr:pwd@proxy.com:8080")

        req4 = V1Request(cmd="request.get", url="http://example.com", proxy={"server": "http://proxy.com:8080", "username": "usr", "password": "pwd"})
        self.assertEqual(req4.get_proxy_url(), "http://usr:pwd@proxy.com:8080")

    def test_proxy_object_normalization(self):
        req = V1Request(cmd="request.get", url="http://example.com", proxy=ProxyConfig(url="http://proxy.com:8080"))
        self.assertEqual(req.get_proxy_url(), "http://proxy.com:8080")

    def test_custom_user_agent_and_headers(self):
        req = V1Request(
            cmd="request.get",
            url="http://example.com",
            userAgent="CustomUA/1.0",
            headers={"X-Custom-Header": "value123"},
            fastTlsOnly=True
        )
        self.assertEqual(req.userAgent, "CustomUA/1.0")
        self.assertEqual(req.headers, {"X-Custom-Header": "value123"})
        self.assertTrue(req.fastTlsOnly)

    def test_scrape_request_conversion(self):
        scrape_req = ScrapeRequest(
            url="https://nowsecure.nl",
            method="POST",
            postData='{"test": 1}',
            tier="tier3_browser",
            screenshot=True,
            wait_selector="#ready"
        )
        v1_req = scrape_req.to_v1_request()
        self.assertEqual(v1_req.cmd, "request.post")
        self.assertEqual(v1_req.url, "https://nowsecure.nl")
        self.assertTrue(v1_req.forceBrowser)
        self.assertTrue(v1_req.screenshot)
        self.assertEqual(v1_req.wait_selector, "#ready")

    def test_cookie_model_null_fields(self):
        from app.models.flaresolverr import CookieModel
        c = CookieModel(
            name="test",
            value="val",
            expires=None,
            size=None,
            httpOnly=None,
            secure=None,
            session=None,
            sameSite=None,
            path=None
        )
        self.assertEqual(c.name, "test")
        self.assertEqual(c.value, "val")
        self.assertEqual(c.expires, -1)
        self.assertEqual(c.size, 0)
        self.assertFalse(c.httpOnly)
        self.assertFalse(c.secure)
        self.assertFalse(c.session)
        self.assertEqual(c.sameSite, "Lax")
        self.assertEqual(c.path, "/")

    def test_headers_numeric_and_boolean_coercion(self):
        req = V1Request(cmd="request.get", url="http://example.com", headers={"x-num": 123, "x-bool": True, "x-none": None})
        self.assertEqual(req.headers["x-num"], "123")
        self.assertEqual(req.headers["x-bool"], "True")
        self.assertEqual(req.headers["x-none"], "")


if __name__ == "__main__":
    unittest.main()


class TestFlareSolverrCompatibilityFields(unittest.TestCase):
    def test_return_screenshot_and_wait_in_seconds_map_to_solverr_fields(self):
        req = V1Request(cmd="request.get", url="https://a.test", returnScreenshot=True, waitInSeconds=3)
        self.assertTrue(req.screenshot)
        self.assertTrue(req.forceBrowser, "a screenshot needs the browser tier")
        self.assertEqual(req.wait_delay_ms, 3000)

    def test_explicit_wait_delay_wins_over_wait_in_seconds(self):
        req = V1Request(cmd="request.get", url="https://a.test", waitInSeconds=3, wait_delay_ms=500)
        self.assertEqual(req.wait_delay_ms, 500)

    def test_disable_media_and_tabs_till_verify_are_accepted(self):
        req = V1Request(cmd="request.get", url="https://a.test", disableMedia=True, tabs_till_verify=3)
        self.assertTrue(req.disableMedia)
        self.assertFalse(req.forceBrowser)

    def test_capture_forces_the_browser_and_rejects_unknown_kinds(self):
        req = V1Request(cmd="request.get", url="https://a.test", capture=["redirects", "console", "console"])
        self.assertTrue(req.forceBrowser)
        self.assertEqual(req.debug_capture(), ("console", "redirects"))
        with self.assertRaises(ValueError):
            V1Request(cmd="request.get", url="https://a.test", capture=["har"])


class TestScrapeTiers(unittest.TestCase):
    def test_tier_mapping(self):
        self.assertTrue(ScrapeRequest(url="https://a.test", tier="tier2_cache").to_v1_request().fastTlsOnly)
        tier4 = ScrapeRequest(url="https://a.test", tier="tier4_proxy").to_v1_request()
        self.assertTrue(tier4.forceFallbackProxy)
        self.assertTrue(tier4.forceBrowser)
        tier3 = ScrapeRequest(url="https://a.test", tier="tier3_browser").to_v1_request()
        self.assertTrue(tier3.forceBrowser)
        self.assertFalse(tier3.forceFallbackProxy)

    def test_unknown_tier_is_rejected(self):
        with self.assertRaises(ValueError):
            ScrapeRequest(url="https://a.test", tier="tier5_magic")

    def test_capture_and_disable_media_pass_through(self):
        req = ScrapeRequest(url="https://a.test", capture=["network"], disable_media=True).to_v1_request()
        self.assertEqual(req.debug_capture(), ("network",))
        self.assertTrue(req.disableMedia)
