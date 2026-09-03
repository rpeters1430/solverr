import logging
from typing import List, Optional, Tuple

from playwright.async_api import Page

from app.solver.captcha_solver import captcha_solver

logger = logging.getLogger("solverr.browser")

# Widgets the paid captcha-solver escalation knows how to handle: which
# challenge-type key maps to which CaptchaSolverClient method, which DOM
# selectors carry the sitekey, and which response field(s) the solved
# token needs to be written into.
CAPTCHA_SOLVER_WIDGETS = {
    "recaptcha": {
        "solve_method": "solve_recaptcha_v2",
        "selectors": [".g-recaptcha", "div[data-sitekey][class*='recaptcha']"],
        "response_fields": ["g-recaptcha-response"],
    },
    "hcaptcha": {
        "solve_method": "solve_hcaptcha",
        "selectors": [".h-captcha", "div[data-sitekey][class*='hcaptcha']"],
        "response_fields": ["h-captcha-response", "g-recaptcha-response"],
    },
    "cloudflare_turnstile": {
        "solve_method": "solve_turnstile",
        "selectors": [".cf-turnstile", "div[data-sitekey][class*='turnstile']"],
        "response_fields": ["cf-turnstile-response"],
    },
}


async def extract_sitekey(page: Page, selectors: List[str]) -> Optional[Tuple[str, Optional[str]]]:
    """Find the first matching widget's data-sitekey (and data-callback, if
    the site defines one - the stable, documented way to hand a solved
    token back to the widget's own JS instead of poking internal client
    state). Returns None if no widget with a sitekey is found."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                sitekey = await loc.get_attribute("data-sitekey")
                if sitekey:
                    callback = await loc.get_attribute("data-callback")
                    return sitekey, callback
        except Exception as e:
            logger.debug(f"[CaptchaSolver] Sitekey lookup notice for '{sel}': {e}")
    return None


async def inject_captcha_token(page: Page, response_field_names: List[str], token: str, callback_name: Optional[str]):
    """Write a solved token into the widget's response field(s) and invoke
    its data-callback if one is declared, mirroring what the widget's own
    JS does when a human solves it interactively."""
    try:
        await page.evaluate(
            """(args) => {
                for (const name of args.names) {
                    const el = document.querySelector(`[name="${name}"]`) || document.getElementById(name);
                    if (el) {
                        el.innerHTML = args.token;
                        try { el.value = args.token; } catch (e) {}
                        el.style.display = 'block';
                    }
                }
                if (args.callback && typeof window[args.callback] === 'function') {
                    window[args.callback](args.token);
                }
            }""",
            {"names": response_field_names, "token": token, "callback": callback_name}
        )
    except Exception as e:
        logger.debug(f"[CaptchaSolver] Token injection notice: {e}")


async def try_captcha_solver_escalation(page: Page, url: str, challenge_type: str) -> bool:
    """Tier 3.5: hand an unsolved widget's sitekey to the paid captcha-solver
    service and inject the returned token back into the page. No-ops when
    the challenge type has no known widget or `captcha_solver` is disabled
    (see BrowserPool._execute_solve_flow, the only caller)."""
    widget = CAPTCHA_SOLVER_WIDGETS.get(challenge_type)
    if not widget:
        return False

    sitekey_info = await extract_sitekey(page, widget["selectors"])
    if not sitekey_info:
        logger.info(f"[CaptchaSolver] No sitekey found for '{challenge_type}' widget - skipping paid solver escalation.")
        return False

    sitekey, callback_name = sitekey_info
    logger.info(f"[CaptchaSolver] Escalating '{challenge_type}' (sitekey='{sitekey[:16]}...') to paid solver service...")

    solve_fn = getattr(captcha_solver, widget["solve_method"])
    token = await solve_fn(sitekey, url)
    if not token:
        logger.warning(f"[CaptchaSolver] Paid solver did not return a token for '{challenge_type}'.")
        return False

    await inject_captcha_token(page, widget["response_fields"], token, callback_name)
    logger.info(f"[CaptchaSolver] Token injected for '{challenge_type}'.")
    return True
