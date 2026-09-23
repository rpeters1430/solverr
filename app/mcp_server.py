"""MCP server mounted at /mcp, exposing Solverr's solver as agent tools.

Tools share the HTTP routes' singletons, so MCP solves appear in /metrics and the dashboard.
"""
import base64
import logging
from typing import Any, Dict, Optional

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

from app.api.flaresolverr import _extract_data
from app.config import settings
from app.models.flaresolverr import ScrapeRequest
from app.solver.browser import browser_pool
from app.solver.cache import cookie_cache
from app.solver.engine import metrics, solver_engine

logger = logging.getLogger("solverr.mcp")

mcp_server: MCPServer = MCPServer(
    name="solverr",
    version=settings.VERSION,
    instructions=(
        "Solverr solves Cloudflare (Turnstile, 5s interstitial), Google reCAPTCHA v2, "
        "hCaptcha, GeeTest, Imperva, DataDome, Akamai, and AWS WAF challenges, then "
        "returns the resulting page. Use solverr_scrape to fetch a URL's content, "
        "solverr_screenshot for a visual capture, solverr_get_cookies to inspect "
        "clearance cookies Solverr already holds for a domain, and solverr_get_stats "
        "for engine/browser-pool health."
    ),
)


@mcp_server.tool()
async def solverr_scrape(
    url: str,
    method: str = "GET",
    post_data: Optional[str] = None,
    tier: str = "auto",
    wait_selector: Optional[str] = None,
    extract_rules: Optional[Dict[str, str]] = None,
    max_timeout_ms: int = 60000,
) -> Dict[str, Any]:
    """Fetch a URL through Solverr's tiered solver, automatically clearing any
    Cloudflare/CAPTCHA/WAF challenge in the way, and return its content.

    method: "GET" (default) or "POST" - anything else is rejected rather
    than silently sent as GET. post_data: request body for a POST.
    tier: "auto" (default, escalates only as needed), "tier1_tls" (Fast TLS
    only, no browser - fails rather than escalating if a challenge is hit),
    or "tier3_browser" (force a stealth browser solve). There is no tier
    value that forces Tier 4 directly - that's an automatic engine-side
    escalation (FALLBACK_PROXY_URL) after a direct browser solve fails, not
    a mode a caller can select up front.
    extract_rules: optional {name: rule} map for pulling fields out of the
    returned HTML - rule is a CSS selector (text), "selector@attr" (an
    attribute), "selector[]" (a list of matches), or "regex:pattern".
    """
    if method.upper() not in ("GET", "POST"):
        raise ToolError(f"Unsupported method '{method}' - only GET and POST are supported.")

    try:
        req = ScrapeRequest(
            url=url,
            method=method,
            postData=post_data,
            tier=tier,
            wait_selector=wait_selector,
            maxTimeout=max_timeout_ms,
        ).to_v1_request()
        solution = await solver_engine.process_request(req)
    except Exception as e:
        # Exception text can carry proxy credentials, so details go to the log only.
        logger.error(f"[MCP] solverr_scrape failed for {url}: {type(e).__name__}: {e}", exc_info=True)
        raise ToolError(f"Scrape failed for {url}; see server logs for details.") from e

    extracted = None
    if extract_rules and solution.response:
        extracted = _extract_data(solution.response, extract_rules)

    return {
        "url": solution.url,
        "http_status": solution.status,
        "tier_used": solution.tier or "tier1_fast_tls",
        "challenge_type": solution.challengeType,
        "content": solution.response,
        "extracted": extracted,
        "cookie_count": len(solution.cookies),
    }


@mcp_server.tool()
async def solverr_screenshot(url: str, max_timeout_ms: int = 60000) -> Image:
    """Solve any challenge on a URL and return a JPEG screenshot of the
    resulting page. Always uses the stealth browser tier, since a screenshot
    requires a real rendered page rather than a raw HTTP response."""
    try:
        req = ScrapeRequest(
            url=url,
            tier="tier3_browser",
            screenshot=True,
            maxTimeout=max_timeout_ms,
        ).to_v1_request()
        solution = await solver_engine.process_request(req)
    except Exception as e:
        logger.error(f"[MCP] solverr_screenshot failed for {url}: {type(e).__name__}: {e}", exc_info=True)
        raise ToolError(f"Screenshot failed for {url}; see server logs for details.") from e

    if not solution.screenshot:
        raise ToolError(f"No screenshot was captured for {url} (http_status={solution.status})")
    # BrowserPool captures JPEG, and the format must match the bytes.
    return Image(data=base64.b64decode(solution.screenshot), format="jpeg")


@mcp_server.tool()
async def solverr_get_cookies(domain: str) -> Dict[str, str]:
    """Return clearance cookies (e.g. cf_clearance) Solverr already has
    cached for a domain, without making a new request. Empty if nothing is
    cached yet for that domain."""
    cookies = await cookie_cache.get_cookies_async(domain)
    return {cookie.name: cookie.value for cookie in cookies}


@mcp_server.tool()
def solverr_get_stats() -> Dict[str, Any]:
    """Return Solverr's current engine health: per-tier request counts,
    challenge types solved, cache hit rate, timeouts, and browser pool
    utilization."""
    stats = metrics.to_dict()
    stats["browser_pool"] = browser_pool.pool_stats()
    return stats


# The SDK has no "any host" wildcard, so with no API_KEY the default allowlist is localhost only.
_LOCAL_ONLY_ALLOWED_HOSTS = [
    "127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*", "[::1]", "[::1]:*",
]
_LOCAL_ONLY_ALLOWED_ORIGINS = [
    "http://127.0.0.1", "http://127.0.0.1:*", "http://localhost", "http://localhost:*",
    "http://[::1]", "http://[::1]:*",
    "https://127.0.0.1", "https://127.0.0.1:*", "https://localhost", "https://localhost:*",
    "https://[::1]", "https://[::1]:*",
]


def _mcp_transport_security() -> TransportSecuritySettings:
    if settings.API_KEY:
        # The API key already gates every call; a Host check would only reject real deployment hostnames.
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    # Without a key, any browser tab could reach the tools via DNS rebinding.
    # MCP_ALLOWED_HOSTS/_ORIGINS add to localhost rather than replace it.
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_LOCAL_ONLY_ALLOWED_HOSTS + settings.MCP_ALLOWED_HOSTS,
        allowed_origins=_LOCAL_ONLY_ALLOWED_ORIGINS + settings.MCP_ALLOWED_ORIGINS,
    )


def create_mcp_asgi_app() -> Starlette:
    """Call exactly once, before `mcp_server.session_manager` is accessed.

    Stateless so replicas behind a load balancer don't need sticky sessions.
    """
    return mcp_server.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
        transport_security=_mcp_transport_security(),
    )
