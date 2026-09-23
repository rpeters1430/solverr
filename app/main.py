import os
import time
import uuid
import hmac
import asyncio
import logging
import traceback
import contextlib
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse

from app.config import settings
from app.api.flaresolverr import router as flaresolverr_router
from app.api.dashboard import router as dashboard_router
from app.solver.browser import browser_pool, CAMOUFOX_AVAILABLE
from app.solver.fast_tls import fast_tls_engine
from app.metrics import generate_prometheus_metrics
from app.logging_config import setup_logging, set_request_id
from app.solver.sessions import session_manager
from app.solver.cache import cookie_cache

setup_logging(settings.LOG_LEVEL)
logger = logging.getLogger("solverr.main")

async def periodic_session_cleanup():
    while True:
        try:
            await asyncio.sleep(600)
            pruned = await session_manager.prune_expired_sessions_async()
            if pruned > 0:
                logger.info(f"[Lifespan] Periodic session cleanup pruned {pruned} expired session(s)")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"Error in session cleanup task: {e}")

# Built at import so session_manager exists; the lifespan enters its run() because mounting doesn't.
mcp_asgi_app = None
if settings.ENABLE_MCP:
    from app.mcp_server import create_mcp_asgi_app, mcp_server
    mcp_asgi_app = create_mcp_asgi_app()

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with contextlib.AsyncExitStack() as stack:
        logger.info(f"Initializing Solverr Engine v{settings.DISPLAY_VERSION}...")
        logger.info(f"Configuration | Host: {settings.HOST}:{settings.PORT} | Log Level: {settings.LOG_LEVEL.upper()} | Workers: {settings.MAX_BROWSER_WORKERS} | Fast TLS: {settings.ENABLE_FAST_TLS}")
        cleanup_task = asyncio.create_task(periodic_session_cleanup())
        if CAMOUFOX_AVAILABLE:
            logger.info("Camoufox stealth engine ready; the warm browser pool launches lazily on first solve.")
        else:
            logger.error("Camoufox stealth engine is not available (import failed) - Tier 3 browser-based solving will fail for every request until this is fixed.")
        if mcp_asgi_app is not None:
            await stack.enter_async_context(mcp_server.session_manager.run())
            logger.info("MCP server ready at /mcp (set ENABLE_MCP=false to disable).")
        yield
        logger.info("Shutting down Solverr Engine...")
        cleanup_task.cancel()
        await browser_pool.close()
        await fast_tls_engine.close()

app = FastAPI(
    title="Solverr",
    description="Ultra-fast FlareSolverr & Trawl replacement for Cloudflare, CAPTCHA bypass & HTTP proxy",
    version=settings.VERSION,
    lifespan=lifespan
)

# Health checks stay open so the container HEALTHCHECK works with API_KEY set.
_ALWAYS_UNAUTHENTICATED_PATHS = {"/health", "/health/live", "/health/ready"}

@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    req_id = uuid.uuid4().hex[:8]
    set_request_id(req_id)
    start_time = time.time()

    is_health_or_metrics = request.url.path in _ALWAYS_UNAUTHENTICATED_PATHS or request.url.path == "/metrics"
    if not is_health_or_metrics or logger.isEnabledFor(logging.DEBUG):
        client_ip = request.client.host if request.client else "unknown"
        logger.info(f"Incoming HTTP {request.method} {request.url.path} from {client_ip}")

    is_exempt_path = (
        request.url.path in _ALWAYS_UNAUTHENTICATED_PATHS
        or (request.url.path == "/metrics" and not settings.METRICS_REQUIRE_AUTH)
        or request.url.path.startswith("/static")
        or request.url.path == "/favicon.ico"
    )
    if settings.API_KEY and not is_exempt_path:
        # Header-only: a query-string key would leak into logs, history, and Referer headers.
        supplied = request.headers.get("x-api-key")
        if not supplied:
            auth_header = request.headers.get("authorization", "")
            if auth_header.lower().startswith("bearer "):
                supplied = auth_header[7:].strip()
        if not hmac.compare_digest((supplied or "").encode(), settings.API_KEY.encode()):
            logger.warning(f"Rejected unauthenticated request to {request.url.path} from {request.client.host if request.client else 'unknown'}")
            return JSONResponse(status_code=401, content={"status": "error", "error": "Missing or invalid X-Api-Key header"})

    if settings.MAX_REQUEST_BODY_MB > 0:
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > settings.MAX_REQUEST_BODY_MB * 1024 * 1024:
                    return JSONResponse(
                        status_code=413,
                        content={"status": "error", "error": f"Request body exceeds {settings.MAX_REQUEST_BODY_MB}MB limit"}
                    )
            except ValueError:
                pass

    try:
        response = await call_next(request)
        elapsed_ms = (time.time() - start_time) * 1000
        if not is_health_or_metrics or logger.isEnabledFor(logging.DEBUG):
            logger.info(f"HTTP {request.method} {request.url.path} -> {response.status_code} ({elapsed_ms:.1f}ms)")
        return response
    except Exception as exc:
        elapsed_ms = (time.time() - start_time) * 1000
        err_msg = f"{type(exc).__name__}: {str(exc)}"
        logger.error(f"Unhandled Exception on {request.method} {request.url.path} after {elapsed_ms:.1f}ms: {err_msg}\n{traceback.format_exc()}")
        # Exception text can carry proxy credentials; clients get only the request_id.
        return JSONResponse(
            status_code=500,
            content={"status": "error", "error": "Internal solver error", "request_id": req_id}
        )

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
static_dir = os.path.join(BASE_DIR, "static")
if not os.path.exists(static_dir):
    static_dir = os.path.join(os.path.dirname(BASE_DIR), "static")

if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

app.include_router(flaresolverr_router)
app.include_router(dashboard_router, prefix="/api")

if mcp_asgi_app is not None:
    app.mount("/mcp", mcp_asgi_app)

@app.get("/", response_class=HTMLResponse)
async def dashboard_index():
    search_paths = [
        os.path.join(BASE_DIR, "templates", "index.html"),
        os.path.join(os.path.dirname(BASE_DIR), "templates", "index.html"),
        "app/templates/index.html",
        "templates/index.html"
    ]
    for path in search_paths:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return HTMLResponse(f.read())
    return HTMLResponse("<h1>⚡ Solverr Engine Active</h1><p>API Endpoint active at <code>/v1</code></p>")

def _readiness_body() -> dict:
    # An unused lazy pool is healthy; only a failed Camoufox import leaves Tier 3 with no engine.
    ready = CAMOUFOX_AVAILABLE
    return {
        "status": "ok" if ready else "degraded",
        "version": settings.VERSION,
        "stealth_engine": "Camoufox Firefox",
        "workers": settings.MAX_BROWSER_WORKERS,
        "camoufox_pool_active_instances": browser_pool.camoufox_pool._created if browser_pool.camoufox_pool else 0,
        "camoufox_available": CAMOUFOX_AVAILABLE
    }, ready

@app.get("/health")
async def health_check():
    body, ready = _readiness_body()
    return JSONResponse(status_code=200 if ready else 503, content=body)

@app.get("/health/live")
async def liveness_check():
    """Never checks Camoufox, so a degraded Tier 3 doesn't trigger a restart."""
    return JSONResponse(status_code=200, content={"status": "ok"})

@app.get("/health/ready")
async def readiness_check():
    """Same check as /health, split out so a load balancer can drain without restarting."""
    body, ready = _readiness_body()
    return JSONResponse(status_code=200 if ready else 503, content=body)

@app.get("/metrics")
async def prometheus_metrics():
    cached_domains, sessions = await asyncio.gather(
        cookie_cache.count_domains_async(),
        session_manager.list_sessions_async(),
    )
    metrics_text = await asyncio.to_thread(
        generate_prometheus_metrics,
        cached_domains,
        len(sessions),
    )
    return Response(content=metrics_text, media_type="text/plain; version=0.0.4; charset=utf-8")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=settings.HOST, port=settings.PORT, reload=False)
