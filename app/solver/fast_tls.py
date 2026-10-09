import asyncio
import logging
import re
import time
import zlib
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse
from curl_cffi.requests import AsyncSession, BrowserType
from app.models.flaresolverr import CookieModel, SolutionModel
from app.solver.browser import WIDGET_CHALLENGES, detect_challenge, is_challenge_title, is_challenge_wall
from app.config import settings
from app.logging_config import sanitize_proxy_url
from app.security import check_target_url_async
from app.solver.meta_refresh import MAX_REFRESH_HOPS, meta_refresh_target

logger = logging.getLogger("solverr.fast_tls")

MAX_REDIRECTS = 10

# Target and UA always travel together; a mismatched pair is itself a bot signal.
FIREFOX_PROFILES = [
    ("firefox147", "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:147.0) Gecko/20100101 Firefox/147.0"),
    ("firefox144", "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:144.0) Gecko/20100101 Firefox/144.0"),
    ("firefox133", "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0"),
]
CHROME_PROFILES = [
    ("chrome150", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"),
    ("chrome146", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"),
    ("chrome145", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"),
    ("chrome142", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"),
]

SUPPORTED_TARGETS = {b.value for b in BrowserType}
_UA_VERSION_RE = {
    "firefox": re.compile(r"Firefox/(\d+)"),
    "chrome": re.compile(r"Chrome/(\d+)"),
}


def _platform_hint_for(target: str, user_agent: str) -> Optional[str]:
    """sec-ch-ua-platform matching the UA's OS; curl_cffi's Chrome targets default to "macOS"
    while our profile UAs are Windows. Firefox sends no client hints at all."""
    if not target.startswith("chrome"):
        return None
    if "Windows" in user_agent:
        return '"Windows"'
    if "Android" in user_agent:
        return '"Android"'
    if "Macintosh" in user_agent:
        return '"macOS"'
    if "CrOS" in user_agent:
        return '"Chrome OS"'
    if "Linux" in user_agent or "X11" in user_agent:
        return '"Linux"'
    return None


class FastTLSEngine:
    def __init__(self):
        self.impersonate_target = getattr(settings, "FAST_TLS_TARGET", "firefox")
        self.rotate = getattr(settings, "FAST_TLS_ROTATE", True)
        self.profiles = FIREFOX_PROFILES if self.impersonate_target.startswith("firefox") else CHROME_PROFILES
        self._domain_scores: "OrderedDict[str, Dict[str, int]]" = OrderedDict()
        # Clamp to at least 1 - a non-positive value would make the eviction
        # check in record_outcome() always true, causing popitem(last=False)
        # to raise KeyError on the still-empty mapping on the very first
        # new domain seen.
        self._max_domain_scores: int = max(1, getattr(settings, "MAX_FAST_TLS_DOMAIN_SCORES", 2000))
        self._sessions: Dict[Tuple[str, str, str, str], AsyncSession] = {}
        self._pool_enabled: bool = getattr(settings, "FAST_TLS_POOL_ENABLED", True)
        self._pool_size: int = getattr(settings, "FAST_TLS_POOL_SIZE", 50)
        self._lock = asyncio.Lock()

    def _normalize_domain(self, url: str) -> str:
        if "://" in url:
            domain = url.split("://", 1)[-1].split("/", 1)[0].split(":")[0].lower()
        else:
            domain = url.split("/")[0].split(":")[0].lower()
        return domain.lstrip(".")

    def is_compatible_user_agent(self, user_agent: Optional[str]) -> bool:
        """Whether a UA can ride this engine's TLS target; a Chrome UA on a Firefox handshake is itself a bot signal."""
        if not user_agent:
            return False
        if self.impersonate_target.startswith("firefox"):
            return "Firefox/" in user_agent
        return "Chrome/" in user_agent and "Firefox/" not in user_agent

    def target_for_user_agent(self, user_agent: str) -> str:
        """The impersonation target matching the UA's browser version: exact when curl_cffi
        supports it, else the newest supported one not newer than the UA, else the default."""
        family = "firefox" if self.impersonate_target.startswith("firefox") else "chrome"
        match = _UA_VERSION_RE[family].search(user_agent or "")
        if not match:
            return self.impersonate_target
        ua_version = int(match.group(1))
        candidates = [
            int(t[len(family):]) for t in SUPPORTED_TARGETS
            if t.startswith(family) and t[len(family):].isdigit() and int(t[len(family):]) <= ua_version
        ]
        return f"{family}{max(candidates)}" if candidates else self.impersonate_target

    def _select_cookies_for_url(self, cookies: Optional[List[CookieModel]], url: str) -> Dict[str, str]:
        """Collapse identity-distinct cookies (domain+path+name) to the flat
        name->value mapping curl_cffi's `cookies` kwarg accepts for a single
        request, applying standard cookie-matching rules (domain suffix,
        path prefix) rather than letting same-name cookies from unrelated
        domains/paths silently overwrite each other by insertion order.
        """
        if not cookies:
            return {}
        target_domain = self._normalize_domain(url)
        target_path = urlparse(url).path or "/"
        best: Dict[str, Tuple[str, str]] = {}  # name -> (path, value), keeping the most specific path
        for c in cookies:
            cookie_domain = (c.domain or "").lstrip(".").lower()
            if cookie_domain and cookie_domain != target_domain and not target_domain.endswith("." + cookie_domain):
                continue
            cookie_path = c.path or "/"
            if not (target_path == cookie_path or target_path.startswith(cookie_path.rstrip("/") + "/") or cookie_path == "/"):
                continue
            existing = best.get(c.name)
            if existing is None or len(cookie_path) >= len(existing[0]):
                best[c.name] = (cookie_path, c.value)
        return {name: value for name, (_, value) in best.items()}

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

    async def _get_session(self, pool_key: Tuple[str, str, str, str], impersonate_target: str) -> AsyncSession:
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

    async def _evict_session(self, pool_key: Tuple[str, str, str, str]):
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

    async def _fetch_following_redirects(
        self,
        session: AsyncSession,
        url: str,
        method: str,
        post_data: Optional[str],
        req_headers: Dict[str, str],
        cookie_dict: Optional[Dict[str, str]],
        proxies: Optional[Dict[str, str]],
        deadline: float,
        first_label: str = "Target",
    ):
        """Follow HTTP redirects by hand so every hop gets the SSRF check, within one deadline."""
        current_url = url
        current_method = method
        current_post_data = post_data
        resp = None

        for redirect_count in range(MAX_REDIRECTS + 1):
            await check_target_url_async(
                current_url,
                label=first_label if redirect_count == 0 else "Redirect target",
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

        return resp

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
        session_id: Optional[str] = None,
        follow_meta_refresh: bool = False,
    ) -> Tuple[bool, Optional[SolutionModel]]:
        # A pinned UA gets the TLS target closest to its own browser version, not a rotated one.
        if user_agent:
            active_ua = user_agent
            impersonate_target = self.target_for_user_agent(user_agent)
        else:
            impersonate_target, active_ua = self._profile_for_domain(url)

        domain = self._normalize_domain(url)
        # Include the caller's FlareSolverr session id in the pool key - the
        # pooled AsyncSession carries its own persistent cookie jar, so two
        # concurrent callers hitting the same domain under different (or no)
        # session ids must not share one jar and bleed cookies into each
        # other's requests/responses. A tuple key (rather than colon-joined
        # string) avoids ambiguous collisions between fields that can
        # themselves contain colons (e.g. a proxy URL's port).
        pool_key = (domain, impersonate_target, proxy or "", session_id or "")

        cookie_dict = self._select_cookies_for_url(cookies, url)

        # curl_cffi sends each target's real browser headers (Accept, Sec-Fetch-*, sec-ch-ua
        # with its per-version GREASE brand) in the browser's own order; overriding a name
        # keeps its position, so only override what must follow our UA.
        req_headers = {"User-Agent": active_ua}
        platform_hint = _platform_hint_for(impersonate_target, active_ua)
        if platform_hint:
            req_headers["sec-ch-ua-platform"] = platform_hint
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
            resp = await self._fetch_following_redirects(
                session, url, method.upper(), post_data, req_headers, cookie_dict, proxies, deadline
            )

            # Opt-in: a short-delay <meta http-equiv="refresh"> is followed like any other redirect.
            visited = {url, str(resp.url)}
            for hop in range(1, MAX_REFRESH_HOPS + 1):
                if not follow_meta_refresh or not (200 <= resp.status_code < 300):
                    break
                if "html" not in (resp.headers.get("content-type") or "").lower():
                    break
                refresh = meta_refresh_target(resp.text or "", str(resp.url))
                if not refresh or refresh[1] in visited:
                    break
                visited.add(refresh[1])
                logger.info(f"[FastTLS] Following meta refresh (hop {hop}/{MAX_REFRESH_HOPS}) -> {refresh[1]}")
                resp = await self._fetch_following_redirects(
                    session, refresh[1], "GET", None, req_headers, None, proxies, deadline, first_label="Redirect target"
                )
                visited.add(str(resp.url))

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

            if not (100 <= resp.status_code <= 599):
                logger.warning(f"[FastTLS] Invalid HTTP status code received: {resp.status_code}")
                return True, None

            if is_non_html_api and (resp.status_code not in [403, 429, 503] or not any(marker in body_lower for marker in ["cf-challenge", "turnstile", "challenges.cloudflare.com", "ddos-guard", "captcha"])):
                # Non-HTML API responses (json, text, xml) without challenge scripts are legitimate API responses
                is_cf_challenge = False
            else:
                title_match = re.search(r"<title[^>]*>(.*?)</title>", body_text, re.IGNORECASE | re.DOTALL)
                page_title = title_match.group(1).strip() if title_match else ""

                detected_challenge = detect_challenge(
                    page_title, body_lower, check_content=True, headers=resp.headers, status=resp.status_code
                )
                # A provider's telemetry script on an otherwise real page needs no browser; a wall does,
                # and so does an embedded captcha widget, which only renders once its script runs.
                if detected_challenge and detected_challenge not in WIDGET_CHALLENGES and not is_challenge_wall(
                    detected_challenge, page_title, body_lower, status=resp.status_code, headers=resp.headers
                ):
                    detected_challenge = None

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
            logger.warning(f"[FastTLS] Fast TLS request failed or timed out for {url}: {type(e).__name__} - {sanitize_proxy_url(str(e))}")
            return True, None
        finally:
            if session is not None and not self._pool_enabled:
                try:
                    await session.close()
                except Exception:
                    pass

fast_tls_engine = FastTLSEngine()
