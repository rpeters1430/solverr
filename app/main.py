import os
import time
import uuid
import asyncio
import logging
import traceback
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse

from app.config import settings
from app.api.flaresolverr import router as flaresolverr_router
from app.api.dashboard import router as dashboard_router
from app.solver.browser import browser_pool, CAMOUFOX_AVAILABLE
from app.metrics import generate_prometheus_metrics
from app.logging_config import setup_logging, set_request_id, sanitize_proxy_url
from app.solver.sessions import session_manager

# Configure Logging
setup_logging(settings.LOG_LEVEL)
logger = logging.getLogger("solverr.main")

async def periodic_session_cleanup():
    while True:
        try:
            await asyncio.sleep(600)
            pruned = session_manager.prune_expired_sessions()
            if pruned > 0:
                logger.info(f"[Lifespan] Periodic session cleanup pruned {pruned} expired session(s)")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"Error in session cleanup task: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Initializing Solverr Engine v{settings.VERSION}...")
    logger.info(f"Configuration | Host: {settings.HOST}:{settings.PORT} | Log Level: {settings.LOG_LEVEL.upper()} | Workers: {settings.MAX_BROWSER_WORKERS} | Fast TLS: {settings.ENABLE_FAST_TLS}")
    cleanup_task = asyncio.create_task(periodic_session_cleanup())
    if CAMOUFOX_AVAILABLE:
        # The warm Camoufox pool launches its instances lazily on first
        # solve (see CamoufoxPool.acquire), instead of paying that cost on
        # every process start.
        logger.info("Camoufox stealth engine ready; the warm browser pool launches lazily on first solve.")
    else:
        logger.error("Camoufox stealth engine is not available (import failed) - Tier 3 browser-based solving will fail for every request until this is fixed.")
    yield
    logger.info("Shutting down Solverr Engine...")
    cleanup_task.cancel()
    await browser_pool.close()

app = FastAPI(
    title="Solverr",
    description="Ultra-fast FlareSolverr & Trawl replacement for Cloudflare, CAPTCHA bypass & HTTP proxy",
    version=settings.VERSION,
    lifespan=lifespan
)

# HTTP Request Logging, Optional API-Key Auth & Exception Middleware
_UNAUTHENTICATED_PATHS = {"/health", "/metrics"}

@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    req_id = uuid.uuid4().hex[:8]
    set_request_id(req_id)
    start_time = time.time()

    is_health_or_metrics = request.url.path in ["/health", "/metrics"]
    if not is_health_or_metrics or logger.isEnabledFor(logging.DEBUG):
        client_ip = request.client.host if request.client else "unknown"
        logger.info(f"Incoming HTTP {request.method} {request.url.path} from {client_ip}")

    if (settings.API_KEY and request.url.path not in _UNAUTHENTICATED_PATHS
            and not request.url.path.startswith("/static")
            and request.url.path != "/favicon.ico"):
        supplied = (
            request.headers.get("x-api-key")
            or request.query_params.get("api_key")
            or request.query_params.get("key")
        )
        if not supplied:
            auth_header = request.headers.get("authorization", "")
            if auth_header.lower().startswith("bearer "):
                supplied = auth_header[7:].strip()
        if supplied != settings.API_KEY:
            logger.warning(f"Rejected unauthenticated request to {request.url.path} from {request.client.host if request.client else 'unknown'}")
            return JSONResponse(status_code=401, content={"status": "error", "error": "Missing or invalid X-Api-Key header"})

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
        return JSONResponse(
            status_code=500,
            content={"status": "error", "error": err_msg, "path": str(request.url.path), "detail": str(exc)}
        )

# Setup Static Files Directory
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
static_dir = os.path.join(BASE_DIR, "static")
if not os.path.exists(static_dir):
    static_dir = os.path.join(os.path.dirname(BASE_DIR), "static")

if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

# Mount Routers
app.include_router(flaresolverr_router)
app.include_router(dashboard_router, prefix="/api")

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

@app.get("/health")
async def health_check():
    # Camoufox launches lazily on first solve (see lifespan) and its pool
    # instances are also lazy, so "not yet used" is healthy - the only
    # unhealthy state is the Camoufox import itself having failed, since
    # there's no other engine left to service Tier 3 requests.
    ready = CAMOUFOX_AVAILABLE

    body = {
        "status": "ok" if ready else "degraded",
        "version": settings.VERSION,
        "stealth_engine": "Camoufox Firefox",
        "workers": settings.MAX_BROWSER_WORKERS,
        "camoufox_pool_active_instances": browser_pool.camoufox_pool._created if browser_pool.camoufox_pool else 0,
        "camoufox_available": CAMOUFOX_AVAILABLE
    }
    return JSONResponse(status_code=200 if ready else 503, content=body)

@app.get("/metrics")
async def prometheus_metrics():
    """Exposes Prometheus-formatted metrics for Grafana / VictoriaMetrics."""
    metrics_text = generate_prometheus_metrics()
    return Response(content=metrics_text, media_type="text/plain; version=0.0.4; charset=utf-8")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=settings.HOST, port=settings.PORT, reload=False)
