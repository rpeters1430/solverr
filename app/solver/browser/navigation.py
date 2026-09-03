import html
import json
import logging
from typing import Any, Optional, Tuple
from urllib.parse import parse_qsl

from playwright.async_api import Page

logger = logging.getLogger("solverr.browser")


async def install_media_blocking(page: Page) -> None:
    """Block only video/audio media to save bandwidth, while preserving
    fonts & challenge canvases (which some WAF challenges render to)."""
    async def block_heavy_media(route, request):
        if request.resource_type in ["media"]:
            await route.abort()
        else:
            await route.continue_()

    try:
        await page.route("**/*", block_heavy_media)
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
    """Navigate the page to `url`, either via a real GET (page.goto) or by
    building and auto-submitting a hidden POST form (Playwright has no
    direct "navigate with POST body" API). Returns (response, initial_status)
    - initial_status is best-effort and gets overridden later by the real
    final main-frame status once a response listener is attached (see
    BrowserPool._execute_solve_flow)."""
    logger.info(f"[BrowserPool] Navigating to {url} ({method.upper()}, timeout: {timeout_ms}ms)")
    initial_status = 0
    response = None
    if method.upper() == "POST" and post_data:
        try:
            form_html = _build_post_form_html(url, post_data)
            try:
                # page.set_content() itself doesn't navigate - the injected
                # <script> auto-submitting the form is what triggers the real
                # navigation, so expect_navigation() has to wrap the
                # set_content() call to observe it and hand back its Response
                # (wait_for_load_state() would not - it always returns None).
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
