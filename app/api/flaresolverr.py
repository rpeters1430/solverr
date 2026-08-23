import time
import re
import logging
from typing import Dict, Any, Optional
from fastapi import APIRouter, HTTPException, Request as FastAPIRequest, Response
from app.models.flaresolverr import V1Request, V1Response, ScrapeRequest, ScrapeResponse
from app.solver.engine import solver_engine
from app.solver.sessions import session_manager
from app.config import settings
from app.logging_config import sanitize_proxy_url

try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

logger = logging.getLogger("solverr.api.flaresolverr")
router = APIRouter()

def _extract_data(html: str, rules: Dict[str, str]) -> Dict[str, Any]:
    """Helper to extract data from HTML via CSS selectors or regex patterns."""
    extracted = {}
    if not html or not rules:
        return extracted

    soup = BeautifulSoup(html, "html.parser") if BS4_AVAILABLE else None

    for key, rule in rules.items():
        try:
            if rule.startswith("regex:"):
                pattern = rule[6:]
                match = re.search(pattern, html, re.DOTALL)
                extracted[key] = match.group(1) if match and match.groups() else (match.group(0) if match else None)
            elif soup:
                if "@" in rule:
                    sel, attr = rule.split("@", 1)
                    elem = soup.select_one(sel.strip())
                    extracted[key] = elem.get(attr.strip()) if elem else None
                elif rule.endswith("[]"):
                    sel = rule[:-2].strip()
                    elems = soup.select(sel)
                    extracted[key] = [e.get_text(strip=True) for e in elems]
                else:
                    elem = soup.select_one(rule.strip())
                    extracted[key] = elem.get_text(strip=True) if elem else None
        except Exception as e:
            logger.debug(f"[Extractor] Error extracting key '{key}': {e}")
            extracted[key] = None

    return extracted

@router.post("/v1", response_model=V1Response)
@router.post("/v2", response_model=V1Response)
async def flaresolverr_api(req: V1Request):
    start_ts = int(time.time() * 1000)
    cmd = req.cmd.lower() if req.cmd else ""

    proxy_str = None
    if req.proxy:
        raw_p = req.proxy.get("url") if isinstance(req.proxy, dict) else str(req.proxy)
        proxy_str = sanitize_proxy_url(raw_p)

    extra_opts = []
    if req.session:
        extra_opts.append(f"session='{req.session}'")
    if proxy_str:
        extra_opts.append(f"proxy='{proxy_str}'")
    if req.forceBrowser:
        extra_opts.append("forceBrowser=True")
    if req.maxTimeout:
        extra_opts.append(f"maxTimeout={req.maxTimeout}ms")
    
    opts_desc = f" ({', '.join(extra_opts)})" if extra_opts else ""
    logger.info(f"FlareSolverr Command -> '{cmd}' | Target: '{req.url}'{opts_desc}")

    if cmd in ["request.get", "request.post"]:
        if not req.url:
            logger.warning("Rejecting request: Parameter 'url' is required")
            return V1Response(
                status="error",
                message="Error: Parameter 'url' is required for request commands",
                startTimestamp=start_ts,
                endTimestamp=int(time.time() * 1000)
            )
        
        # Attach session proxy/cookies if session ID is provided
        if req.session:
            sess = session_manager.get_session(req.session)
            if sess:
                if sess.cookies:
                    req.cookies = (req.cookies or []) + sess.cookies
                if sess.proxy and not req.proxy:
                    req.proxy = {"url": sess.proxy}

        try:
            solution = await solver_engine.process_request(req)
            
            # Update session cookies if applicable
            if req.session and solution.cookies:
                session_manager.update_session_cookies(req.session, solution.cookies)

            # Filter solution if returnOnlyCookies requested
            if req.returnOnlyCookies:
                solution.response = ""
                solution.headers = {}

            elapsed_ms = int(time.time() * 1000) - start_ts
            cookie_count = len(solution.cookies) if solution.cookies else 0
            logger.info(f"Solve completed in {elapsed_ms}ms -> Status: {solution.status}, Cookies: {cookie_count}")

            return V1Response(
                status="ok",
                message="Challenge solved!",
                startTimestamp=start_ts,
                endTimestamp=int(time.time() * 1000),
                version=settings.VERSION,
                solution=solution
            )

        except Exception as e:
            elapsed_ms = int(time.time() * 1000) - start_ts
            logger.error(f"Solve request failed after {elapsed_ms}ms for {req.url}: {type(e).__name__} - {str(e)}", exc_info=True)
            return V1Response(
                status="error",
                message=f"Error solving request: {str(e)}",
                startTimestamp=start_ts,
                endTimestamp=int(time.time() * 1000),
                version=settings.VERSION
            )

    elif cmd == "sessions.create":
        proxy_url = req.get_proxy_url()
        sid = session_manager.create_session(session_id=req.session, proxy=proxy_url, ttl=req.session_ttl or 7200)
        return V1Response(
            status="ok",
            message=f"Session created with ID: {sid}",
            startTimestamp=start_ts,
            endTimestamp=int(time.time() * 1000),
            version=settings.VERSION,
            session=sid
        )

    elif cmd == "sessions.destroy":
        if not req.session:
            return V1Response(
                status="error",
                message="Error: Parameter 'session' is required for sessions.destroy",
                startTimestamp=start_ts,
                endTimestamp=int(time.time() * 1000)
            )
        
        success = session_manager.destroy_session(req.session)
        msg = f"Session '{req.session}' destroyed" if success else f"Session '{req.session}' not found"
        return V1Response(
            status="ok" if success else "error",
            message=msg,
            startTimestamp=start_ts,
            endTimestamp=int(time.time() * 1000),
            version=settings.VERSION
        )

    elif cmd == "sessions.list":
        active_sessions = session_manager.list_sessions()
        return V1Response(
            status="ok",
            message="Active sessions retrieved",
            startTimestamp=start_ts,
            endTimestamp=int(time.time() * 1000),
            version=settings.VERSION,
            sessions=active_sessions
        )

    else:
        return V1Response(
            status="error",
            message=f"Unknown command '{req.cmd}'",
            startTimestamp=start_ts,
            endTimestamp=int(time.time() * 1000),
            version=settings.VERSION
        )

@router.post("/scrape", response_model=ScrapeResponse)
async def native_scrape_api(req: ScrapeRequest):
    """
    High-Performance Native Scrape API (Trawl Parity & Extensions).
    Provides fine-grained tier controls, DOM selector waiting, data extraction, and screenshots.
    """
    start_ts = time.time()
    v1_req = req.to_v1_request()

    try:
        solution = await solver_engine.process_request(v1_req)
        duration_ms = round((time.time() - start_ts) * 1000, 2)

        tier_used = solution.tier or "tier1_fast_tls"

        extracted = None
        if req.extract_rules and solution.response:
            extracted = _extract_data(solution.response, req.extract_rules)

        return ScrapeResponse(
            status="ok",
            url=solution.url,
            http_status=solution.status,
            tier_used=tier_used,
            duration_ms=duration_ms,
            cookies=solution.cookies,
            headers=solution.headers,
            content=solution.response,
            extracted=extracted,
            screenshot=solution.screenshot
        )
    except Exception as e:
        logger.error(f"[ScrapeAPI] Scrape failed for {req.url}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Scrape failed: {str(e)}")

@router.get("/proxy")
@router.post("/proxy")
async def transparent_proxy(request: FastAPIRequest, url: Optional[str] = None):
    """
    Transparent Challenge-Aware HTTP Proxy Endpoint (TRAWL-Style).
    Passes requests through Solverr's 4-tier engine and returns the solved response directly.
    """
    cmd = f"request.{request.method.lower()}"
    # A caller's own target URL commonly carries its own unescaped "&"-joined
    # query string (e.g. ?url=https://example.com/api?a=1&b=2) - Starlette's
    # query_params would split that at the outer "&" and truncate it, so take
    # everything after "url=" verbatim instead of relying on parsed params.
    raw_query = str(request.url.query)
    target_url = url or ""
    if "url=" in raw_query:
        target_url = raw_query.split("url=", 1)[1]
        if target_url.startswith("http%3A") or target_url.startswith("https%3A"):
            from urllib.parse import unquote
            target_url = unquote(target_url)

    if not target_url:
        raise HTTPException(status_code=400, detail="Missing target 'url' parameter")

    body = await request.body()
    post_data = body.decode("utf-8") if body else None

    # Strip hop-by-hop / identity headers before forwarding: these describe
    # the connection between the caller and Solverr, not the outbound
    # request Solverr makes to the target site (a stale Content-Length after
    # the body's been re-read, or a Host pointing at Solverr itself, would
    # otherwise leak straight into the outbound request).
    _HOP_BY_HOP_HEADERS = {
        "host", "connection", "content-length", "transfer-encoding",
        "keep-alive", "proxy-authenticate", "proxy-authorization",
        "te", "trailer", "upgrade", "accept-encoding", "x-api-key",
    }
    forward_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP_HEADERS
    }

    v1_req = V1Request(
        cmd=cmd,
        url=target_url,
        postData=post_data,
        headers=forward_headers
    )

    try:
        solution = await solver_engine.process_request(v1_req)
        media_type = solution.headers.get("content-type", "text/html")
        if ";" in media_type:
            media_type = media_type.split(";")[0].strip()
        response = Response(
            content=solution.response,
            status_code=solution.status,
            media_type=media_type
        )
        if solution.cookies:
            for c in solution.cookies:
                clean_dom = c.domain.lstrip(".") if c.domain else None
                try:
                    response.set_cookie(
                        key=c.name,
                        value=c.value,
                        domain=clean_dom,
                        path=c.path or "/",
                        httponly=bool(c.httpOnly),
                        secure=bool(c.secure)
                    )
                except Exception:
                    pass
        return response
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Proxy error solving '{target_url}': {e}")
