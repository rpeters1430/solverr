import os
import psutil
import logging
from fastapi import APIRouter, HTTPException
from app.models.flaresolverr import TestRequestModel, V1Request
from app.solver.engine import metrics, solver_engine
from app.solver.cache import cookie_cache
from app.solver.sessions import session_manager
from app.solver.fast_tls import fast_tls_engine
from app.config import settings

logger = logging.getLogger("solverr.api.dashboard")
router = APIRouter()

@router.get("/stats")
async def get_stats():
    proc = psutil.Process(os.getpid())
    mem_info = proc.memory_info()
    ram_mb = round(mem_info.rss / 1024 / 1024, 1)
    cpu_percent = psutil.cpu_percent(interval=None)

    from app.solver.browser import CAMOUFOX_AVAILABLE
    stealth_engine_name = "Camoufox Stealth Firefox" if (settings.USE_CAMOUFOX and CAMOUFOX_AVAILABLE) else "Playwright Chromium"

    data = metrics.to_dict()
    data.update({
        "ram_usage_mb": ram_mb,
        "cpu_usage_pct": cpu_percent,
        "active_sessions": len(session_manager.list_sessions()),
        "cached_domains_count": len(cookie_cache.get_all_entries()),
        "cache_backend": "Redis (Distributed)" if cookie_cache.redis_client else "Local JSON Disk Cache",
        "session_backend": "Redis (Distributed)" if session_manager.redis_client else "In-Memory (Per-Process)",
        "max_workers": settings.MAX_BROWSER_WORKERS,
        "total_cpu_cores": settings.TOTAL_CPU_CORES,
        "total_ram_gb": settings.TOTAL_RAM_GB,
        "worker_auto_tuned": getattr(settings, "WORKER_AUTO_TUNED", False),
        "js_heap_mb": settings.BROWSER_MAX_OLD_SPACE_SIZE,
        "gpu_enabled": settings.ENABLE_GPU,
        "version": settings.VERSION,
        "stealth_engine": stealth_engine_name,
        "tls_impersonation": fast_tls_engine.impersonate_target
    })
    return data

@router.get("/cookies")
async def get_cookies():
    return {"domains": cookie_cache.get_all_entries()}

@router.post("/cookies/clear")
async def clear_cookies():
    cookie_cache.clear()
    return {"status": "ok", "message": "Cookie cache cleared"}

@router.get("/sessions")
async def list_sessions():
    return {"sessions": session_manager.list_sessions()}

@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    success = session_manager.destroy_session(session_id)
    if not success:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"status": "ok", "message": f"Session '{session_id}' deleted"}

@router.post("/test")
async def test_solver(req: TestRequestModel):
    v1_req = V1Request(
        cmd=f"request.{req.method.lower()}",
        url=req.url,
        postData=req.postData,
        forceBrowser=req.forceBrowser,
        screenshot=req.screenshot
    )
    
    if not req.useCache:
        v1_req.cookies = []

    try:
        sol = await solver_engine.process_request(v1_req)
        return {
            "status": "ok",
            "url": sol.url,
            "http_status": sol.status,
            "cookies_count": len(sol.cookies),
            "cookies": [c.model_dump() for c in sol.cookies],
            "html_snippet": sol.response[:1500] if sol.response else "",
            "headers": sol.headers,
            "screenshot": sol.screenshot
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
