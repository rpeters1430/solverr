import asyncio
import time
import unittest
from unittest.mock import patch
from app.solver.browser import BrowserPool, CamoufoxPool, ChallengeNotSolvedError, _PooledCamoufox
from app.solver.browser import browser as browser_module
from app.config import settings
from app.models.flaresolverr import SolutionModel


class FakeCamoufoxPool(CamoufoxPool):
    """CamoufoxPool with the real AsyncCamoufox launch/close swapped for
    cheap fakes, so pool bookkeeping (acquire/release/recycle/capacity) can
    be tested without spawning a real browser process."""

    def __init__(self, size, memory_usage=lambda: None):
        super().__init__(size, memory_usage=memory_usage)
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


class _DeadBrowser:
    def is_connected(self):
        return False


class TestCamoufoxPoolResilience(unittest.IsolatedAsyncioTestCase):
    async def test_dead_idle_browser_is_never_handed_out(self):
        with patch.object(settings, "CAMOUFOX_POOL_RECYCLE_USES", 100), \
             patch.object(settings, "CAMOUFOX_POOL_RECYCLE_SECONDS", 100000):
            pool = FakeCamoufoxPool(1)
            inst = await pool.acquire()
            await pool.release(inst)
            inst.browser = _DeadBrowser()  # Firefox crashed while idle
            fresh = await pool.acquire()
            self.assertIsNot(fresh, inst)
            self.assertEqual(pool.launch_count, 2)
            self.assertEqual(pool.close_count, 1)
            self.assertEqual(pool.dead_reclaimed_total, 1)
            self.assertEqual(pool._created, 1)

    async def test_dead_browser_returned_by_a_peer_is_replaced_for_the_waiter(self):
        pool = FakeCamoufoxPool(1)
        inst = await pool.acquire()
        waiter = asyncio.create_task(pool.acquire(wait_timeout=2))
        await asyncio.sleep(0.01)
        inst.browser = _DeadBrowser()
        with patch.object(settings, "CAMOUFOX_POOL_RECYCLE_USES", 100), \
             patch.object(settings, "CAMOUFOX_POOL_RECYCLE_SECONDS", 100000):
            await pool.release(inst)
            got = await waiter
        self.assertIsNot(got, inst)
        self.assertEqual(pool._created, 1)

    async def test_memory_pressure_retires_on_release_without_warm_replacement(self):
        gib = 1024 ** 3
        with patch.object(settings, "CAMOUFOX_POOL_RECYCLE_USES", 100), \
             patch.object(settings, "CAMOUFOX_POOL_RECYCLE_SECONDS", 100000), \
             patch.object(settings, "CAMOUFOX_MEMORY_RECYCLE_PERCENT", 85), \
             patch.object(settings, "CAMOUFOX_MIN_REPLACE_HEADROOM_MB", 512):
            pool = FakeCamoufoxPool(2, memory_usage=lambda: (int(1.9 * gib), 2 * gib))
            inst = await pool.acquire()
            await pool.release(inst)
            self.assertEqual(pool.memory_recycles_total, 1)
            self.assertEqual(pool.close_count, 1)
            self.assertEqual(pool.launch_count, 1)  # no warm replacement at 100MB headroom
            self.assertEqual(pool._created, 0)
            await pool.acquire()  # relaunched on demand
            self.assertEqual(pool.launch_count, 2)

    async def test_recycle_with_headroom_relaunches_warm(self):
        gib = 1024 ** 3
        with patch.object(settings, "CAMOUFOX_POOL_RECYCLE_USES", 1), \
             patch.object(settings, "CAMOUFOX_MEMORY_RECYCLE_PERCENT", 85):
            pool = FakeCamoufoxPool(1, memory_usage=lambda: (1 * gib, 8 * gib))
            inst = await pool.acquire()
            await pool.release(inst)
            self.assertEqual(pool.memory_recycles_total, 0)
            self.assertEqual(pool.launch_count, 2)
            self.assertEqual(pool._idle.qsize(), 1)

    async def test_memory_reader_failure_is_ignored(self):
        def broken():
            raise OSError("no cgroup")
        with patch.object(settings, "CAMOUFOX_POOL_RECYCLE_USES", 100), \
             patch.object(settings, "CAMOUFOX_POOL_RECYCLE_SECONDS", 100000):
            pool = FakeCamoufoxPool(1, memory_usage=broken)
            inst = await pool.acquire()
            await pool.release(inst)
            self.assertEqual(pool._idle.qsize(), 1)

    async def test_maintain_retires_idle_and_reclaims_dead(self):
        with patch.object(settings, "CAMOUFOX_POOL_RECYCLE_USES", 100), \
             patch.object(settings, "CAMOUFOX_POOL_RECYCLE_SECONDS", 100000), \
             patch.object(settings, "CAMOUFOX_POOL_IDLE_TIMEOUT_SECONDS", 60):
            pool = FakeCamoufoxPool(3)
            a, b, c = await pool.acquire(), await pool.acquire(), await pool.acquire()
            for inst in (a, b, c):
                await pool.release(inst)
            a.last_used_at = time.monotonic() - 120  # idle past the timeout
            b.browser = _DeadBrowser()
            await pool.maintain()
            self.assertEqual(pool.idle_retired_total, 1)
            self.assertEqual(pool.dead_reclaimed_total, 1)
            self.assertEqual(pool._created, 1)
            self.assertEqual(pool._idle.qsize(), 1)
            self.assertIs(await pool.acquire(), c)

    async def test_maintain_keeps_idle_instances_when_timeout_disabled(self):
        with patch.object(settings, "CAMOUFOX_POOL_IDLE_TIMEOUT_SECONDS", 0):
            pool = FakeCamoufoxPool(1)
            inst = await pool.acquire()
            with patch.object(settings, "CAMOUFOX_POOL_RECYCLE_USES", 100):
                await pool.release(inst)
            inst.last_used_at = time.monotonic() - 10 ** 6
            await pool.maintain()
            self.assertEqual(pool._idle.qsize(), 1)
            self.assertEqual(pool.idle_retired_total, 0)


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

    async def test_user_prefs_are_passed_with_proxy_safety_enforced(self):
        prefs = {"network.dns.blockDotOnion": False, "network.proxy.failover_direct": True, "test.int": 7}
        with patch.object(settings, "USER_PREFS", prefs):
            await self._solve(pw_proxy={"server": "http://proxy.example.com:8080"})
        passed = FakeAsyncCamoufoxCtx.captured_kwargs[-1]["firefox_user_prefs"]
        self.assertEqual(passed["network.dns.blockDotOnion"], False)
        self.assertEqual(passed["test.int"], 7)
        self.assertIs(passed["network.proxy.failover_direct"], False)
        self.assertIs(passed["network.proxy.socks_remote_dns"], True)
        self.assertIsNot(passed, prefs)

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
            # The wall never clears, so the attempt fails rather than returning it as a solved page.
            with self.assertRaises(ChallengeNotSolvedError):
                await pool._execute_solve_flow(
                    context=Ctx(), page=ChallengePage(), url="https://example.com", method="GET",
                    post_data=None, cookies=None, timeout_ms=1500, active_ua="ua",
                    headers=None, start_time=start, deadline=start + 1.5,
                )
        # Old behaviour: 0.5s navigation + a fresh 1.5s loop + finalisation.
        self.assertLess(time.monotonic() - start, 2.0)


class _StaticPage(FakePage):
    """A page that never navigates: fixed title, content and main-document status."""
    url = "https://example.com/page"
    main_frame = object()
    frames = []
    page_title = "Search results"
    html = ""

    def on(self, *args):
        pass

    async def title(self):
        return self.page_title

    async def content(self):
        return self.html

    async def evaluate(self, script, *args):
        # No widget token is ever populated; body/readyState probes report a rendered page.
        return "response" not in script


class _NoCookieContext(FakeContext):
    async def cookies(self, *args, **kwargs):
        return []


REAL_BODY = "<body>" + "<p>search result row</p>" * 200 + "</body>"


class TestSolveFlowWallVersusRealPage(unittest.IsolatedAsyncioTestCase):
    async def _run(self, page, status=200, timeout_s=3.0, **patches):
        pool = BrowserPool()

        async def nav(*args, **kwargs):
            return None, status

        start = time.monotonic()
        with patch.object(browser_module, "navigate_to_target", side_effect=nav), \
             patch.object(browser_module, "install_media_blocking", return_value=None), \
             patch.object(browser_module, "dispatch_challenge_click", return_value=(False, False)), \
             patch.multiple(browser_module, SOLVE_FINALIZE_RESERVE_SECONDS=0.2, **patches):
            try:
                return await pool._execute_solve_flow(
                    context=_NoCookieContext(), page=page, url=page.url, method="GET",
                    post_data=None, cookies=None, timeout_ms=int(timeout_s * 1000), active_ua="ua",
                    headers=None, start_time=start, deadline=start + timeout_s,
                ), time.monotonic() - start
            except ChallengeNotSolvedError as e:
                return e, time.monotonic() - start

    async def test_provider_telemetry_on_a_real_page_returns_at_once(self):
        page = _StaticPage()
        page.html = '<html><script src="/_Incapsula_Resource?SWJIYLWA=1"></script>' + REAL_BODY + "</html>"
        sol, elapsed = await self._run(page)
        self.assertEqual(sol.status, 200)
        self.assertIsNone(sol.challengeType)
        self.assertLess(elapsed, 1.5)

    async def test_embedded_widget_gets_a_bounded_window_then_the_page_is_returned(self):
        page = _StaticPage()
        page.html = '<html><div class="g-recaptcha" data-sitekey="x"></div>' + REAL_BODY + "</html>"
        sol, elapsed = await self._run(page, timeout_s=6.0, WIDGET_SOLVE_WINDOW_SECONDS=0.5)
        self.assertEqual(sol.status, 200)
        self.assertEqual(sol.challengeType, "recaptcha")
        self.assertLess(elapsed, 3.0)

    async def test_wall_served_with_200_fails_instead_of_returning_as_solved(self):
        page = _StaticPage()
        page.page_title = "example.com"
        page.html = "<html><script>var dd={'rt':'c','cid':'x','host':'geo.captcha-delivery.com'}</script></html>"
        err, _ = await self._run(page, timeout_s=1.5)
        self.assertIsInstance(err, ChallengeNotSolvedError)
        self.assertEqual(err.challenge, "datadome")
        self.assertFalse(err.ip_blocked)

    async def test_ip_block_fails_fast_without_waiting_out_the_budget(self):
        page = _StaticPage()
        page.page_title = "Attention Required! | Cloudflare"
        page.html = '<html><div id="cf-error-details"><h1>Sorry, you have been blocked</h1></div></html>'
        err, elapsed = await self._run(page, status=403, timeout_s=10.0)
        self.assertIsInstance(err, ChallengeNotSolvedError)
        self.assertTrue(err.ip_blocked)
        self.assertLess(elapsed, 2.0)


class TestSolveFlowAnubis(unittest.IsolatedAsyncioTestCase):
    _run = TestSolveFlowWallVersusRealPage._run

    async def test_anubis_reject_page_fails_at_once(self):
        from tests.test_challenge_detection import ANUBIS_REJECT_PAGE
        page = _StaticPage()
        page.page_title = "Oh noes!"
        page.html = ANUBIS_REJECT_PAGE
        err, elapsed = await self._run(page, timeout_s=10.0)
        self.assertIsInstance(err, ChallengeNotSolvedError)
        self.assertEqual(err.challenge, "anubis")
        self.assertFalse(err.ip_blocked)
        self.assertLess(elapsed, 2.0)

    async def test_anubis_wall_that_never_clears_is_not_returned_as_solved(self):
        from tests.test_challenge_detection import ANUBIS_CHALLENGE_PAGE
        page = _StaticPage()
        page.page_title = "Making sure you're not a bot!"
        page.html = ANUBIS_CHALLENGE_PAGE
        err, _ = await self._run(page, timeout_s=1.5)
        self.assertIsInstance(err, ChallengeNotSolvedError)
        self.assertEqual(err.challenge, "anubis")

    async def test_anubis_wall_clears_once_its_proof_of_work_redirects(self):
        from tests.test_challenge_detection import ANUBIS_CHALLENGE_PAGE

        class SolvingPage(_StaticPage):
            reads = 0

            async def content(self):
                SolvingPage.reads += 1
                return ANUBIS_CHALLENGE_PAGE if SolvingPage.reads <= 2 else REAL_HTML

        page = SolvingPage()
        sol, _ = await self._run(page, timeout_s=6.0)
        self.assertEqual(sol.status, 200)
        self.assertEqual(sol.challengeType, "anubis")

    async def test_cap_widget_is_solved_by_its_own_proof_of_work(self):
        from unittest.mock import AsyncMock
        page = _StaticPage()
        page.page_title = "Contact us"
        page.html = '<html><form><cap-widget data-cap-api-endpoint="/cap/"></cap-widget></form>' + REAL_BODY + "</html>"
        start = AsyncMock(return_value=True)
        solved = AsyncMock(side_effect=[False, True, True, True])
        sol, elapsed = await self._run(page, timeout_s=6.0, start_cap_solve=start, cap_widgets_solved=solved)
        self.assertEqual(sol.challengeType, "cap")
        start.assert_awaited()
        self.assertLess(elapsed, 5.0)

    async def test_google_sorry_page_is_an_ip_block_without_a_paid_solver(self):
        page = _StaticPage()
        page.url = "https://www.google.com/sorry/index?continue=x"
        page.page_title = "https://www.google.com/search?q=x"
        page.html = '<html><div id="recaptcha" class="g-recaptcha" data-sitekey="k"></div></html>'
        with patch.object(browser_module.captcha_solver, "api_key", None):
            err, elapsed = await self._run(page, status=429, timeout_s=10.0)
        self.assertIsInstance(err, ChallengeNotSolvedError)
        self.assertEqual(err.challenge, "google")
        self.assertTrue(err.ip_blocked)
        self.assertLess(elapsed, 2.0)


CF_WALL_HTML = '<html><h2 id="challenge-running">Checking</h2><script>window._cf_chl_opt={}</script></html>'
REAL_HTML = "<html>" + REAL_BODY + "</html>"


class _ScriptedContext(FakeContext):
    """Cookie jar the test rewrites while the solve loop is polling."""

    def __init__(self, cookies=None):
        self.jar = list(cookies or [])

    async def cookies(self, *args, **kwargs):
        return list(self.jar)


def _sensor(name, value, domain="example.com"):
    return {"name": name, "value": value, "domain": domain, "path": "/"}


class TestSolveFlowSensorCookies(unittest.IsolatedAsyncioTestCase):
    async def _run(self, page, context, timeout_s=6.0, on_navigate=None, **patches):
        pool = BrowserPool()
        navigations = []

        async def nav(*args, **kwargs):
            navigations.append(time.monotonic())
            if on_navigate:
                on_navigate(len(navigations))
            return None, 403

        patches.setdefault("SOLVE_FINALIZE_RESERVE_SECONDS", 0.2)
        start = time.monotonic()
        with patch.object(browser_module, "navigate_to_target", side_effect=nav), \
             patch.object(browser_module, "install_media_blocking", return_value=None), \
             patch.object(browser_module, "dispatch_challenge_click", return_value=(False, False)), \
             patch.multiple(browser_module, **patches):
            try:
                result = await pool._execute_solve_flow(
                    context=context, page=page, url=page.url, method="GET",
                    post_data=None, cookies=None, timeout_ms=int(timeout_s * 1000), active_ua="ua",
                    headers=None, start_time=start, deadline=start + timeout_s,
                )
            except ChallengeNotSolvedError as e:
                result = e
        return result, navigations, time.monotonic() - start

    def _wall_page(self, html=CF_WALL_HTML, title="example.com"):
        page = _StaticPage()
        page.page_title = title
        # Padded past the flow's "is this a full document yet" re-read threshold, which only costs time here.
        page.html = html + "<body><!--" + "x" * 400 + "--></body>"
        return page

    async def test_reloads_the_target_when_the_cookie_is_issued_but_no_redirect_fires(self):
        page = self._wall_page()
        context = _ScriptedContext()

        def on_navigate(count):
            if count == 1:
                # The check passes shortly after the wall loads, but the page never redirects.
                asyncio.get_running_loop().call_later(
                    0.3, lambda: context.jar.append(_sensor("cf_clearance", "fresh")))
            else:
                page.html = REAL_HTML

        sol, navigations, _ = await self._run(
            page, context, on_navigate=on_navigate, SENSOR_REDIRECT_GRACE_SECONDS=0.5)
        self.assertEqual(len(navigations), 2)
        self.assertEqual(sol.status, 200)
        self.assertIn("search result row", sol.response)
        self.assertEqual(sol.challengeType, "cloudflare_turnstile")

    async def test_cached_cookie_the_site_no_longer_honours_does_not_clear_the_wall(self):
        page = self._wall_page()
        context = _ScriptedContext([_sensor("cf_clearance", "cached")])
        err, navigations, _ = await self._run(page, context, timeout_s=1.5, SENSOR_REDIRECT_GRACE_SECONDS=0.2)
        self.assertIsInstance(err, ChallengeNotSolvedError)
        self.assertEqual(len(navigations), 1)

    async def test_wall_that_survives_the_reload_fails_the_attempt_early(self):
        page = self._wall_page()
        context = _ScriptedContext()

        def on_navigate(count):
            if count == 1:
                asyncio.get_running_loop().call_later(
                    0.2, lambda: context.jar.append(_sensor("cf_clearance", "fresh")))

        err, navigations, elapsed = await self._run(
            page, context, timeout_s=30.0, on_navigate=on_navigate,
            SENSOR_REDIRECT_GRACE_SECONDS=0.3, POST_RENAVIGATE_GRACE_SECONDS=0.5)
        self.assertIsInstance(err, ChallengeNotSolvedError)
        self.assertIn("clearance cookie was issued", err.reason)
        self.assertFalse(err.ip_blocked)
        self.assertEqual(len(navigations), 2)
        self.assertLess(elapsed, 6.0)

    async def test_datadome_cookie_handed_out_with_the_wall_is_not_a_pass(self):
        page = self._wall_page("<html><script>var dd={'rt':'i','cid':'x','host':'geo.captcha-delivery.com'}</script></html>")
        context = _ScriptedContext()

        def on_navigate(count):
            context.jar.append(_sensor("datadome", "issued-with-the-wall"))

        err, navigations, _ = await self._run(
            page, context, timeout_s=1.5, on_navigate=on_navigate, SENSOR_REDIRECT_GRACE_SECONDS=0.2)
        self.assertIsInstance(err, ChallengeNotSolvedError)
        self.assertEqual(len(navigations), 1)

    async def test_datadome_slider_is_written_off_without_waiting_out_the_budget(self):
        page = self._wall_page("<html><script>var dd={'rt':'c','cid':'x','host':'geo.captcha-delivery.com'}</script></html>")
        err, _, elapsed = await self._run(page, _ScriptedContext(), timeout_s=30.0)
        self.assertIsInstance(err, ChallengeNotSolvedError)
        self.assertEqual(err.challenge, "datadome")
        self.assertIn("interactive captcha", err.reason)
        self.assertLess(elapsed, 4.0)

    async def test_akamai_wall_gets_one_press_and_hold_and_continuous_pointer_movement(self):
        page = self._wall_page('<html><div id="sec-if-cpt-container"><div id="progress-button"></div></div></html>')
        held, wandered = [], []

        async def hold(_page):
            held.append(1)
            return True

        async def wander(_page, *args):
            wandered.append(1)

        clicks = []

        async def click(*args):
            clicks.append(1)
            return False, False

        with patch.object(browser_module, "akamai_press_and_hold", side_effect=hold), \
             patch.object(browser_module, "wander_mouse", side_effect=wander):
            err, _, _ = await self._run(
                page, _ScriptedContext(), timeout_s=4.5,
                AKAMAI_HOLD_SECONDS=0.1, CHALLENGE_CLICK_RETRY_SECONDS=0.3, dispatch_challenge_click=click)
        self.assertIsInstance(err, ChallengeNotSolvedError)
        self.assertEqual(err.challenge, "akamai")
        self.assertEqual(len(held), 1)
        self.assertGreater(len(wandered), 2)
        self.assertEqual(clicks, [])


class TestIpBlockSkipsSameIpRetry(unittest.IsolatedAsyncioTestCase):
    async def test_ip_blocked_pooled_attempt_does_not_retry_from_the_same_ip(self):
        pool = BrowserPool()
        pool.camoufox_pool = FakeCamoufoxPoolWithBrowser(1)

        async def pooled(**kwargs):
            raise ChallengeNotSolvedError("cloudflare", "the site refused this IP address outright", ip_blocked=True)

        with patch.object(pool, "_solve_with_pooled_camoufox", side_effect=pooled), \
             patch.object(pool, "_solve_with_ephemeral_camoufox") as ephemeral:
            with self.assertRaises(ChallengeNotSolvedError):
                await pool.solve("https://example.com", timeout_ms=60000)
        ephemeral.assert_not_called()

    async def test_unsolved_wall_still_gets_the_fresh_fingerprint_retry(self):
        pool = BrowserPool()
        pool.camoufox_pool = FakeCamoufoxPoolWithBrowser(1)

        async def pooled(**kwargs):
            raise ChallengeNotSolvedError("cloudflare_turnstile", "still present after 40s")

        async def ephemeral(**kwargs):
            return SolutionModel(url="https://example.com", status=200, cookies=[], userAgent="ua")

        with patch.object(pool, "_solve_with_pooled_camoufox", side_effect=pooled), \
             patch.object(pool, "_solve_with_ephemeral_camoufox", side_effect=ephemeral) as retry:
            sol = await pool.solve("https://example.com", timeout_ms=60000)
        self.assertEqual(sol.status, 200)
        retry.assert_called_once()


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
