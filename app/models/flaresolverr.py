from typing import List, Dict, Any, Optional, Union
from pydantic import BaseModel, Field
import time

class CookieModel(BaseModel):
    name: str
    value: str
    domain: Optional[str] = None
    path: Optional[str] = "/"
    expires: Union[float, int] = -1
    size: int = 0
    httpOnly: bool = False
    secure: bool = False
    session: bool = False
    sameSite: str = "Lax"

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
    userAgent: Optional[str] = None
    headers: Optional[Dict[str, str]] = None
    
    # Custom extension flags for high-performance tuning
    fastTlsOnly: Optional[bool] = False
    forceBrowser: Optional[bool] = False
    screenshot: Optional[bool] = False
    wait_selector: Optional[str] = None
    wait_delay_ms: Optional[int] = None

    def get_proxy_url(self) -> Optional[str]:
        if not self.proxy:
            return None
        if isinstance(self.proxy, str):
            return self.proxy
        if isinstance(self.proxy, dict):
            if "url" in self.proxy:
                url_str = str(self.proxy["url"])
                if "username" in self.proxy and "password" in self.proxy and "@" not in url_str:
                    scheme, rest = url_str.split("://", 1) if "://" in url_str else ("http", url_str)
                    return f"{scheme}://{self.proxy['username']}:{self.proxy['password']}@{rest}"
                return url_str
            if "server" in self.proxy:
                srv_str = str(self.proxy["server"])
                if "username" in self.proxy and "password" in self.proxy and "@" not in srv_str:
                    scheme, rest = srv_str.split("://", 1) if "://" in srv_str else ("http", srv_str)
                    return f"{scheme}://{self.proxy['username']}:{self.proxy['password']}@{rest}"
                return srv_str
            if "host" in self.proxy and "port" in self.proxy:
                user_pass = ""
                if "username" in self.proxy and "password" in self.proxy:
                    user_pass = f"{self.proxy['username']}:{self.proxy['password']}@"
                scheme = self.proxy.get("scheme", "http")
                return f"{scheme}://{user_pass}{self.proxy['host']}:{self.proxy['port']}"
        if hasattr(self.proxy, "url"):
            url_str = str(self.proxy.url)
            username = getattr(self.proxy, "username", None)
            password = getattr(self.proxy, "password", None)
            if username and password and "@" not in url_str:
                scheme, rest = url_str.split("://", 1) if "://" in url_str else ("http", url_str)
                return f"{scheme}://{username}:{password}@{rest}"
            return url_str
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

class V1Response(BaseModel):
    status: str = "ok"  # "ok" or "error"
    message: str = "Challenge solved!"
    startTimestamp: int = Field(default_factory=lambda: int(time.time() * 1000))
    endTimestamp: int = Field(default_factory=lambda: int(time.time() * 1000))
    version: str = "v1.0.0"
    solution: Optional[SolutionModel] = None
    sessions: Optional[List[str]] = None
    session: Optional[str] = None

class ScrapeRequest(BaseModel):
    url: str
    method: str = "GET"
    postData: Optional[str] = None
    headers: Optional[Dict[str, str]] = None
    cookies: Optional[List[CookieModel]] = None
    proxy: Optional[Union[ProxyConfig, Dict[str, Any], str]] = None
    session: Optional[str] = None
    tier: Optional[str] = "auto"  # "auto", "tier1_tls", "tier2_cache", "tier3_browser", "tier4_proxy"
    wait_selector: Optional[str] = None
    wait_delay_ms: Optional[int] = None
    extract_rules: Optional[Dict[str, str]] = None
    screenshot: Optional[bool] = False
    maxTimeout: Optional[int] = 60000
    userAgent: Optional[str] = None

    def to_v1_request(self) -> V1Request:
        force_browser = (self.tier in ["tier3_browser", "tier4_proxy", "browser"])
        fast_tls_only = (self.tier in ["tier1_tls", "fast_tls"])
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
            forceBrowser=force_browser,
            fastTlsOnly=fast_tls_only,
            screenshot=self.screenshot,
            wait_selector=self.wait_selector,
            wait_delay_ms=self.wait_delay_ms
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

class TestRequestModel(BaseModel):
    url: str
    method: str = "GET"
    postData: Optional[str] = None
    forceBrowser: bool = False
    useCache: bool = True
    screenshot: bool = False
