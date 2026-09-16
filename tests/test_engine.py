import unittest
from unittest.mock import AsyncMock, patch
from app.models.flaresolverr import V1Request, SolutionModel
from app.solver.engine import HybridSolverEngine
from app.config import settings


def _sol(status=200, cookies=None, challenge_type=None):
    return SolutionModel(url="https://example.com", status=status, response="<html></html>", cookies=cookies or [], challengeType=challenge_type)


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
            # A plain scrape and a screenshot request for the same URL must
            # never share one in-flight answer - a caller that didn't ask
            # for a screenshot could otherwise get one back, or vice versa.
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


    async def test_max_timeout_bounds_entire_browser_operation(self):
        import asyncio
        from app.solver.engine import PerformanceMetrics

        observed = PerformanceMetrics()

        async def hangs(*args, **kwargs):
            await asyncio.sleep(2.0)

        with patch("app.solver.engine.metrics", observed), \
             patch("app.solver.engine.browser_pool.solve", new=AsyncMock(side_effect=hangs)), \
             patch.object(settings, "FALLBACK_PROXY_URL", "http://fallback.invalid:8080"):
            req = V1Request(cmd="request.get", url="https://example.com", forceBrowser=True, maxTimeout=1000)
            started = asyncio.get_running_loop().time()
            with self.assertRaises(TimeoutError):
                await self.engine.process_request(req)
            elapsed = asyncio.get_running_loop().time() - started

        self.assertLess(elapsed, 1.5)
        self.assertEqual(observed.failed_requests, 1)
        self.assertEqual(observed.failure_reasons["budget_exhausted"], 1)

    def test_classify_failure_uses_only_bounded_reasons(self):
        from app.solver.engine import RequestBudget, classify_failure
        budget = RequestBudget(5000)
        self.assertEqual(classify_failure(TimeoutError("operation timed out"), budget), "timeout")
        self.assertEqual(classify_failure(RuntimeError("https://secret.example"), budget), "browser_error")

    async def test_terminal_browser_failure_is_recorded_once(self):
        from app.solver.engine import PerformanceMetrics
        observed = PerformanceMetrics()
        with patch("app.solver.engine.metrics", observed), \
             patch("app.solver.engine.browser_pool.solve", new=AsyncMock(side_effect=RuntimeError("boom"))), \
             patch.object(settings, "FALLBACK_PROXY_URL", None):
            req = V1Request(cmd="request.get", url="https://example.com", forceBrowser=True)
            with self.assertRaises(RuntimeError):
                await self.engine.process_request(req)
        self.assertEqual(observed.total_requests, 1)
        self.assertEqual(observed.failed_requests, 1)
        self.assertEqual(observed.outcome_duration_histograms["failure"].count, 1)


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
