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
from app.solver.browser.challenges import (
    ANUBIS_REJECTED_REASON,
    IP_BLOCKED_REASON,
    UNSOLVABLE_CAPTCHA_REASON,
    WIDGET_CHALLENGES,
    ChallengeNotSolvedError,
    anubis_state,
    detect_challenge,
    is_anubis_verification_url,
    is_challenge_title,
    is_challenge_wall,
    ip_block_provider,
    needs_unsolvable_captcha,
    has_age_gate_marker,
    is_browser_error,
)
from app.solver.browser.captcha import CAPTCHA_SOLVER_WIDGETS, try_captcha_solver_escalation
from app.solver.browser.cookies import build_playwright_cookies, read_context_cookies, extract_captured_cookies
from app.solver.browser.content import decode_text_body, is_non_html_text
from app.solver.browser.navigation import follow_meta_refresh as follow_meta_refresh_in_page, install_media_blocking, navigate_to_target
from app.solver.browser.interactions import (
    AKAMAI_HOLD_SECONDS,
    akamai_press_and_hold,
    cap_widgets_solved,
    describe_challenge_frames,
    dispatch_challenge_click,
    start_cap_solve,
    wander_mouse,
)
from app.solver.browser.clearance import BLOCK_PAGE_COOKIES, has_earned_sensor_cookie, host_of, sensor_snapshot

if CAMOUFOX_AVAILABLE:
    from camoufox.async_api import AsyncCamoufox

logger = logging.getLogger("solverr.browser")

# Outer wall-clock cap per tier, for Playwright calls that ignore their own timeout= and would pin a pool slot.
SOLVE_WALLCLOCK_GRACE_SECONDS = 15
# Below this, a fresh-browser retry can't launch and clear a challenge, so fail fast instead.
MIN_RETRY_TIMEOUT_MS = 5000
# The worker semaphore already caps concurrency at pool size, so a longer wait means a peer is
# relaunching a recycled instance (or the pool is wedged) - fall back to an ephemeral browser instead.
POOL_ACQUIRE_TIMEOUT_SECONDS = 20
# Page/context close on a wedged browser can hang; past this the instance is treated as broken and recycled.
POOLED_CLEANUP_TIMEOUT_SECONDS = 5
# Held back from the pooled attempt so a stuck fingerprint still leaves the fresh-fingerprint retry
# a real window: the smaller of this and RETRY_RESERVE_FRACTION of the budget.
EPHEMERAL_RETRY_RESERVE_MS = 20000
RETRY_RESERVE_FRACTION = 0.4
# The challenge loop stops this early so reading content/cookies afterward fits inside the attempt's budget.
SOLVE_FINALIZE_RESERVE_SECONDS = 3.0
# After clicking a widget, let it verify before clicking again; re-clicking mid-verification resets Turnstile.
CHALLENGE_CLICK_COOLDOWN_SECONDS = 4.0
CHALLENGE_CLICK_RETRY_SECONDS = 1.2
WIDGET_REPORT_INTERVAL_SECONDS = 10.0
# A captcha widget embedded in a real page gets this long to be clicked through; after that the
# page is returned as it stands rather than holding the request for the whole budget.
WIDGET_SOLVE_WINDOW_SECONDS = 15.0
# How long a wall may stay up after its sensor cookie is issued before the target is reloaded by
# hand. Providers normally redirect within 2-3s; some never do.
SENSOR_REDIRECT_GRACE_SECONDS = 5.0
# After that reload, a wall still up this long is the site refusing the cookie it just issued.
POST_RENAVIGATE_GRACE_SECONDS = 10.0


def _describe_solve_error(e: BaseException, tier_timeout: float, tier_label: str) -> BaseException:
    """Name the tier and timeout on bare TimeoutErrors, whose str() is empty."""
    if isinstance(e, (asyncio.TimeoutError, TimeoutError)) and not str(e):
        return TimeoutError(f"{tier_label} timed out after {tier_timeout:.0f}s")
    msg = sanitize_proxy_url(str(e))
    if msg != str(e):
        try:
            return type(e)(msg)
        except Exception:
            return RuntimeError(msg)
    return e


async def _safe_title(page: Page) -> str:
    try:
        return await page.title()
    except Exception:
        return ""


def _header_value(headers: Dict[str, str], name: str) -> str:
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return ""


def _raise_if_ip_blocked(content: str, url: str) -> None:
    blocked_by = ip_block_provider(content, url)
    # Google's /sorry/ page is a reCAPTCHA; with a paid solver configured it is worth a try
    # before giving the budget to another IP.
    if blocked_by and not (blocked_by == "google" and captcha_solver.enabled):
        raise ChallengeNotSolvedError(blocked_by, IP_BLOCKED_REASON, ip_blocked=True)


def _is_solution_acceptable(sol: Optional[SolutionModel]) -> bool:
    """Check if solution is a valid HTTP response rather than an incomplete solve or network error."""
    if not sol or sol.status <= 0 or sol.status == 502:
        return False
    if sol.status < 400 or sol.status in (400, 401, 404, 405, 410, 422):
        return True
    return False


def _pooled_attempt_budget_ms(timeout_ms: int) -> int:
    """The pooled attempt's share of the budget, leaving the ephemeral retry a usable window when it can."""
    reserve_ms = min(EPHEMERAL_RETRY_RESERVE_MS, int(timeout_ms * RETRY_RESERVE_FRACTION))
    if reserve_ms < MIN_RETRY_TIMEOUT_MS:
        return timeout_ms
    return timeout_ms - reserve_ms


def _browser_is_connected(browser: Any) -> bool:
    """Playwright exposes is_connected(); keep compatibility with test doubles."""
    try:
        probe = getattr(browser, "is_connected", None)
        return bool(probe()) if callable(probe) else True
    except Exception:
        return False


def _is_browser_disconnected(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in (
        "browser has been closed", "browser closed", "browser disconnected",
        "target page, context or browser has been closed", "connection closed",
        "connection is closed", "connection lost",
    ))


async def extract_rendered_records(page: Page, spec: Dict[str, Any]) -> Dict[str, Any]:
    """Extract a bounded set of repeated DOM records from the rendered page."""
    container = spec.get("container")
    fields = spec.get("fields")
    if not isinstance(container, str) or not container or len(container) > 300:
        raise ValueError("extract_records requires a short container CSS selector")
    if not isinstance(fields, dict) or not 1 <= len(fields) <= 12 or any(
        not isinstance(k, str) or not isinstance(v, str) or len(k) > 60 or len(v) > 300
        for k, v in fields.items()
    ):
        raise ValueError("extract_records requires 1-12 short CSS field selectors")
    return {"records": await page.evaluate("""({container, fields}) => {
        const nodes = [...document.querySelectorAll(container)].slice(0, 50);
        return nodes.map(root => Object.fromEntries(Object.entries(fields).map(([name, rule]) => {
            const at = rule.lastIndexOf('@');
            const selector = at < 0 ? rule : rule.slice(0, at);
            const attribute = at < 0 ? null : rule.slice(at + 1);
            const element = selector === ':scope' ? root : root.querySelector(selector);
            const value = element ? (attribute ? element.getAttribute(attribute) : element.textContent) : null;
            return [name, value == null ? null : value.trim().slice(0, 500)];
        })));
    }""", {"container": container, "fields": fields})}



class BrowserPool:
    def __init__(self):
        self.semaphore = asyncio.Semaphore(settings.MAX_BROWSER_WORKERS)
        self.camoufox_pool: Optional[CamoufoxPool] = (
            CamoufoxPool(settings.MAX_BROWSER_WORKERS)
            if (CAMOUFOX_AVAILABLE and settings.CAMOUFOX_POOL_ENABLED)
            else None
        )
        # Exposed via pool_stats() for /metrics and the dashboard.
        self._queue_wait_total_s: float = 0.0
        self._queue_wait_count: int = 0
        self._crashes_total: int = 0
        self._queued_at: set[float] = set()
        self._checkout_at: dict[int, float] = {}
        # Strong refs to shielded cleanups that outlive a cancelled solve, so they aren't GC'd mid-run.
        self._background_cleanups: set[asyncio.Future] = set()

    async def close(self):
        # Let cleanups orphaned by cancelled solves finish first, or they could requeue or
        # relaunch an instance into the pool after close() has shut it down.
        if self._background_cleanups:
            await asyncio.wait(set(self._background_cleanups), timeout=POOLED_CLEANUP_TIMEOUT_SECONDS * 2 + 10)
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
            "queue_depth": len(self._queued_at),
            "oldest_queue_wait_seconds": round(max(0.0, time.monotonic() - min(self._queued_at)), 3) if self._queued_at else 0.0,
            "oldest_checkout_seconds": round(max(0.0, time.monotonic() - min(self._checkout_at.values())), 3) if self._checkout_at else 0.0,
        }

    async def self_test(self) -> Dict[str, Any]:
        """Launch an ephemeral Camoufox and run JS to prove Tier 3 works end to end.

        Has its own timeout because a PUID/PGID launch can hang the IPC handshake forever."""
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
            context = await browser_instance.new_context(service_workers="block") if hasattr(browser_instance, "new_context") else browser_instance
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
        capture_screenshot: bool = False,
        screenshot_full_page: bool = False,
        screenshot_selector: Optional[str] = None,
        extract_records: Optional[Dict[str, Any]] = None,
        follow_meta_refresh: bool = False,
    ) -> SolutionModel:
        wait_start = time.monotonic()
        self._queued_at.add(wait_start)
        try:
            await self.semaphore.acquire()
        finally:
            self._queued_at.discard(wait_start)
        try:
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

            # A warm process's UA and proxy are fixed at launch, so custom-UA/proxy requests skip the pool.
            use_pool = self.camoufox_pool is not None and not pw_proxy and not user_agent
            last_error: Optional[BaseException] = None

            if use_pool:
                pooled_timeout_ms = _pooled_attempt_budget_ms(timeout_ms)
                tier_timeout = (pooled_timeout_ms / 1000.0) + SOLVE_WALLCLOCK_GRACE_SECONDS
                try:
                    sol = await asyncio.wait_for(
                        self._solve_with_pooled_camoufox(
                            url=url, method=method, post_data=post_data, cookies=cookies, timeout_ms=pooled_timeout_ms,
                            headers=headers, start_time=start_time, wait_selector=wait_selector,
                            wait_delay_ms=wait_delay_ms, capture_screenshot=capture_screenshot,
                            screenshot_full_page=screenshot_full_page, screenshot_selector=screenshot_selector,
                            extract_records=extract_records,
                            follow_meta_refresh=follow_meta_refresh,
                        ),
                        timeout=tier_timeout
                    )
                    if _is_solution_acceptable(sol):
                        return sol
                    last_error = RuntimeError(f"Pooled Camoufox solve incomplete (status {sol.status if sol else 'N/A'})")
                    logger.warning(f"[CamoufoxEngine] Pooled Camoufox solve incomplete (Status {sol.status if sol else 'N/A'}).")
                except Exception as e:
                    last_error = _describe_solve_error(e, tier_timeout, "Pooled Camoufox solve")
                    logger.warning(f"[CamoufoxEngine] Pooled Camoufox solve notice/fallback: {last_error}.")
                    if isinstance(e, ChallengeNotSolvedError) and e.ip_blocked:
                        # The retry would leave from the same IP, so fail now and leave the budget to Tier 4.
                        self._crashes_total += 1
                        raise last_error

            # Fresh fingerprint: the pooled path's retry, or the only attempt for proxy/custom-UA requests.
            # A retry gets only what's left of the caller's budget, not a second full timeout.
            ephemeral_timeout_ms = timeout_ms
            tier_timeout = (timeout_ms / 1000.0) + SOLVE_WALLCLOCK_GRACE_SECONDS
            if use_pool:
                ephemeral_timeout_ms = int(timeout_ms - (time.monotonic() - start_time) * 1000)
                if ephemeral_timeout_ms < MIN_RETRY_TIMEOUT_MS:
                    logger.warning(f"[CamoufoxEngine] Skipping fresh ephemeral Camoufox retry: only {max(0, ephemeral_timeout_ms)}ms of budget left.")
                    self._crashes_total += 1
                    raise last_error or RuntimeError(f"Camoufox solve failed for {url}")
                logger.info(f"[CamoufoxEngine] Retrying with a fresh ephemeral Camoufox instance ({ephemeral_timeout_ms}ms of budget left)...")
                tier_timeout = (ephemeral_timeout_ms / 1000.0) + SOLVE_WALLCLOCK_GRACE_SECONDS
            try:
                sol = await asyncio.wait_for(
                    self._solve_with_ephemeral_camoufox(
                        url=url, method=method, post_data=post_data, cookies=cookies, pw_proxy=pw_proxy,
                        user_agent=user_agent, timeout_ms=ephemeral_timeout_ms, active_ua=active_ua, headers=headers,
                        start_time=start_time, wait_selector=wait_selector, wait_delay_ms=wait_delay_ms,
                        capture_screenshot=capture_screenshot, screenshot_full_page=screenshot_full_page,
                        screenshot_selector=screenshot_selector, extract_records=extract_records,
                        follow_meta_refresh=follow_meta_refresh,
                    ),
                    timeout=tier_timeout
                )
                if _is_solution_acceptable(sol):
                    return sol
                last_error = RuntimeError(f"Ephemeral Camoufox solve incomplete (status {sol.status if sol else 'N/A'})")
                logger.warning(f"[CamoufoxEngine] Ephemeral Camoufox solve incomplete (Status {sol.status if sol else 'N/A'}).")
            except Exception as e:
                last_error = _describe_solve_error(e, tier_timeout, "Ephemeral Camoufox solve")
                logger.warning(f"[CamoufoxEngine] Ephemeral Camoufox solve notice/fallback: {last_error}.")

            self._crashes_total += 1
            raise last_error or RuntimeError(f"Camoufox solve failed for {url}")
        finally:
            self.semaphore.release()

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
        capture_screenshot: bool,
        screenshot_full_page: bool = False,
        screenshot_selector: Optional[str] = None,
        extract_records: Optional[Dict[str, Any]] = None,
        follow_meta_refresh: bool = False,
    ) -> SolutionModel:
        # Leave the ephemeral retry its minimum window (plus launch slack) out of the caller's budget.
        remaining_s = timeout_ms / 1000.0 - (time.monotonic() - start_time)
        wait_timeout = min(POOL_ACQUIRE_TIMEOUT_SECONDS, max(1.0, remaining_s - MIN_RETRY_TIMEOUT_MS / 1000.0 - 1.0))
        try:
            inst = await self.camoufox_pool.acquire(wait_timeout=wait_timeout)
        except asyncio.TimeoutError as e:
            raise TimeoutError(
                f"No warm Camoufox instance became available within {wait_timeout:.0f}s"
            ) from e
        self._checkout_at[id(inst)] = time.monotonic()
        context = None
        page = None
        setup_ok = False
        disconnected = False
        try:
            logger.info(f"[CamoufoxPool] Checked out warm instance (use #{inst.uses}) for {url}...")
            if hasattr(inst.browser, "new_context"):
                context = await inst.browser.new_context(service_workers="block")
            elif inst.browser.contexts:
                context = inst.browser.contexts[0]
            else:
                context = inst.browser
            page = await context.new_page()
            active_ua = await page.evaluate("() => navigator.userAgent")
            # The process answered JS, so later failures are page-level and shouldn't evict it.
            setup_ok = True
            return await self._execute_solve_flow(
                context=context,
                page=page,
                url=url,
                method=method,
                post_data=post_data,
                cookies=cookies,
                timeout_ms=timeout_ms,
                deadline=start_time + timeout_ms / 1000.0,
                active_ua=active_ua or settings.DEFAULT_USER_AGENT,
                headers=headers,
                start_time=start_time,
                wait_selector=wait_selector,
                wait_delay_ms=wait_delay_ms,
                capture_screenshot=capture_screenshot, screenshot_full_page=screenshot_full_page,
                screenshot_selector=screenshot_selector, extract_records=extract_records,
                follow_meta_refresh=follow_meta_refresh,
            )
        except BaseException as exc:
            disconnected = _is_browser_disconnected(exc)
            raise
        finally:
            # Shielded: a client disconnect followed by the tier timeout cancels this task twice,
            # and a cancel landing mid-cleanup used to skip release() and leak the pool slot for good.
            cleanup = asyncio.ensure_future(self._release_pooled(inst, context, page, setup_ok, disconnected))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                if not cleanup.done():
                    self._background_cleanups.add(cleanup)
                    cleanup.add_done_callback(self._background_cleanups.discard)
                raise

    async def _release_pooled(self, inst: Any, context: Any, page: Any, setup_ok: bool, disconnected: bool):
        try:
            if page:
                try:
                    await asyncio.wait_for(page.close(), timeout=POOLED_CLEANUP_TIMEOUT_SECONDS)
                except Exception:
                    disconnected = True
            if context and context is not inst.browser:
                try:
                    await asyncio.wait_for(context.close(), timeout=POOLED_CLEANUP_TIMEOUT_SECONDS)
                except Exception:
                    disconnected = True
            # A failure before setup_ok (or a close that hung) means the process may be wedged, so recycle it.
            await self.camoufox_pool.release(
                inst, force_recycle=not setup_ok or disconnected or not _browser_is_connected(inst.browser)
            )
        except Exception as e:
            logger.warning(f"[CamoufoxPool] Release of pooled instance failed: {e}")
        finally:
            self._checkout_at.pop(id(inst), None)

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
        capture_screenshot: bool,
        screenshot_full_page: bool = False,
        screenshot_selector: Optional[str] = None,
        extract_records: Optional[Dict[str, Any]] = None,
        follow_meta_refresh: bool = False,
    ) -> SolutionModel:
        """Non-pooled launch for requests with their own proxy or user_agent, both fixed at launch."""
        # timeout_ms is what's left when this attempt starts, so its clock starts here (launch included).
        deadline = time.monotonic() + timeout_ms / 1000.0
        use_geoip = bool(pw_proxy) and settings.CAMOUFOX_GEOIP_ON_PROXY
        logger.info(f"[CamoufoxEngine] Spawning ephemeral Camoufox stealth Firefox solve for {url} (proxy={'yes' if pw_proxy else 'no'}, custom_ua={'yes' if user_agent else 'no'}, geoip={'yes' if use_geoip else 'no'})...")
        async with AsyncCamoufox(
            headless=settings.HEADLESS,
            proxy=pw_proxy,
            geoip=use_geoip,
            humanize=True,
            disable_coop=True,
            os="linux",
            config={'forceScopeAccess': True},
            i_know_what_im_doing=True
        ) as browser_instance:
            if hasattr(browser_instance, "new_context"):
                context_opts: Dict[str, Any] = {"service_workers": "block"}
                if user_agent:
                    context_opts["user_agent"] = user_agent
                context = await browser_instance.new_context(**context_opts)
            elif hasattr(browser_instance, "contexts") and browser_instance.contexts:
                context = browser_instance.contexts[0]
            else:
                context = browser_instance
            page = await context.new_page()
            # Report the UA the page really sent: Camoufox generates its own, and cf_clearance is bound to it.
            # If it can't be read, report only a caller-pinned UA - never DEFAULT_USER_AGENT, which
            # Camoufox didn't send and would get cached as a false clearance binding.
            try:
                active_ua = await page.evaluate("() => navigator.userAgent") or (user_agent or "")
            except Exception:
                active_ua = user_agent or ""
            return await self._execute_solve_flow(
                context=context,
                page=page,
                url=url,
                method=method,
                post_data=post_data,
                cookies=cookies,
                timeout_ms=timeout_ms,
                deadline=deadline,
                active_ua=active_ua,
                headers=headers,
                start_time=start_time,
                wait_selector=wait_selector,
                wait_delay_ms=wait_delay_ms,
                capture_screenshot=capture_screenshot, screenshot_full_page=screenshot_full_page,
                screenshot_selector=screenshot_selector, extract_records=extract_records,
                follow_meta_refresh=follow_meta_refresh,
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
        capture_screenshot: bool = False,
        screenshot_full_page: bool = False,
        screenshot_selector: Optional[str] = None,
        extract_records: Optional[Dict[str, Any]] = None,
        deadline: Optional[float] = None,
        follow_meta_refresh: bool = False,
    ) -> SolutionModel:
        # Navigation and the challenge loop share one deadline; the loop used to get a fresh full
        # timeout_ms after navigation, overrunning the tier's wait_for and starving the ephemeral retry.
        if deadline is None:
            deadline = time.monotonic() + timeout_ms / 1000.0
        pw_cookies = build_playwright_cookies(url, cookies)
        if pw_cookies:
            try:
                await context.add_cookies(pw_cookies)
                logger.debug(f"[BrowserPool] Pre-loaded {len(pw_cookies)} cookie(s) into browser context")
            except Exception as e:
                logger.warning(f"[BrowserPool] Error pre-loading cookies: {e}")

        await install_media_blocking(context)

        # Sensor cookies already in the context (cached ones replayed above) are not proof of a pass
        # during this solve: if a wall is up despite them, the site has stopped honouring them.
        target_host = host_of(url)
        sensor_baseline = sensor_snapshot(await read_context_cookies(context), target_host)

        # Track the real final status across challenge redirects; a clean title can still be a 404.
        last_main_status: Dict[str, Optional[int]] = {"code": None}
        # Providers declare some walls only in response headers (cf-mitigated, x-amzn-waf-action, x-dd-b).
        last_main_headers: Dict[str, str] = {}
        # Kept so a non-HTML document (JSON, XML, plain text) is returned as its own bytes rather
        # than as the viewer page Firefox renders around it.
        last_main_response: Dict[str, Any] = {}

        def _on_response(resp):
            try:
                req = resp.request
                if req.resource_type == "document" and req.frame == page.main_frame:
                    last_main_status["code"] = resp.status
                    last_main_headers.clear()
                    last_main_headers.update(resp.headers)
                    last_main_response["resp"] = resp
            except Exception:
                pass

        page.on("response", _on_response)

        nav_timeout_ms = max(1000, int((deadline - time.monotonic()) * 1000))
        response, initial_status = await navigate_to_target(page, url, method, post_data, nav_timeout_ms)

        curr_page_url = page.url or ""
        if response is None and initial_status == 0 and (not curr_page_url or curr_page_url.startswith("about:blank")):
            logger.warning(f"[BrowserPool] Browser navigation failed to reach target URL '{url}'")
            return SolutionModel(
                url=url,
                status=502,
                headers={"content-type": "text/html"},
                response="<html><body><h1>502 Bad Gateway</h1><p>Browser navigation failed to reach target URL</p></body></html>",
                cookies=[],
                userAgent=active_ua
            )

        initial_title = ""
        try:
            initial_title = await page.title()
        except Exception:
            pass

        logger.info(f"[BrowserPool] Initial page load complete (HTTP Status: {initial_status}, Title: '{initial_title}')")

        step = 0.2
        loop_start = time.monotonic()
        loop_deadline = deadline - SOLVE_FINALIZE_RESERVE_SECONDS
        iteration = 0
        content_check_every = 4
        next_click_at = 0.0
        last_widget_report = 0.0
        last_logged_step = 0.0
        age_gate_clicked = False
        last_detected_challenge: Optional[str] = None
        # Wall verdict from the last pass that read the page content; title-only passes can't refresh it.
        active_wall = False
        widget_seen_at: Optional[float] = None
        wall_seen = False
        unsolvable_passes = 0
        sensor_earned_at: Optional[float] = None
        renavigated_at: Optional[float] = None
        akamai_held = False
        cleared = False

        while time.monotonic() < loop_deadline:
            check_content = (iteration % content_check_every) == 0
            iteration += 1

            try:
                title = await page.title()
            except Exception:
                title = ""

            curr_url = page.url or ""
            if is_browser_error(title, curr_url):
                logger.warning(f"[BrowserPool] Browser error page encountered: '{title}' ({curr_url})")
                break

            content = ""
            if check_content:
                try:
                    content = await asyncio.wait_for(page.content(), timeout=2.0)
                except Exception:
                    content = ""

            content_lower = content.lower() if content else ""

            if check_content:
                _raise_if_ip_blocked(content, curr_url)
                if anubis_state(content) == "blocked":
                    raise ChallengeNotSolvedError("anubis", ANUBIS_REJECTED_REASON)

            active_challenge = detect_challenge(
                title, content, check_content, headers=last_main_headers, status=last_main_status["code"]
            )
            if check_content:
                active_wall = is_challenge_wall(
                    active_challenge, title, content, status=last_main_status["code"], headers=last_main_headers
                )
                if active_challenge and not active_wall and active_challenge not in WIDGET_CHALLENGES:
                    # A provider's telemetry script on the real page, not a challenge.
                    active_challenge = None
            if not active_challenge and is_anubis_verification_url(curr_url):
                # Between a solved proof-of-work and the redirect back to the page asked for.
                active_challenge = "anubis"
            if active_challenge:
                last_detected_challenge = active_challenge
            elif not check_content and last_detected_challenge:
                # Most markers live only in page content, so a content-skipped pass can't prove it cleared.
                active_challenge = last_detected_challenge

            if check_content and active_wall and active_challenge:
                wall_cookies = await read_context_cookies(context)
                if not wall_seen:
                    wall_seen = True
                    sensor_baseline |= sensor_snapshot(wall_cookies, target_host, BLOCK_PAGE_COOKIES)

                if needs_unsolvable_captcha(active_challenge, content, last_main_headers, last_main_status["code"]):
                    # Two readings, so a page caught mid-transition isn't written off.
                    unsolvable_passes += 1
                    if unsolvable_passes >= 2:
                        raise ChallengeNotSolvedError(active_challenge, UNSOLVABLE_CAPTCHA_REASON)
                else:
                    unsolvable_passes = 0

                now_wall = time.monotonic()
                if renavigated_at is not None:
                    if now_wall - renavigated_at >= POST_RENAVIGATE_GRACE_SECONDS:
                        # Fail the attempt now so the retry and Tier 4 get what's left of the budget.
                        raise ChallengeNotSolvedError(
                            active_challenge, "its clearance cookie was issued but the challenge kept being served"
                        )
                elif has_earned_sensor_cookie(active_challenge, wall_cookies, target_host, sensor_baseline):
                    if sensor_earned_at is None:
                        sensor_earned_at = now_wall
                        logger.info(f"[BrowserPool] {active_challenge} clearance cookie issued; waiting for the site to redirect.")
                    elif now_wall - sensor_earned_at >= SENSOR_REDIRECT_GRACE_SECONDS and loop_deadline - now_wall > 2.0:
                        # The check passed but the provider's own redirect never fired: load the target ourselves.
                        logger.info(f"[BrowserPool] {active_challenge} cookie set but still on the challenge page; reloading {url}")
                        await navigate_to_target(
                            page, url, method, post_data, max(1000, int((deadline - now_wall) * 1000))
                        )
                        renavigated_at = time.monotonic()
                        continue

            # A captcha widget embedded in the real page gets a bounded window, not the whole budget.
            if active_challenge in WIDGET_CHALLENGES and not active_wall and not is_challenge_title(title):
                if widget_seen_at is None:
                    widget_seen_at = time.monotonic()
                elif time.monotonic() - widget_seen_at >= WIDGET_SOLVE_WINDOW_SECONDS:
                    logger.info(f"[BrowserPool] Embedded {active_challenge} widget left unsolved after {WIDGET_SOLVE_WINDOW_SECONDS:.0f}s; returning the page as-is.")
                    break
            else:
                widget_seen_at = None

            # Cleared means no challenge, a clearance cookie, or a populated widget response token.
            challenge_cleared = False
            if not is_challenge_title(title):
                if not active_challenge:
                    challenge_cleared = True
                elif check_content:
                    # Only for a widget on a real page. On a wall the cookie alone isn't the page:
                    # the sensor-cookie handling above waits for the redirect or reloads the target.
                    if not active_wall:
                        try:
                            raw_cookies = await read_context_cookies(context)
                            if any(c.get("name") in ("cf_clearance", "aws-waf-token") for c in raw_cookies):
                                challenge_cleared = True
                        except Exception:
                            pass

                    if not challenge_cleared and active_challenge == "cap":
                        challenge_cleared = await cap_widgets_solved(page)

                    if not challenge_cleared and active_challenge != "cap":
                        try:
                            widget_solved = await page.evaluate("""() => {
                                const ts = document.querySelector('[name="cf-turnstile-response"], input[name*="turnstile-response"]');
                                if (ts && ts.value && ts.value.length > 10) return true;
                                const rc = document.querySelector('[name="g-recaptcha-response"]');
                                if (rc && rc.value && rc.value.length > 10) return true;
                                const hc = document.querySelector('[name="h-captcha-response"]');
                                if (hc && hc.value && hc.value.length > 10) return true;
                                return false;
                            }""")
                            if widget_solved:
                                challenge_cleared = True
                        except Exception:
                            pass

            if challenge_cleared:
                # Avoid returning a hollow mid-redirect snapshot.
                page_ready = False
                if is_non_html_text(_header_value(last_main_headers, "content-type")):
                    # A short JSON or text answer has no title and a tiny body; it is complete as served.
                    page_ready = True
                elif title and title.strip():
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

            if now_ts >= next_click_at and (now_ts - loop_start) > 0.6 and active_challenge == "akamai" and active_wall:
                # Akamai scores pointer telemetry rather than a click: hold its button once, then keep moving.
                if not akamai_held and loop_deadline - now_ts > AKAMAI_HOLD_SECONDS + 3.0:
                    akamai_held = await akamai_press_and_hold(page)
                await wander_mouse(page)
                next_click_at = time.monotonic() + CHALLENGE_CLICK_RETRY_SECONDS
            elif active_challenge == "anubis":
                # The page's own JS computes Anubis's proof-of-work and redirects; nothing to click.
                pass
            elif now_ts >= next_click_at and (now_ts - loop_start) > 0.6 and active_challenge == "cap":
                started = await start_cap_solve(page)
                next_click_at = time.monotonic() + (CHALLENGE_CLICK_COOLDOWN_SECONDS if started else CHALLENGE_CLICK_RETRY_SECONDS)
            elif now_ts >= next_click_at and (now_ts - loop_start) > 0.6:
                clicked, age_gate_clicked = await dispatch_challenge_click(page, active_challenge, title, age_gate_clicked)
                next_click_at = time.monotonic() + (CHALLENGE_CLICK_COOLDOWN_SECONDS if clicked else CHALLENGE_CLICK_RETRY_SECONDS)
                if not clicked and active_challenge and (now_ts - last_widget_report) >= WIDGET_REPORT_INTERVAL_SECONDS:
                    # Nothing clickable on a live challenge: log what the page has so the selectors can be fixed.
                    last_widget_report = now_ts
                    logger.info(f"[BrowserPool] No clickable {active_challenge} widget found. {await describe_challenge_frames(page)}")

            await asyncio.sleep(step)

        # Tier 3.5: paid solver, only after the free click loop used its whole timeout.
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

        if follow_meta_refresh and not is_browser_error(await _safe_title(page), page.url or ""):
            await follow_meta_refresh_in_page(page, deadline - SOLVE_FINALIZE_RESERVE_SECONDS)

        if wait_selector:
            try:
                logger.info(f"[BrowserPool] Waiting for custom selector '{wait_selector}'...")
                await page.wait_for_selector(wait_selector, timeout=5000)
            except Exception as e:
                logger.warning(f"[BrowserPool] wait_selector '{wait_selector}' timed out: {e}")

        if wait_delay_ms and wait_delay_ms > 0:
            await asyncio.sleep(wait_delay_ms / 1000.0)
        else:
            await asyncio.sleep(0.3)

        rendered_records = await extract_rendered_records(page, extract_records) if extract_records else None
        screenshot_b64 = None
        if capture_screenshot:
            try:
                if screenshot_selector:
                    locator = page.locator(screenshot_selector).first
                    bounds = await locator.bounding_box(timeout=3000)
                    if not bounds or bounds["height"] > settings.MAX_SCREENSHOT_HEIGHT_PX:
                        raise ValueError("Screenshot element is missing or exceeds height limit")
                    img_bytes = await locator.screenshot(type="jpeg", quality=60, timeout=5000)
                else:
                    if screenshot_full_page:
                        height = await page.evaluate("() => document.documentElement.scrollHeight")
                        if height > settings.MAX_SCREENSHOT_HEIGHT_PX:
                            raise ValueError("Full-page screenshot exceeds height limit")
                    img_bytes = await page.screenshot(type="jpeg", quality=60, full_page=screenshot_full_page, timeout=5000)
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

        response_headers = {"content-type": "text/html"}
        main_content_type = _header_value(last_main_headers, "content-type")
        main_resp = last_main_response.get("resp")
        if main_resp is not None and is_non_html_text(main_content_type):
            try:
                raw = await asyncio.wait_for(main_resp.body(), timeout=5.0)
                html_content = decode_text_body(raw, main_content_type)
                response_headers = {"content-type": main_content_type}
            except Exception as e:
                logger.debug(f"[BrowserPool] Raw body read notice: {e}")

        # A wall that outlived the attempt is a failed solve, not a page to hand back as solved.
        if not cleared and not is_browser_error(final_title, final_url):
            final_status = last_main_status["code"]
            final_challenge = detect_challenge(final_title, html_content, True, headers=last_main_headers, status=final_status)
            _raise_if_ip_blocked(html_content, final_url)
            if anubis_state(html_content) == "blocked":
                raise ChallengeNotSolvedError("anubis", ANUBIS_REJECTED_REASON)
            if is_challenge_wall(final_challenge, final_title, html_content, status=final_status, headers=last_main_headers):
                raise ChallengeNotSolvedError(
                    final_challenge or last_detected_challenge or "unrecognized",
                    f"still present after {time.monotonic() - loop_start:.0f}s",
                )

        raw_cookies = await read_context_cookies(context)
        captured_cookies = extract_captured_cookies(raw_cookies)

        # Prefer the last main-frame status, then the initial response, then a guess.
        if is_browser_error(final_title, final_url):
            status_code = 502
        elif last_main_status["code"] is not None:
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
            headers=response_headers,
            response=html_content,
            cookies=captured_cookies,
            userAgent=active_ua,
            challengeType=last_detected_challenge
        )

        if screenshot_b64:
            solution.screenshot = screenshot_b64
        if rendered_records is not None:
            solution.extracted = rendered_records

        return solution


browser_pool = BrowserPool()
