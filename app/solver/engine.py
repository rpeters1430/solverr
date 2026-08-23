import time
import json
import hashlib
import asyncio
import logging
from typing import Dict, List, Optional
from app.models.flaresolverr import V1Request, SolutionModel, CookieModel
from app.solver.cache import cookie_cache
from app.solver.fast_tls import fast_tls_engine
from app.solver.browser import browser_pool
from app.config import settings
from app.events import event_broadcaster
from app.security import check_target_url

logger = logging.getLogger("solverr.engine")

# Seconds. Spans Fast TLS (tens of ms) through a full browser challenge
# solve (tens of seconds) in one bucket set, since both tiers share this
# histogram (partitioned by the "tier" label instead of separate metrics).
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
            "akamai": 0
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
        # Distinct from self.cache_hits (tier2 = a request whose outcome was
        # cached cookies) - this counts every cookie_cache.get_cookies()
        # lookup regardless of which tier ultimately handled the request.
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

def _cap_response_body(solution: SolutionModel) -> None:
    max_bytes = settings.MAX_RESPONSE_BODY_MB * 1024 * 1024
    if max_bytes <= 0 or not solution.response:
        return
    body_bytes = len(solution.response.encode("utf-8", errors="ignore"))
    if body_bytes > max_bytes:
        logger.warning(
            f"[HybridEngine] Response body ({body_bytes / 1024 / 1024:.1f}MB) exceeds "
            f"MAX_RESPONSE_BODY_MB={settings.MAX_RESPONSE_BODY_MB}, truncating"
        )
        truncated_chars = int(max_bytes)
        solution.response = solution.response[:truncated_chars] + "\n<!-- truncated: response exceeded MAX_RESPONSE_BODY_MB -->"

class HybridSolverEngine:
    def __init__(self):
        self._inflight: Dict[str, asyncio.Future] = {}

    async def process_request(self, req: V1Request) -> SolutionModel:
        start_time = time.time()
        url = req.url
        method = req.cmd.split(".")[-1].upper() if "." in req.cmd else "GET"

        check_target_url(url)

        # Deduplication key for identical concurrent solves. Must cover every
        # field that can change the outcome - two requests that only differ
        # in, say, postData or session must never coalesce into one answer.
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
        }
        inflight_key = hashlib.sha256(
            json.dumps(fingerprint, sort_keys=True, default=str).encode()
        ).hexdigest()
        if inflight_key in self._inflight:
            logger.info(f"[HybridEngine] Coalescing duplicate concurrent solve for {url}...")
            try:
                return await asyncio.shield(self._inflight[inflight_key])
            except Exception:
                pass

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._inflight[inflight_key] = future

        try:
            res = await self._do_process_request(req, start_time, url, method)
            _cap_response_body(res)
            if not future.done():
                future.set_result(res)
            return res
        except Exception as e:
            if not future.done():
                future.set_exception(e)
                # Mark the exception as retrieved even if no concurrent
                # waiter ever awaits this future, so asyncio doesn't log a
                # spurious "exception was never retrieved" warning on GC.
                future.exception()
            raise e
        finally:
            self._inflight.pop(inflight_key, None)

    async def _do_process_request(self, req: V1Request, start_time: float, url: str, method: str) -> SolutionModel:
        proxy_url = req.get_proxy_url()

        # Combine input cookies with cached domain cookies
        combined_cookies: List[CookieModel] = []
        cached_cookies = cookie_cache.get_cookies(url)
        metrics.record_cookie_cache_lookup(hit=bool(cached_cookies))

        input_cookie_names = set()
        if req.cookies:
            for c in req.cookies:
                combined_cookies.append(c)
                input_cookie_names.add(c.name)
        
        had_cache = False
        for cc in cached_cookies:
            if cc.name not in input_cookie_names:
                combined_cookies.append(cc)
                had_cache = True

        if cached_cookies:
            logger.info(f"[HybridEngine] Merged {len(cached_cookies)} cached cookie(s) for domain '{cookie_cache._normalize_domain(url)}'")

        # Level 1 & 2: Try Fast TLS (curl_cffi) first unless forceBrowser requested
        if settings.ENABLE_FAST_TLS and not req.forceBrowser:
            tls_timeout = min(15, int((req.maxTimeout or 60000) / 1000))
            logger.info(f"[HybridEngine] Level 1/2 Fast TLS: Attempting direct HTTP request (timeout={tls_timeout}s)...")
            
            is_cf_challenge, solution = await fast_tls_engine.request(
                url=url,
                method=method,
                post_data=req.postData,
                cookies=combined_cookies,
                headers=req.headers,
                proxy=proxy_url,
                timeout=tls_timeout,
                user_agent=req.userAgent
            )

            if not is_cf_challenge and solution and (solution.status < 400 or solution.status == 404):
                elapsed_ms = (time.time() - start_time) * 1000
                tier_name = "tier2_cache" if had_cache else "tier1_fast_tls"
                if had_cache:
                    metrics.record_cache(elapsed_ms)
                else:
                    metrics.record_fast(elapsed_ms)
                logger.info(f"[HybridEngine] Level 1/2 Fast TLS SUCCESS in {elapsed_ms:.1f}ms | Status: {solution.status}")
                solution.tier = tier_name

                if solution.cookies:
                    cookie_cache.set_cookies(url, solution.cookies)
                event_broadcaster.emit("solve", {
                    "url": url,
                    "tier": tier_name,
                    "status": solution.status,
                    "duration_ms": round(elapsed_ms, 1),
                    "cookies_count": len(solution.cookies) if solution.cookies else 0
                })
                return solution

            if req.fastTlsOnly:
                elapsed_ms = (time.time() - start_time) * 1000
                if solution:
                    metrics.record_fast(elapsed_ms)
                    logger.info("[HybridEngine] fastTlsOnly=True requested. Returning Fast TLS solution without browser escalation.")
                    solution.tier = "tier1_fast_tls"
                    if solution.cookies:
                        cookie_cache.set_cookies(url, solution.cookies)
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

            if is_cf_challenge:
                logger.info(f"[HybridEngine] Fast TLS detected WAF challenge (Status: {solution.status if solution else 'N/A'}). Escalating to Level 3 Stealth Browser...")
            else:
                status_str = str(solution.status) if solution else "No response"
                logger.info(f"[HybridEngine] Fast TLS path incomplete (Status: {status_str}). Escalating to Level 3 Stealth Browser...")
        else:
            reason = "forceBrowser=True requested" if req.forceBrowser else "ENABLE_FAST_TLS=false"
            logger.info(f"[HybridEngine] Skipping Level 1/2 Fast TLS ({reason}). Proceeding directly to Level 3 Stealth Browser...")

        # Level 3: Stealth Camoufox / Playwright Browser Solve
        try:
            solution = await browser_pool.solve(
                url=url,
                method=method,
                post_data=req.postData,
                cookies=combined_cookies,
                proxy={"url": proxy_url} if proxy_url else None,
                timeout_ms=req.maxTimeout or settings.BROWSER_TIMEOUT_MS,
                user_agent=req.userAgent,
                headers=req.headers,
                wait_selector=req.wait_selector,
                wait_delay_ms=req.wait_delay_ms,
                capture_screenshot=bool(req.screenshot)
            )
            
            elapsed_ms = (time.time() - start_time) * 1000
            metrics.record_browser(elapsed_ms, solution.challengeType)
            logger.info(f"[HybridEngine] Level 3 Stealth Browser SUCCESS in {elapsed_ms:.1f}ms | Status: {solution.status} | Challenge: {solution.challengeType or 'none'}")
            solution.tier = "tier3_stealth_browser"

            if solution.cookies:
                cookie_cache.set_cookies(url, solution.cookies)

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
            # Tier 4: Fallback Proxy Escalation if configured and direct attempt failed
            fallback_proxy = settings.FALLBACK_PROXY_URL
            if fallback_proxy and not proxy_url:
                logger.warning(f"[HybridEngine] Level 3 direct solve failed ({e}). Escalating to Tier 4 Fallback Proxy ({fallback_proxy})...")
                try:
                    solution = await browser_pool.solve(
                        url=url,
                        method=method,
                        post_data=req.postData,
                        cookies=combined_cookies,
                        proxy={"url": fallback_proxy},
                        timeout_ms=req.maxTimeout or settings.BROWSER_TIMEOUT_MS,
                        user_agent=req.userAgent,
                        headers=req.headers,
                        wait_selector=req.wait_selector,
                        wait_delay_ms=req.wait_delay_ms,
                        capture_screenshot=bool(req.screenshot)
                    )
                    elapsed_ms = (time.time() - start_time) * 1000
                    metrics.record_fallback_proxy(elapsed_ms)
                    logger.info(f"[HybridEngine] Tier 4 Fallback Proxy SUCCESS in {elapsed_ms:.1f}ms | Status: {solution.status}")
                    solution.tier = "tier4_fallback_proxy"
                    if solution.cookies:
                        cookie_cache.set_cookies(url, solution.cookies)
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
