import asyncio
import base64
import dataclasses
import html
import json
import logging
import time
from urllib.parse import urlparse
from typing import Dict, List, Optional, Tuple, Any
from playwright.async_api import async_playwright, Playwright, Browser, BrowserContext, Page
from app.config import settings
from app.models.flaresolverr import CookieModel, SolutionModel
from app.solver.stealth import STEALTH_INIT_SCRIPT
from app.solver.human_cursor import human_click, human_mouse_move
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
            await self._close_instance(inst)
            async with self._lock:
                self._created -= 1
            try:
                async with self._lock:
                    fresh = await self._launch_instance()
                    self._created += 1
                self._idle.put_nowait(fresh)
            except Exception as e:
                logger.warning(f"[CamoufoxPool] Failed to relaunch recycled instance: {e}")
            return
        self._idle.put_nowait(inst)

    def _should_recycle(self, inst: _PooledCamoufox) -> bool:
        return (
            inst.uses >= settings.CAMOUFOX_POOL_RECYCLE_USES
            or (time.time() - inst.created_at) >= settings.CAMOUFOX_POOL_RECYCLE_SECONDS
        )

    async def _launch_instance(self) -> _PooledCamoufox:
        cm = AsyncCamoufox(
            headless=settings.HEADLESS,
            humanize=True,
            disable_coop=True,
            config={'forceScopeAccess': True},
            i_know_what_im_doing=True
        )
        browser = await cm.__aenter__()
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

class BrowserPool:
    def __init__(self):
        self.playwright: Optional[Playwright] = None
        self.browser: Optional[Browser] = None
        self.semaphore = asyncio.Semaphore(settings.MAX_BROWSER_WORKERS)
        self._lock = asyncio.Lock()
        self.camoufox_pool: Optional[CamoufoxPool] = (
            CamoufoxPool(settings.MAX_BROWSER_WORKERS)
            if (settings.USE_CAMOUFOX and CAMOUFOX_AVAILABLE and settings.CAMOUFOX_POOL_ENABLED)
            else None
        )

    async def initialize(self):
        async with self._lock:
            if not self.playwright or not self.browser or not self.browser.is_connected():
                logger.info("Initializing Chromium Playwright Engine...")
                if self.browser:
                    try:
                        await self.browser.close()
                    except Exception:
                        pass
                if self.playwright:
                    try:
                        await self.playwright.stop()
                    except Exception:
                        pass
                
                self.playwright = await async_playwright().start()
                
                launch_args = [
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--no-first-run",
                    "--disable-blink-features=AutomationControlled",
                    "--window-size=1920,1080",
                    f"--js-flags=--max-old-space-size={settings.BROWSER_MAX_OLD_SPACE_SIZE}",
                    "--enable-features=NetworkService,NetworkServiceInProcess",
                    "--disable-background-networking",
                    "--disable-component-update"
                ]
                
                if not settings.ENABLE_GPU:
                    launch_args.extend(["--disable-gpu", "--disable-accelerated-2d-canvas"])
                else:
                    launch_args.extend(["--enable-gpu-rasterization"])
                
                self.browser = await self.playwright.chromium.launch(
                    headless=settings.HEADLESS,
                    channel="chromium",
                    ignore_default_args=["--enable-automation"],
                    args=launch_args
                )
                tuning_mode = "Auto-Tuned" if getattr(settings, "WORKER_AUTO_TUNED", False) else "Custom"
                logger.info(f"Chromium Browser Engine active ({tuning_mode}): {settings.MAX_BROWSER_WORKERS} workers | JS Heap: {settings.BROWSER_MAX_OLD_SPACE_SIZE}MB")
            
            if settings.USE_CAMOUFOX and CAMOUFOX_AVAILABLE:
                logger.info(f"Camoufox Stealth Firefox Engine active: {settings.MAX_BROWSER_WORKERS} worker semaphore limit")

    async def close(self):
        if self.camoufox_pool:
            try:
                await self.camoufox_pool.close()
            except Exception as e:
                logger.warning(f"[CamoufoxPool] Shutdown notice: {e}")
        async with self._lock:
            if self.browser:
                try:
                    await self.browser.close()
                except Exception as e:
                    # e.g. "Connection closed while reading from the driver" -
                    # the Playwright Node driver subprocess can die before
                    # this runs (Ctrl+C kills the whole process group), which
                    # must never abort the rest of shutdown.
                    logger.warning(f"Chromium browser close notice: {e}")
                self.browser = None
            if self.playwright:
                try:
                    await self.playwright.stop()
                except Exception as e:
                    logger.warning(f"Playwright driver stop notice: {e}")
                self.playwright = None
            logger.info("Browser Pool stopped.")

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
        async with self.semaphore:
            start_time = time.time()
            active_ua = user_agent or settings.DEFAULT_USER_AGENT
            pw_proxy = None
            if proxy and "url" in proxy and proxy["url"]:
                pw_proxy = {"server": proxy["url"]}
                logger.info(f"[BrowserPool] Routing request through proxy: {sanitize_proxy_url(proxy['url'])}")

            if settings.USE_CAMOUFOX and CAMOUFOX_AVAILABLE:
                # Pooled path: reuse a warm browser process (no per-request UA
                # override, since Camoufox ties navigator.userAgent to the
                # fingerprint it generated when that process launched - the
                # HTTP UA header and JS-visible UA must match). Only usable
                # when there's no proxy and no explicit user_agent request.
                use_pool = self.camoufox_pool is not None and not pw_proxy and not user_agent
                if use_pool:
                    try:
                        sol = await self._solve_with_pooled_camoufox(
                            url=url, method=method, post_data=post_data, cookies=cookies, timeout_ms=timeout_ms,
                            headers=headers, start_time=start_time, wait_selector=wait_selector,
                            wait_delay_ms=wait_delay_ms, capture_screenshot=capture_screenshot
                        )
                        if sol and sol.status < 400:
                            return sol
                        else:
                            logger.warning(f"[CamoufoxEngine] Pooled Camoufox solve incomplete (Status {sol.status if sol else 'N/A'}). Escalating fallback to Chromium engine...")
                    except Exception as e:
                        logger.warning(f"[CamoufoxEngine] Pooled Camoufox solve notice/fallback: {e}. Falling back to Chromium engine...")
                else:
                    try:
                        logger.info(f"[CamoufoxEngine] Spawning ephemeral Camoufox stealth Firefox solve for {url} (proxy={'yes' if pw_proxy else 'no'}, custom_ua={'yes' if user_agent else 'no'})...")
                        async with AsyncCamoufox(
                            headless=settings.HEADLESS,
                            proxy=pw_proxy,
                            humanize=True,
                            disable_coop=True,
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
                            sol = await self._execute_solve_flow(
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
                            if sol and sol.status < 400:
                                return sol
                            else:
                                logger.warning(f"[CamoufoxEngine] Camoufox solve incomplete (Status {sol.status if sol else 'N/A'}). Escalating fallback to Chromium engine...")
                    except Exception as e:
                        logger.warning(f"[CamoufoxEngine] Camoufox solve notice/fallback: {e}. Falling back to Chromium engine...")

            # Ensure Chromium engine is initialized for fallback
            if not self.browser or not self.browser.is_connected():
                await self.initialize()

            req_headers = {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                "Accept-Language": "en-US,en;q=0.9",
                "Sec-Ch-Ua": '"Chromium";v="146", "Not?A_Brand";v="24", "Google Chrome";v="146"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1"
            }
            if headers:
                req_headers.update(headers)

            context = await self.browser.new_context(
                user_agent=active_ua,
                viewport={"width": 1920, "height": 1080},
                proxy=pw_proxy,
                extra_http_headers=req_headers,
                ignore_https_errors=True
            )

            await context.add_init_script(STEALTH_INIT_SCRIPT)
            page = await context.new_page()

            try:
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
            finally:
                if page:
                    try:
                        await page.close()
                    except Exception:
                        pass
                if context:
                    try:
                        await context.close()
                    except Exception:
                        pass

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

            title_lower = title.lower()
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
                        if any(x in frame.url.lower() for x in ["challenges.cloudflare.com", "turnstile"]):
                            try:
                                body_loc = frame.locator("body").first
                                box = await body_loc.bounding_box()
                                if box and box['width'] > 20 and box['height'] > 20:
                                    click_x = box['x'] + 28.0
                                    click_y = box['y'] + (box['height'] / 2.0)
                                    logger.info(f"[Turnstile] Cloudflare frame detected at ({click_x:.0f}, {click_y:.0f}). Dispatching human click...")
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
                        "div.cf-turnstile iframe",
                        "#challenge-stage iframe",
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
                                    click_x = box['x'] + 28.0
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
