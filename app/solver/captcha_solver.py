import asyncio
import logging
import time
from typing import Any, Dict, Optional
from urllib.parse import unquote, urlparse
import httpx
from app.config import settings

logger = logging.getLogger("solverr.captcha_solver")

def proxy_task_fields(proxy_url: Optional[str]) -> Optional[Dict[str, Any]]:
    """API v2 proxy fields (proxyType/Address/Port/Login/Password) for a proxy URL, or None."""
    if not proxy_url:
        return None
    try:
        parsed = urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
        port = parsed.port
    except ValueError:
        return None
    scheme = (parsed.scheme or "http").lower()
    proxy_type = {"http": "http", "https": "http", "socks4": "socks4", "socks5": "socks5", "socks5h": "socks5"}.get(scheme)
    if not proxy_type or not parsed.hostname or not port:
        return None
    fields: Dict[str, Any] = {"proxyType": proxy_type, "proxyAddress": parsed.hostname, "proxyPort": port}
    if parsed.username:
        fields["proxyLogin"] = unquote(parsed.username)
    if parsed.password:
        fields["proxyPassword"] = unquote(parsed.password)
    return fields


class CaptchaSolverClient:
    """Paid captcha-solver client for image challenges the click loop can't clear.

    Speaks the 2Captcha protocol (POST /in.php, poll /res.php), which CapSolver and others also accept,
    and 2Captcha's API v2 (createTask/getTaskResult) for task types the legacy protocol lacks.
    solve_* returns None when no API key is set.
    """

    def __init__(self):
        self.api_key = settings.CAPTCHA_SOLVER_API_KEY
        self.base_url = settings.CAPTCHA_SOLVER_BASE_URL.rstrip("/")
        self.api_v2_url = settings.CAPTCHA_SOLVER_API_V2_URL.rstrip("/")
        self.poll_interval = settings.CAPTCHA_SOLVER_POLL_INTERVAL
        self.timeout = settings.CAPTCHA_SOLVER_TIMEOUT

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def solve_recaptcha_v2(
        self,
        sitekey: str,
        page_url: str,
        *,
        enterprise: bool = False,
        invisible: bool = False,
        data_s: Optional[str] = None,
    ) -> Optional[str]:
        params = {"method": "userrecaptcha", "googlekey": sitekey, "pageurl": page_url}
        if enterprise:
            params["enterprise"] = 1
        if invisible:
            params["invisible"] = 1
        if data_s:
            params["data-s"] = data_s
        return await self._submit_and_poll(params)

    async def solve_hcaptcha(self, sitekey: str, page_url: str) -> Optional[str]:
        return await self._submit_and_poll({
            "method": "hcaptcha",
            "sitekey": sitekey,
            "pageurl": page_url,
        })

    async def solve_turnstile(
        self, sitekey: str, page_url: str, *, action: Optional[str] = None, cdata: Optional[str] = None
    ) -> Optional[str]:
        params = {"method": "turnstile", "sitekey": sitekey, "pageurl": page_url}
        # A widget rendered with an action or cData only accepts tokens minted for the same values.
        if action:
            params["action"] = action
        if cdata:
            params["data"] = cdata
        return await self._submit_and_poll(params)

    async def solve_datadome_slider(
        self, captcha_url: str, page_url: str, user_agent: str, proxy_url: str, timeout: Optional[float] = None
    ) -> Optional[str]:
        """DataDome's slider. Returns the `datadome` Set-Cookie string, or None.

        DataDome binds its cookie to the IP that solved the slider, so the service must work through
        the same proxy the browser uses; without one there is nothing to submit."""
        proxy = proxy_task_fields(proxy_url)
        if not proxy:
            return None
        solution = await self._create_task_and_poll({
            "type": "DataDomeSliderTask",
            "websiteURL": page_url,
            "captchaUrl": captcha_url,
            "userAgent": user_agent,
            **proxy,
        }, timeout=timeout)
        cookie = (solution or {}).get("cookie")
        return cookie if isinstance(cookie, str) and cookie else None

    async def _create_task_and_poll(self, task: Dict[str, Any], timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None
        deadline = time.monotonic() + min(self.timeout, timeout if timeout is not None else self.timeout)
        try:
            async with httpx.AsyncClient(base_url=self.api_v2_url, timeout=15.0) as client:
                resp = await client.post("/createTask", json={"clientKey": self.api_key, "task": task})
                data = resp.json()
                if data.get("errorId"):
                    logger.warning(f"[CaptchaSolver] {task.get('type')} rejected: {data.get('errorCode')}")
                    return None
                task_id = data.get("taskId")
                logger.info(f"[CaptchaSolver] Submitted {task.get('type')} task '{task_id}', polling for solution...")
                while time.monotonic() < deadline:
                    await asyncio.sleep(self.poll_interval)
                    poll = (await client.post("/getTaskResult", json={"clientKey": self.api_key, "taskId": task_id})).json()
                    if poll.get("errorId"):
                        logger.warning(f"[CaptchaSolver] Task '{task_id}' failed: {poll.get('errorCode')}")
                        return None
                    if poll.get("status") == "ready":
                        logger.info(f"[CaptchaSolver] Task '{task_id}' solved")
                        return poll.get("solution") or {}
                logger.warning(f"[CaptchaSolver] Task '{task_id}' timed out")
        except Exception as e:
            logger.warning(f"[CaptchaSolver] Request error: {type(e).__name__}")
        return None

    async def _submit_and_poll(self, params: dict) -> Optional[str]:
        if not self.enabled:
            return None
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=15.0) as client:
                submit_params = {**params, "key": self.api_key, "json": 1}
                resp = await client.post("/in.php", data=submit_params)
                data = resp.json()
                if data.get("status") != 1:
                    logger.warning(f"[CaptchaSolver] Submit rejected: {data.get('request')}")
                    return None
                task_id = data["request"]
                logger.info(f"[CaptchaSolver] Submitted {params.get('method')} task '{task_id}', polling for solution...")
                return await self._poll(client, task_id)
        except Exception as e:
            logger.warning(f"[CaptchaSolver] Request error: {e}")
            return None

    async def _poll(self, client: httpx.AsyncClient, task_id: str) -> Optional[str]:
        elapsed = 0.0
        while elapsed < self.timeout:
            await asyncio.sleep(self.poll_interval)
            elapsed += self.poll_interval
            poll_resp = await client.get("/res.php", params={
                "key": self.api_key, "action": "get", "id": task_id, "json": 1
            })
            poll_data = poll_resp.json()
            if poll_data.get("status") == 1:
                logger.info(f"[CaptchaSolver] Task '{task_id}' solved in {elapsed:.0f}s")
                return poll_data["request"]
            if poll_data.get("request") != "CAPCHA_NOT_READY":
                logger.warning(f"[CaptchaSolver] Task '{task_id}' failed: {poll_data.get('request')}")
                return None
        logger.warning(f"[CaptchaSolver] Task '{task_id}' timed out after {self.timeout}s")
        return None

captcha_solver = CaptchaSolverClient()
