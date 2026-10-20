from typing import List, Dict, Any, Literal, Optional, Tuple, Union
from pydantic import BaseModel, Field, field_validator, model_validator
import time

# Debug capture kinds a browser solve can record (app/solver/browser/debug_capture.py).
CAPTURE_KINDS = ("console", "network", "redirects")


def _validate_capture(kinds: Optional[List[str]]) -> Optional[List[str]]:
    unknown = sorted(set(kinds or []) - set(CAPTURE_KINDS))
    if unknown:
        raise ValueError(f"unknown capture kind(s) {unknown}; allowed: {list(CAPTURE_KINDS)}")
    return kinds


def _drop_empty_proxy(v):
    # Prowlarr sends {"url": null, "username": null, "password": null} when it has no proxy set.
    if type(v) is dict:
        # Fast path: a real proxy (the common case) has its address set.
        if v.get("url") or v.get("server") or v.get("host"):
            return v
        return v if any(val is not None and str(val).strip() for val in v.values()) else None
    if type(v) is str and not v.strip():
        return None
    return v


def _with_proxy_credentials(url: str, username: Optional[str], password: Optional[str]) -> str:
    if not (username and password) or "@" in url:
        return url
    scheme, rest = url.split("://", 1) if "://" in url else ("http", url)
    return f"{scheme}://{username}:{password}@{rest}"


class CookieModel(BaseModel):
    name: str
    value: str
    domain: Optional[str] = None
    path: Optional[str] = "/"
    expires: Optional[Union[float, int]] = -1
    size: Optional[int] = 0
    httpOnly: Optional[bool] = False
    secure: Optional[bool] = False
    session: Optional[bool] = False
    sameSite: Optional[str] = "Lax"

    @field_validator("path", mode="before")
    @classmethod
    def _coerce_path(cls, v):
        return "/" if v is None else str(v)

    @field_validator("expires", mode="before")
    @classmethod
    def _coerce_expires(cls, v):
        return -1 if v is None else v

    @field_validator("size", mode="before")
    @classmethod
    def _coerce_size(cls, v):
        return 0 if v is None else v

    @field_validator("httpOnly", "secure", "session", mode="before")
    @classmethod
    def _coerce_bools(cls, v):
        return False if v is None else bool(v)

    @field_validator("sameSite", mode="before")
    @classmethod
    def _coerce_same_site(cls, v):
        return "Lax" if v is None else str(v)

class ProxyConfig(BaseModel):
    url: str
    username: Optional[str] = None
    password: Optional[str] = None

class V1Request(BaseModel):
    cmd: str
    url: Optional[str] = None
    postData: Optional[str] = None
    cookies: Optional[List[CookieModel]] = None
    returnOnlyCookies: Optional[bool] = False
    maxTimeout: Optional[int] = 60000
    proxy: Optional[Union[ProxyConfig, Dict[str, Any], str]] = None
    session: Optional[str] = None
    session_ttl: Optional[int] = None
    # FlareSolverr's name: on a request command, a session older than this is recreated.
    session_ttl_minutes: Optional[int] = None
    userAgent: Optional[str] = None
    headers: Optional[Dict[str, Any]] = None
    # FlareSolverr 3.x request options, mapped onto Solverr's own fields in _map_flaresolverr_options.
    returnScreenshot: Optional[bool] = False
    waitInSeconds: Optional[int] = None
    disableMedia: Optional[bool] = False
    # Clicking a captcha embedded in the real page (a login or comment form) only earns that form's
    # token. FlareSolverr clients (Prowlarr, Jackett, ...) only want the page, so /v1 skips it
    # unless asked; /scrape and MCP turn it on. Provider walls are solved either way.
    solveEmbeddedCaptchas: Optional[bool] = False
    # Accepted for compatibility only: Solverr locates the Turnstile checkbox itself, not by tabbing to it.
    tabs_till_verify: Optional[int] = None

    @field_validator("proxy", mode="before")
    @classmethod
    def _coerce_proxy(cls, v):
        return _drop_empty_proxy(v)

    @field_validator("headers", mode="before")
    @classmethod
    def _coerce_headers(cls, v):
        if isinstance(v, dict):
            return {str(k): str(val) if val is not None else "" for k, val in v.items()}
        return v
    
    # Solverr extensions beyond the FlareSolverr API.
    fastTlsOnly: Optional[bool] = False
    forceBrowser: Optional[bool] = False
    # Skip Tiers 1-3 and solve straight through the fallback proxy pool (/scrape's tier="tier4_proxy").
    forceFallbackProxy: Optional[bool] = False
    screenshot: Optional[bool] = False
    screenshot_full_page: bool = False
    screenshot_selector: Optional[str] = None
    extract_records: Optional[Dict[str, Any]] = None
    wait_selector: Optional[str] = None
    wait_delay_ms: Optional[int] = None
    skip_cache: Optional[bool] = False
    # None falls back to FOLLOW_META_REFRESH.
    followMetaRefresh: Optional[bool] = None
    # Browser debug capture: any of "console", "network", "redirects". Forces the browser tier.
    capture: Optional[List[str]] = None

    @field_validator("capture")
    @classmethod
    def _check_capture(cls, v):
        return _validate_capture(v)

    @model_validator(mode="after")
    def _map_flaresolverr_options(self):
        if self.returnScreenshot:
            self.screenshot = True
        if self.waitInSeconds and self.waitInSeconds > 0 and not self.wait_delay_ms:
            self.wait_delay_ms = self.waitInSeconds * 1000
        # Screenshots and debug capture only exist in the browser; FlareSolverr always used one.
        if self.screenshot or self.capture or self.forceFallbackProxy:
            self.forceBrowser = True
        return self

    def debug_capture(self) -> Optional[Tuple[str, ...]]:
        return tuple(sorted(set(self.capture))) if self.capture else None

    def follows_meta_refresh(self) -> bool:
        from app.config import settings
        return settings.FOLLOW_META_REFRESH if self.followMetaRefresh is None else bool(self.followMetaRefresh)

    def get_proxy_url(self) -> Optional[str]:
        if not self.proxy:
            return None
        if isinstance(self.proxy, str):
            return self.proxy.strip() or None
        if isinstance(self.proxy, dict):
            # Prowlarr always sends {"url": null, "username": null, "password": null} when it has no
            # proxy configured: a null or blank field means "not set", never the string "None".
            fields = {k: str(v).strip() for k, v in self.proxy.items() if v is not None and str(v).strip()}
            username, password = fields.get("username"), fields.get("password")
            server = fields.get("url") or fields.get("server")
            if server:
                return _with_proxy_credentials(server, username, password)
            if fields.get("host") and fields.get("port"):
                user_pass = f"{username}:{password}@" if username and password else ""
                scheme = fields.get("scheme", "http")
                return f"{scheme}://{user_pass}{fields['host']}:{fields['port']}"
            return None
        if hasattr(self.proxy, "url"):
            url_str = (self.proxy.url or "").strip()
            if not url_str:
                return None
            return _with_proxy_credentials(
                url_str, getattr(self.proxy, "username", None), getattr(self.proxy, "password", None)
            )
        return None

class SolutionModel(BaseModel):
    url: str
    status: int
    headers: Dict[str, str] = Field(default_factory=dict)
    response: str = ""
    cookies: List[CookieModel] = Field(default_factory=list)
    userAgent: str = ""
    screenshot: Optional[str] = None
    extracted: Optional[Dict[str, Any]] = None
    challengeType: Optional[str] = None
    tier: Optional[str] = None
    # Opt-in debug capture (console / network / redirects), present only when requested.
    debug: Optional[Dict[str, Any]] = None
    # Browser localStorage/sessionStorage snapshot for the request's session; never sent to clients.
    storage: Optional[Dict[str, Any]] = Field(default=None, exclude=True)

class V1Response(BaseModel):
    status: str = "ok"  # "ok" or "error"
    message: str = "Challenge solved!"
    startTimestamp: int = Field(default_factory=lambda: int(time.time() * 1000))
    endTimestamp: int = Field(default_factory=lambda: int(time.time() * 1000))
    version: str = "v1.0.0"
    solution: Optional[SolutionModel] = None
    sessions: Optional[List[str]] = None
    session: Optional[str] = None

# /scrape tier overrides; "fast_tls" and "browser" are aliases of tier1_tls and tier3_browser.
SCRAPE_TIERS = {"auto", "tier1_tls", "fast_tls", "tier2_cache", "tier3_browser", "browser", "tier4_proxy"}


class ScrapeRequest(BaseModel):
    url: str
    method: str = "GET"
    postData: Optional[str] = None
    headers: Optional[Dict[str, Any]] = None

    @field_validator("headers", mode="before")
    @classmethod
    def _coerce_headers(cls, v):
        if isinstance(v, dict):
            return {str(k): str(val) if val is not None else "" for k, val in v.items()}
        return v
    cookies: Optional[List[CookieModel]] = None
    proxy: Optional[Union[ProxyConfig, Dict[str, Any], str]] = None
    session: Optional[str] = None
    tier: Optional[str] = "auto"
    wait_selector: Optional[str] = None
    wait_delay_ms: Optional[int] = None
    extract_rules: Optional[Dict[str, str]] = None
    screenshot: Optional[bool] = False
    screenshot_full_page: bool = False
    screenshot_selector: Optional[str] = None
    extract_records: Optional[Dict[str, Any]] = None
    maxTimeout: Optional[int] = 60000
    userAgent: Optional[str] = None
    skip_cache: Optional[bool] = False
    followMetaRefresh: Optional[bool] = None
    capture: Optional[List[str]] = None
    disable_media: bool = False
    solve_embedded_captchas: bool = True
    # "markdown"/"text" convert an HTML page after extraction; other content types pass through.
    format: Literal["html", "markdown", "text"] = "html"

    @field_validator("capture")
    @classmethod
    def _check_capture(cls, v):
        return _validate_capture(v)

    @field_validator("proxy", mode="before")
    @classmethod
    def _coerce_proxy(cls, v):
        return _drop_empty_proxy(v)

    @field_validator("tier")
    @classmethod
    def _check_tier(cls, v):
        if v is not None and v not in SCRAPE_TIERS:
            raise ValueError(f"unknown tier '{v}'; allowed: {sorted(SCRAPE_TIERS)}")
        return v

    def to_v1_request(self) -> V1Request:
        force_browser = (self.tier in ["tier3_browser", "browser"])
        # Tier 2 is Tier 1 replaying cached clearance cookies; neither may escalate to a browser.
        fast_tls_only = (self.tier in ["tier1_tls", "fast_tls", "tier2_cache"])
        return V1Request(
            cmd=f"request.{self.method.lower()}",
            url=self.url,
            postData=self.postData,
            headers=self.headers,
            cookies=self.cookies,
            proxy=self.proxy,
            session=self.session,
            maxTimeout=self.maxTimeout,
            userAgent=self.userAgent,
            forceBrowser=force_browser or bool(self.screenshot or self.extract_records or self.screenshot_full_page or self.screenshot_selector),
            fastTlsOnly=fast_tls_only,
            screenshot=self.screenshot,
            screenshot_full_page=self.screenshot_full_page,
            screenshot_selector=self.screenshot_selector,
            extract_records=self.extract_records,
            wait_selector=self.wait_selector,
            wait_delay_ms=self.wait_delay_ms,
            skip_cache=self.skip_cache,
            followMetaRefresh=self.followMetaRefresh,
            forceFallbackProxy=self.tier == "tier4_proxy",
            capture=self.capture,
            disableMedia=self.disable_media,
            solveEmbeddedCaptchas=self.solve_embedded_captchas,
        )

class ScrapeResponse(BaseModel):
    status: str = "ok"
    url: str
    http_status: int
    tier_used: str
    duration_ms: float
    cookies: List[CookieModel] = Field(default_factory=list)
    headers: Dict[str, str] = Field(default_factory=dict)
    content: str = ""
    extracted: Optional[Dict[str, Any]] = None
    screenshot: Optional[str] = None
    debug: Optional[Dict[str, Any]] = None

class TestRequestModel(BaseModel):
    url: str
    method: str = "GET"
    postData: Optional[str] = None
    forceBrowser: bool = False
    useCache: bool = True
    screenshot: bool = False
