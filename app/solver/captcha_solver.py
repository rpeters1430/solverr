import asyncio
import logging
from typing import Optional
import httpx
from app.config import settings

logger = logging.getLogger("solverr.captcha_solver")

class CaptchaSolverClient:
    """Paid captcha-solver client for image challenges the click loop can't clear.

    Speaks the 2Captcha protocol (POST /in.php, poll /res.php), which CapSolver and others also accept.
    solve_* returns None when no API key is set.
    """

    def __init__(self):
        self.api_key = settings.CAPTCHA_SOLVER_API_KEY
        self.base_url = settings.CAPTCHA_SOLVER_BASE_URL.rstrip("/")
        self.poll_interval = settings.CAPTCHA_SOLVER_POLL_INTERVAL
        self.timeout = settings.CAPTCHA_SOLVER_TIMEOUT

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def solve_recaptcha_v2(self, sitekey: str, page_url: str) -> Optional[str]:
        return await self._submit_and_poll({
            "method": "userrecaptcha",
            "googlekey": sitekey,
            "pageurl": page_url,
        })

    async def solve_hcaptcha(self, sitekey: str, page_url: str) -> Optional[str]:
        return await self._submit_and_poll({
            "method": "hcaptcha",
            "sitekey": sitekey,
            "pageurl": page_url,
        })

    async def solve_turnstile(self, sitekey: str, page_url: str) -> Optional[str]:
        return await self._submit_and_poll({
            "method": "turnstile",
            "sitekey": sitekey,
            "pageurl": page_url,
        })

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
