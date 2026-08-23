import unittest
from app.solver.fast_tls import FastTLSEngine, FIREFOX_PROFILES, CHROME_PROFILES, _sec_ch_ua_for


class TestFastTLSProfileRotation(unittest.TestCase):
    def test_profile_is_deterministic_per_domain(self):
        engine = FastTLSEngine()
        engine.rotate = True
        engine.profiles = FIREFOX_PROFILES
        first = engine._profile_for_domain("https://example.com/page1")
        second = engine._profile_for_domain("https://example.com/other-page")
        self.assertEqual(first, second)

    def test_profile_target_and_ua_always_match_browser_family(self):
        for target, ua in FIREFOX_PROFILES:
            self.assertTrue(target.startswith("firefox"))
            self.assertIn("Firefox/", ua)
            self.assertIn(target.replace("firefox", ""), ua)
        for target, ua in CHROME_PROFILES:
            self.assertTrue(target.startswith("chrome"))
            self.assertIn("Chrome/", ua)
            self.assertIn(target.replace("chrome", ""), ua)

    def test_rotation_disabled_always_returns_first_profile(self):
        engine = FastTLSEngine()
        engine.rotate = False
        engine.profiles = FIREFOX_PROFILES
        for domain in ["a.com", "b.com", "c.com"]:
            self.assertEqual(engine._profile_for_domain(f"https://{domain}"), FIREFOX_PROFILES[0])

    def test_sec_ch_ua_only_generated_for_chrome_targets(self):
        self.assertIsNone(_sec_ch_ua_for("firefox147"))
        header = _sec_ch_ua_for("chrome146")
        self.assertIn("146", header)
        self.assertIn("Chromium", header)

    def test_adaptive_profile_scoring_favors_successful_profile(self):
        engine = FastTLSEngine()
        engine.rotate = True
        engine.profiles = FIREFOX_PROFILES
        url = "https://tracker.test/search"

        # Record multiple successes for firefox144 on this domain
        engine.record_outcome(url, "firefox144", success=True)
        engine.record_outcome(url, "firefox144", success=True)

        # Record failure for firefox147 on this domain
        engine.record_outcome(url, "firefox147", success=False)

        profile = engine._profile_for_domain(url)
        self.assertEqual(profile[0], "firefox144")


class TestFastTLSChallengeDetection(unittest.IsolatedAsyncioTestCase):
    async def test_detects_challenge_on_status_200_with_cloudflare_title(self):
        from unittest.mock import AsyncMock, patch, MagicMock
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<html><head><title>Attention Required! | Cloudflare</title></head><body>Please solve captcha</body></html>"
        mock_resp.headers = {"content-type": "text/html"}
        mock_resp.cookies = {}
        mock_resp.url = "https://example.com"

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("app.solver.fast_tls.AsyncSession", return_value=mock_session):
            engine = FastTLSEngine()
            is_challenge, sol = await engine.request("https://example.com")
            self.assertTrue(is_challenge)

    async def test_detects_challenge_on_status_200_with_embedded_turnstile(self):
        from unittest.mock import AsyncMock, patch, MagicMock
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<html><head><title>Welcome</title></head><body><script src='https://challenges.cloudflare.com/turnstile/v0/api.js'></script></body></html>"
        mock_resp.headers = {"content-type": "text/html"}
        mock_resp.cookies = {}
        mock_resp.url = "https://example.com"

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("app.solver.fast_tls.AsyncSession", return_value=mock_session):
            engine = FastTLSEngine()
            is_challenge, sol = await engine.request("https://example.com")
            self.assertTrue(is_challenge)

    async def test_clean_200_response_marked_as_not_challenge(self):
        from unittest.mock import AsyncMock, patch, MagicMock
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<html><head><title>Home Page</title></head><body><h1>Welcome to my website</h1></body></html>"
        mock_resp.headers = {"content-type": "text/html"}
        mock_resp.cookies = {"sess": "123"}
        mock_resp.url = "https://example.com"

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("app.solver.fast_tls.AsyncSession", return_value=mock_session):
            engine = FastTLSEngine()
            is_challenge, sol = await engine.request("https://example.com")
            self.assertFalse(is_challenge)
            self.assertEqual(sol.status, 200)
            self.assertEqual(len(sol.cookies), 1)


if __name__ == "__main__":
    unittest.main()
