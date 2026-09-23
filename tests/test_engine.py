import unittest
from unittest.mock import AsyncMock, patch
from app.models.flaresolverr import V1Request, SolutionModel, CookieModel
from app.solver.engine import HybridSolverEngine
from app.config import settings


def _sol(status=200, cookies=None, challenge_type=None):
    return SolutionModel(url="https://example.com", status=status, response="<html></html>", cookies=cookies or [], challengeType=challenge_type)


def _cookie(name, value, domain, path="/"):
    return CookieModel(name=name, value=value, domain=domain, path=path, expires=-1, size=len(name) + len(value), httpOnly=False, secure=False, session=False, sameSite="Lax")


class TestHybridSolverEngine(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = HybridSolverEngine()

    async def test_fast_tls_success_skips_browser(self):
        with patch("app.solver.engine.fast_tls_engine.request", new=AsyncMock(return_value=(False, _sol(200)))) as fast_mock, \
             patch("app.solver.engine.browser_pool.solve", new=AsyncMock()) as browser_mock:
            req = V1Request(cmd="request.get", url="https://example.com")
            sol = await self.engine.process_request(req)
            self.assertEqual(sol.status, 200)
            fast_mock.assert_called_once()
            browser_mock.assert_not_called()

    async def test_fast_tls_challenge_escalates_to_browser(self):
        with patch("app.solver.engine.fast_tls_engine.request", new=AsyncMock(return_value=(True, _sol(503)))), \
             patch("app.solver.engine.browser_pool.solve", new=AsyncMock(return_value=_sol(200, challenge_type="cloudflare_turnstile"))) as browser_mock:
            req = V1Request(cmd="request.get", url="https://example.com")
            sol = await self.engine.process_request(req)
            self.assertEqual(sol.status, 200)
            self.assertEqual(sol.challengeType, "cloudflare_turnstile")
            browser_mock.assert_called_once()

    async def test_force_browser_skips_fast_tls(self):
        with patch("app.solver.engine.fast_tls_engine.request", new=AsyncMock()) as fast_mock, \
             patch("app.solver.engine.browser_pool.solve", new=AsyncMock(return_value=_sol(200))) as browser_mock:
            req = V1Request(cmd="request.get", url="https://example.com", forceBrowser=True)
            sol = await self.engine.process_request(req)
            self.assertEqual(sol.status, 200)
            fast_mock.assert_not_called()
            browser_mock.assert_called_once()

    async def test_fast_tls_only_returns_without_escalating_even_on_challenge(self):
        with patch("app.solver.engine.fast_tls_engine.request", new=AsyncMock(return_value=(True, _sol(503)))), \
             patch("app.solver.engine.browser_pool.solve", new=AsyncMock()) as browser_mock:
            req = V1Request(cmd="request.get", url="https://example.com", fastTlsOnly=True)
            sol = await self.engine.process_request(req)
            self.assertEqual(sol.status, 503)
            browser_mock.assert_not_called()

    async def test_browser_failure_escalates_to_fallback_proxy(self):
        with patch("app.solver.engine.fast_tls_engine.request", new=AsyncMock(return_value=(True, None))), \
             patch("app.solver.engine.browser_pool.solve", new=AsyncMock(side_effect=[RuntimeError("boom"), _sol(200)])) as browser_mock, \
             patch.object(settings, "FALLBACK_PROXY_URL", "http://fallback.proxy:8080"):
            req = V1Request(cmd="request.get", url="https://example.com")
            sol = await self.engine.process_request(req)
            self.assertEqual(sol.status, 200)
            self.assertEqual(browser_mock.call_count, 2)

    async def test_browser_failure_without_fallback_proxy_raises(self):
        with patch("app.solver.engine.fast_tls_engine.request", new=AsyncMock(return_value=(True, None))), \
             patch("app.solver.engine.browser_pool.solve", new=AsyncMock(side_effect=RuntimeError("boom"))), \
             patch.object(settings, "FALLBACK_PROXY_URL", None):
            req = V1Request(cmd="request.get", url="https://example.com")
            with self.assertRaises(RuntimeError):
                await self.engine.process_request(req)

    async def test_concurrent_identical_requests_are_coalesced(self):
        import asyncio

        async def slow_browser_solve(*args, **kwargs):
            await asyncio.sleep(0.05)
            return _sol(200)

        with patch("app.solver.engine.fast_tls_engine.request", new=AsyncMock(return_value=(True, None))), \
             patch("app.solver.engine.browser_pool.solve", new=AsyncMock(side_effect=slow_browser_solve)) as browser_mock:
            req = V1Request(cmd="request.get", url="https://example.com/dedup")
            results = await asyncio.gather(
                self.engine.process_request(req),
                self.engine.process_request(req),
                self.engine.process_request(req),
            )
            self.assertTrue(all(r.status == 200 for r in results))
            browser_mock.assert_called_once()

    async def test_concurrent_requests_differing_only_by_screenshot_are_not_coalesced(self):
        import asyncio

        async def slow_browser_solve(*args, **kwargs):
            await asyncio.sleep(0.05)
            return _sol(200)

        with patch("app.solver.engine.fast_tls_engine.request", new=AsyncMock(return_value=(True, None))), \
             patch("app.solver.engine.browser_pool.solve", new=AsyncMock(side_effect=slow_browser_solve)) as browser_mock:
            req_plain = V1Request(cmd="request.get", url="https://example.com/dedup-screenshot", forceBrowser=True, screenshot=False)
            req_screenshot = V1Request(cmd="request.get", url="https://example.com/dedup-screenshot", forceBrowser=True, screenshot=True)
            await asyncio.gather(
                self.engine.process_request(req_plain),
                self.engine.process_request(req_screenshot),
            )
            # Screenshot and plain requests for one URL must not coalesce.
            self.assertEqual(browser_mock.call_count, 2)

    async def test_concurrent_requests_differing_only_by_max_timeout_are_not_coalesced(self):
        import asyncio

        async def slow_browser_solve(*args, **kwargs):
            await asyncio.sleep(0.05)
            return _sol(200)

        with patch("app.solver.engine.fast_tls_engine.request", new=AsyncMock(return_value=(True, None))), \
             patch("app.solver.engine.browser_pool.solve", new=AsyncMock(side_effect=slow_browser_solve)) as browser_mock:
            req_short = V1Request(cmd="request.get", url="https://example.com/dedup-timeout", forceBrowser=True, maxTimeout=5000)
            req_long = V1Request(cmd="request.get", url="https://example.com/dedup-timeout", forceBrowser=True, maxTimeout=60000)
            await asyncio.gather(
                self.engine.process_request(req_short),
                self.engine.process_request(req_long),
            )
            # A caller's requested timeout budget must not be silently
            # inherited from a concurrent request for the same URL.
            self.assertEqual(browser_mock.call_count, 2)

    async def test_concurrent_requests_collapse_retry_after_shared_failure(self):
        import asyncio
        call_count = {"n": 0}

        async def flaky_solve(*args, **kwargs):
            call_count["n"] += 1
            await asyncio.sleep(0.02)
            if call_count["n"] == 1:
                raise RuntimeError("boom")
            return _sol(200)

        with patch("app.solver.engine.fast_tls_engine.request", new=AsyncMock(return_value=(True, None))), \
             patch("app.solver.engine.browser_pool.solve", new=AsyncMock(side_effect=flaky_solve)), \
             patch.object(settings, "FALLBACK_PROXY_URL", None):
            req = V1Request(cmd="request.get", url="https://example.com/thundering-herd")
            results = await asyncio.gather(
                *[self.engine.process_request(req) for _ in range(5)],
                return_exceptions=True,
            )
            # The shared failure must trigger exactly one retry, not one
            # independent retry per coalesced waiter (thundering herd).
            self.assertEqual(call_count["n"], 2)
            # The request that failed sees its failure; every joiner shares a single retry.
            errors = [r for r in results if isinstance(r, Exception)]
            successes = [r for r in results if not isinstance(r, Exception)]
            self.assertEqual(len(errors), 1)
            self.assertEqual(len(successes), 4)
            self.assertTrue(all(r.status == 200 for r in successes))

    async def test_cookie_merge_keys_by_domain_path_name_not_name_alone(self):
        input_cookie = _cookie("session", "input-value", domain="other.example.com")
        cached_cookie = _cookie("session", "cached-value", domain="example.com")
        with patch("app.solver.engine.cookie_cache.get_cookies_async", new=AsyncMock(return_value=[cached_cookie])), \
             patch("app.solver.engine.fast_tls_engine.request", new=AsyncMock(return_value=(False, _sol(200)))) as fast_mock, \
             patch("app.solver.engine.browser_pool.solve", new=AsyncMock()):
            req = V1Request(cmd="request.get", url="https://example.com", cookies=[input_cookie])
            await self.engine.process_request(req)
            called_cookies = fast_mock.call_args.kwargs["cookies"]
            seen = {(c.domain, c.name, c.value) for c in called_cookies}
            # Same-name cookies on different domains must both survive the merge.
            self.assertIn(("other.example.com", "session", "input-value"), seen)
            self.assertIn(("example.com", "session", "cached-value"), seen)

    async def test_request_budget_propagates_remaining_timeout_to_browser(self):
        with patch("app.solver.engine.fast_tls_engine.request", new=AsyncMock(return_value=(True, None))), \
             patch("app.solver.engine.browser_pool.solve", new=AsyncMock(return_value=_sol(200))) as browser_mock:
            req = V1Request(cmd="request.get", url="https://example.com", maxTimeout=20000)
            await self.engine.process_request(req)
            browser_mock.assert_called_once()
            called_kwargs = browser_mock.call_args.kwargs
            # Timeout passed to browser should be positive and <= 20000
            self.assertGreater(called_kwargs["timeout_ms"], 0)
            self.assertLessEqual(called_kwargs["timeout_ms"], 20000)


class TestRequestBudget(unittest.TestCase):
    def test_budget_properties(self):
        from app.solver.engine import RequestBudget
        import time
        budget = RequestBudget(5000)
        self.assertAlmostEqual(budget.total_timeout_s, 5.0, places=1)
        self.assertGreater(budget.remaining_s, 4.0)
        self.assertGreater(budget.remaining_ms, 4000)
        self.assertFalse(budget.is_expired)
        self.assertGreaterEqual(budget.elapsed_ms, 0.0)


if __name__ == "__main__":
    unittest.main()
