import logging
import time
import zlib
from typing import Dict, List, Optional, Tuple
from curl_cffi.requests import AsyncSession
from app.models.flaresolverr import CookieModel, SolutionModel
from app.config import settings
from app.logging_config import sanitize_proxy_url

logger = logging.getLogger("solverr.fast_tls")

# TLS impersonation profiles, each paired with a User-Agent that actually
# matches the claimed browser/version. A JA3/JA4 fingerprint that says
# "Firefox 147" alongside a "Firefox 135" User-Agent header is itself a
# strong bot signal, so target and UA must always travel together.
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

    def _profile_for_domain(self, url: str) -> Tuple[str, str]:
        """Deterministically pick a TLS/UA profile per-domain so repeat
        requests to the same site stay consistent, while different domains
        get spread across profiles to avoid a single reusable fingerprint
        being linkable across every site this instance touches."""
        if not self.rotate or len(self.profiles) == 1:
            return self.profiles[0]
        domain = url.split("://", 1)[-1].split("/", 1)[0].lower()
        idx = zlib.crc32(domain.encode("utf-8")) % len(self.profiles)
        return self.profiles[idx]

    async def request(
        self,
        url: str,
        method: str = "GET",
        post_data: Optional[str] = None,
        cookies: Optional[List[CookieModel]] = None,
        headers: Optional[Dict[str, str]] = None,
        proxy: Optional[str] = None,
        timeout: int = 15,
        user_agent: Optional[str] = None
    ) -> Tuple[bool, Optional[SolutionModel]]:
        # Only rotate the TLS/UA pair when the caller didn't pin a specific
        # User-Agent - an explicit user_agent means the caller wants control,
        # so we keep the configured default impersonation target as-is
        # rather than risk a UA/JA3 mismatch.
        if user_agent:
            active_ua = user_agent
            impersonate_target = self.impersonate_target
        else:
            impersonate_target, active_ua = self._profile_for_domain(url)

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

        try:
            async with AsyncSession(impersonate=impersonate_target) as session:
                if method.upper() == "POST":
                    resp = await session.post(
                        url,
                        data=post_data,
                        headers=req_headers,
                        cookies=cookie_dict,
                        proxies=proxies,
                        timeout=timeout,
                        allow_redirects=True
                    )
                else:
                    resp = await session.get(
                        url,
                        headers=req_headers,
                        cookies=cookie_dict,
                        proxies=proxies,
                        timeout=timeout,
                        allow_redirects=True
                    )

            # Check if page returned a WAF challenge response
            is_cf_challenge = False
            matched_marker = None
            body_lower = resp.text.lower() if resp.text else ""

            waf_markers = [
                "just a moment", "cf-challenge", "turnstile", "checking your browser",
                "attention required!", "akamai", "sec-cpt", "incapsula", "visid_incap",
                "datadome", "geetest", "recaptcha", "hcaptcha", "ddos-guard",
                "check.ddos-guard.net", "ddg-captcha", "cf-please-wait", "under attack"
            ]

            embedded_script_markers = [
                "challenges.cloudflare.com", "cf-challenge", "turnstile.min.js",
                "check.ddos-guard.net", "geo.captcha-delivery.com"
            ]

            if resp.status_code in [403, 429, 503]:
                for marker in waf_markers:
                    if marker in body_lower:
                        is_cf_challenge = True
                        matched_marker = marker
                        break
                if not is_cf_challenge:
                    # Generic 403/503 might still be an undetected bot wall
                    is_cf_challenge = True
                    matched_marker = f"http_{resp.status_code}"
            elif any(marker in body_lower for marker in embedded_script_markers):
                is_cf_challenge = True
                matched_marker = "embedded_challenge_script"
            elif resp.status_code == 200 and ("<title>just a moment" in body_lower or "checking your browser" in body_lower):
                is_cf_challenge = True
                matched_marker = "title_challenge_marker"

            if is_cf_challenge:
                logger.info(f"[FastTLS] WAF challenge detected (HTTP {resp.status_code}, marker: '{matched_marker}')")
            else:
                logger.info(f"[FastTLS] Direct HTTP response received (HTTP Status: {resp.status_code}, Length: {len(resp.text)} bytes)")

            captured_cookies: List[CookieModel] = []
            parsed_req_url = urlparse(url)
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
            logger.warning(f"[FastTLS] Fast TLS request failed or timed out for {url}: {type(e).__name__} - {e}")
            return True, None

fast_tls_engine = FastTLSEngine()
