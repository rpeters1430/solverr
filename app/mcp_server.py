"""MCP server optionally mounted at /mcp, exposing Solverr's solver as agent tools.

Tools share the HTTP routes' singletons, so MCP solves appear in /metrics and the dashboard.
"""
import base64
import logging
from typing import Any, Dict, List, Optional

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import ValidationError
from starlette.applications import Starlette

from app.api.flaresolverr import _extract_data
from app.config import settings
from app.content_format import convert_content
from app.models.flaresolverr import CAPTURE_KINDS, ScrapeRequest, SolutionModel, V1Request
from app.solver.browser import ChallengeNotSolvedError, browser_pool
from app.solver.cache import cookie_cache
from app.solver.engine import RequestConfigError, metrics, solver_engine
from app.solver.sessions import apply_session, persist_session

logger = logging.getLogger("solverr.mcp")

mcp_server: MCPServer = MCPServer(
    name="solverr",
    version=settings.VERSION,
    instructions=(
        "Solverr solves Cloudflare (Turnstile, 5s interstitial), Google reCAPTCHA v2, "
        "hCaptcha, GeeTest, Imperva, DataDome, Akamai, and AWS WAF challenges, then "
        "returns the resulting page. Use solverr_scrape to fetch a URL's content "
        "(format=\"markdown\" is the most compact to read), solverr_screenshot for a "
        "visual capture, solverr_inspect to debug a page (console, network requests, "
        "redirect chain), solverr_get_cookies to inspect "
        "clearance cookies Solverr already holds for a domain, and solverr_get_stats "
        "for engine/browser-pool health."
    ),
)


async def _solve(action: str, url: str, build_request) -> tuple:
    """Build and run a request for a tool; returns (request, solution). Errors become ToolErrors
    that say what was wrong with the input, and nothing else (exception text can carry proxy credentials)."""
    try:
        req: V1Request = build_request()
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(str(p) for p in err['loc']) or 'request'}: {err['msg']}" for err in e.errors())
        raise ToolError(f"Invalid arguments: {problems}") from e
    try:
        storage = await apply_session(req)
        solution: SolutionModel = await solver_engine.process_request(req, browser_storage=storage)
        await persist_session(req, solution)
    except Exception as e:
        logger.error(f"[MCP] {action} failed for {url}: {type(e).__name__}: {e}", exc_info=True)
        if isinstance(e, (ChallengeNotSolvedError, RequestConfigError)):
            raise ToolError(f"{action} failed for {url}: {e}") from e
        raise ToolError(f"{action} failed for {url}; see server logs for details.") from e
    return req, solution


@mcp_server.tool()
async def solverr_scrape(
    url: str,
    method: str = "GET",
    post_data: Optional[str] = None,
    tier: str = "auto",
    wait_selector: Optional[str] = None,
    extract_rules: Optional[Dict[str, str]] = None,
    extract_records: Optional[Dict[str, Any]] = None,
    max_timeout_ms: int = 60000,
    follow_meta_refresh: Optional[bool] = None,
    session: Optional[str] = None,
    format: str = "html",
    solve_embedded_captchas: bool = True,
) -> Dict[str, Any]:
    """Fetch a URL through Solverr's tiered solver, automatically clearing any
    Cloudflare/CAPTCHA/WAF challenge in the way, and return its content.

    method: "GET" (default) or "POST" - anything else is rejected rather
    than silently sent as GET. post_data: request body for a POST.
    tier: "auto" (default, escalates only as needed), "tier1_tls" (Fast TLS
    only, no browser - fails rather than escalating if a challenge is hit),
    "tier2_cache" (Fast TLS replaying cached clearance cookies, no browser),
    "tier3_browser" (force a stealth browser solve), or "tier4_proxy"
    (stealth browser through the server's fallback proxy pool; an error if
    none is configured).
    format: "html" (default), "markdown" (readable and much smaller - best
    for reading a page), or "text". Applies to HTML pages only; JSON and
    other documents are returned as-is. Extraction always runs on the HTML.
    extract_rules: optional {name: rule} map for pulling fields out of the
    returned HTML - rule is a CSS selector (text), "selector@attr" (an
    attribute), "selector[]" (a list of matches), or "regex:pattern".
    follow_meta_refresh: follow short-delay <meta http-equiv="refresh">
    redirects (defaults to the server's FOLLOW_META_REFRESH setting).
    session: a FlareSolverr session id (sessions.create on /v1) whose cookies,
    proxy and browser storage this request should use and update.
    solve_embedded_captchas: try to solve a captcha that is part of the page
    itself (a login or comment form) before returning it (default true). Set
    false to get the page straight away; challenge walls are solved either way.
    """
    if method.upper() not in ("GET", "POST"):
        raise ToolError(f"Unsupported method '{method}' - only GET and POST are supported.")

    _req, solution = await _solve("Scrape", url, lambda: ScrapeRequest(
        url=url,
        method=method,
        postData=post_data,
        tier=tier,
        wait_selector=wait_selector,
        extract_records=extract_records,
        maxTimeout=max_timeout_ms,
        followMetaRefresh=follow_meta_refresh,
        session=session,
        format=format,
        solve_embedded_captchas=solve_embedded_captchas,
    ).to_v1_request())

    extracted = None
    if solution.extracted is not None:
        extracted = solution.extracted
    elif extract_rules and solution.response:
        extracted = _extract_data(solution.response, extract_rules)

    return {
        "url": solution.url,
        "http_status": solution.status,
        "tier_used": solution.tier or "tier1_fast_tls",
        "challenge_type": solution.challengeType,
        "content": convert_content(solution.response, solution.headers.get("content-type", ""), format),
        "extracted": extracted,
        "cookie_count": len(solution.cookies),
    }


@mcp_server.tool()
async def solverr_inspect(
    url: str,
    capture: Optional[List[str]] = None,
    max_timeout_ms: int = 60000,
    include_content: bool = False,
) -> Dict[str, Any]:
    """Debug a page: solve any challenge in a stealth browser and report what
    happened along the way - browser console messages and page errors, every
    network request with its status (failed ones with the failure reason), and
    the main-frame redirect chain (including challenge hops).

    capture: any of "console", "network", "redirects" (default: all three).
    include_content: also return the final page as markdown.
    """
    kinds = capture or list(CAPTURE_KINDS)
    _req, solution = await _solve("Inspect", url, lambda: ScrapeRequest(
        url=url,
        tier="tier3_browser",
        capture=kinds,
        maxTimeout=max_timeout_ms,
    ).to_v1_request())
    result: Dict[str, Any] = {
        "url": solution.url,
        "http_status": solution.status,
        "tier_used": solution.tier,
        "challenge_type": solution.challengeType,
        "content_type": solution.headers.get("content-type"),
        "cookies": sorted(c.name for c in solution.cookies),
        "debug": solution.debug or {},
    }
    if include_content:
        result["content"] = convert_content(solution.response, solution.headers.get("content-type", ""), "markdown")
    return result


@mcp_server.tool()
async def solverr_screenshot(url: str, max_timeout_ms: int = 60000,
                             full_page: bool = False, selector: Optional[str] = None) -> Image:
    """Solve any challenge on a URL and return a JPEG screenshot of the
    resulting page. Always uses the stealth browser tier, since a screenshot
    requires a real rendered page rather than a raw HTTP response."""
    _req, solution = await _solve("Screenshot", url, lambda: ScrapeRequest(
        url=url,
        tier="tier3_browser",
        screenshot=True,
        screenshot_full_page=full_page,
        screenshot_selector=selector,
        maxTimeout=max_timeout_ms,
    ).to_v1_request())

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
