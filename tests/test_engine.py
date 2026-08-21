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


if __name__ == "__main__":
    unittest.main()
