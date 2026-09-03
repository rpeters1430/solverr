import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from app.models.flaresolverr import CookieModel

logger = logging.getLogger("solverr.browser")


def build_playwright_cookies(url: str, cookies: Optional[List[CookieModel]]) -> List[Dict[str, str]]:
    """Convert incoming CookieModels into the dict shape Playwright's
    context.add_cookies() expects, defaulting an unset cookie domain to the
    target URL's host."""
    if not cookies:
        return []
    parsed_url = urlparse(url)
    default_domain = parsed_url.netloc.split(":")[0].lstrip(".")
    pw_cookies = []
    for c in cookies:
        domain_val = c.domain.lstrip(".") if c.domain else default_domain
        pw_cookies.append({
            "name": c.name,
            "value": c.value,
            "domain": domain_val,
            "path": c.path or "/"
        })
    return pw_cookies


async def read_context_cookies(context: Any) -> List[Dict[str, Any]]:
    """Read raw cookies back off a Playwright context/browser, tolerating
    either a real BrowserContext (`.cookies()`) or a bare Browser object
    that only exposes `.contexts` (the ephemeral-Camoufox path)."""
    try:
        if hasattr(context, "cookies"):
            return await context.cookies()
        elif hasattr(context, "contexts") and context.contexts:
            return await context.contexts[0].cookies()
    except Exception as c_err:
        logger.debug(f"[BrowserPool] Error reading cookies: {c_err}")
    return []


def extract_captured_cookies(raw_cookies: List[Dict[str, Any]]) -> List[CookieModel]:
    """Normalize Playwright's raw cookie dicts into CookieModel, tolerating
    missing/malformed expires and size fields from different browser
    versions."""
    captured_cookies: List[CookieModel] = []
    for rc in raw_cookies:
        c_name = str(rc.get("name", "") or "")
        c_val = str(rc.get("value", "") or "")
        c_expires = rc.get("expires", -1)
        if c_expires is None or c_expires < 0:
            c_expires = -1
        else:
            try:
                c_expires = float(c_expires)
            except Exception:
                c_expires = -1

        c_size = rc.get("size")
        if c_size is None or not isinstance(c_size, int):
            c_size = len(c_name) + len(c_val)

        captured_cookies.append(
            CookieModel(
                name=c_name,
                value=c_val,
                domain=rc.get("domain") or "",
                path=rc.get("path") or "/",
                expires=c_expires,
                size=c_size,
                httpOnly=bool(rc.get("httpOnly", False)),
                secure=bool(rc.get("secure", False)),
                session=bool(rc.get("session", False)),
                sameSite=str(rc.get("sameSite", "Lax") or "Lax")
            )
        )
    return captured_cookies
