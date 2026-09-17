"""MCP (Model Context Protocol) server exposing Solverr's tiered solver as
tools an AI agent can call directly, alongside the existing FlareSolverr
(`/v1`, `/v2`) and native (`/scrape`) HTTP APIs. Mounted at `/mcp` by
app/main.py when `settings.ENABLE_MCP` is true (the default).

Uses the `mcp` package's `MCPServer` (the `mcp` 2.x successor to the 1.x
`FastMCP` class - see the migration note in `mcp.server.fastmcp`). Tools
reuse the same `solver_engine`/`cookie_cache`/`browser_pool` singletons the
HTTP routes use, so a solve/cache hit through MCP shows up in the same
`/metrics` and dashboard the rest of Solverr does.
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
        # Never surface the raw exception to the MCP caller - like
        # app/main.py's catch-all handler, it can carry internal paths or
        # proxy credentials from deep in the solve pipeline. Full detail
        # goes to the server log only.
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
    # BrowserPool captures screenshots as JPEG (page.screenshot(type="jpeg"),
    # app/solver/browser/browser.py) - format must match the actual bytes,
    # not just the file extension callers might expect.
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


# DNS-rebinding protection only ever matches literal Host/Origin values (or
# a "host:*" port wildcard) - it has no "any host" wildcard, so it can't
# simply be pointed at "whatever Solverr's real deployment hostname turns
# out to be" (a NAS IP, a Docker network alias, a custom domain behind a
# reverse proxy - all unknown at container-build time). These are the
# defaults when API_KEY is unset and the operator hasn't set
# MCP_ALLOWED_HOSTS/MCP_ALLOWED_ORIGINS: local-only, so MCP still works out
# of the box for the common localhost/dev case without leaving an
# unauthenticated deployment reachable from the whole network via DNS
# rebinding.
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
        # A shared secret already gates every call (app/main.py's
        # middleware) - a Host-header check on top of that would only ever
        # reject legitimate requests to Solverr's actual deployment hostname
        # without stopping anyone who doesn't already have the key.
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    # No API_KEY: MCP would otherwise be a fully open surface (tools like
    # solverr_get_cookies included) reachable from any browser tab via DNS
    # rebinding. Keep the SDK's protection on, restricted to localhost
    # unless the operator opts into a wider deployment explicitly.
    # Additive, not a replacement: an operator adding a real deployment
    # hostname still expects localhost to keep working for local testing -
    # MCP_ALLOWED_HOSTS/_ORIGINS widen the allowlist, they don't narrow it.
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_LOCAL_ONLY_ALLOWED_HOSTS + settings.MCP_ALLOWED_HOSTS,
        allowed_origins=_LOCAL_ONLY_ALLOWED_ORIGINS + settings.MCP_ALLOWED_ORIGINS,
    )


def create_mcp_asgi_app() -> Starlette:
    """Build the MCP Streamable HTTP ASGI app. Must be called exactly once,
    before `mcp_server.session_manager` is accessed - app/main.py's lifespan
    enters that session manager's run() context alongside its own setup.

    `stateless_http=True` avoids sticky-session requirements (each request
    gets its own transport), which matters once Solverr runs multiple
    replicas behind a load balancer (see the "Horizontal Scaling" README
    section) with no guarantee two requests from the same MCP client land on
    the same replica. See `_mcp_transport_security()` for the DNS-rebinding
    protection decision.
    """
    return mcp_server.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
        transport_security=_mcp_transport_security(),
    )
