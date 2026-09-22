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

    def test_domain_scores_are_bounded_and_evict_oldest(self):
        engine = FastTLSEngine()
        engine._max_domain_scores = 3
        for i in range(5):
            engine.record_outcome(f"https://domain{i}.test/", "firefox144", success=True)
        # Never allowed to grow past the configured bound...
        self.assertLessEqual(len(engine._domain_scores), 3)
        # ...and it's the oldest (least-recently-touched) domains that get
        # evicted, not an arbitrary one - the most recent 3 must survive.
        self.assertNotIn("domain0.test", engine._domain_scores)
        self.assertNotIn("domain1.test", engine._domain_scores)
        self.assertIn("domain2.test", engine._domain_scores)
        self.assertIn("domain3.test", engine._domain_scores)
        self.assertIn("domain4.test", engine._domain_scores)


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

    async def test_api_json_response_not_marked_as_challenge(self):
        from unittest.mock import AsyncMock, patch, MagicMock
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = '{"akamai_fingerprint": "1:65536;2:0;4:1310", "status": "ok"}'
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.cookies = {}
        mock_resp.url = "https://tls.peet.ws/api/all"

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("app.solver.fast_tls.AsyncSession", return_value=mock_session):
            engine = FastTLSEngine()
            is_challenge, sol = await engine.request("https://tls.peet.ws/api/all")
            self.assertFalse(is_challenge)
            self.assertEqual(sol.status, 200)


class TestFastTLSSessionPool(unittest.IsolatedAsyncioTestCase):
    async def test_session_reused_across_requests_for_same_domain(self):
        from unittest.mock import AsyncMock, patch, MagicMock
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<html><head><title>OK</title></head><body>Hello</body></html>"
        mock_resp.headers = {}
        mock_resp.cookies = {}
        mock_resp.url = "https://example.com"

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=mock_resp)
        mock_session.close = AsyncMock()

        engine = FastTLSEngine()
        engine._pool_enabled = True

        with patch("app.solver.fast_tls.AsyncSession", return_value=mock_session) as session_ctor:
            await engine.request("https://example.com/page1")
            await engine.request("https://example.com/page2")
            # Session constructor should only be called once due to pooling
            self.assertEqual(session_ctor.call_count, 1)
            self.assertIn("example.com", list(engine._sessions.keys())[0])

    async def test_session_evicted_on_failure(self):
        from unittest.mock import AsyncMock, patch
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(side_effect=RuntimeError("Connection reset"))
        mock_session.close = AsyncMock()

        engine = FastTLSEngine()
        engine._pool_enabled = True

        with patch("app.solver.fast_tls.AsyncSession", return_value=mock_session):
            is_challenge, sol = await engine.request("https://fail.com")
            self.assertTrue(is_challenge)
            self.assertIsNone(sol)
            self.assertEqual(len(engine._sessions), 0)

    async def test_close_shuts_down_all_pooled_sessions(self):
        from unittest.mock import AsyncMock
        mock_session = AsyncMock()
        mock_session.close = AsyncMock()

        engine = FastTLSEngine()
        engine._sessions["test.com:firefox:"] = mock_session

        await engine.close()
        mock_session.close.assert_awaited_once()
        self.assertEqual(len(engine._sessions), 0)


    async def test_public_redirect_is_followed_manually(self):
        from unittest.mock import AsyncMock, patch, MagicMock
        redirect = MagicMock(status_code=302, text="", headers={"location": "/final"}, cookies={})
        redirect.url = "https://example.com/start"
        final = MagicMock(status_code=200, text="<html><title>OK</title><body>done</body></html>", headers={}, cookies={})
        final.url = "https://example.com/final"
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(side_effect=[redirect, final])
        mock_session.close = AsyncMock()
        with patch("app.solver.fast_tls.AsyncSession", return_value=mock_session), \
             patch("app.solver.fast_tls.check_target_url_async", new=AsyncMock()):
            engine = FastTLSEngine()
            engine._pool_enabled = False
            challenged, solution = await engine.request("https://example.com/start")
        self.assertFalse(challenged)
        self.assertEqual(solution.url, "https://example.com/final")
        self.assertEqual(mock_session.get.await_count, 2)
        self.assertFalse(mock_session.get.await_args_list[0].kwargs["allow_redirects"])

    async def test_private_redirect_is_blocked_before_second_request(self):
        from unittest.mock import AsyncMock, patch, MagicMock
        from app.security import SSRFBlockedError
        redirect = MagicMock(status_code=302, text="", headers={"location": "http://127.0.0.1/admin"}, cookies={})
        redirect.url = "https://example.com/start"
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=redirect)
        mock_session.close = AsyncMock()
        async def validate(url, label="Target"):
            if "127.0.0.1" in url:
                raise SSRFBlockedError("blocked")
        with patch("app.solver.fast_tls.AsyncSession", return_value=mock_session), \
             patch("app.solver.fast_tls.check_target_url_async", new=AsyncMock(side_effect=validate)):
            engine = FastTLSEngine()
            engine._pool_enabled = False
            challenged, solution = await engine.request("https://example.com/start")
        self.assertTrue(challenged)
        self.assertIsNone(solution)
        self.assertEqual(mock_session.get.await_count, 1)
        mock_session.close.assert_awaited_once()

if __name__ == "__main__":
    unittest.main()
