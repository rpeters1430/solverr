import unittest
from unittest.mock import AsyncMock, patch
from app.solver.fast_tls import FastTLSEngine, FIREFOX_PROFILES, CHROME_PROFILES, _platform_hint_for


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

    def test_platform_hint_only_for_chrome_targets_and_follows_ua(self):
        self.assertIsNone(_platform_hint_for("firefox147", FIREFOX_PROFILES[0][1]))
        self.assertEqual(_platform_hint_for("chrome150", CHROME_PROFILES[0][1]), '"Windows"')
        mac_ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
        self.assertEqual(_platform_hint_for("chrome150", mac_ua), '"macOS"')

    def test_profile_targets_are_supported_by_curl_cffi(self):
        from app.solver.fast_tls import SUPPORTED_TARGETS
        for target, _ in FIREFOX_PROFILES + CHROME_PROFILES:
            self.assertIn(target, SUPPORTED_TARGETS)

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

    def test_non_positive_max_domain_scores_setting_is_clamped(self):
        # MAX_FAST_TLS_DOMAIN_SCORES=0 (or negative) must not leave the
        # engine with a cap that makes the eviction branch always true on an
        # empty mapping, which raised KeyError on the first new domain.
        from unittest.mock import patch
        with patch("app.solver.fast_tls.settings") as mock_settings:
            mock_settings.FAST_TLS_TARGET = "firefox"
            mock_settings.FAST_TLS_ROTATE = True
            mock_settings.MAX_FAST_TLS_DOMAIN_SCORES = 0
            mock_settings.FAST_TLS_POOL_ENABLED = True
            mock_settings.FAST_TLS_POOL_SIZE = 50
            engine = FastTLSEngine()
        self.assertGreaterEqual(engine._max_domain_scores, 1)
        engine.record_outcome("https://example.com/", "firefox144", success=True)
        self.assertIn("example.com", engine._domain_scores)


class TestFastTLSUserAgentCompatibility(unittest.TestCase):
    def test_firefox_target_accepts_only_firefox_uas(self):
        engine = FastTLSEngine()
        engine.impersonate_target = "firefox147"
        self.assertTrue(engine.is_compatible_user_agent("Mozilla/5.0 (X11; Linux x86_64; rv:152.0) Gecko/20100101 Firefox/152.0"))
        self.assertFalse(engine.is_compatible_user_agent(CHROME_PROFILES[0][1]))
        self.assertFalse(engine.is_compatible_user_agent(None))

    def test_target_matches_ua_version_when_supported(self):
        engine = FastTLSEngine()
        engine.impersonate_target = "firefox147"
        self.assertEqual(engine.target_for_user_agent(FIREFOX_PROFILES[1][1]), "firefox144")

    def test_target_falls_back_to_newest_not_newer_than_ua(self):
        engine = FastTLSEngine()
        engine.impersonate_target = "firefox133"
        target = engine.target_for_user_agent("Mozilla/5.0 (X11; Linux x86_64; rv:999.0) Gecko/20100101 Firefox/999.0")
        self.assertTrue(target.startswith("firefox"))
        self.assertLessEqual(int(target[len("firefox"):]), 999)
        self.assertNotEqual(target, "firefox133")

    def test_target_defaults_when_ua_has_no_version(self):
        engine = FastTLSEngine()
        engine.impersonate_target = "firefox147"
        self.assertEqual(engine.target_for_user_agent("custom-agent"), "firefox147")

    def test_chrome_target_accepts_only_chrome_uas(self):
        engine = FastTLSEngine()
        engine.impersonate_target = "chrome146"
        self.assertTrue(engine.is_compatible_user_agent(CHROME_PROFILES[0][1]))
        self.assertFalse(engine.is_compatible_user_agent(FIREFOX_PROFILES[0][1]))


class TestFastTLSCookieSelection(unittest.TestCase):
    def _cookie(self, name, value, domain, path="/"):
        from app.models.flaresolverr import CookieModel
        return CookieModel(name=name, value=value, domain=domain, path=path)

    def test_same_name_cookies_on_different_domains_do_not_shadow_each_other(self):
        engine = FastTLSEngine()
        cookies = [
            self._cookie("session", "unrelated-domain-value", domain="other.test"),
            self._cookie("session", "target-domain-value", domain="target.test"),
        ]
        selected = engine._select_cookies_for_url(cookies, "https://target.test/page")
        self.assertEqual(selected.get("session"), "target-domain-value")

    def test_same_name_cookies_on_different_paths_prefer_more_specific_path(self):
        engine = FastTLSEngine()
        cookies = [
            self._cookie("token", "root-value", domain="target.test", path="/"),
            self._cookie("token", "scoped-value", domain="target.test", path="/account"),
        ]
        selected = engine._select_cookies_for_url(cookies, "https://target.test/account/settings")
        self.assertEqual(selected.get("token"), "scoped-value")

    def test_cookie_scoped_to_unrelated_path_is_excluded(self):
        engine = FastTLSEngine()
        cookies = [self._cookie("token", "scoped-value", domain="target.test", path="/account")]
        selected = engine._select_cookies_for_url(cookies, "https://target.test/other")
        self.assertNotIn("token", selected)


class TestFastTLSChallengeDetection(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.target_check = patch("app.solver.fast_tls.check_target_url_async", new=AsyncMock())
        self.target_check.start()

    def tearDown(self):
        self.target_check.stop()

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

    async def _request(self, text, status=200, headers=None):
        from unittest.mock import AsyncMock, patch, MagicMock
        mock_resp = MagicMock()
        mock_resp.status_code = status
        mock_resp.text = text
        mock_resp.headers = {"content-type": "text/html", **(headers or {})}
        mock_resp.cookies = {}
        mock_resp.url = "https://example.com"
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=mock_resp)
        with patch("app.solver.fast_tls.AsyncSession", return_value=mock_session):
            return await FastTLSEngine().request("https://example.com")

    async def test_provider_telemetry_on_a_real_page_stays_on_fast_tls(self):
        body = "<body>" + "<p>row</p>" * 800 + "</body>"
        for tag in (
            '<script src="https://js.datadome.co/tags.js"></script>',
            '<script src="/_Incapsula_Resource?SWJIYLWA=1"></script>',
            '<script src="https://abc.token.awswaf.com/abc/challenge.js"></script>',
        ):
            is_challenge, sol = await self._request(f"<html><head><title>Shop</title>{tag}</head>{body}</html>")
            self.assertFalse(is_challenge, tag)
            self.assertEqual(sol.status, 200)

    async def test_provider_declared_challenge_header_escalates(self):
        is_challenge, _ = await self._request(
            "<html><head><title>example.com</title></head><body></body></html>",
            headers={"cf-mitigated": "challenge"},
        )
        self.assertTrue(is_challenge)

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
    def setUp(self):
        self.target_check = patch("app.solver.fast_tls.check_target_url_async", new=AsyncMock())
        self.target_check.start()

    def tearDown(self):
        self.target_check.stop()

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

    def _refresh_session(self, *responses):
        from unittest.mock import AsyncMock
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(side_effect=list(responses))
        mock_session.close = AsyncMock()
        return mock_session

    def _html(self, url, body):
        from unittest.mock import MagicMock
        resp = MagicMock(status_code=200, text=body, headers={"content-type": "text/html"}, cookies={})
        resp.url = url
        return resp

    async def test_meta_refresh_is_ignored_unless_opted_in(self):
        from unittest.mock import AsyncMock, patch
        page = self._html("https://example.com/a", '<meta http-equiv="refresh" content="0;url=/b">')
        session = self._refresh_session(page)
        with patch("app.solver.fast_tls.AsyncSession", return_value=session), \
             patch("app.solver.fast_tls.check_target_url_async", new=AsyncMock()):
            engine = FastTLSEngine()
            engine._pool_enabled = False
            _, solution = await engine.request("https://example.com/a")
        self.assertEqual(solution.url, "https://example.com/a")
        self.assertEqual(session.get.await_count, 1)

    async def test_opted_in_meta_refresh_is_followed_and_checked(self):
        from unittest.mock import AsyncMock, patch
        first = self._html("https://example.com/a", '<meta http-equiv="refresh" content="0; URL=\'/b\'">')
        final = self._html("https://example.com/b", "<html><title>B</title><body>landed</body></html>")
        session = self._refresh_session(first, final)
        check = AsyncMock()
        with patch("app.solver.fast_tls.AsyncSession", return_value=session), \
             patch("app.solver.fast_tls.check_target_url_async", new=check):
            engine = FastTLSEngine()
            engine._pool_enabled = False
            challenged, solution = await engine.request("https://example.com/a", follow_meta_refresh=True)
        self.assertFalse(challenged)
        self.assertEqual(solution.url, "https://example.com/b")
        self.assertIn("https://example.com/b", [c.args[0] for c in check.await_args_list])

    async def test_meta_refresh_loop_and_hop_limit(self):
        from unittest.mock import AsyncMock, patch
        from app.solver.meta_refresh import MAX_REFRESH_HOPS
        a = self._html("https://example.com/a", '<meta http-equiv="refresh" content="0;url=/b">')
        b = self._html("https://example.com/b", '<meta http-equiv="refresh" content="0;url=/a">')
        session = self._refresh_session(a, b)
        with patch("app.solver.fast_tls.AsyncSession", return_value=session), \
             patch("app.solver.fast_tls.check_target_url_async", new=AsyncMock()):
            engine = FastTLSEngine()
            engine._pool_enabled = False
            _, solution = await engine.request("https://example.com/a", follow_meta_refresh=True)
        self.assertEqual(solution.url, "https://example.com/b")
        self.assertEqual(session.get.await_count, 2)

        chain = [self._html(f"https://example.com/{i}", f'<meta http-equiv="refresh" content="0;url=/{i + 1}">')
                 for i in range(MAX_REFRESH_HOPS + 2)]
        session = self._refresh_session(*chain)
        with patch("app.solver.fast_tls.AsyncSession", return_value=session), \
             patch("app.solver.fast_tls.check_target_url_async", new=AsyncMock()):
            engine = FastTLSEngine()
            engine._pool_enabled = False
            await engine.request("https://example.com/0", follow_meta_refresh=True)
        self.assertEqual(session.get.await_count, MAX_REFRESH_HOPS + 1)


    async def test_plain_429_is_returned_when_escalation_is_off(self):
        from unittest.mock import AsyncMock, MagicMock, patch
        from app.config import settings
        resp = MagicMock(status_code=429, text="Too many requests", cookies={},
                         headers={"content-type": "text/html", "retry-after": "60"})
        resp.url = "https://example.com/api"
        for escalate, expected in ((True, True), (False, False)):
            session = self._refresh_session(resp)
            with patch("app.solver.fast_tls.AsyncSession", return_value=session), \
                 patch("app.solver.fast_tls.check_target_url_async", new=AsyncMock()), \
                 patch.object(settings, "ESCALATE_HTTP_429", escalate):
                engine = FastTLSEngine()
                engine._pool_enabled = False
                challenged, solution = await engine.request("https://example.com/api")
            self.assertEqual(challenged, expected)
            self.assertEqual(solution.status, 429)

    async def test_429_challenge_page_still_escalates_when_escalation_is_off(self):
        from unittest.mock import AsyncMock, MagicMock, patch
        from app.config import settings
        resp = MagicMock(status_code=429, text="<html><title>Just a moment...</title></html>", cookies={},
                         headers={"content-type": "text/html"})
        resp.url = "https://example.com/"
        session = self._refresh_session(resp)
        with patch("app.solver.fast_tls.AsyncSession", return_value=session), \
             patch("app.solver.fast_tls.check_target_url_async", new=AsyncMock()), \
             patch.object(settings, "ESCALATE_HTTP_429", False):
            engine = FastTLSEngine()
            engine._pool_enabled = False
            challenged, _ = await engine.request("https://example.com/")
        self.assertTrue(challenged)


if __name__ == "__main__":
    unittest.main()
