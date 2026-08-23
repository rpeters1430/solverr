import os
import psutil
import logging
from typing import Optional
from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse
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

    from app.solver.browser import CAMOUFOX_AVAILABLE, browser_pool
    stealth_engine_name = "Camoufox Stealth Firefox" if CAMOUFOX_AVAILABLE else "Unavailable"

    cache_lookups = metrics.cookie_cache_lookup_hits + metrics.cookie_cache_lookup_misses
    cache_hit_ratio_pct = round(metrics.cookie_cache_lookup_hits / cache_lookups * 100, 1) if cache_lookups else 0.0

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
        "version": settings.DISPLAY_VERSION,
        "stealth_engine": stealth_engine_name,
        "tls_impersonation": fast_tls_engine.impersonate_target,
        "cache_hit_ratio_pct": cache_hit_ratio_pct,
        "browser_pool": browser_pool.pool_stats(),
    })
    return data

@router.get("/cookies")
async def get_cookies():
    return {"domains": cookie_cache.get_all_entries()}

@router.get("/cookies/export")
async def export_cookies(format: str = "netscape", domain: Optional[str] = None):
    """Export cached cookies in Netscape format or JSON."""
    if format.lower() in ["netscape", "txt"]:
        content = cookie_cache.export_netscape(domain_filter=domain)
        filename = f"cookies_{domain or 'all'}.txt"
        return PlainTextResponse(
            content,
            media_type="text/plain",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )
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

@router.get("/diagnostics/browser")
async def diagnostics_browser():
    """Browser self-test: launches a real Camoufox instance end-to-end
    (context, page, JS execution) instead of just checking the import
    succeeded. Covered by the same global X-Api-Key auth as every other
    /api route (see app/main.py's middleware) - there's nothing
    endpoint-specific to add here."""
    from app.solver.browser import browser_pool
    result = await browser_pool.self_test()
    status_code = 200 if result.get("ok") else 503
    return JSONResponse(status_code=status_code, content=result)

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

@router.get("/events")
async def sse_event_stream():
    """Server-Sent Events stream for real-time dashboard updates."""
    import asyncio
    from fastapi.responses import StreamingResponse
    import json
    import time
    from app.events import event_broadcaster

    async def event_generator():
        q = event_broadcaster.subscribe()
        try:
            init_payload = json.dumps({"type": "connected", "timestamp": round(time.time(), 3), "data": {"status": "ok"}})
            yield f"data: {init_payload}\n\n"
            while True:
                data = await q.get()
                yield f"data: {data}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            event_broadcaster.unsubscribe(q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )

