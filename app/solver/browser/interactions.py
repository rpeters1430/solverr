import asyncio
import logging
import random
import time
from typing import Optional, Tuple

from playwright.async_api import Page

from app.solver.human_cursor import human_click, human_mouse_move
from app.solver.browser.challenges import is_challenge_title

logger = logging.getLogger("solverr.browser")

# Akamai's behavioral interstitial: a button that must be held down while its sensor samples the pointer.
AKAMAI_HOLD_SELECTOR = "#progress-button, .behavioral-button, #sec-if-cpt-container [role='button']"
AKAMAI_HOLD_SECONDS = 5.5

# Top-level widget selectors, tried in order; the first visible match larger than 15x15 is clicked.
WIDGET_LOCATORS = [
    ["iframe[src*='challenges.cloudflare.com']", "Turnstile"],
    ["iframe[src*='turnstile']", "Turnstile"],
    ["iframe[src*='cloudflare']", "Turnstile"],
    ["div.cf-turnstile iframe", "Turnstile"],
    ["#turnstile-wrapper iframe", "Turnstile"],
    ["#challenge-stage iframe", "Turnstile"],
    ["div[data-sitekey] iframe", "Turnstile"],
    ["iframe[title*='Cloudflare']", "Turnstile"],
    ["iframe[title*='Turnstile']", "Turnstile"],
    ["#turnstile-wrapper", "Turnstile"],
    ["#challenge-stage", "Turnstile"],
    [".cf-turnstile", "Turnstile"],
    ["iframe[src*='recaptcha/api2/anchor'], iframe[src*='google.com/recaptcha']", "reCAPTCHA"],
    ["iframe[src*='hcaptcha.com']", "hCaptcha"],
]

_FIND_WIDGET_JS = """(locators) => {
    for (const [selector, kind] of locators) {
        let el = null;
        try { el = document.querySelector(selector); } catch (e) { continue; }
        if (!el) continue;
        const r = el.getBoundingClientRect();
        if (r.width > 15 && r.height > 15) {
            return { selector, kind, x: r.left, y: r.top, width: r.width, height: r.height };
        }
    }
    return null;
}"""


async def wander_mouse(page: Page, points: int = 3) -> None:
    """Drift the pointer between a few random spots; Akamai's sensor scores pointer telemetry
    and an idle cursor reads as a bot."""
    try:
        viewport = page.viewport_size or {}
        width = float(viewport.get("width") or 1280)
        height = float(viewport.get("height") or 720)
        for _ in range(points):
            await human_mouse_move(page, random.uniform(0.1, 0.9) * width, random.uniform(0.1, 0.9) * height)
            await asyncio.sleep(random.uniform(0.06, 0.2))
    except Exception as e:
        # Navigation can invalidate the input target mid-move.
        logger.debug(f"[Akamai] Pointer wander notice: {e}")


async def akamai_press_and_hold(page: Page) -> bool:
    """Hold Akamai's press-and-hold button down for its full sampling window. False if there is none."""
    mouse_down = False
    try:
        button = page.locator(AKAMAI_HOLD_SELECTOR).first
        if await button.count() == 0:
            return False
        box = await button.bounding_box()
        if not box or box["width"] < 4 or box["height"] < 4:
            return False
        center_x = box["x"] + box["width"] / 2.0
        center_y = box["y"] + box["height"] / 2.0
        await human_mouse_move(page, center_x, center_y)
        await page.mouse.down()
        mouse_down = True
        hold_until = time.monotonic() + AKAMAI_HOLD_SECONDS
        while time.monotonic() < hold_until:
            # A real held finger is never perfectly still.
            await page.mouse.move(center_x + random.uniform(-1.5, 1.5), center_y + random.uniform(-1.5, 1.5))
            await asyncio.sleep(0.22)
        logger.info(f"[Akamai] Held the behavioral button for {AKAMAI_HOLD_SECONDS:.1f}s.")
        return True
    except Exception as e:
        logger.debug(f"[Akamai] Press-and-hold notice: {e}")
        return False
    finally:
        if mouse_down:
            try:
                await page.mouse.up()
            except Exception:
                pass


async def dispatch_challenge_click(
    page: Page,
    active_challenge: Optional[str],
    title: str,
    age_gate_clicked: bool
) -> Tuple[bool, bool]:
    """Human-click the first challenge widget found, trying each known location in order.

    Returns (clicked, age_gate_clicked); age_gate_clicked latches so the gate isn't re-clicked."""
    clicked = False

    # Turnstile checkbox inside its own frame.
    if not clicked:
        for frame in page.frames:
            if any(x in frame.url.lower() for x in ["challenges.cloudflare.com", "turnstile", "cf-challenge"]):
                try:
                    cb_loc = frame.locator("input[type='checkbox'], .ctp-checkbox-label, body").first
                    box = await cb_loc.bounding_box()
                    if box and box['width'] > 15 and box['height'] > 15:
                        click_x = box['x'] + (28.0 if box['width'] > 60 else box['width'] / 2.0)
                        click_y = box['y'] + (box['height'] / 2.0)
                        logger.info(f"[Turnstile] Cloudflare frame target detected at ({click_x:.0f}, {click_y:.0f}). Dispatching human click...")
                        await human_click(page, click_x, click_y)
                        clicked = True
                        break
                except Exception as f_err:
                    logger.debug(f"[Turnstile] Frame click notice: {f_err}")

    # Turnstile, reCAPTCHA and hCaptcha widgets located from the top-level page, in one round trip.
    if not clicked:
        try:
            widget = await page.evaluate(_FIND_WIDGET_JS, WIDGET_LOCATORS)
            if widget:
                bw = widget["width"]
                offset_x = 28.0 if (widget["kind"] != "Turnstile" or bw > 60) else bw / 2.0
                click_x = widget["x"] + offset_x
                click_y = widget["y"] + (widget["height"] / 2.0)
                logger.info(f"[{widget['kind']}] Detected widget '{widget['selector']}' at ({click_x:.0f}, {click_y:.0f}). Dispatching human click...")
                await human_click(page, click_x, click_y)
                clicked = True
        except Exception as w_err:
            logger.debug(f"[WidgetClick] Locator notice: {w_err}")

    # Fallback: walk shadow roots for widgets the locators above can't reach.
    if not clicked and (active_challenge in ["cloudflare_turnstile", "recaptcha", "hcaptcha"] or is_challenge_title(title)):
        try:
            shadow_box = await page.evaluate("""() => {
                function findInRoot(root) {
                    if (!root) return null;
                    const candidates = root.querySelectorAll("iframe, input[type='checkbox'], div[class*='turnstile'], div[class*='captcha'], div[id*='turnstile'], div[id*='challenge']");
                    for (const el of candidates) {
                        const rect = el.getBoundingClientRect();
                        if (rect && rect.width > 15 && rect.height > 15 && rect.top >= 0 && rect.left >= 0) {
                            const src = (el.src || "").toLowerCase();
                            const cls = (el.className || "").toString().toLowerCase();
                            const id = (el.id || "").toLowerCase();
                            if (src.includes("turnstile") || src.includes("challenges.cloudflare") || src.includes("captcha") ||
                                cls.includes("turnstile") || cls.includes("captcha") || id.includes("turnstile") || id.includes("challenge")) {
                                return { x: rect.left, y: rect.top, width: rect.width, height: rect.height };
                            }
                        }
                    }
                    const all = root.querySelectorAll('*');
                    for (const el of all) {
                        if (el.shadowRoot) {
                            const found = findInRoot(el.shadowRoot);
                            if (found) return found;
                        }
                    }
                    return null;
                }
                return findInRoot(document);
            }""")
            if shadow_box and shadow_box.get('width', 0) > 15:
                bx = shadow_box['x']
                by = shadow_box['y']
                bw = shadow_box['width']
                bh = shadow_box['height']
                click_x = bx + (28.0 if bw > 60 else bw / 2.0)
                click_y = by + (bh / 2.0)
                logger.info(f"[ShadowDOM] Detected widget inside shadow root at ({click_x:.0f}, {click_y:.0f}). Dispatching human click...")
                await human_click(page, click_x, click_y)
                clicked = True
        except Exception as s_err:
            logger.debug(f"[ShadowDOM] Walker notice: {s_err}")

    # Age-gate / disclaimer modal.
    if not clicked and not age_gate_clicked:
        try:
            age_selectors = [
                "button#enter_site", "#close_enter_site_button", ".btn-agree", "#btn-agree",
                "a.btn-agree", "button[data-action='agree']", "button[data-action='enter']",
                "#age-verification button", ".age-verification button", ".ageGate button",
                "#ageGate button", "div.disclaimer-dialog button", ".age_verify button",
                "button:has-text('I AGREE')", "button:has-text('I AM 18')"
            ]
            for ag_sel in age_selectors:
                ag_loc = page.locator(ag_sel).first
                if await ag_loc.count() > 0:
                    box = await ag_loc.bounding_box()
                    if box and box['width'] > 10 and box['height'] > 10 and box['y'] < 1200:
                        click_x = box['x'] + (box['width'] / 2.0)
                        click_y = box['y'] + (box['height'] / 2.0)
                        logger.info(f"[AgeGate] Detected modal button '{ag_sel}' at ({click_x:.0f}, {click_y:.0f}). Dispatching human click...")
                        await human_click(page, click_x, click_y)
                        clicked = True
                        age_gate_clicked = True
                        break
        except Exception as age_err:
            logger.debug(f"[AgeGate] Notice: {age_err}")

    return clicked, age_gate_clicked


async def describe_challenge_frames(page: Page) -> str:
    """One-line summary of the page's frames and iframes, for diagnosing a widget no selector matched."""
    try:
        frames = [f.url[:100] for f in page.frames[1:6]]
    except Exception:
        frames = []
    try:
        iframes = await page.evaluate("""() => {
            const out = [];
            const walk = (root) => {
                for (const el of root.querySelectorAll('iframe')) {
                    const r = el.getBoundingClientRect();
                    out.push(`${(el.src || '').slice(0, 80) || '(no src)'} ${Math.round(r.width)}x${Math.round(r.height)}`);
                }
                for (const el of root.querySelectorAll('*')) if (el.shadowRoot) walk(el.shadowRoot);
            };
            walk(document);
            return out.slice(0, 5);
        }""")
    except Exception:
        iframes = []
    return f"Child frames: {frames or 'none'} | iframes in DOM: {iframes or 'none'}"


# Cap's token lands in the widget's tokenValue and a hidden input (data-cap-hidden-field-name,
# default "cap-token"); only widgets that render are counted, since a hidden one is never redeemed.
_CAP_JS_HELPERS = """
    const capVisible = (el) => {
        const box = el.getBoundingClientRect();
        return box.width > 0 && box.height > 0 && getComputedStyle(el).visibility !== 'hidden';
    };
    const capToken = (el) => {
        const name = el.getAttribute('data-cap-hidden-field-name') || 'cap-token';
        const field = [...el.querySelectorAll("input[type='hidden']")].find((i) => i.name === name)
            || [...document.querySelectorAll("input[type='hidden']")].find((i) => i.name === name && el.contains(i));
        const token = el.tokenValue || (field && field.value);
        return typeof token === 'string' && token.length > 0;
    };
"""


async def cap_widgets_solved(page: Page) -> bool:
    """True once every visible Cap widget holds a token (False when there is none)."""
    try:
        return bool(await page.evaluate("() => {" + _CAP_JS_HELPERS + """
            const widgets = [...document.querySelectorAll('cap-widget')].filter(capVisible);
            return widgets.length > 0 && widgets.every(capToken);
        }"""))
    except Exception:
        return False


async def start_cap_solve(page: Page) -> bool:
    """Start the proof-of-work on the first visible Cap widget still without a token.

    The widget does the work itself; the surrounding form is never submitted. Widgets are solved
    one at a time so several PoW workers don't run at once. Returns whether one was started."""
    try:
        started = await page.evaluate("() => {" + _CAP_JS_HELPERS + """
            const widget = [...document.querySelectorAll('cap-widget')].filter(capVisible).find((el) => !capToken(el));
            if (!widget) return false;
            if (typeof widget.solve === 'function') {
                widget.solve().catch(() => {});
                return {started: true};
            }
            // Firefox can hide component methods from the evaluation world; click its trigger instead.
            const trigger = widget.shadowRoot && widget.shadowRoot.querySelector('.captcha-trigger, [part=trigger]');
            if (!trigger || trigger.hasAttribute('disabled')) return false;
            trigger.scrollIntoView({block: 'center', inline: 'nearest'});
            const box = trigger.getBoundingClientRect();
            return box.width > 0 && box.height > 0 ? {x: box.x + box.width / 2, y: box.y + box.height / 2} : false;
        }""")
    except Exception as e:
        logger.debug(f"[Cap] Solve start notice: {e}")
        return False
    if not started:
        return False
    if "x" in started and "y" in started:
        await human_click(page, started["x"], started["y"])
    logger.info("[Cap] Started the proof-of-work on a Cap widget.")
    return True
