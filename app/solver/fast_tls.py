import asyncio
import logging
import re
import time
import zlib
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse
from curl_cffi.requests import AsyncSession
from app.models.flaresolverr import CookieModel, SolutionModel
from app.solver.browser import detect_challenge, is_challenge_title
from app.config import settings
from app.logging_config import sanitize_proxy_url
from app.security import check_target_url_async

logger = logging.getLogger("solverr.fast_tls")

MAX_REDIRECTS = 10

# Target and UA always travel together; a mismatched pair is itself a bot signal.
FIREFOX_PROFILES = [
    ("firefox147", "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:147.0) Gecko/20100101 Firefox/147.0"),
    ("firefox144", "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:144.0) Gecko/20100101 Firefox/144.0"),
    ("firefox133", "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0"),
]
CHROME_PROFILES = [
    ("chrome146", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"),
    ("chrome145", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"),
    ("chrome142", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"),
]

def _sec_ch_ua_for(target: str) -> Optional[str]:
    if not target.startswith("chrome"):
        return None
    version = "".join(ch for ch in target if ch.isdigit()) or "146"
    return f'"Chromium";v="{version}", "Not?A_Brand";v="24", "Google Chrome";v="{version}"'

class FastTLSEngine:
    def __init__(self):
        self.impersonate_target = getattr(settings, "FAST_TLS_TARGET", "firefox")
        self.rotate = getattr(settings, "FAST_TLS_ROTATE", True)
        self.profiles = FIREFOX_PROFILES if self.impersonate_target.startswith("firefox") else CHROME_PROFILES
        self._domain_scores: "OrderedDict[str, Dict[str, int]]" = OrderedDict()
        self._max_domain_scores: int = getattr(settings, "MAX_FAST_TLS_DOMAIN_SCORES", 2000)
        self._sessions: Dict[str, AsyncSession] = {}
        self._pool_enabled: bool = getattr(settings, "FAST_TLS_POOL_ENABLED", True)
        self._pool_size: int = getattr(settings, "FAST_TLS_POOL_SIZE", 50)
        self._lock = asyncio.Lock()

    def _normalize_domain(self, url: str) -> str:
        if "://" in url:
            domain = url.split("://", 1)[-1].split("/", 1)[0].split(":")[0].lower()
        else:
            domain = url.split("/")[0].split(":")[0].lower()
        return domain.lstrip(".")

    def record_outcome(self, url: str, profile_target: str, success: bool):
        """Track success/failure per-domain profile to adaptively pick optimal TLS profiles."""
        domain = self._normalize_domain(url)
        if domain not in self._domain_scores:
            # Evict the least recently touched domain so this dict stays bounded.
            if len(self._domain_scores) >= self._max_domain_scores:
                self._domain_scores.popitem(last=False)
            self._domain_scores[domain] = {}
        else:
            self._domain_scores.move_to_end(domain)
        curr = self._domain_scores[domain].get(profile_target, 0)
        if success:
            self._domain_scores[domain][profile_target] = min(curr + 1, 10)
        else:
            self._domain_scores[domain][profile_target] = max(curr - 2, -10)

    def _profile_for_domain(self, url: str) -> Tuple[str, str]:
        """Deterministically pick a TLS/UA profile per-domain, with adaptive scoring."""
        if not self.rotate or len(self.profiles) == 1:
            return self.profiles[0]
        domain = self._normalize_domain(url)
        scores = self._domain_scores.get(domain, {})
        
        # Filter out heavily penalized profiles unless all are penalized
        valid_profiles = [p for p in self.profiles if scores.get(p[0], 0) >= -2]
        if not valid_profiles:
            valid_profiles = self.profiles
            
        # If any profile has positive success history, pick the highest
        positive_profiles = [p for p in valid_profiles if scores.get(p[0], 0) > 0]
        if positive_profiles:
            return max(positive_profiles, key=lambda p: scores.get(p[0], 0))

        idx = zlib.crc32(domain.encode("utf-8")) % len(valid_profiles)
        return valid_profiles[idx]

    async def _get_session(self, pool_key: str, impersonate_target: str) -> AsyncSession:
        if not self._pool_enabled:
            return AsyncSession(impersonate=impersonate_target)
        async with self._lock:
            if pool_key in self._sessions:
                return self._sessions[pool_key]
            # Evict oldest session if at capacity
            if len(self._sessions) >= self._pool_size:
                oldest_key = next(iter(self._sessions))
                old_sess = self._sessions.pop(oldest_key)
                try:
                    await old_sess.close()
                except Exception:
                    pass
            sess = AsyncSession(impersonate=impersonate_target)
            self._sessions[pool_key] = sess
            return sess

    async def _evict_session(self, pool_key: str):
        if not self._pool_enabled:
            return
        async with self._lock:
            sess = self._sessions.pop(pool_key, None)
            if sess:
                try:
                    await sess.close()
                except Exception:
                    pass

    async def close(self):
        """Close all pooled sessions on application shutdown."""
        async with self._lock:
            for key, sess in list(self._sessions.items()):
                try:
                    await sess.close()
                except Exception:
                    pass
            self._sessions.clear()
            logger.info("[FastTLS] Session pool closed.")

    async def request(
        self,
        url: str,
        method: str = "GET",
        post_data: Optional[str] = None,
        cookies: Optional[List[CookieModel]] = None,
        headers: Optional[Dict[str, str]] = None,
        proxy: Optional[str] = None,
        timeout: int = 15,
        user_agent: Optional[str] = None,
        session_id: Optional[str] = None
    ) -> Tuple[bool, Optional[SolutionModel]]:
        # A caller-pinned UA keeps the default target instead of rotating.
        if user_agent:
            active_ua = user_agent
            impersonate_target = self.impersonate_target
        else:
            impersonate_target, active_ua = self._profile_for_domain(url)

        domain = self._normalize_domain(url)
        # Each pooled session has its own cookie jar, so the session id keys it to prevent cookie bleed.
        pool_key = f"{domain}:{impersonate_target}:{proxy or ''}:{session_id or ''}"

        cookie_dict = {}
        if cookies:
            for c in cookies:
                cookie_dict[c.name] = c.value

        req_headers = {
            "User-Agent": active_ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Language": "en-US,en;q=0.9",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Sec-Ch-Ua-Platform": '"Windows"',
        }
        sec_ch_ua = _sec_ch_ua_for(impersonate_target)
        if sec_ch_ua:
            req_headers["Sec-Ch-Ua"] = sec_ch_ua
            req_headers["Sec-Ch-Ua-Mobile"] = "?0"
        if headers:
            # Case-insensitively merge caller headers over defaults
            lower_to_key = {k.lower(): k for k in req_headers}
            for k, v in headers.items():
                existing_key = lower_to_key.get(k.lower())
                if existing_key and existing_key != k:
                    del req_headers[existing_key]
                req_headers[k] = v

        proxies = None
        if proxy:
            proxies = {"http": proxy, "https": proxy}

        proxy_desc = f" | Proxy: {sanitize_proxy_url(proxy)}" if proxy else ""
        logger.info(f"[FastTLS] Executing async {method.upper()} -> {url} (impersonate='{impersonate_target}', timeout={timeout}s{proxy_desc})")

        session = None
        deadline = time.monotonic() + timeout
        try:
            session = await self._get_session(pool_key, impersonate_target)
            current_url = url
            current_method = method.upper()
            current_post_data = post_data
            resp = None

            for redirect_count in range(MAX_REDIRECTS + 1):
                await check_target_url_async(
                    current_url,
                    label="Target" if redirect_count == 0 else "Redirect target",
                )
                remaining_timeout = deadline - time.monotonic()
                if remaining_timeout <= 0:
                    raise asyncio.TimeoutError("Fast TLS redirect chain exhausted its timeout")
                request_kwargs = {
                    "headers": req_headers,
                    "cookies": cookie_dict if redirect_count == 0 else None,
                    "proxies": proxies,
                    "timeout": remaining_timeout,
                    "allow_redirects": False,
                }
                if current_method == "POST":
                    resp = await session.post(current_url, data=current_post_data, **request_kwargs)
                else:
                    resp = await session.get(current_url, **request_kwargs)

                location = resp.headers.get("location")
                if resp.status_code not in (301, 302, 303, 307, 308) or not location:
                    break
                if redirect_count >= MAX_REDIRECTS:
                    raise RuntimeError(f"Redirect limit ({MAX_REDIRECTS}) exceeded")

                next_url = urljoin(str(resp.url), location)
                await check_target_url_async(next_url, label="Redirect target")
                if resp.status_code == 303 or (resp.status_code in (301, 302) and current_method == "POST"):
                    current_method = "GET"
                    current_post_data = None
                current_url = next_url

            is_cf_challenge = False
            matched_marker = None
            body_text = resp.text or ""
            body_lower = body_text.lower()
            content_type = (resp.headers.get("content-type") or "").lower()
            is_non_html_api = any(
                ct in content_type for ct in [
                    "application/json",
                    "application/problem+json",
                    "text/plain",
                    "application/xml",
                    "text/xml",
                    "application/octet-stream",
                ]
            )

            if resp.status_code == 200 and is_non_html_api:
                # A 200 non-HTML API response is never a challenge page.
                is_cf_challenge = False
            else:
                title_match = re.search(r"<title[^>]*>(.*?)</title>", body_text, re.IGNORECASE | re.DOTALL)
                page_title = title_match.group(1).strip() if title_match else ""

                detected_challenge = detect_challenge(page_title, body_lower, check_content=True)

                embedded_script_markers = [
                    "challenges.cloudflare.com", "cf-challenge", "turnstile.min.js",
                    "check.ddos-guard.net", "geo.captcha-delivery.com"
                ]

                if detected_challenge:
                    is_cf_challenge = True
                    matched_marker = detected_challenge
                elif is_challenge_title(page_title):
                    is_cf_challenge = True
                    matched_marker = "challenge_title"
                elif resp.status_code in [403, 429, 503]:
                    is_cf_challenge = True
                    matched_marker = f"http_{resp.status_code}"
                elif any(marker in body_lower for marker in embedded_script_markers):
                    is_cf_challenge = True
                    matched_marker = "embedded_challenge_script"

            if is_cf_challenge:
                self.record_outcome(url, impersonate_target, False)
                logger.info(f"[FastTLS] WAF challenge detected (HTTP {resp.status_code}, marker: '{matched_marker}')")
            else:
                self.record_outcome(url, impersonate_target, True)
                logger.info(f"[FastTLS] Direct HTTP response received (HTTP Status: {resp.status_code}, Length: {len(resp.text)} bytes)")

            captured_cookies: List[CookieModel] = []
            # Use the post-redirect URL; a cross-domain chain would otherwise file cookies under the wrong domain.
            parsed_req_url = urlparse(str(resp.url))
            cookie_domain = parsed_req_url.netloc.split(":")[0].lstrip(".")
            for name, val in resp.cookies.items():
                captured_cookies.append(
                    CookieModel(
                        name=name,
                        value=val,
                        domain=cookie_domain,
                        path="/",
                        expires=-1,
                        size=len(name) + len(val),
                        httpOnly=False,
                        secure=False,
                        session=False,
                        sameSite="Lax"
                    )
                )

            solution = SolutionModel(
                url=str(resp.url),
                status=resp.status_code,
                headers=dict(resp.headers),
                response=resp.text,
                cookies=captured_cookies,
                userAgent=active_ua
            )

            return is_cf_challenge, solution

        except Exception as e:
            await self._evict_session(pool_key)
            self.record_outcome(url, impersonate_target, False)
            logger.warning(f"[FastTLS] Fast TLS request failed or timed out for {url}: {type(e).__name__} - {e}")
            return True, None
        finally:
            if session is not None and not self._pool_enabled:
                try:
                    await session.close()
                except Exception:
                    pass

fast_tls_engine = FastTLSEngine()
