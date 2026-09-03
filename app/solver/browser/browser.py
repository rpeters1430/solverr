import asyncio
import base64
import logging
import time
from typing import Dict, List, Optional, Any
from playwright.async_api import Page

from app.config import settings
from app.models.flaresolverr import CookieModel, SolutionModel
from app.solver.captcha_solver import captcha_solver
from app.logging_config import sanitize_proxy_url

from app.solver.browser.pool import CamoufoxPool, CAMOUFOX_AVAILABLE
from app.solver.browser.challenges import detect_challenge, is_challenge_title, has_age_gate_marker
from app.solver.browser.captcha import CAPTCHA_SOLVER_WIDGETS, try_captcha_solver_escalation
from app.solver.browser.cookies import build_playwright_cookies, read_context_cookies, extract_captured_cookies
from app.solver.browser.navigation import install_media_blocking, navigate_to_target
from app.solver.browser.interactions import dispatch_challenge_click

if CAMOUFOX_AVAILABLE:
    from camoufox.async_api import AsyncCamoufox

logger = logging.getLogger("solverr.browser")

# Wall-clock safety net layered on top of each solve tier's own internal
# timeouts (navigation waits, selector waits, etc). If a Playwright/Camoufox
# call hangs on something that doesn't respect its own timeout=, this forces
# the tier to give up and release its browser/pool slot rather than pinning
# it (and, on a small worker count, a meaningful fraction of total capacity)
# indefinitely.
SOLVE_WALLCLOCK_GRACE_SECONDS = 15


def _describe_solve_error(e: BaseException, tier_timeout: float, tier_label: str) -> BaseException:
    """asyncio.wait_for raises a bare TimeoutError/CancelledError with no
    message (str(e) == "") - left as-is, that surfaces to API callers as an
    empty "Error solving request: " with no actual information. Wrap it with
    a message identifying which tier and timeout actually fired."""
    if isinstance(e, (asyncio.TimeoutError, TimeoutError)) and not str(e):
        return TimeoutError(f"{tier_label} timed out after {tier_timeout:.0f}s")
    return e


class BrowserPool:
    def __init__(self):
        self.semaphore = asyncio.Semaphore(settings.MAX_BROWSER_WORKERS)
        self.camoufox_pool: Optional[CamoufoxPool] = (
            CamoufoxPool(settings.MAX_BROWSER_WORKERS)
            if (CAMOUFOX_AVAILABLE and settings.CAMOUFOX_POOL_ENABLED)
            else None
        )
        # Queue-pressure/crash telemetry, exposed via pool_stats() for
        # /metrics and the dashboard - see app/metrics.py.
        self._queue_wait_total_s: float = 0.0
        self._queue_wait_count: int = 0
        self._crashes_total: int = 0

    async def close(self):
        if self.camoufox_pool:
            try:
                await self.camoufox_pool.close()
            except Exception as e:
                logger.warning(f"[CamoufoxPool] Shutdown notice: {e}")
        logger.info("Browser Pool stopped.")

    def pool_stats(self) -> Dict[str, Any]:
        cp = self.camoufox_pool
        created = cp._created if cp else 0
        idle = cp._idle.qsize() if cp else 0
        avg_wait = (self._queue_wait_total_s / self._queue_wait_count) if self._queue_wait_count else 0.0
        return {
            "pool_size": cp.size if cp else 0,
            "created": created,
            "busy": max(0, created - idle),
            "idle": idle,
            "recycles_total": cp.recycles_total if cp else 0,
            "crashes_total": self._crashes_total,
            "avg_queue_wait_seconds": round(avg_wait, 3),
            "queue_wait_samples": self._queue_wait_count,
        }

    async def self_test(self) -> Dict[str, Any]:
        """Diagnostic-only smoke test: launch a real (ephemeral) Camoufox
        instance, create a context/page, execute JS, then tear it all down.
        Verifies Tier 3 is actually usable end-to-end, not just that the
        Camoufox package imported successfully (see GET /health, which only
        checks the import). Used by GET /api/diagnostics/browser.

        Explicitly timeout-bounded: launching under some deployment
        configurations (observed: PUID/PGID non-root + certain capability
        restrictions) can leave the Playwright<->Firefox IPC handshake
        hanging indefinitely rather than erroring, and this is the one
        BrowserPool code path that doesn't already sit under an
        asyncio.wait_for from solve()'s tier_timeout."""
        if not CAMOUFOX_AVAILABLE:
            return {"ok": False, "error": "Camoufox import failed - stealth engine unavailable"}

        start = time.monotonic()
        try:
            return await asyncio.wait_for(self._self_test_inner(start), timeout=SOLVE_WALLCLOCK_GRACE_SECONDS + 30)
        except asyncio.TimeoutError:
            return {
                "ok": False,
                "error": "Browser self-test timed out - Camoufox launch or IPC handshake did not complete",
                "duration_ms": round((time.monotonic() - start) * 1000, 1)
            }
        except Exception as e:
            return {
                "ok": False,
                "error": f"{type(e).__name__}: {e}",
                "duration_ms": round((time.monotonic() - start) * 1000, 1)
            }

    async def _self_test_inner(self, start: float) -> Dict[str, Any]:
        async with AsyncCamoufox(
            headless=settings.HEADLESS,
            os="linux",
            config={'forceScopeAccess': True},
            i_know_what_im_doing=True
        ) as browser_instance:
            context = await browser_instance.new_context() if hasattr(browser_instance, "new_context") else browser_instance
            page = await context.new_page()
            try:
                result = await page.evaluate("() => 1 + 1")
                ua = await page.evaluate("() => navigator.userAgent")
            finally:
                await page.close()
                if context is not browser_instance:
                    await context.close()
        return {
            "ok": result == 2,
            "user_agent": ua,
            "duration_ms": round((time.monotonic() - start) * 1000, 1)
        }

    async def solve(
        self,
        url: str,
        method: str = "GET",
        post_data: Optional[str] = None,
        cookies: Optional[List[CookieModel]] = None,
        proxy: Optional[Dict[str, str]] = None,
        timeout_ms: int = settings.BROWSER_TIMEOUT_MS,
        user_agent: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        wait_selector: Optional[str] = None,
        wait_delay_ms: Optional[int] = None,
        capture_screenshot: bool = False
    ) -> SolutionModel:
        wait_start = time.monotonic()
        async with self.semaphore:
            self._queue_wait_total_s += time.monotonic() - wait_start
            self._queue_wait_count += 1
            start_time = time.monotonic()
            active_ua = user_agent or settings.DEFAULT_USER_AGENT
            pw_proxy = None
            if proxy and "url" in proxy and proxy["url"]:
                pw_proxy = {"server": proxy["url"]}
                logger.info(f"[BrowserPool] Routing request through proxy: {sanitize_proxy_url(proxy['url'])}")

            if not CAMOUFOX_AVAILABLE:
                raise RuntimeError(
                    "Camoufox stealth engine is not available (import failed) - "
                    "no browser engine can service this request."
                )

            tier_timeout = (timeout_ms / 1000.0) + SOLVE_WALLCLOCK_GRACE_SECONDS

            # Pooled path: reuse a warm browser process (no per-request UA
            # override, since Camoufox ties navigator.userAgent to the
            # fingerprint it generated when that process launched - the
            # HTTP UA header and JS-visible UA must match). Only usable
            # when there's no proxy and no explicit user_agent request.
            use_pool = self.camoufox_pool is not None and not pw_proxy and not user_agent
            last_error: Optional[BaseException] = None

            if use_pool:
                try:
                    sol = await asyncio.wait_for(
                        self._solve_with_pooled_camoufox(
                            url=url, method=method, post_data=post_data, cookies=cookies, timeout_ms=timeout_ms,
                            headers=headers, start_time=start_time, wait_selector=wait_selector,
                            wait_delay_ms=wait_delay_ms, capture_screenshot=capture_screenshot
                        ),
                        timeout=tier_timeout
                    )
                    if sol and sol.status < 400:
                        return sol
                    last_error = RuntimeError(f"Pooled Camoufox solve incomplete (status {sol.status if sol else 'N/A'})")
                    logger.warning(f"[CamoufoxEngine] Pooled Camoufox solve incomplete (Status {sol.status if sol else 'N/A'}). Retrying with a fresh ephemeral Camoufox instance...")
                except Exception as e:
                    last_error = _describe_solve_error(e, tier_timeout, "Pooled Camoufox solve")
                    logger.warning(f"[CamoufoxEngine] Pooled Camoufox solve notice/fallback: {last_error}. Retrying with a fresh ephemeral Camoufox instance...")

            # Fresh-fingerprint escalation: a brand-new Camoufox process with
            # its own randomly generated fingerprint (and its own proxy
            # binding, if one was requested) - either the pooled path's
            # retry after a warm-process-specific failure, or the only
            # attempt for proxy/custom-UA requests that can't share the
            # warm pool to begin with.
            try:
                sol = await asyncio.wait_for(
                    self._solve_with_ephemeral_camoufox(
                        url=url, method=method, post_data=post_data, cookies=cookies, pw_proxy=pw_proxy,
                        user_agent=user_agent, timeout_ms=timeout_ms, active_ua=active_ua, headers=headers,
                        start_time=start_time, wait_selector=wait_selector, wait_delay_ms=wait_delay_ms,
                        capture_screenshot=capture_screenshot
                    ),
                    timeout=tier_timeout
                )
                if sol and sol.status < 400:
                    return sol
                last_error = RuntimeError(f"Ephemeral Camoufox solve incomplete (status {sol.status if sol else 'N/A'})")
                logger.warning(f"[CamoufoxEngine] Ephemeral Camoufox solve incomplete (Status {sol.status if sol else 'N/A'}).")
            except Exception as e:
                last_error = _describe_solve_error(e, tier_timeout, "Ephemeral Camoufox solve")
                logger.warning(f"[CamoufoxEngine] Ephemeral Camoufox solve notice/fallback: {last_error}.")

            self._crashes_total += 1
            raise last_error or RuntimeError(f"Camoufox solve failed for {url}")

    async def _solve_with_pooled_camoufox(
        self,
        url: str,
        method: str,
        post_data: Optional[str],
        cookies: Optional[List[CookieModel]],
        timeout_ms: int,
        headers: Optional[Dict[str, str]],
        start_time: float,
        wait_selector: Optional[str],
        wait_delay_ms: Optional[int],
        capture_screenshot: bool
    ) -> SolutionModel:
        inst = await self.camoufox_pool.acquire()
        context = None
        page = None
        try:
            logger.info(f"[CamoufoxPool] Checked out warm instance (use #{inst.uses}) for {url}...")
            if hasattr(inst.browser, "new_context"):
                context = await inst.browser.new_context()
            elif inst.browser.contexts:
                context = inst.browser.contexts[0]
            else:
                context = inst.browser
            page = await context.new_page()
            active_ua = await page.evaluate("() => navigator.userAgent")
            return await self._execute_solve_flow(
                context=context,
                page=page,
                url=url,
                method=method,
                post_data=post_data,
                cookies=cookies,
                timeout_ms=timeout_ms,
                active_ua=active_ua or settings.DEFAULT_USER_AGENT,
                headers=headers,
                start_time=start_time,
                wait_selector=wait_selector,
                wait_delay_ms=wait_delay_ms,
                capture_screenshot=capture_screenshot
            )
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass
            if context and context is not inst.browser:
                try:
                    await context.close()
                except Exception:
                    pass
            await self.camoufox_pool.release(inst)

    async def _solve_with_ephemeral_camoufox(
        self,
        url: str,
        method: str,
        post_data: Optional[str],
        cookies: Optional[List[CookieModel]],
        pw_proxy: Optional[Dict[str, str]],
        user_agent: Optional[str],
        timeout_ms: int,
        active_ua: str,
        headers: Optional[Dict[str, str]],
        start_time: float,
        wait_selector: Optional[str],
        wait_delay_ms: Optional[int],
        capture_screenshot: bool
    ) -> SolutionModel:
        """Dedicated (non-pooled) Camoufox launch for requests carrying their
        own proxy or an explicit user_agent - Camoufox ties geolocation/
        timezone/WebRTC fingerprint derivation to the proxy's exit IP and to
        the launch-time UA, so these can't share the warm no-proxy pool."""
        logger.info(f"[CamoufoxEngine] Spawning ephemeral Camoufox stealth Firefox solve for {url} (proxy={'yes' if pw_proxy else 'no'}, custom_ua={'yes' if user_agent else 'no'})...")
        async with AsyncCamoufox(
            headless=settings.HEADLESS,
            proxy=pw_proxy,
            humanize=True,
            disable_coop=True,
            os="linux",
            config={'forceScopeAccess': True},
            i_know_what_im_doing=True
        ) as browser_instance:
            if hasattr(browser_instance, "contexts") and browser_instance.contexts:
                context = browser_instance.contexts[0]
            elif hasattr(browser_instance, "new_context"):
                context = await browser_instance.new_context()
            else:
                context = browser_instance
            page = await context.new_page()
            return await self._execute_solve_flow(
                context=context,
                page=page,
                url=url,
                method=method,
                post_data=post_data,
                cookies=cookies,
                timeout_ms=timeout_ms,
                active_ua=active_ua,
                headers=headers,
                start_time=start_time,
                wait_selector=wait_selector,
                wait_delay_ms=wait_delay_ms,
                capture_screenshot=capture_screenshot
            )

    async def _execute_solve_flow(
        self,
        context: Any,
        page: Page,
        url: str,
        method: str,
        post_data: Optional[str],
        cookies: Optional[List[CookieModel]],
        timeout_ms: int,
        active_ua: str,
        headers: Optional[Dict[str, str]],
        start_time: float,
        wait_selector: Optional[str] = None,
        wait_delay_ms: Optional[int] = None,
        capture_screenshot: bool = False
    ) -> SolutionModel:
        # Pre-load cookies into context
        pw_cookies = build_playwright_cookies(url, cookies)
        if pw_cookies:
            try:
                await context.add_cookies(pw_cookies)
                logger.debug(f"[BrowserPool] Pre-loaded {len(pw_cookies)} cookie(s) into browser context")
            except Exception as e:
                logger.warning(f"[BrowserPool] Error pre-loading cookies: {e}")

        await install_media_blocking(page)

        # Track the true final HTTP status across the challenge-clearing
        # navigations/redirects/reloads, instead of assuming 200 once the
        # title looks clean - the real final page could be a 404/500/etc.
        last_main_status: Dict[str, Optional[int]] = {"code": None}

        def _on_response(resp):
            try:
                req = resp.request
                if req.resource_type == "document" and req.frame == page.main_frame:
                    last_main_status["code"] = resp.status
            except Exception:
                pass

        page.on("response", _on_response)

        response, initial_status = await navigate_to_target(page, url, method, post_data, timeout_ms)

        initial_title = ""
        try:
            initial_title = await page.title()
        except Exception:
            pass

        logger.info(f"[BrowserPool] Initial page load complete (HTTP Status: {initial_status}, Title: '{initial_title}')")

        # Multi-WAF challenge detection and auto-resolver loop
        max_wait = timeout_ms / 1000.0
        step = 0.2
        loop_start = time.monotonic()
        iteration = 0
        content_check_every = 4
        last_click_ts = 0.0
        last_logged_step = 0.0
        age_gate_clicked = False
        last_detected_challenge: Optional[str] = None
        cleared = False

        while (time.monotonic() - loop_start) < max_wait:
            check_content = (iteration % content_check_every) == 0
            iteration += 1

            try:
                title = await page.title()
            except Exception:
                title = ""

            content = ""
            if check_content:
                try:
                    content = await asyncio.wait_for(page.content(), timeout=2.0)
                except Exception:
                    content = ""

            content_lower = content.lower() if content else ""

            active_challenge = detect_challenge(title, content, check_content)
            if active_challenge:
                last_detected_challenge = active_challenge

            # Check if page is clean and ready
            if not is_challenge_title(title) and not active_challenge:
                # Ensure the page has actually navigated and the body has arrived (avoid mid-redirect hollow snapshots)
                page_ready = False
                if title and title.strip():
                    try:
                        page_ready = await page.evaluate("""() => {
                            return (document.body && document.body.innerHTML.trim().length > 100) || document.readyState === 'complete';
                        }""")
                    except Exception:
                        page_ready = True
                else:
                    # Empty title: only accept if body has substantial content
                    try:
                        page_ready = await page.evaluate("""() => {
                            return Boolean(document.body && document.body.innerHTML.trim().length > 300);
                        }""")
                    except Exception:
                        page_ready = False

                if page_ready:
                    # If an age gate was already clicked or no modal is blocking, we are done!
                    if age_gate_clicked or not has_age_gate_marker(content_lower, check_content):
                        elapsed = time.monotonic() - loop_start
                        logger.info(f"[BrowserPool] Challenge cleared! Final Title: '{title}' in {elapsed:.2f}s")
                        cleared = True
                        break

            now_ts = time.monotonic()
            if (now_ts - last_logged_step) >= 3.0:
                elapsed = now_ts - loop_start
                state_label = active_challenge or ("age_gate" if not age_gate_clicked else "page_stabilizing")
                logger.info(f"[BrowserPool] Anti-bot / gate active ({state_label}, {elapsed:.1f}s elapsed) | Current Title: '{title}'")
                last_logged_step = now_ts

            # Periodic Interactive Challenge Solver Dispatcher
            if (now_ts - last_click_ts) >= 1.2 and (now_ts - loop_start) > 0.6:
                last_click_ts = now_ts
                _clicked, age_gate_clicked = await dispatch_challenge_click(page, active_challenge, title, age_gate_clicked)

            await asyncio.sleep(step)

        # Tier 3.5: paid captcha-solver escalation. Only reached when the
        # free click-based approach above ran out the full timeout without
        # clearing - covers interactive image challenges (hCaptcha puzzle
        # grids, reCAPTCHA image selection) a checkbox click can't solve.
        # No-ops entirely when CAPTCHA_SOLVER_API_KEY isn't configured.
        if not cleared and captcha_solver.enabled and last_detected_challenge in CAPTCHA_SOLVER_WIDGETS:
            solved = await try_captcha_solver_escalation(page, url, last_detected_challenge)
            if solved:
                settle_deadline = time.monotonic() + 8.0
                while time.monotonic() < settle_deadline:
                    try:
                        settle_title = await page.title()
                    except Exception:
                        settle_title = ""
                    if not is_challenge_title(settle_title) and not detect_challenge(settle_title, "", check_content=False):
                        logger.info(f"[CaptchaSolver] Page settled after token injection. Final Title: '{settle_title}'")
                        break
                    await asyncio.sleep(0.3)

        # Optional wait_selector support
        if wait_selector:
            try:
                logger.info(f"[BrowserPool] Waiting for custom selector '{wait_selector}'...")
                await page.wait_for_selector(wait_selector, timeout=5000)
            except Exception as e:
                logger.warning(f"[BrowserPool] wait_selector '{wait_selector}' timed out: {e}")

        # Optional stabilization delay
        if wait_delay_ms and wait_delay_ms > 0:
            await asyncio.sleep(wait_delay_ms / 1000.0)
        else:
            await asyncio.sleep(0.3)

        # Optional screenshot capture
        screenshot_b64 = None
        if capture_screenshot:
            try:
                img_bytes = await page.screenshot(type="jpeg", quality=60)
                max_bytes = settings.MAX_SCREENSHOT_MB * 1024 * 1024
                if max_bytes > 0 and len(img_bytes) > max_bytes:
                    logger.warning(
                        f"[BrowserPool] Screenshot ({len(img_bytes) / 1024 / 1024:.1f}MB) exceeds "
                        f"MAX_SCREENSHOT_MB={settings.MAX_SCREENSHOT_MB}, dropping it"
                    )
                else:
                    screenshot_b64 = base64.b64encode(img_bytes).decode("utf-8")
            except Exception as e:
                logger.warning(f"[BrowserPool] Screenshot capture error: {e}")

        final_url = page.url
        html_content = ""
        final_title = ""
        for attempt in range(3):
            try:
                final_title = await page.title()
                html_content = await page.content()
                if html_content and ("<body" in html_content.lower()) and len(html_content) > 300:
                    break
            except Exception as e:
                logger.debug(f"[BrowserPool] State reading notice: {e}")
            await asyncio.sleep(0.3)

        if not html_content:
            try:
                final_title = await page.title()
                html_content = await page.content()
            except Exception:
                final_title = final_title or ""
                html_content = ""

        # Extract captured cookies safely
        raw_cookies = await read_context_cookies(context)
        captured_cookies = extract_captured_cookies(raw_cookies)

        # Prefer the real last main-frame document status captured across
        # every redirect/reload of the challenge flow. Fall back to the
        # initial navigation response, then a generic guess only if neither
        # is available.
        if last_main_status["code"] is not None:
            status_code = last_main_status["code"]
        elif response:
            status_code = response.status
        else:
            status_code = 200 if final_title and "just a moment" not in final_title.lower() else 503

        solve_duration = time.monotonic() - start_time
        cookie_names = [c.name for c in captured_cookies]
        cookie_summary = f"[{', '.join(cookie_names[:5])}{'...' if len(cookie_names) > 5 else ''}]"
        logger.info(f"[BrowserPool] Solve finished in {solve_duration:.2f}s | Final Title: '{final_title}' | Status: {status_code} | Challenge: {last_detected_challenge or 'none'} | Captured {len(captured_cookies)} cookies {cookie_summary}")

        solution = SolutionModel(
            url=final_url,
            status=status_code,
            headers={"content-type": "text/html"},
            response=html_content,
            cookies=captured_cookies,
            userAgent=active_ua,
            challengeType=last_detected_challenge
        )

        if screenshot_b64:
            solution.screenshot = screenshot_b64

        return solution


browser_pool = BrowserPool()
