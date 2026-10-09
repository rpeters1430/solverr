import asyncio
import html
import json
import logging
import time
from typing import Any, Optional, Tuple
from urllib.parse import parse_qsl

from playwright.async_api import BrowserContext, Page

from app.security import SSRFBlockedError, check_target_url_async
from app.solver.meta_refresh import MAX_REFRESH_HOPS, meta_refresh_target

logger = logging.getLogger("solverr.browser")

# How long past a refresh's own delay to wait for Firefox to fire it before loading it by hand.
META_REFRESH_FIRE_GRACE_SECONDS = 2.0


async def install_media_blocking(context: BrowserContext) -> None:
    """Block video/audio and SSRF targets; fonts and canvases stay because some challenges need them."""
    async def block_heavy_media(route, request):
        if request.resource_type in ["media"]:
            await route.abort()
            return
        try:
            await check_target_url_async(request.url, label="Browser request")
        except SSRFBlockedError as exc:
            logger.warning(f"[BrowserPool] Blocked private-network request: {exc}")
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    try:
        await context.route("**/*", block_heavy_media)
    except Exception:
        pass


def _build_post_form_html(url: str, post_data: str) -> str:
    form_inputs = []
    is_json = False
    try:
        json_obj = json.loads(post_data)
        if isinstance(json_obj, dict):
            is_json = True
            for k, v in json_obj.items():
                val_str = json.dumps(v) if isinstance(v, (dict, list)) else str(v)
                escaped_k = html.escape(str(k), quote=True)
                escaped_v = html.escape(val_str, quote=True)
                form_inputs.append(f'<input type="hidden" name="{escaped_k}" value="{escaped_v}">')
    except Exception:
        pass

    if not is_json:
        if "=" in post_data:
            for k, v in parse_qsl(post_data, keep_blank_values=True):
                escaped_k = html.escape(str(k), quote=True)
                escaped_v = html.escape(str(v), quote=True)
                form_inputs.append(f'<input type="hidden" name="{escaped_k}" value="{escaped_v}">')
        else:
            escaped_data = html.escape(post_data, quote=True)
            form_inputs.append(f'<input type="hidden" name="data" value="{escaped_data}">')

    return (
        '<!DOCTYPE html><html><body>'
        f'<form id="_solverr_f" method="POST" action="{html.escape(url, quote=True)}">'
        f"{''.join(form_inputs)}</form>"
        '<script>document.getElementById(\'_solverr_f\').submit();</script>'
        '</body></html>'
    )


async def navigate_to_target(
    page: Page,
    url: str,
    method: str,
    post_data: Optional[str],
    timeout_ms: int
) -> Tuple[Optional[Any], int]:
    """GET via page.goto, or POST via an auto-submitting form since Playwright can't navigate with a body.

    initial_status is best-effort; the caller's response listener supersedes it."""
    logger.info(f"[BrowserPool] Navigating to {url} ({method.upper()}, timeout: {timeout_ms}ms)")
    initial_status = 0
    response = None
    if method.upper() == "POST" and post_data:
        try:
            form_html = _build_post_form_html(url, post_data)
            try:
                # The form's submit script navigates, so wrap set_content to capture that Response.
                async with page.expect_navigation(wait_until="domcontentloaded", timeout=timeout_ms) as nav_info:
                    await page.set_content(form_html)
                response = await nav_info.value
                initial_status = response.status if response else 0
            except Exception:
                initial_status = 0
        except Exception as nav_err:
            logger.warning(f"[BrowserPool] POST form navigation notice: {nav_err}")
            initial_status = 0
    else:
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            initial_status = response.status if response else 0
        except Exception as nav_err:
            logger.warning(f"[BrowserPool] Navigation notice: {nav_err}")
            initial_status = 0

    return response, initial_status


async def follow_meta_refresh(page: Page, deadline: float) -> None:
    """Follow short-delay meta refresh redirects, at most MAX_REFRESH_HOPS, within `deadline`.

    Firefox fires a refresh by itself, so each hop first waits for that; only one that never fires
    is loaded by hand. Navigation stays in this context, so cookies, proxy and the request route
    (with its SSRF check) all still apply."""
    visited = set()
    for hop in range(1, MAX_REFRESH_HOPS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0.5:
            return
        current = page.url
        try:
            html = await asyncio.wait_for(page.content(), timeout=min(2.0, remaining))
        except Exception:
            return
        refresh = meta_refresh_target(html, current)
        if not refresh or refresh[1] in visited:
            return
        visited.add(current)
        delay, target = refresh
        try:
            await check_target_url_async(target, label="Redirect target")
        except SSRFBlockedError as exc:
            logger.warning(f"[BrowserPool] Not following meta refresh: {exc}")
            return
        logger.info(f"[BrowserPool] Following meta refresh (hop {hop}/{MAX_REFRESH_HOPS}) -> {target}")
        wait_s = min(deadline - time.monotonic(), delay + META_REFRESH_FIRE_GRACE_SECONDS)
        try:
            await page.wait_for_url(lambda u: u != current, wait_until="domcontentloaded", timeout=max(1.0, wait_s) * 1000)
        except Exception:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms < 1000:
                return
            try:
                await page.goto(target, wait_until="domcontentloaded", timeout=remaining_ms)
            except Exception as nav_err:
                logger.warning(f"[BrowserPool] Meta refresh navigation notice: {nav_err}")
                return
        if page.url == current:
            return
