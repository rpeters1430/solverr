import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.config import settings
from app.models.flaresolverr import CookieModel, SolutionModel, V1Request
from app.solver.engine import HybridSolverEngine


class ProwlarrHandoffTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = HybridSolverEngine()
        self.enterContext(patch("app.solver.engine.check_target_url_async", new=AsyncMock()))
        self.enterContext(patch("app.solver.engine.request_history.record", new=AsyncMock()))
        self.enterContext(patch("app.solver.engine.cookie_cache.get_cookies_with_user_agent_async", new=AsyncMock(return_value=([], None))))
        self.enterContext(patch("app.solver.engine.cookie_cache.set_cookies_async", new=AsyncMock()))
        self.enterContext(patch.object(settings, "FALLBACK_PROXY_URL", None))
        self.enterContext(patch.object(settings, "FALLBACK_PROXY_URLS", []))
        self.http = SolutionModel(url="https://indexer.test/search", status=200, response="index", headers={"content-type": "text/html"})
        self.fast = self.enterContext(patch("app.solver.engine.fast_tls_engine.request", new=AsyncMock(return_value=(False, self.http))))
        self.browser = self.enterContext(patch("app.solver.engine.browser_pool.solve", new=AsyncMock(return_value=self.http.model_copy(update={"cookies": [CookieModel(name="cf_clearance", value="token", domain="indexer.test")]}))))

    async def test_cookie_free_http_success_gets_browser_cookie_handoff(self):
        result = await self.engine.process_request(V1Request(cmd="request.get", url=self.http.url), require_cookies=True)
        self.assertEqual(result.cookies[0].value, "token")
        self.assertEqual(result.tier, "tier3_stealth_browser")
        self.browser.assert_awaited_once()

    async def test_usable_http_cookies_do_not_launch_browser(self):
        self.http.cookies = [CookieModel(name="cf_clearance", value="token", domain="indexer.test")]
        result = await self.engine.process_request(V1Request(cmd="request.get", url=self.http.url), require_cookies=True)
        self.assertEqual(result.tier, "tier1_fast_tls")
        self.browser.assert_not_awaited()

    async def test_only_exact_prowlarr_ping_can_succeed_without_cookies(self):
        for url in ("https://prowlarr.servarr.com/v1/ping", "HTTPS://PROWLARR.SERVARR.COM:443/v1/ping"):
            self.http.url = url
            self.http.response = "Pong\n"
            result = await self.engine.process_request(V1Request(cmd="request.get", url=url), require_cookies=True)
            self.assertEqual(result.tier, "tier1_fast_tls")
        self.browser.assert_not_awaited()

    async def test_lookalike_ping_and_unusable_cookies_still_require_handoff(self):
        bad = [CookieModel(name="empty", value="", domain="indexer.test"), CookieModel(name="expired", value="x", domain="indexer.test", expires=1), CookieModel(name="other", value="x", domain="other.test"), CookieModel(name="path", value="x", domain="indexer.test", path="/account")]
        for url in ("https://indexer.test/search", "https://prowlarr.servarr.com.evil.test/v1/ping", "https://prowlarr.servarr.com/v1/ping?indexer=1"):
            self.http.url = url
            self.http.response = "Pong"
            self.http.cookies = bad
            self.browser.return_value = self.http
            with self.assertRaisesRegex(ValueError, "no usable cookies"):
                await self.engine.process_request(V1Request(cmd="request.get", url=url), require_cookies=True)

    async def test_default_and_named_sessions_keep_existing_behavior(self):
        for args in ({}, {"session": "named"}):
            result = await self.engine.process_request(V1Request(cmd="request.get", url=self.http.url, **args), require_cookies=bool(args))
            self.assertEqual(result.tier, "tier1_fast_tls")
        self.browser.assert_not_awaited()

    async def test_session_setup_is_inside_v1_deadline(self):
        from app.api.flaresolverr import flaresolverr_api
        stopped = asyncio.Event()
        async def stalled(req):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        with patch("app.api.flaresolverr.apply_session", side_effect=stalled):
            result = await asyncio.wait_for(flaresolverr_api(V1Request(cmd="request.get", url=self.http.url, maxTimeout=40)), 0.5)
        self.assertEqual(result.status, "error")
        self.assertTrue(stopped.is_set())

    async def test_fallback_proxy_cannot_report_success_without_usable_cookies(self):
        from app.solver.proxy_pool import FallbackProxyPool
        for forced in (True, False):
            for cookies in ([], [CookieModel(name="expired", value="x", domain="indexer.test", expires=1)]):
                self.http.cookies = cookies
                self.browser.side_effect = [self.http] if forced else [RuntimeError("direct blocked"), self.http]
                with patch("app.solver.engine.fallback_proxy_pool", FallbackProxyPool(["http://p1.test:8080"])):
                    with self.assertRaisesRegex(ValueError, "no usable cookies"):
                        await self.engine.process_request(V1Request(cmd="request.get", url=self.http.url, forceFallbackProxy=forced), require_cookies=True)
