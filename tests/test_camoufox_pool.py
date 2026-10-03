import asyncio
import time
import unittest
from unittest.mock import patch
from app.solver.browser import BrowserPool, CamoufoxPool, _PooledCamoufox
from app.solver.browser import browser as browser_module
from app.config import settings
from app.models.flaresolverr import SolutionModel


class FakeCamoufoxPool(CamoufoxPool):
    """CamoufoxPool with the real AsyncCamoufox launch/close swapped for
    cheap fakes, so pool bookkeeping (acquire/release/recycle/capacity) can
    be tested without spawning a real browser process."""

    def __init__(self, size):
        super().__init__(size)
        self.launch_count = 0
        self.close_count = 0

    async def _launch_instance(self) -> _PooledCamoufox:
        self.launch_count += 1
        return _PooledCamoufox(cm=object(), browser=f"fake-browser-{self.launch_count}", created_at=__import__("time").time())

    async def _close_instance(self, inst: _PooledCamoufox):
        self.close_count += 1


class TestCamoufoxPool(unittest.IsolatedAsyncioTestCase):
    async def test_pool_grows_lazily_up_to_size(self):
        pool = FakeCamoufoxPool(3)
        self.assertEqual(pool.launch_count, 0)
        inst1 = await pool.acquire()
        self.assertEqual(pool.launch_count, 1)
        inst2 = await pool.acquire()
        self.assertEqual(pool.launch_count, 2)
        self.assertNotEqual(inst1.browser, inst2.browser)

    async def test_released_instance_is_reused_not_recreated(self):
        with patch.object(settings, "CAMOUFOX_POOL_RECYCLE_USES", 100), \
             patch.object(settings, "CAMOUFOX_POOL_RECYCLE_SECONDS", 100000):
            pool = FakeCamoufoxPool(2)
            inst = await pool.acquire()
            await pool.release(inst)
            self.assertEqual(pool.launch_count, 1)
            reused = await pool.acquire()
            self.assertEqual(pool.launch_count, 1)
            self.assertEqual(reused.browser, inst.browser)

    async def test_instance_recycled_after_use_budget_exhausted(self):
        with patch.object(settings, "CAMOUFOX_POOL_RECYCLE_USES", 2), \
             patch.object(settings, "CAMOUFOX_POOL_RECYCLE_SECONDS", 100000):
            pool = FakeCamoufoxPool(1)
            inst = await pool.acquire()  # uses=1
            await pool.release(inst)
            inst = await pool.acquire()  # uses=2, hits budget
            await pool.release(inst)     # should recycle: close old, launch new
            self.assertEqual(pool.close_count, 1)
            self.assertEqual(pool.launch_count, 2)

    async def test_acquire_blocks_at_capacity_until_release(self):
        pool = FakeCamoufoxPool(1)
        inst = await pool.acquire()
        self.assertEqual(pool.launch_count, 1)

        acquired_second = asyncio.Event()

        async def acquire_second():
            await pool.acquire()
            acquired_second.set()

        task = asyncio.create_task(acquire_second())
        await asyncio.sleep(0.05)
        self.assertFalse(acquired_second.is_set())  # still waiting, pool at capacity

        await pool.release(inst)
        await asyncio.wait_for(acquired_second.wait(), timeout=1.0)
        self.assertEqual(pool.launch_count, 1)  # reused, no new launch
        task.cancel()

    async def test_close_drains_and_closes_idle_instances(self):
        pool = FakeCamoufoxPool(3)
        i1 = await pool.acquire()
        i2 = await pool.acquire()
        await pool.release(i1)
        await pool.release(i2)
        await pool.close()
        self.assertEqual(pool.close_count, 2)
        self.assertEqual(pool._created, 0)


class FakePage:
    async def evaluate(self, script):
        return "fake-ua"

    async def close(self):
        pass


class FakeContext:
    async def new_page(self):
        return FakePage()

    async def close(self):
        pass


class FakeBrowser:
    contexts = []
    context_kwargs: list = []

    async def new_context(self, **kwargs):
        FakeBrowser.context_kwargs.append(kwargs)
        return FakeContext()


class FakeCamoufoxPoolWithBrowser(FakeCamoufoxPool):
    async def _launch_instance(self) -> _PooledCamoufox:
        self.launch_count += 1
        return _PooledCamoufox(cm=object(), browser=FakeBrowser(), created_at=time.time())


class FakeAsyncCamoufoxCtx:
    """Records the kwargs it was launched with and hands back a FakeBrowser,
    so tests can assert on what BrowserPool actually passes to Camoufox
    without spawning a real browser process."""

    captured_kwargs: list = []

    def __init__(self, **kwargs):
        FakeAsyncCamoufoxCtx.captured_kwargs.append(kwargs)

    async def __aenter__(self):
        return FakeBrowser()

    async def __aexit__(self, exc_type, exc, tb):
        return False


class TestEphemeralCamoufoxClose(unittest.IsolatedAsyncioTestCase):
    """A slow Camoufox shutdown must not delay the response past the caller's budget."""

    async def test_result_returns_before_slow_close_finishes(self):
        closed = asyncio.Event()

        class SlowCloseCtx(FakeAsyncCamoufoxCtx):
            async def __aexit__(self, exc_type, exc, tb):
                await asyncio.sleep(0.3)
                closed.set()
                return False

        pool = BrowserPool()
        with patch("app.solver.browser.browser.AsyncCamoufox", SlowCloseCtx), \
             patch.object(pool, "_execute_solve_flow", return_value="ok"):
            start = time.monotonic()
            result = await pool._solve_with_ephemeral_camoufox(
                url="https://example.com", method="GET", post_data=None, cookies=None,
                pw_proxy=None, user_agent=None, timeout_ms=5000, active_ua="fake-ua",
                headers=None, start_time=time.time(), wait_selector=None,
                wait_delay_ms=None, capture_screenshot=False,
            )
            self.assertEqual(result, "ok")
            self.assertLess(time.monotonic() - start, 0.2)
            self.assertFalse(closed.is_set())
            await pool.close()  # close() drains background shutdowns
        self.assertTrue(closed.is_set())


class TestEphemeralCamoufoxGeoip(unittest.IsolatedAsyncioTestCase):
    """A request carrying its own proxy (including Tier 4 fallback-proxy
    escalation, which re-enters this same path) should have Camoufox derive
    timezone/locale/geolocation/WebRTC from that proxy's real exit IP via
    the `geoip` launch option - otherwise the browser fingerprint can
    contradict the proxy's IP, which is exactly the mismatch WAFs look for."""

    def setUp(self):
        FakeAsyncCamoufoxCtx.captured_kwargs = []
        patcher = patch("app.solver.browser.browser.AsyncCamoufox", FakeAsyncCamoufoxCtx)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _solve(self, pw_proxy):
        pool = BrowserPool()
        with patch.object(pool, "_execute_solve_flow", return_value="ok"):
            return await pool._solve_with_ephemeral_camoufox(
                url="https://example.com", method="GET", post_data=None, cookies=None,
                pw_proxy=pw_proxy, user_agent=None, timeout_ms=5000, active_ua="fake-ua",
                headers=None, start_time=time.time(), wait_selector=None,
                wait_delay_ms=None, capture_screenshot=False
            )

    async def test_geoip_enabled_when_proxy_set(self):
        await self._solve(pw_proxy={"server": "http://proxy.example.com:8080"})
        self.assertTrue(FakeAsyncCamoufoxCtx.captured_kwargs[-1]["geoip"])

    async def test_geoip_disabled_without_proxy(self):
        await self._solve(pw_proxy=None)
        self.assertFalse(FakeAsyncCamoufoxCtx.captured_kwargs[-1]["geoip"])

    async def test_geoip_respects_config_toggle(self):
        with patch.object(settings, "CAMOUFOX_GEOIP_ON_PROXY", False):
            await self._solve(pw_proxy={"server": "http://proxy.example.com:8080"})
        self.assertFalse(FakeAsyncCamoufoxCtx.captured_kwargs[-1]["geoip"])


class TestEphemeralCamoufoxUserAgent(unittest.IsolatedAsyncioTestCase):
    """cf_clearance is bound to the UA that earned it, so the solution must
    report the UA the page actually sent - not DEFAULT_USER_AGENT, which
    Camoufox never uses - or callers replay the cookie with the wrong UA."""

    def setUp(self):
        FakeBrowser.context_kwargs = []
        patcher = patch("app.solver.browser.browser.AsyncCamoufox", FakeAsyncCamoufoxCtx)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _solve(self, user_agent):
        pool = BrowserPool()
        captured = {}

        async def fake_flow(**kwargs):
            captured.update(kwargs)
            return "ok"

        with patch.object(pool, "_execute_solve_flow", side_effect=fake_flow):
            await pool._solve_with_ephemeral_camoufox(
                url="https://example.com", method="GET", post_data=None, cookies=None,
                pw_proxy=None, user_agent=user_agent, timeout_ms=5000, active_ua="default-ua",
                headers=None, start_time=time.time(), wait_selector=None,
                wait_delay_ms=None, capture_screenshot=False
            )
        return captured

    async def test_reports_real_navigator_user_agent(self):
        captured = await self._solve(user_agent=None)
        self.assertEqual(captured["active_ua"], "fake-ua")
        self.assertNotIn("user_agent", FakeBrowser.context_kwargs[-1])

    async def test_unreadable_ua_is_not_reported_as_the_default(self):
        async def failing_evaluate(self, script):
            raise RuntimeError("page crashed")

        with patch.object(FakePage, "evaluate", failing_evaluate):
            captured = await self._solve(user_agent=None)
        self.assertEqual(captured["active_ua"], "")

    async def test_unreadable_ua_keeps_the_pinned_one(self):
        async def failing_evaluate(self, script):
            raise RuntimeError("page crashed")

        with patch.object(FakePage, "evaluate", failing_evaluate):
            captured = await self._solve(user_agent="custom-ua")
        self.assertEqual(captured["active_ua"], "custom-ua")

    async def test_custom_user_agent_is_applied_to_context(self):
        await self._solve(user_agent="custom-ua")
        self.assertEqual(FakeBrowser.context_kwargs[-1]["user_agent"], "custom-ua")


class TestEphemeralRetryBudget(unittest.IsolatedAsyncioTestCase):
    """The ephemeral retry after a failed pooled attempt must get only the
    remaining budget, not a second full timeout (which doubled worst-case
    latency past the caller's maxTimeout)."""

    async def test_retry_gets_remaining_budget(self):
        pool = BrowserPool()
        pool.camoufox_pool = FakeCamoufoxPoolWithBrowser(1)
        retry_timeouts = []

        async def slow_pooled(**kwargs):
            await asyncio.sleep(0.2)
            raise RuntimeError("challenge never cleared")

        async def ephemeral(**kwargs):
            retry_timeouts.append(kwargs["timeout_ms"])
            return type("Sol", (), {"status": 200})()

        with patch.object(pool, "_solve_with_pooled_camoufox", side_effect=slow_pooled), \
             patch.object(pool, "_solve_with_ephemeral_camoufox", side_effect=ephemeral):
            await pool.solve("https://example.com", timeout_ms=10000)
        self.assertEqual(len(retry_timeouts), 1)
        self.assertLessEqual(retry_timeouts[0], 9800)

    async def test_retry_skipped_when_budget_exhausted(self):
        pool = BrowserPool()
        pool.camoufox_pool = FakeCamoufoxPoolWithBrowser(1)

        async def slow_pooled(**kwargs):
            await asyncio.sleep(0.1)
            raise RuntimeError("challenge never cleared")

        with patch.object(pool, "_solve_with_pooled_camoufox", side_effect=slow_pooled), \
             patch.object(pool, "_solve_with_ephemeral_camoufox") as ephemeral:
            with self.assertRaises(RuntimeError):
                await pool.solve("https://example.com", timeout_ms=3000)
        ephemeral.assert_not_called()

    async def test_pooled_attempt_leaves_reserve_for_ephemeral_retry(self):
        """A pooled attempt that never clears must not spend the whole budget, or
        the fresh-fingerprint retry is always skipped."""
        pool = BrowserPool()
        pool.camoufox_pool = FakeCamoufoxPoolWithBrowser(1)
        seen = {}

        async def pooled(**kwargs):
            seen["pooled"] = kwargs["timeout_ms"]
            return SolutionModel(url="https://example.com", status=403, cookies=[], userAgent="ua")

        async def ephemeral(**kwargs):
            seen["ephemeral"] = kwargs["timeout_ms"]
            return SolutionModel(url="https://example.com", status=200, cookies=[], userAgent="ua")

        with patch.object(pool, "_solve_with_pooled_camoufox", side_effect=pooled), \
             patch.object(pool, "_solve_with_ephemeral_camoufox", side_effect=ephemeral):
            sol = await pool.solve("https://example.com", timeout_ms=50000)
        self.assertEqual(sol.status, 200)
        self.assertEqual(seen["pooled"], 30000)
        self.assertGreaterEqual(seen["ephemeral"], 49000)

    def test_pooled_budget_split(self):
        self.assertEqual(browser_module._pooled_attempt_budget_ms(60000), 40000)
        self.assertEqual(browser_module._pooled_attempt_budget_ms(50000), 30000)
        # Too small to split usefully: the pooled attempt keeps everything.
        self.assertEqual(browser_module._pooled_attempt_budget_ms(10000), 10000)


class TestSolveFlowDeadline(unittest.IsolatedAsyncioTestCase):
    """The challenge loop shares the attempt's deadline with navigation instead of
    getting a fresh full timeout after it."""

    async def test_loop_stops_at_attempt_deadline(self):
        pool = BrowserPool()

        class ChallengePage(FakePage):
            url = "https://example.com"
            main_frame = object()
            frames = []

            def on(self, *args):
                pass

            async def title(self):
                return "Just a moment..."

            async def content(self):
                return "<html><body>cf-turnstile" + "x" * 400 + "</body></html>"

        class Ctx(FakeContext):
            async def cookies(self, *args, **kwargs):
                return []

        async def slow_nav(*args, **kwargs):
            await asyncio.sleep(0.5)
            return None, 403

        start = time.monotonic()
        with patch.object(browser_module, "navigate_to_target", side_effect=slow_nav), \
             patch.object(browser_module, "install_media_blocking", return_value=None), \
             patch.object(browser_module, "dispatch_challenge_click", return_value=(False, False)), \
             patch.object(browser_module, "SOLVE_FINALIZE_RESERVE_SECONDS", 0.2):
            sol = await pool._execute_solve_flow(
                context=Ctx(), page=ChallengePage(), url="https://example.com", method="GET",
                post_data=None, cookies=None, timeout_ms=1500, active_ua="ua",
                headers=None, start_time=start, deadline=start + 1.5,
            )
        # Old behaviour: 0.5s navigation + a fresh 1.5s loop + finalisation.
        self.assertLess(time.monotonic() - start, 2.0)
        self.assertEqual(sol.status, 503)


class TestBrowserPoolCancellation(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_after_setup_recycles_browser(self):
        pool = BrowserPool()
        fake = FakeCamoufoxPoolWithBrowser(1)
        pool.camoufox_pool = fake

        async def disconnect(*args, **kwargs):
            raise RuntimeError("Target page, context or browser has been closed")

        with patch.object(pool, "_execute_solve_flow", side_effect=disconnect):
            with self.assertRaises(RuntimeError):
                await pool._solve_with_pooled_camoufox(
                    url="https://example.com", method="GET", post_data=None, cookies=None,
                    timeout_ms=5000, headers=None, start_time=time.time(),
                    wait_selector=None, wait_delay_ms=None, capture_screenshot=False,
                )
        self.assertEqual(fake.close_count, 1)
        self.assertEqual(fake.launch_count, 2)
        self.assertEqual(pool.pool_stats()["oldest_checkout_seconds"], 0)

    async def test_queue_depth_tracks_waiter(self):
        pool = BrowserPool()
        pool.semaphore = asyncio.Semaphore(0)
        task = asyncio.create_task(pool.solve("https://example.com"))
        await asyncio.sleep(0)
        self.assertEqual(pool.pool_stats()["queue_depth"], 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(pool.pool_stats()["queue_depth"], 0)

    async def test_pooled_instance_is_released_when_solve_flow_raises(self):
        """A crash (or cancellation) mid-solve must still close the page/
        context and check the warm instance back into the pool - otherwise
        every failed solve permanently leaks a pool slot."""
        pool = BrowserPool()
        fake_camoufox_pool = FakeCamoufoxPoolWithBrowser(2)
        pool.camoufox_pool = fake_camoufox_pool

        async def raising_execute_solve_flow(*args, **kwargs):
            raise asyncio.CancelledError()

        with patch.object(pool, "_execute_solve_flow", side_effect=raising_execute_solve_flow):
            with self.assertRaises(asyncio.CancelledError):
                await pool._solve_with_pooled_camoufox(
                    url="https://example.com", method="GET", post_data=None, cookies=None,
                    timeout_ms=5000, headers=None, start_time=time.time(),
                    wait_selector=None, wait_delay_ms=None, capture_screenshot=False
                )

        self.assertEqual(fake_camoufox_pool._idle.qsize(), 1)
        self.assertEqual(fake_camoufox_pool._created, 1)

    async def test_second_cancel_during_cleanup_does_not_leak_slot(self):
        """A client disconnect cancels the request, then the tier's wait_for fires
        while page.close() is still running: that second cancel used to skip
        release() and leave the slot counted as created forever, so every later
        pooled solve waited on an empty queue until its timeout."""
        pool = BrowserPool()
        fake = FakeCamoufoxPool(1)
        close_started = asyncio.Event()

        class SlowClosePage(FakePage):
            async def close(self):
                close_started.set()
                await asyncio.sleep(0.2)

        class SlowCloseContext(FakeContext):
            async def new_page(self):
                return SlowClosePage()

        class SlowCloseBrowser(FakeBrowser):
            async def new_context(self, **kwargs):
                return SlowCloseContext()

        async def launch():
            fake.launch_count += 1
            return _PooledCamoufox(cm=object(), browser=SlowCloseBrowser(), created_at=time.time())

        fake._launch_instance = launch
        pool.camoufox_pool = fake

        async def hang(*args, **kwargs):
            await asyncio.sleep(10)

        with patch.object(pool, "_execute_solve_flow", side_effect=hang):
            task = asyncio.create_task(pool._solve_with_pooled_camoufox(
                url="https://example.com", method="GET", post_data=None, cookies=None,
                timeout_ms=5000, headers=None, start_time=time.time(),
                wait_selector=None, wait_delay_ms=None, capture_screenshot=False,
            ))
            await asyncio.sleep(0.05)
            task.cancel()  # client disconnect
            await close_started.wait()
            task.cancel()  # tier timeout fires mid-cleanup
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0.4)

        self.assertEqual(fake._created, 1)
        self.assertEqual(fake._idle.qsize(), 1)
        self.assertEqual(pool.pool_stats()["busy"], 0)
        self.assertEqual(pool.pool_stats()["oldest_checkout_seconds"], 0)

    async def test_exhausted_pool_fails_fast_so_ephemeral_retry_gets_budget(self):
        pool = BrowserPool()
        fake = FakeCamoufoxPoolWithBrowser(1)
        await fake.acquire()  # the only slot stays checked out
        pool.camoufox_pool = fake

        start = time.monotonic()
        with patch.object(browser_module, "POOL_ACQUIRE_TIMEOUT_SECONDS", 0.1):
            with self.assertRaises(TimeoutError) as ctx:
                await pool._solve_with_pooled_camoufox(
                    url="https://example.com", method="GET", post_data=None, cookies=None,
                    timeout_ms=5000, headers=None, start_time=time.time(),
                    wait_selector=None, wait_delay_ms=None, capture_screenshot=False,
                )
        self.assertLess(time.monotonic() - start, 1.0)
        self.assertIn("No warm Camoufox instance", str(ctx.exception))

    async def test_exhausted_pool_leaves_short_budget_for_ephemeral_retry(self):
        """A request whose budget is below POOL_ACQUIRE_TIMEOUT_SECONDS must still
        reach the ephemeral retry instead of spending it all waiting on the pool."""
        pool = BrowserPool()
        fake = FakeCamoufoxPoolWithBrowser(1)
        await fake.acquire()  # the only slot stays checked out
        pool.camoufox_pool = fake
        ok = SolutionModel(url="https://example.com", status=200, cookies=[], userAgent="ua")

        with patch.object(pool, "_solve_with_ephemeral_camoufox", return_value=ok) as ephemeral:
            sol = await pool.solve("https://example.com", timeout_ms=7000)
        self.assertIs(sol, ok)
        self.assertGreaterEqual(ephemeral.call_args.kwargs["timeout_ms"], browser_module.MIN_RETRY_TIMEOUT_MS)

    async def test_close_waits_for_orphaned_cleanups(self):
        pool = BrowserPool()
        fake = FakeCamoufoxPool(1)
        pool.camoufox_pool = fake
        order = []

        async def cleanup():
            await asyncio.sleep(0.1)
            order.append("cleanup")

        task = asyncio.ensure_future(cleanup())
        pool._background_cleanups.add(task)
        task.add_done_callback(pool._background_cleanups.discard)
        original_close = fake.close

        async def close():
            order.append("pool_close")
            await original_close()

        fake.close = close
        await pool.close()
        self.assertEqual(order, ["cleanup", "pool_close"])


if __name__ == "__main__":
    unittest.main()
