"""Regressions for request identity, replay and shared request deadlines."""
import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

from app.config import settings
from app.models.flaresolverr import CookieModel, SolutionModel, V1Request
from app.solver.engine import HybridSolverEngine


def solution(status=200, cookies=None):
    return SolutionModel(url="https://example.com/", status=status, response="origin", cookies=cookies or [])


def cookie(value="private"):
    return CookieModel(name="login", value=value, domain="example.com")


class RequestReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = HybridSolverEngine()
        self.enterContext(patch("app.solver.engine.check_target_url_async", new=AsyncMock()))
        self.enterContext(patch("app.solver.engine.request_history.record", new=AsyncMock()))
        self.lookup = self.enterContext(patch("app.solver.engine.cookie_cache.get_cookies_with_user_agent_async", new=AsyncMock(return_value=([cookie("shared")], None))))
        self.write = self.enterContext(patch("app.solver.engine.cookie_cache.set_cookies_async", new=AsyncMock()))
        self.enterContext(patch.object(settings, "ENABLE_FAST_TLS", True))
        self.enterContext(patch.object(settings, "FALLBACK_PROXY_URL", None))
        self.enterContext(patch.object(settings, "FALLBACK_PROXY_URLS", []))

    async def test_ambiguous_post_failure_never_replays_in_browser(self):
        browser = self.enterContext(patch("app.solver.engine.browser_pool.solve", new=AsyncMock(return_value=solution())))
        self.enterContext(patch("app.solver.engine.fast_tls_engine.request", new=AsyncMock(return_value=(True, None))))
        with self.assertRaisesRegex(RuntimeError, "POST.*replay"):
            await self.engine.process_request(V1Request(cmd="request.post", url="https://example.com/", postData="a=1"))
        browser.assert_not_awaited()

    async def test_origin_post_500_is_returned_without_replay(self):
        self.enterContext(patch("app.solver.engine.fast_tls_engine.request", new=AsyncMock(return_value=(False, solution(500)))))
        browser = self.enterContext(patch("app.solver.engine.browser_pool.solve", new=AsyncMock(return_value=solution())))
        result = await self.engine.process_request(V1Request(cmd="request.post", url="https://example.com/", postData="a=1"))
        self.assertEqual(result.status, 500)
        browser.assert_not_awaited()

    async def test_post_application_reply_with_widget_is_not_replayed(self):
        self.enterContext(patch("app.solver.engine.fast_tls_engine.request", new=AsyncMock(return_value=(True, solution(200)))))
        browser = self.enterContext(patch("app.solver.engine.browser_pool.solve", new=AsyncMock(return_value=solution())))
        result = await self.engine.process_request(V1Request(cmd="request.post", url="https://example.com/", postData="a=1"))
        self.assertEqual(result.response, "origin")
        browser.assert_not_awaited()

    async def test_private_requests_neither_read_nor_write_shared_clearance(self):
        fast = self.enterContext(patch("app.solver.engine.fast_tls_engine.request", new=AsyncMock(return_value=(False, solution(cookies=[cookie()])))))
        for identity in ({"headers": {"aUtHoRiZaTiOn": "Bearer secret"}}, {"headers": {"Cookie": "login=private"}}, {"cookies": [cookie()]}, {"session": "my-session"}):
            with self.subTest(identity=list(identity)):
                result = await self.engine.process_request(V1Request(cmd="request.get", url="https://example.com/", **identity))
                self.assertEqual(result.cookies[0].value, "private")
                self.assertNotIn("shared", [c.value for c in fast.await_args.kwargs["cookies"]])
        self.lookup.assert_not_awaited()
        self.write.assert_not_awaited()

    async def test_authorization_cannot_be_silently_dropped_by_browser(self):
        self.enterContext(patch("app.solver.engine.fast_tls_engine.request", new=AsyncMock(return_value=(True, solution(403)))))
        browser = self.enterContext(patch("app.solver.engine.browser_pool.solve", new=AsyncMock(return_value=solution())))
        with self.assertRaisesRegex(ValueError, "Authorization"):
            await self.engine.process_request(V1Request(cmd="request.get", url="https://example.com/", headers={"Authorization": "Bearer secret"}))
        browser.assert_not_awaited()

    async def test_browser_queue_and_work_cannot_exceed_request_deadline(self):
        stopped = asyncio.Event()
        async def stalled(**kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        self.enterContext(patch("app.solver.engine.browser_pool.solve", side_effect=stalled))
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(self.engine.process_request(V1Request(cmd="request.get", url="https://example.com/", forceBrowser=True, maxTimeout=40)), timeout=0.5)
        self.assertLess(time.monotonic() - start, 0.3)
        self.assertTrue(stopped.is_set())

    async def test_fallback_proxy_receives_reserved_time(self):
        from app.solver.proxy_pool import FallbackProxyPool
        self.enterContext(patch("app.solver.engine.fallback_proxy_pool", FallbackProxyPool(["http://proxy.test:8080"])))
        attempts = []
        async def browser(**kwargs):
            attempts.append(kwargs)
            if kwargs["proxy"] is None:
                raise RuntimeError("direct blocked")
            return solution()
        self.enterContext(patch("app.solver.engine.browser_pool.solve", side_effect=browser))
        result = await self.engine.process_request(V1Request(cmd="request.get", url="https://example.com/", forceBrowser=True, maxTimeout=30000))
        self.assertEqual(result.tier, "tier4_fallback_proxy")
        self.assertLessEqual(attempts[0]["timeout_ms"], 21000)
        self.assertGreater(attempts[1]["timeout_ms"], 2000)


class BrowserReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_post_is_submitted_once_without_fingerprint_retry(self):
        from app.solver.browser import BrowserPool
        pool = BrowserPool()
        # A pool exists, but unsafe requests must use one owned attempt.
        pool.camoufox_pool = object()
        with patch("app.solver.browser.browser.CAMOUFOX_AVAILABLE", True), \
             patch.object(pool, "_solve_with_pooled_camoufox", new=AsyncMock(return_value=solution(503))) as pooled, \
             patch.object(pool, "_solve_with_ephemeral_camoufox", new=AsyncMock(side_effect=RuntimeError("lost reply"))) as fresh:
            with self.assertRaises(RuntimeError):
                await pool.solve("https://example.com/", method="POST", post_data="a=1")
        pooled.assert_not_awaited()
        fresh.assert_awaited_once()


class CloudflarePrecisionTests(unittest.TestCase):
    def test_verification_prose_and_inert_examples_are_not_challenges(self):
        from app.solver.browser import detect_challenge, is_challenge_wall, is_challenge_title
        for title in ("Just a moment in history", "Checking your browser settings", "Attention required for your order"):
            self.assertFalse(is_challenge_title(title), title)
            self.assertIsNone(detect_challenge(title, "<p>A real article</p>", True), title)
        marker = '<div id="challenge-form">_cf_chl_opt cf-turnstile</div>'
        for content in (f'<!--{marker}-->', f'<template>{marker}</template>', f'<textarea>{marker}</textarea>', '<pre><code>&lt;script src="https://challenges.cloudflare.com/turnstile/v0/api.js"&gt;</code></pre>', '<p>Please wait just a moment... while checking your browser settings.</p>'):
            self.assertIsNone(detect_challenge("Article", content, True), content)
            self.assertFalse(is_challenge_wall("cloudflare_turnstile", "Article", content, 200), content)

    def test_active_cloudflare_wall_and_header_remain_detected(self):
        from app.solver.browser import detect_challenge, is_challenge_wall
        html = '<script>window._cf_chl_opt = {a:1};</script><form id="challenge-form"></form>'
        self.assertEqual(detect_challenge("", html, True), "cloudflare_turnstile")
        self.assertTrue(is_challenge_wall("cloudflare_turnstile", "", html, 200))
        self.assertEqual(detect_challenge("", "", True, {"CF-Mitigated": "challenge"}, 403), "cloudflare_turnstile")


class TLSIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_http_requests_do_not_reuse_anonymous_cookie_jar(self):
        from types import SimpleNamespace
        from app.solver.fast_tls import FastTLSEngine
        engine = FastTLSEngine()
        pooled = AsyncMock()
        fresh = AsyncMock()
        response = SimpleNamespace(url="https://example.com/", status_code=200, text="ok", headers={"content-type": "text/plain"}, cookies={})
        pooled.get.return_value = fresh.get.return_value = response
        with patch.object(engine, "_get_session", new=AsyncMock(return_value=pooled)), \
             patch("app.solver.fast_tls.AsyncSession", return_value=fresh), \
             patch("app.solver.fast_tls.check_target_url_async", new=AsyncMock()):
            await engine.request("https://example.com/", headers={"Authorization": "Bearer secret"})
        pooled.get.assert_not_awaited()
        fresh.get.assert_awaited_once()
        fresh.close.assert_awaited_once()


class ProxyFailureDetectionTests(unittest.IsolatedAsyncioTestCase):
    setUp = RequestReliabilityTests.setUp
    async def test_generated_proxy_failure_is_not_scraped_content(self):
        upstream = solution(502)
        upstream.headers = {"Proxy-Status": "Solverr; error=http_request_error"}
        self.enterContext(patch("app.solver.engine.fast_tls_engine.request", new=AsyncMock(return_value=(False, upstream))))
        browser = self.enterContext(patch("app.solver.engine.browser_pool.solve", new=AsyncMock(return_value=solution())))
        with self.assertRaisesRegex(RuntimeError, "proxy"):
            await self.engine.process_request(V1Request(cmd="request.get", url="https://example.com/"))
        browser.assert_not_awaited()

    async def test_cookie_header_is_available_to_browser_as_scoped_cookies(self):
        self.enterContext(patch("app.solver.engine.fast_tls_engine.request", new=AsyncMock(return_value=(True, solution(403)))))
        browser = self.enterContext(patch("app.solver.engine.browser_pool.solve", new=AsyncMock(return_value=solution())))
        await self.engine.process_request(V1Request(cmd="request.get", url="https://example.com/", headers={"Cookie": "login=secret"}))
        self.assertEqual({c.name: c.value for c in browser.await_args.kwargs["cookies"]}, {"login": "secret"})


class HeadTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_head_uses_head_upstream(self):
        from types import SimpleNamespace
        from app.solver.fast_tls import FastTLSEngine
        response = SimpleNamespace(url="https://example.com/", status_code=200, text="", headers={"content-type": "text/plain"}, cookies={})
        session = AsyncMock()
        session.head.return_value = session.get.return_value = response
        with patch("app.solver.fast_tls.AsyncSession", return_value=session), patch("app.solver.fast_tls.check_target_url_async", new=AsyncMock()):
            result = await FastTLSEngine().request("https://example.com/", method="HEAD")
        self.assertEqual(result[1].status, 200)
        session.head.assert_awaited_once()
        session.get.assert_not_awaited()


class ReviewReplayTests(unittest.IsolatedAsyncioTestCase):
    setUp = RequestReliabilityTests.setUp

    async def test_forced_fallback_post_never_tries_a_second_proxy(self):
        from app.solver.proxy_pool import FallbackProxyPool
        self.enterContext(patch("app.solver.engine.fallback_proxy_pool", FallbackProxyPool(["http://p1.test:8080", "http://p2.test:8080"])))
        browser = self.enterContext(patch("app.solver.engine.browser_pool.solve", new=AsyncMock(side_effect=[RuntimeError("lost POST reply"), solution()])))
        with self.assertRaisesRegex(RuntimeError, "lost POST reply"):
            await self.engine.process_request(V1Request(cmd="request.post", url="https://example.com/", postData="a=1", forceFallbackProxy=True))
        self.assertEqual(browser.await_count, 1)

    async def test_post_redirect_to_blocked_confirmation_does_not_resubmit_post(self):
        from types import SimpleNamespace
        from app.solver.fast_tls import FastTLSEngine
        transport = FastTLSEngine()
        transport._pool_enabled = False
        session = AsyncMock()
        session.post.return_value = SimpleNamespace(url="https://example.com/", status_code=303, text="", headers={"location": "/confirmation"}, cookies={})
        session.get.return_value = SimpleNamespace(url="https://example.com/confirmation", status_code=403, text='<script>window._cf_chl_opt={};</script>', headers={"content-type": "text/html", "cf-mitigated": "challenge"}, cookies={})
        self.enterContext(patch("app.solver.fast_tls.AsyncSession", return_value=session))
        self.enterContext(patch("app.solver.fast_tls.check_target_url_async", new=AsyncMock()))
        self.enterContext(patch("app.solver.engine.fast_tls_engine.request", side_effect=transport.request))
        browser = self.enterContext(patch("app.solver.engine.browser_pool.solve", new=AsyncMock(return_value=solution())))
        with self.assertRaisesRegex(RuntimeError, "replay"):
            await self.engine.process_request(V1Request(cmd="request.post", url="https://example.com/", postData="a=1"))
        session.post.assert_awaited_once()
        browser.assert_not_awaited()
