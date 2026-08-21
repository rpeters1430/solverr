import time
import asyncio
import logging
import re
from typing import Dict, List, Optional, Any
from app.models.flaresolverr import V1Request, SolutionModel, CookieModel
from app.solver.cache import cookie_cache
from app.solver.fast_tls import fast_tls_engine
from app.solver.browser import browser_pool
from app.config import settings

logger = logging.getLogger("solverr.engine")

class PerformanceMetrics:
    def __init__(self):
        self.total_requests: int = 0
        self.fast_tls_hits: int = 0
        self.cache_hits: int = 0
        self.browser_solves: int = 0
        self.fallback_proxy_hits: int = 0
        self.failed_requests: int = 0
        self.total_fast_ms: float = 0.0
        self.total_browser_ms: float = 0.0
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

    def record_cache(self, duration_ms: float):
        self.total_requests += 1
        self.cache_hits += 1
        self.total_fast_ms += duration_ms

    def record_browser(self, duration_ms: float, challenge_type: Optional[str] = None):
        self.total_requests += 1
        self.browser_solves += 1
        self.total_browser_ms += duration_ms
        if challenge_type and challenge_type in self.challenges_solved:
            self.challenges_solved[challenge_type] += 1

    def record_fallback_proxy(self, duration_ms: float):
        self.total_requests += 1
        self.fallback_proxy_hits += 1
        self.total_browser_ms += duration_ms

    def record_failure(self):
        self.total_requests += 1
        self.failed_requests += 1

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
            "challenges_solved": self.challenges_solved
        }

metrics = PerformanceMetrics()

class HybridSolverEngine:
    def __init__(self):
        self._inflight: Dict[str, asyncio.Future] = {}

    async def process_request(self, req: V1Request) -> SolutionModel:
        start_time = time.time()
        url = req.url
        method = req.cmd.split(".")[-1].upper() if "." in req.cmd else "GET"
        
        # Deduplication key for identical concurrent solves
        inflight_key = f"{method}:{url}:{req.forceBrowser}"
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
                if had_cache:
                    metrics.record_cache(elapsed_ms)
                else:
                    metrics.record_fast(elapsed_ms)
                logger.info(f"[HybridEngine] Level 1/2 Fast TLS SUCCESS in {elapsed_ms:.1f}ms | Status: {solution.status}")
                
                if solution.cookies:
                    cookie_cache.set_cookies(url, solution.cookies)
                return solution

            if req.fastTlsOnly:
                elapsed_ms = (time.time() - start_time) * 1000
                if solution:
                    metrics.record_fast(elapsed_ms)
                    logger.info(f"[HybridEngine] fastTlsOnly=True requested. Returning Fast TLS solution without browser escalation.")
                    if solution.cookies:
                        cookie_cache.set_cookies(url, solution.cookies)
                    return solution
                else:
                    metrics.record_failure()
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
            
            if solution.cookies:
                cookie_cache.set_cookies(url, solution.cookies)

            return solution

        except Exception as e:
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
                    if solution.cookies:
                        cookie_cache.set_cookies(url, solution.cookies)
                    return solution
                except Exception as fallback_err:
                    logger.error(f"[HybridEngine] Tier 4 Fallback Proxy solve also FAILED for {url}: {fallback_err}")
                    metrics.record_failure()
                    raise fallback_err

            metrics.record_failure()
            logger.error(f"[HybridEngine] Level 3 Stealth Browser solve FAILED for {url}: {type(e).__name__} - {e}")
            raise e

solver_engine = HybridSolverEngine()
