import time
import json
import hashlib
import asyncio
import logging
from typing import Dict, List, Optional
from app.models.flaresolverr import V1Request, SolutionModel, CookieModel
from app.solver.cache import cookie_cache
from app.solver.fast_tls import fast_tls_engine
from app.solver.browser import IP_BLOCKED_REASON, ChallengeNotSolvedError, browser_pool, is_google_sorry_url
from app.solver.captcha_solver import captcha_solver
from app.config import settings
from app.events import event_broadcaster
from app.history import request_history
from app.security import check_target_url_async

logger = logging.getLogger("solverr.engine")

# Seconds. One bucket set spans Fast TLS (ms) to browser solves (tens of s) since all tiers share it.
HISTOGRAM_BUCKETS_SECONDS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60)

class Histogram:
    """Minimal Prometheus-style cumulative histogram: fixed buckets + sum + count."""
    def __init__(self, buckets=HISTOGRAM_BUCKETS_SECONDS):
        self.buckets = buckets
        self.bucket_counts: Dict[float, int] = {b: 0 for b in buckets}
        self.count: int = 0
        self.sum: float = 0.0

    def observe(self, value_seconds: float):
        self.count += 1
        self.sum += value_seconds
        for b in self.buckets:
            if value_seconds <= b:
                self.bucket_counts[b] += 1

class PerformanceMetrics:
    def __init__(self):
        self.total_requests: int = 0
        self.fast_tls_hits: int = 0
        self.cache_hits: int = 0  # tier2: requests served using cached cookies
        self.cookie_cache_lookup_hits: int = 0  # raw cookie_cache.get_cookies() hit/miss, independent of tier
        self.cookie_cache_lookup_misses: int = 0
        self.browser_solves: int = 0
        self.fallback_proxy_hits: int = 0
        self.failed_requests: int = 0
        self.timeouts_total: int = 0
        self.total_fast_ms: float = 0.0
        self.total_browser_ms: float = 0.0
        self.duration_histograms: Dict[str, Histogram] = {
            "tier1_fast_tls": Histogram(),
            "tier2_cache": Histogram(),
            "tier3_stealth_browser": Histogram(),
            "tier4_fallback_proxy": Histogram(),
        }
        self.challenges_solved: Dict[str, int] = {
            "cloudflare_turnstile": 0,
            "cloudflare_5s": 0,
            "ddos_guard": 0,
            "recaptcha": 0,
            "hcaptcha": 0,
            "geetest": 0,
            "imperva": 0,
            "datadome": 0,
            "akamai": 0,
            "aws_waf": 0,
            "anubis": 0,
            "cap": 0,
        }

    def record_fast(self, duration_ms: float):
        self.total_requests += 1
        self.fast_tls_hits += 1
        self.total_fast_ms += duration_ms
        self.duration_histograms["tier1_fast_tls"].observe(duration_ms / 1000.0)

    def record_cache(self, duration_ms: float):
        self.total_requests += 1
        self.cache_hits += 1
        self.total_fast_ms += duration_ms
        self.duration_histograms["tier2_cache"].observe(duration_ms / 1000.0)

    def record_browser(self, duration_ms: float, challenge_type: Optional[str] = None):
        self.total_requests += 1
        self.browser_solves += 1
        self.total_browser_ms += duration_ms
        self.duration_histograms["tier3_stealth_browser"].observe(duration_ms / 1000.0)
        if challenge_type and challenge_type in self.challenges_solved:
            self.challenges_solved[challenge_type] += 1

    def record_fallback_proxy(self, duration_ms: float):
        self.total_requests += 1
        self.fallback_proxy_hits += 1
        self.total_browser_ms += duration_ms
        self.duration_histograms["tier4_fallback_proxy"].observe(duration_ms / 1000.0)

    def record_failure(self):
        self.total_requests += 1
        self.failed_requests += 1

    def record_timeout(self):
        self.timeouts_total += 1

    def record_cookie_cache_lookup(self, hit: bool):
        # Counts every lookup, unlike cache_hits which counts requests served by Tier 2.
        if hit:
            self.cookie_cache_lookup_hits += 1
        else:
            self.cookie_cache_lookup_misses += 1

    def to_dict(self) -> dict:
        fast_total = self.fast_tls_hits + self.cache_hits
        avg_fast = (self.total_fast_ms / fast_total) if fast_total > 0 else 0.0
        browser_total = self.browser_solves + self.fallback_proxy_hits
        avg_browser = (self.total_browser_ms / browser_total) if browser_total > 0 else 0.0
        fast_rate = (fast_total / self.total_requests * 100) if self.total_requests > 0 else 0.0
        
        return {
            "total_requests": self.total_requests,
            "tier1_fast_tls_hits": self.fast_tls_hits,
            "tier2_cache_hits": self.cache_hits,
            "tier3_stealth_browser_solves": self.browser_solves,
            "tier4_fallback_proxy_hits": self.fallback_proxy_hits,
            "fast_tls_hits": self.fast_tls_hits,
            "browser_solves": self.browser_solves,
            "failed_requests": self.failed_requests,
            "avg_fast_ms": round(avg_fast, 2),
            "avg_browser_ms": round(avg_browser, 2),
            "fast_hit_rate_pct": round(fast_rate, 1),
            "challenges_solved": self.challenges_solved,
            "cookie_cache_lookup_hits": self.cookie_cache_lookup_hits,
            "cookie_cache_lookup_misses": self.cookie_cache_lookup_misses,
            "timeouts_total": self.timeouts_total,
        }

metrics = PerformanceMetrics()

class RequestBudget:
    """Tracks remaining timeout budget across multi-tier solve stages using a monotonic clock."""
    def __init__(self, timeout_ms: int):
        self.total_timeout_s = max(1.0, timeout_ms / 1000.0)
        self.start_mono = time.monotonic()
        self.deadline = self.start_mono + self.total_timeout_s

    @property
    def remaining_s(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    @property
    def remaining_ms(self) -> int:
        return int(self.remaining_s * 1000)

    @property
    def elapsed_ms(self) -> float:
        return (time.monotonic() - self.start_mono) * 1000.0

    @property
    def is_expired(self) -> bool:
        return time.monotonic() >= self.deadline


def _cap_response_body(solution: SolutionModel) -> None:
    max_bytes = int(settings.MAX_RESPONSE_BODY_MB * 1024 * 1024)
    if max_bytes <= 0 or not solution.response:
        return
    response_bytes = solution.response.encode("utf-8", errors="ignore")
    body_bytes = len(response_bytes)
    if body_bytes > max_bytes:
        logger.warning(
            f"[HybridEngine] Response body ({body_bytes / 1024 / 1024:.1f}MB) exceeds "
            f"MAX_RESPONSE_BODY_MB={settings.MAX_RESPONSE_BODY_MB}, truncating"
        )
        marker = "\n<!-- truncated: response exceeded MAX_RESPONSE_BODY_MB -->"
        marker_bytes = marker.encode("utf-8")
        if len(marker_bytes) <= max_bytes:
            content_budget = max_bytes - len(marker_bytes)
            solution.response = response_bytes[:content_budget].decode("utf-8", errors="ignore") + marker
        else:
            # For very small limits, keep the strict byte cap even when the
            # explanatory marker itself would exceed it.
            solution.response = response_bytes[:max_bytes].decode("utf-8", errors="ignore")

class HybridSolverEngine:
    def __init__(self):
        self._inflight: Dict[str, asyncio.Future] = {}

    async def _record_history(self, url: str, budget: RequestBudget, solution: SolutionModel | None = None,
                              failure: BaseException | None = None):
        try:
            await request_history.record(
                url, solution.tier if solution else "failed",
                "success" if solution and (solution.status < 400 or solution.status == 404) else "failed",
                budget.elapsed_ms, solution.status if solution else None,
                solution.challengeType if solution else None,
                type(failure).__name__ if failure else None,
            )
        except Exception:
            logger.warning("Request history could not be persisted", exc_info=True)

    async def process_request(self, req: V1Request, bypass_cookie_cache: bool = False) -> SolutionModel:
        budget = RequestBudget(req.maxTimeout or settings.BROWSER_TIMEOUT_MS)
        url = req.url
        method = req.cmd.split(".")[-1].upper() if "." in req.cmd else "GET"

        await check_target_url_async(url)
        # The proxy is the real egress point, so it gets the same SSRF check as the URL.
        proxy_for_check = req.get_proxy_url()
        if proxy_for_check:
            await check_target_url_async(proxy_for_check, label="Proxy")

        # Dedup key: every field that can change the outcome, so differing requests never coalesce.
        fingerprint = {
            "method": method,
            "url": url,
            "postData": req.postData,
            "cookies": [(c.name, c.value, c.domain, c.path) for c in (req.cookies or [])],
            "headers": req.headers,
            "proxy": req.get_proxy_url(),
            "session": req.session,
            "userAgent": req.userAgent,
            "forceBrowser": req.forceBrowser,
            "fastTlsOnly": req.fastTlsOnly,
            "wait_selector": req.wait_selector,
            "wait_delay_ms": req.wait_delay_ms,
            "screenshot": bool(req.screenshot),
            "screenshot_full_page": req.screenshot_full_page,
            "screenshot_selector": req.screenshot_selector,
            "extract_records": req.extract_records,
            "maxTimeout": req.maxTimeout,
            "bypassCookieCache": bypass_cookie_cache,
            "followMetaRefresh": req.follows_meta_refresh(),
        }
        inflight_key = hashlib.sha256(
            json.dumps(fingerprint, sort_keys=True, default=str).encode()
        ).hexdigest()

        # After a shared failure, re-check so one waiter retries and the rest join it (no thundering herd).
        # Race-free because no await sits between the check and the install below.
        while True:
            existing = self._inflight.get(inflight_key)
            if existing is None:
                break
            logger.info(f"[HybridEngine] Coalescing duplicate concurrent solve for {url}...")
            try:
                return await asyncio.shield(existing)
            except Exception:
                continue

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._inflight[inflight_key] = future

        try:
            res = await self._do_process_request(req, budget, url, method, bypass_cookie_cache)
            _cap_response_body(res)
            await self._record_history(url, budget, solution=res)
            if not future.done():
                future.set_result(res)
            return res
        except asyncio.CancelledError:
            await self._record_history(url, budget, failure=asyncio.CancelledError())
            # A cancelled owner task must still release any waiters shielded
            # onto this future via asyncio.shield(existing) above - otherwise
            # they block forever on a future nothing will ever resolve.
            if not future.done():
                future.cancel()
            raise
        except Exception as e:
            await self._record_history(url, budget, failure=e)
            if not future.done():
                future.set_exception(e)
                # Mark retrieved so asyncio doesn't warn when no waiter awaited it.
                future.exception()
            raise e
        finally:
            # A waiter's retry may have replaced our future; don't pop theirs.
            if self._inflight.get(inflight_key) is future:
                self._inflight.pop(inflight_key, None)

    async def _do_process_request(
        self, req: V1Request, budget: RequestBudget, url: str, method: str, bypass_cookie_cache: bool = False
    ) -> SolutionModel:
        proxy_url = req.get_proxy_url()

        combined_cookies: List[CookieModel] = []
        cached_cookies = []
        cached_ua: Optional[str] = None
        if not (bypass_cookie_cache or getattr(req, "skip_cache", False)):
            cached_cookies, cached_ua = await cookie_cache.get_cookies_with_user_agent_async(url)
            metrics.record_cookie_cache_lookup(hit=bool(cached_cookies))

        # Same identity as cookie_cache._cookie_key: same-name cookies on other paths are distinct.
        def _cookie_identity(c: CookieModel):
            return ((c.domain or "").lstrip(".").lower(), c.path or "/", c.name)

        input_cookie_ids = set()
        if req.cookies:
            for c in req.cookies:
                combined_cookies.append(c)
                input_cookie_ids.add(_cookie_identity(c))

        had_cache = False
        for cc in cached_cookies:
            if _cookie_identity(cc) not in input_cookie_ids:
                combined_cookies.append(cc)
                had_cache = True

        if cached_cookies:
            logger.info(f"[HybridEngine] Merged {len(cached_cookies)} cached cookie(s) for domain '{cookie_cache._normalize_domain(url)}'")

        # Clearance cookies only validate with the UA that earned them, so Tier 2 replays with it.
        fast_tls_ua = req.userAgent
        if not fast_tls_ua and had_cache and fast_tls_engine.is_compatible_user_agent(cached_ua):
            fast_tls_ua = cached_ua

        # Set when Tier 1 already met a verdict on the egress IP, which a browser from that IP can't change.
        tier1_ip_block: Optional[str] = None

        # Tiers 1 and 2
        if settings.ENABLE_FAST_TLS and not req.forceBrowser:
            tls_timeout = max(1, min(10, int(budget.remaining_s)))
            logger.info(f"[HybridEngine] Level 1/2 Fast TLS: Attempting direct HTTP request (timeout={tls_timeout}s, remaining_budget={budget.remaining_s:.1f}s)...")
            
            is_cf_challenge, solution = await fast_tls_engine.request(
                url=url,
                method=method,
                post_data=req.postData,
                cookies=combined_cookies,
                headers=req.headers,
                proxy=proxy_url,
                timeout=tls_timeout,
                user_agent=fast_tls_ua,
                session_id=req.session,
                follow_meta_refresh=req.follows_meta_refresh(),
            )

            is_valid_http_response = (
                solution.status < 400
                or solution.status in (400, 401, 404, 405, 410, 422)
            ) if solution else False
            if not is_cf_challenge and solution and is_valid_http_response:
                elapsed_ms = budget.elapsed_ms
                tier_name = "tier2_cache" if had_cache else "tier1_fast_tls"
                if had_cache:
                    metrics.record_cache(elapsed_ms)
                else:
                    metrics.record_fast(elapsed_ms)
                logger.info(f"[HybridEngine] Level 1/2 Fast TLS SUCCESS in {elapsed_ms:.1f}ms | Status: {solution.status}")
                solution.tier = tier_name

                if solution.cookies:
                    await cookie_cache.set_cookies_async(url, solution.cookies, user_agent=solution.userAgent)
                event_broadcaster.emit("solve", {
                    "url": url,
                    "tier": tier_name,
                    "status": solution.status,
                    "duration_ms": round(elapsed_ms, 1),
                    "cookies_count": len(solution.cookies) if solution.cookies else 0
                })
                return solution

            if req.fastTlsOnly:
                elapsed_ms = budget.elapsed_ms
                if solution:
                    metrics.record_fast(elapsed_ms)
                    logger.info("[HybridEngine] fastTlsOnly=True requested. Returning Fast TLS solution without browser escalation.")
                    solution.tier = "tier1_fast_tls"
                    if solution.cookies:
                        await cookie_cache.set_cookies_async(url, solution.cookies, user_agent=solution.userAgent)
                    event_broadcaster.emit("solve", {
                        "url": url,
                        "tier": "tier1_fast_tls",
                        "status": solution.status,
                        "duration_ms": round(elapsed_ms, 1),
                        "cookies_count": len(solution.cookies) if solution.cookies else 0
                    })
                    return solution
                else:
                    metrics.record_failure()
                    event_broadcaster.emit("solve_error", {"url": url, "error": "Fast TLS path failed"})
                    raise RuntimeError(f"Fast TLS path failed for {url}")

            # Google's /sorry/ page is a reCAPTCHA only a paid solver could clear from this IP.
            if solution and is_google_sorry_url(solution.url) and not captcha_solver.enabled:
                tier1_ip_block = "google"

            if is_cf_challenge:
                logger.info(f"[HybridEngine] Fast TLS detected WAF challenge (Status: {solution.status if solution else 'N/A'}). Escalating to Level 3 Stealth Browser...")
            else:
                status_str = str(solution.status) if solution else "No response"
                logger.info(f"[HybridEngine] Fast TLS path incomplete (Status: {status_str}). Escalating to Level 3 Stealth Browser...")
        else:
            reason = "forceBrowser=True requested" if req.forceBrowser else "ENABLE_FAST_TLS=false"
            logger.info(f"[HybridEngine] Skipping Level 1/2 Fast TLS ({reason}). Proceeding directly to Level 3 Stealth Browser...")

        # Tier 3
        if budget.is_expired or budget.remaining_s < 1.0:
            raise TimeoutError(f"Request timeout budget exhausted ({budget.elapsed_ms:.0f}ms elapsed)")

        browser_timeout_ms = max(3000, budget.remaining_ms)
        try:
            if tier1_ip_block:
                logger.info(f"[HybridEngine] {tier1_ip_block} refused this IP at Tier 1; skipping the same-IP browser solve.")
                raise ChallengeNotSolvedError(tier1_ip_block, IP_BLOCKED_REASON, ip_blocked=True)
            solution = await browser_pool.solve(
                url=url,
                method=method,
                post_data=req.postData,
                cookies=combined_cookies,
                proxy={"url": proxy_url} if proxy_url else None,
                timeout_ms=browser_timeout_ms,
                user_agent=req.userAgent,
                headers=req.headers,
                wait_selector=req.wait_selector,
                wait_delay_ms=req.wait_delay_ms,
                capture_screenshot=bool(req.screenshot or req.screenshot_selector or req.screenshot_full_page),
                screenshot_full_page=req.screenshot_full_page,
                screenshot_selector=req.screenshot_selector,
                extract_records=req.extract_records,
                follow_meta_refresh=req.follows_meta_refresh(),
            )
            
            elapsed_ms = budget.elapsed_ms
            metrics.record_browser(elapsed_ms, solution.challengeType)
            logger.info(f"[HybridEngine] Level 3 Stealth Browser SUCCESS in {elapsed_ms:.1f}ms | Status: {solution.status} | Challenge: {solution.challengeType or 'none'}")
            solution.tier = "tier3_stealth_browser"

            if solution.cookies:
                await cookie_cache.set_cookies_async(url, solution.cookies, user_agent=solution.userAgent)

            event_broadcaster.emit("solve", {
                "url": url,
                "tier": "tier3_stealth_browser",
                "status": solution.status,
                "challenge": solution.challengeType or "none",
                "duration_ms": round(elapsed_ms, 1),
                "cookies_count": len(solution.cookies) if solution.cookies else 0
            })

            return solution

        except Exception as e:
            if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
                metrics.record_timeout()
            # Tier 4: retry through the fallback proxy.
            fallback_proxy = settings.FALLBACK_PROXY_URL
            if fallback_proxy and not proxy_url and not budget.is_expired and budget.remaining_s >= 2.0:
                await check_target_url_async(fallback_proxy, label="Fallback proxy")
                fallback_timeout_ms = max(3000, budget.remaining_ms)
                logger.warning(f"[HybridEngine] Level 3 direct solve failed ({e}). Escalating to Tier 4 Fallback Proxy ({fallback_proxy}, remaining budget: {budget.remaining_s:.1f}s)...")
                try:
                    solution = await browser_pool.solve(
                        url=url,
                        method=method,
                        post_data=req.postData,
                        cookies=combined_cookies,
                        proxy={"url": fallback_proxy},
                        timeout_ms=fallback_timeout_ms,
                        user_agent=req.userAgent,
                        headers=req.headers,
                        wait_selector=req.wait_selector,
                        wait_delay_ms=req.wait_delay_ms,
                        capture_screenshot=bool(req.screenshot or req.screenshot_selector or req.screenshot_full_page),
                        screenshot_full_page=req.screenshot_full_page,
                        screenshot_selector=req.screenshot_selector,
                        extract_records=req.extract_records,
                        follow_meta_refresh=req.follows_meta_refresh(),
                    )
                    elapsed_ms = budget.elapsed_ms
                    metrics.record_fallback_proxy(elapsed_ms)
                    logger.info(f"[HybridEngine] Tier 4 Fallback Proxy SUCCESS in {elapsed_ms:.1f}ms | Status: {solution.status}")
                    solution.tier = "tier4_fallback_proxy"
                    if solution.cookies:
                        await cookie_cache.set_cookies_async(url, solution.cookies, user_agent=solution.userAgent)
                    event_broadcaster.emit("solve", {
                        "url": url,
                        "tier": "tier4_fallback_proxy",
                        "status": solution.status,
                        "duration_ms": round(elapsed_ms, 1),
                        "cookies_count": len(solution.cookies) if solution.cookies else 0
                    })
                    return solution
                except Exception as fallback_err:
                    logger.error(f"[HybridEngine] Tier 4 Fallback Proxy solve also FAILED for {url}: {fallback_err}")
                    metrics.record_failure()
                    event_broadcaster.emit("solve_error", {"url": url, "error": str(fallback_err)})
                    raise fallback_err

            metrics.record_failure()
            event_broadcaster.emit("solve_error", {"url": url, "error": str(e)})
            logger.error(f"[HybridEngine] Level 3 Stealth Browser solve FAILED for {url}: {type(e).__name__} - {e}")
            raise e

solver_engine = HybridSolverEngine()
