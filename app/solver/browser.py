import asyncio
import base64
import dataclasses
import html
import json
import logging
import time
from urllib.parse import urlparse
from typing import Dict, List, Optional, Tuple, Any
from playwright.async_api import Page
from app.config import settings
from app.models.flaresolverr import CookieModel, SolutionModel
from app.solver.human_cursor import human_click
from app.solver.captcha_solver import captcha_solver
from app.logging_config import sanitize_proxy_url

try:
    from camoufox.async_api import AsyncCamoufox
    CAMOUFOX_AVAILABLE = True
except ImportError:
    CAMOUFOX_AVAILABLE = False

logger = logging.getLogger("solverr.browser")

@dataclasses.dataclass
class _PooledCamoufox:
    cm: Any
    browser: Any
    created_at: float
    uses: int = 0

class CamoufoxPool:
    """Bounded pool of warm, no-proxy Camoufox browser processes.

    Spawning a fresh Camoufox (Firefox) process per solve costs real wall
    time (process start, fingerprint generation, extension load) on every
    single tier-3 request. This pool keeps up to `size` processes alive and
    hands out a fresh *context* per solve instead, closing the context (not
    the process) when done. Instances are recycled after N uses or N
    seconds so a single fingerprint isn't reused indefinitely.

    Only used for the no-proxy path: a request-specific proxy needs its own
    Camoufox launch so geolocation/timezone/WebRTC fingerprint derivation
    (which Camoufox ties to the proxy's exit IP at launch time) stays
    consistent - that path still spawns an ephemeral instance.
    """

    def __init__(self, size: int):
        self.size = max(1, size)
        self._idle: "asyncio.Queue[_PooledCamoufox]" = asyncio.Queue()
        self._created = 0
        self._lock = asyncio.Lock()
        self.recycles_total = 0

    async def acquire(self) -> _PooledCamoufox:
        try:
            inst = self._idle.get_nowait()
            inst.uses += 1
            return inst
        except asyncio.QueueEmpty:
            pass

        async with self._lock:
            if self._created < self.size:
                inst = await self._launch_instance()
                self._created += 1
                inst.uses += 1
                return inst

        # At capacity - wait for a peer to finish and check its instance back in.
        inst = await self._idle.get()
        inst.uses += 1
        return inst

    async def release(self, inst: _PooledCamoufox):
        if self._should_recycle(inst):
            self.recycles_total += 1
            await self._close_instance(inst)
            async with self._lock:
                self._created -= 1
                fresh = await self._relaunch_with_retry()
                if fresh is not None:
                    self._created += 1
            if fresh is not None:
                self._idle.put_nowait(fresh)
            return
        self._idle.put_nowait(inst)

    async def _relaunch_with_retry(self, attempts: int = 3, backoff_seconds: float = 1.0) -> Optional[_PooledCamoufox]:
        """Retry a recycled instance's relaunch a few times before giving up.

        A bare launch failure (transient resource pressure - exactly what's
        likely on a small NAS) used to permanently drop this slot from the
        pool with no retry, which could also strand a concurrent waiter
        blocked on `_idle.get()` since nothing else replenishes it.
        """
        for attempt in range(1, attempts + 1):
            try:
                return await self._launch_instance()
            except Exception as e:
                if attempt == attempts:
                    logger.error(
                        f"[CamoufoxPool] Failed to relaunch recycled instance after {attempts} attempt(s) "
                        f"({e}). Pool capacity reduced by 1 until a future acquire() replenishes it."
                    )
                    return None
                logger.warning(f"[CamoufoxPool] Relaunch attempt {attempt}/{attempts} failed ({e}); retrying in {backoff_seconds}s...")
                await asyncio.sleep(backoff_seconds)
        return None

    def _should_recycle(self, inst: _PooledCamoufox) -> bool:
        return (
            inst.uses >= settings.CAMOUFOX_POOL_RECYCLE_USES
            or (time.time() - inst.created_at) >= settings.CAMOUFOX_POOL_RECYCLE_SECONDS
        )

    async def _launch_instance(self) -> _PooledCamoufox:
        # os="linux" pins fingerprint generation to Linux only (Camoufox
        # otherwise randomizes across windows/macos/linux per launch). This
        # container always runs on Linux, so claiming Linux avoids any
        # OS/host mismatch leaking through elsewhere, and lets the Dockerfile
        # ship only the Linux font set instead of all three (~890MB smaller).
        cm = AsyncCamoufox(
            headless=settings.HEADLESS,
            humanize=True,
            disable_coop=True,
            os="linux",
            config={'forceScopeAccess': True},
            i_know_what_im_doing=True
        )
        try:
            browser = await asyncio.wait_for(cm.__aenter__(), timeout=CAMOUFOX_LAUNCH_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as e:
            # __aenter__ never completed, so __aexit__ won't be called by an
            # `async with` anywhere - close it ourselves to avoid leaking a
            # half-started Firefox process.
            try:
                await cm.__aexit__(None, None, None)
            except Exception:
                pass
            raise TimeoutError(
                f"Camoufox launch did not complete within {CAMOUFOX_LAUNCH_TIMEOUT_SECONDS}s"
            ) from e
        logger.info(f"[CamoufoxPool] Warmed stealth browser instance ({self._created + 1}/{self.size})")
        return _PooledCamoufox(cm=cm, browser=browser, created_at=time.time())

    async def _close_instance(self, inst: _PooledCamoufox):
        try:
            await inst.cm.__aexit__(None, None, None)
        except Exception as e:
            logger.debug(f"[CamoufoxPool] Instance close notice: {e}")

    async def close(self):
        async with self._lock:
            while True:
                try:
                    inst = self._idle.get_nowait()
                except asyncio.QueueEmpty:
                    break
                await self._close_instance(inst)
            self._created = 0
            logger.info("[CamoufoxPool] Pool stopped.")


# Multi-WAF challenge marker table, keyed by the challenge type name used
# both in logs and in PerformanceMetrics.challenges_solved (engine.py).
CHALLENGE_MARKERS: Dict[str, List[str]] = {
    "cloudflare_turnstile": ["just a moment...", "turnstile", "cf-challenge", "checking your browser"],
    "cloudflare_5s": ["attention required!", "ddos protection by cloudflare", "please wait 5 seconds"],
    "ddos_guard": ["ddos-guard", "check.ddos-guard.net", "ddg-captcha", "ddos protection by ddos-guard"],
    "recaptcha": ["g-recaptcha", "google.com/recaptcha", "recaptcha/api2"],
    "hcaptcha": ["hcaptcha.com", "h-captcha", "cf-hcaptcha"],
    "geetest": ["geetest", "gt_captcha"],
    "imperva": ["incapsula", "_incapsula_resource", "visid_incap", "sec-cpt"],
    "datadome": ["datadome", "geo.captcha-delivery.com"],
    "akamai": ["akamai", "ak_bmsc"]
}

AGE_GATE_MARKERS = ["disclaimer-dialog", "close_enter_site_button", "btn-agree"]
CHALLENGE_TITLE_MARKERS = ["just a moment", "checking your browser", "attention required", "ddos-guard", "cloudflare"]

def detect_challenge(title: str, content: str, check_content: bool) -> Optional[str]:
    """Pure lookup over CHALLENGE_MARKERS - no page/browser dependency, so
    it's directly unit-testable. `content` is only consulted when
    check_content is True (the loop only re-fetches page.content() every
    few iterations to save time)."""
    title_lower = title.lower() if title else ""
    content_lower = content.lower() if content else ""
    for ctype, markers in CHALLENGE_MARKERS.items():
        if any(m in title_lower or (check_content and m in content_lower) for m in markers):
            return ctype
    return None

def is_challenge_title(title: str) -> bool:
    title_lower = title.lower() if title else ""
    if any(t in title_lower for t in CHALLENGE_TITLE_MARKERS):
        return True
    # Cloudflare's own transitional title ("Loading https://...") shown
    # while its challenge JS finishes and window.location redirects to the
    # real page - not a genuinely cleared page yet. Treating this as clean
    # makes the loop break and snapshot the page mid-transition, which can
    # capture a stale/incomplete response (and its real status code, now
    # that status is tracked accurately - see the response listener above).
    if title_lower.startswith("loading ") and "://" in title_lower:
        return True
    return False

def has_age_gate_marker(content_lower: str, check_content: bool) -> bool:
    return check_content and any(ag in content_lower for ag in AGE_GATE_MARKERS)

# Widgets the paid captcha-solver escalation knows how to handle: which
# challenge-type key maps to which CaptchaSolverClient method, which DOM
# selectors carry the sitekey, and which response field(s) the solved
# token needs to be written into.
CAPTCHA_SOLVER_WIDGETS = {
    "recaptcha": {
        "solve_method": "solve_recaptcha_v2",
        "selectors": [".g-recaptcha", "div[data-sitekey][class*='recaptcha']"],
        "response_fields": ["g-recaptcha-response"],
    },
    "hcaptcha": {
        "solve_method": "solve_hcaptcha",
        "selectors": [".h-captcha", "div[data-sitekey][class*='hcaptcha']"],
        "response_fields": ["h-captcha-response", "g-recaptcha-response"],
    },
    "cloudflare_turnstile": {
        "solve_method": "solve_turnstile",
        "selectors": [".cf-turnstile", "div[data-sitekey][class*='turnstile']"],
        "response_fields": ["cf-turnstile-response"],
    },
}

async def extract_sitekey(page: Page, selectors: List[str]) -> Optional[Tuple[str, Optional[str]]]:
    """Find the first matching widget's data-sitekey (and data-callback, if
    the site defines one - the stable, documented way to hand a solved
    token back to the widget's own JS instead of poking internal client
    state). Returns None if no widget with a sitekey is found."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                sitekey = await loc.get_attribute("data-sitekey")
                if sitekey:
                    callback = await loc.get_attribute("data-callback")
                    return sitekey, callback
        except Exception as e:
            logger.debug(f"[CaptchaSolver] Sitekey lookup notice for '{sel}': {e}")
    return None

async def inject_captcha_token(page: Page, response_field_names: List[str], token: str, callback_name: Optional[str]):
    """Write a solved token into the widget's response field(s) and invoke
    its data-callback if one is declared, mirroring what the widget's own
    JS does when a human solves it interactively."""
    try:
        await page.evaluate(
            """(args) => {
                for (const name of args.names) {
                    const el = document.querySelector(`[name="${name}"]`) || document.getElementById(name);
                    if (el) {
                        el.innerHTML = args.token;
                        try { el.value = args.token; } catch (e) {}
                        el.style.display = 'block';
                    }
                }
                if (args.callback && typeof window[args.callback] === 'function') {
                    window[args.callback](args.token);
                }
            }""",
            {"names": response_field_names, "token": token, "callback": callback_name}
        )
    except Exception as e:
        logger.debug(f"[CaptchaSolver] Token injection notice: {e}")

# Wall-clock safety net layered on top of each solve tier's own internal
# timeouts (navigation waits, selector waits, etc). If a Playwright/Camoufox
# call hangs on something that doesn't respect its own timeout=, this forces
# the tier to give up and release its browser/pool slot rather than pinning
# it (and, on a small worker count, a meaningful fraction of total capacity)
# indefinitely.
SOLVE_WALLCLOCK_GRACE_SECONDS = 15

# Bounds a single Camoufox process launch (cm.__aenter__()) independently of
# the outer per-solve wall-clock timeout. Without this, a launch that hangs
# (confirmed reproducible under PUID/PGID non-root execution - see CLAUDE.md)
# holds CamoufoxPool._lock for the launch's full duration, serializing every
# other acquire() behind it since the lock is only released when the caller's
# outer asyncio.wait_for eventually cancels this task.
CAMOUFOX_LAUNCH_TIMEOUT_SECONDS = 30


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

        start = time.time()
        try:
            return await asyncio.wait_for(self._self_test_inner(start), timeout=SOLVE_WALLCLOCK_GRACE_SECONDS + 30)
        except asyncio.TimeoutError:
            return {
                "ok": False,
                "error": "Browser self-test timed out - Camoufox launch or IPC handshake did not complete",
                "duration_ms": round((time.time() - start) * 1000, 1)
            }
        except Exception as e:
            return {
                "ok": False,
                "error": f"{type(e).__name__}: {e}",
                "duration_ms": round((time.time() - start) * 1000, 1)
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
            "duration_ms": round((time.time() - start) * 1000, 1)
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
        wait_start = time.time()
        async with self.semaphore:
            self._queue_wait_total_s += time.time() - wait_start
            self._queue_wait_count += 1
            start_time = time.time()
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

    async def _try_captcha_solver_escalation(self, page: Page, url: str, challenge_type: str) -> bool:
        widget = CAPTCHA_SOLVER_WIDGETS.get(challenge_type)
        if not widget:
            return False

        sitekey_info = await extract_sitekey(page, widget["selectors"])
        if not sitekey_info:
            logger.info(f"[CaptchaSolver] No sitekey found for '{challenge_type}' widget - skipping paid solver escalation.")
            return False

        sitekey, callback_name = sitekey_info
        logger.info(f"[CaptchaSolver] Escalating '{challenge_type}' (sitekey='{sitekey[:16]}...') to paid solver service...")

        solve_fn = getattr(captcha_solver, widget["solve_method"])
        token = await solve_fn(sitekey, url)
        if not token:
            logger.warning(f"[CaptchaSolver] Paid solver did not return a token for '{challenge_type}'.")
            return False

        await inject_captcha_token(page, widget["response_fields"], token, callback_name)
        logger.info(f"[CaptchaSolver] Token injected for '{challenge_type}'.")
        return True

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
        pw_cookies = []
        parsed_url = urlparse(url)
        default_domain = parsed_url.netloc.split(":")[0].lstrip(".")

        if cookies:
            for c in cookies:
                domain_val = c.domain.lstrip(".") if c.domain else default_domain
                cookie_dict = {
                    "name": c.name,
                    "value": c.value,
                    "domain": domain_val,
                    "path": c.path or "/"
                }
                pw_cookies.append(cookie_dict)

        if pw_cookies:
            try:
                await context.add_cookies(pw_cookies)
                logger.debug(f"[BrowserPool] Pre-loaded {len(pw_cookies)} cookie(s) into browser context")
            except Exception as e:
                logger.warning(f"[BrowserPool] Error pre-loading cookies: {e}")

        # Block only video/audio media to save bandwidth, while preserving fonts & challenge canvases
        async def block_heavy_media(route, request):
            if request.resource_type in ["media"]:
                await route.abort()
            else:
                await route.continue_()

        try:
            await page.route("**/*", block_heavy_media)
        except Exception:
            pass

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

        # Handle navigation
        logger.info(f"[BrowserPool] Navigating to {url} ({method.upper()}, timeout: {timeout_ms}ms)")
        initial_status = 0
        response = None
        if method.upper() == "POST" and post_data:
            try:
                form_inputs = []
                is_json = False
                try:
                    json_obj = json.loads(post_data)
                    if isinstance(json_obj, dict):
                        is_json = True
                        for k, v in json_obj.items():
                            val_str = json.dumps(v) if isinstance(v, (dict, list)) else str(v)
                            escaped_k = html.escape(str(k), quote=True)
                            escaped_v = html.escape(val_str, quote=True)
                            form_inputs.append(f'<input type="hidden" name="{escaped_k}" value="{escaped_v}">')
                except Exception:
                    pass

                if not is_json:
                    if "=" in post_data:
                        from urllib.parse import parse_qsl
                        for k, v in parse_qsl(post_data, keep_blank_values=True):
                            escaped_k = html.escape(str(k), quote=True)
                            escaped_v = html.escape(str(v), quote=True)
                            form_inputs.append(f'<input type="hidden" name="{escaped_k}" value="{escaped_v}">')
                    else:
                        escaped_data = html.escape(post_data, quote=True)
                        form_inputs.append(f'<input type="hidden" name="data" value="{escaped_data}">')

                form_html = f"""<!DOCTYPE html><html><body><form id="_solverr_f" method="POST" action="{html.escape(url, quote=True)}">{''.join(form_inputs)}</form><script>document.getElementById('_solverr_f').submit();</script></body></html>"""
                await page.set_content(form_html)
                try:
                    response = await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
                    initial_status = response.status if response else 0
                except Exception:
                    initial_status = 0
            except Exception as nav_err:
                logger.warning(f"[BrowserPool] POST form navigation notice: {nav_err}")
                initial_status = 0
        else:
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                initial_status = response.status if response else 0
            except Exception as nav_err:
                logger.warning(f"[BrowserPool] Navigation notice: {nav_err}")
                initial_status = 0

        initial_title = ""
        try:
            initial_title = await page.title()
        except Exception:
            pass

        logger.info(f"[BrowserPool] Initial page load complete (HTTP Status: {initial_status}, Title: '{initial_title}')")

        # Multi-WAF challenge detection and auto-resolver loop
        max_wait = timeout_ms / 1000.0
        step = 0.2
        loop_start = time.time()
        iteration = 0
        content_check_every = 4
        last_click_ts = 0.0
        last_logged_step = 0.0
        age_gate_clicked = False
        last_detected_challenge: Optional[str] = None
        cleared = False

        while (time.time() - loop_start) < max_wait:
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
                        elapsed = time.time() - loop_start
                        logger.info(f"[BrowserPool] Challenge cleared! Final Title: '{title}' in {elapsed:.2f}s")
                        cleared = True
                        break

            now_ts = time.time()
            if (now_ts - last_logged_step) >= 3.0:
                elapsed = now_ts - loop_start
                state_label = active_challenge or ("age_gate" if not age_gate_clicked else "page_stabilizing")
                logger.info(f"[BrowserPool] Anti-bot / gate active ({state_label}, {elapsed:.1f}s elapsed) | Current Title: '{title}'")
                last_logged_step = now_ts

            # Periodic Interactive Challenge Solver Dispatcher
            if (now_ts - last_click_ts) >= 1.2 and (now_ts - loop_start) > 0.6:
                last_click_ts = now_ts
                clicked = False

                # 1. Cloudflare Turnstile Frame-Level Checkbox Clicker
                if not clicked:
                    for frame in page.frames:
                        if any(x in frame.url.lower() for x in ["challenges.cloudflare.com", "turnstile", "cf-challenge"]):
                            try:
                                cb_loc = frame.locator("input[type='checkbox'], .ctp-checkbox-label, body").first
                                box = await cb_loc.bounding_box()
                                if box and box['width'] > 15 and box['height'] > 15:
                                    click_x = box['x'] + (28.0 if box['width'] > 60 else box['width'] / 2.0)
                                    click_y = box['y'] + (box['height'] / 2.0)
                                    logger.info(f"[Turnstile] Cloudflare frame target detected at ({click_x:.0f}, {click_y:.0f}). Dispatching human click...")
                                    await human_click(page, click_x, click_y)
                                    clicked = True
                                    break
                            except Exception as f_err:
                                logger.debug(f"[Turnstile] Frame click notice: {f_err}")

                # 2. Cloudflare Turnstile Element / Top-Level Locator Checkbox Clicker
                if not clicked:
                    turnstile_locators = [
                        "iframe[src*='challenges.cloudflare.com']",
                        "iframe[src*='turnstile']",
                        "iframe[src*='cloudflare']",
                        "div.cf-turnstile iframe",
                        "#turnstile-wrapper iframe",
                        "#challenge-stage iframe",
                        "div[data-sitekey] iframe",
                        "iframe[title*='Cloudflare']",
                        "iframe[title*='Turnstile']",
                        "#turnstile-wrapper",
                        "#challenge-stage",
                        ".cf-turnstile"
                    ]
                    for t_sel in turnstile_locators:
                        try:
                            loc = page.locator(t_sel).first
                            if await loc.count() > 0:
                                box = await loc.bounding_box()
                                if box and box['width'] > 15 and box['height'] > 15:
                                    click_x = box['x'] + (28.0 if box['width'] > 60 else box['width'] / 2.0)
                                    click_y = box['y'] + (box['height'] / 2.0)
                                    logger.info(f"[Turnstile] Detected widget '{t_sel}' at ({click_x:.0f}, {click_y:.0f}). Dispatching human click...")
                                    await human_click(page, click_x, click_y)
                                    clicked = True
                                    break
                        except Exception as t_err:
                            logger.debug(f"[Turnstile] Locator notice for '{t_sel}': {t_err}")

                # 3. reCAPTCHA v2 / Enterprise Checkbox Locator
                if not clicked:
                    try:
                        recap_loc = page.locator("iframe[src*='recaptcha/api2/anchor'], iframe[src*='google.com/recaptcha']").first
                        if await recap_loc.count() > 0:
                            box = await recap_loc.bounding_box()
                            if box and box['width'] > 15 and box['height'] > 15:
                                click_x = box['x'] + 28.0
                                click_y = box['y'] + (box['height'] / 2.0)
                                logger.info(f"[reCAPTCHA] Detected anchor at ({click_x:.0f}, {click_y:.0f}). Dispatching human click...")
                                await human_click(page, click_x, click_y)
                                clicked = True
                    except Exception as recap_err:
                        logger.debug(f"[reCAPTCHA] Notice: {recap_err}")

                # 4. hCaptcha Checkbox Locator
                if not clicked:
                    try:
                        hcap_loc = page.locator("iframe[src*='hcaptcha.com']").first
                        if await hcap_loc.count() > 0:
                            box = await hcap_loc.bounding_box()
                            if box and box['width'] > 15 and box['height'] > 15:
                                click_x = box['x'] + 28.0
                                click_y = box['y'] + (box['height'] / 2.0)
                                logger.info(f"[hCaptcha] Detected frame at ({click_x:.0f}, {click_y:.0f}). Dispatching human click...")
                                await human_click(page, click_x, click_y)
                                clicked = True
                    except Exception as hcap_err:
                        logger.debug(f"[hCaptcha] Notice: {hcap_err}")

                # 4.5. Deep Shadow DOM & Web Components Walker Fallback
                if not clicked and (active_challenge in ["cloudflare_turnstile", "recaptcha", "hcaptcha"] or is_challenge_title(title)):
                    try:
                        shadow_box = await page.evaluate("""() => {
                            function findInRoot(root) {
                                if (!root) return null;
                                const candidates = root.querySelectorAll("iframe, input[type='checkbox'], div[class*='turnstile'], div[class*='captcha'], div[id*='turnstile'], div[id*='challenge']");
                                for (const el of candidates) {
                                    const rect = el.getBoundingClientRect();
                                    if (rect && rect.width > 15 && rect.height > 15 && rect.top >= 0 && rect.left >= 0) {
                                        const src = (el.src || "").toLowerCase();
                                        const cls = (el.className || "").toString().toLowerCase();
                                        const id = (el.id || "").toLowerCase();
                                        if (src.includes("turnstile") || src.includes("challenges.cloudflare") || src.includes("captcha") ||
                                            cls.includes("turnstile") || cls.includes("captcha") || id.includes("turnstile") || id.includes("challenge")) {
                                            return { x: rect.left, y: rect.top, width: rect.width, height: rect.height };
                                        }
                                    }
                                }
                                const all = root.querySelectorAll('*');
                                for (const el of all) {
                                    if (el.shadowRoot) {
                                        const found = findInRoot(el.shadowRoot);
                                        if (found) return found;
                                    }
                                }
                                return null;
                            }
                            return findInRoot(document);
                        }""")
                        if shadow_box and shadow_box.get('width', 0) > 15:
                            bx = shadow_box['x']
                            by = shadow_box['y']
                            bw = shadow_box['width']
                            bh = shadow_box['height']
                            click_x = bx + (28.0 if bw > 60 else bw / 2.0)
                            click_y = by + (bh / 2.0)
                            logger.info(f"[ShadowDOM] Detected widget inside shadow root at ({click_x:.0f}, {click_y:.0f}). Dispatching human click...")
                            await human_click(page, click_x, click_y)
                            clicked = True
                    except Exception as s_err:
                        logger.debug(f"[ShadowDOM] Walker notice: {s_err}")

                # 5. Modal Disclaimer / Age Gate Dismissal (Chaturbate, SpankBang, etc.)
                if not clicked and not age_gate_clicked:
                    try:
                        age_selectors = [
                            "button#enter_site", "#close_enter_site_button", ".btn-agree", "#btn-agree",
                            "a.btn-agree", "button[data-action='agree']", "button[data-action='enter']",
                            "#age-verification button", ".age-verification button", ".ageGate button",
                            "#ageGate button", "div.disclaimer-dialog button", ".age_verify button",
                            "button:has-text('I AGREE')", "button:has-text('I AM 18')"
                        ]
                        for ag_sel in age_selectors:
                            ag_loc = page.locator(ag_sel).first
                            if await ag_loc.count() > 0:
                                box = await ag_loc.bounding_box()
                                if box and box['width'] > 10 and box['height'] > 10 and box['y'] < 1200:
                                    click_x = box['x'] + (box['width'] / 2.0)
                                    click_y = box['y'] + (box['height'] / 2.0)
                                    logger.info(f"[AgeGate] Detected modal button '{ag_sel}' at ({click_x:.0f}, {click_y:.0f}). Dispatching human click...")
                                    await human_click(page, click_x, click_y)
                                    clicked = True
                                    age_gate_clicked = True
                                    break
                    except Exception as age_err:
                        logger.debug(f"[AgeGate] Notice: {age_err}")

            await asyncio.sleep(step)

        # Tier 3.5: paid captcha-solver escalation. Only reached when the
        # free click-based approach above ran out the full timeout without
        # clearing - covers interactive image challenges (hCaptcha puzzle
        # grids, reCAPTCHA image selection) a checkbox click can't solve.
        # No-ops entirely when CAPTCHA_SOLVER_API_KEY isn't configured.
        if not cleared and captcha_solver.enabled and last_detected_challenge in CAPTCHA_SOLVER_WIDGETS:
            solved = await self._try_captcha_solver_escalation(page, url, last_detected_challenge)
            if solved:
                settle_deadline = time.time() + 8.0
                while time.time() < settle_deadline:
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
        raw_cookies = []
        try:
            if hasattr(context, "cookies"):
                raw_cookies = await context.cookies()
            elif hasattr(context, "contexts") and context.contexts:
                raw_cookies = await context.contexts[0].cookies()
        except Exception as c_err:
            logger.debug(f"[BrowserPool] Error reading cookies: {c_err}")

        captured_cookies: List[CookieModel] = []
        for rc in raw_cookies:
            c_name = str(rc.get("name", "") or "")
            c_val = str(rc.get("value", "") or "")
            c_expires = rc.get("expires", -1)
            if c_expires is None or c_expires < 0:
                c_expires = -1
            else:
                try:
                    c_expires = float(c_expires)
                except Exception:
                    c_expires = -1

            c_size = rc.get("size")
            if c_size is None or not isinstance(c_size, int):
                c_size = len(c_name) + len(c_val)

            captured_cookies.append(
                CookieModel(
                    name=c_name,
                    value=c_val,
                    domain=rc.get("domain") or "",
                    path=rc.get("path") or "/",
                    expires=c_expires,
                    size=c_size,
                    httpOnly=bool(rc.get("httpOnly", False)),
                    secure=bool(rc.get("secure", False)),
                    session=bool(rc.get("session", False)),
                    sameSite=str(rc.get("sameSite", "Lax") or "Lax")
                )
            )

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

        solve_duration = time.time() - start_time
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
