import logging
from typing import Optional, Tuple

from playwright.async_api import Page

from app.solver.human_cursor import human_click
from app.solver.browser.challenges import is_challenge_title

logger = logging.getLogger("solverr.browser")


async def dispatch_challenge_click(
    page: Page,
    active_challenge: Optional[str],
    title: str,
    age_gate_clicked: bool
) -> Tuple[bool, bool]:
    """Periodic interactive challenge solver dispatcher: tries each known
    checkbox-style widget location in turn (Turnstile, reCAPTCHA, hCaptcha,
    a generic shadow-DOM walk, then an age-gate dismissal) and dispatches a
    human-like click on the first one found. Returns (clicked,
    age_gate_clicked) - age_gate_clicked latches once an age gate has been
    dismissed so it isn't re-clicked every iteration."""
    clicked = False

    # 1. Cloudflare Turnstile Frame-Level Checkbox Clicker
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

    # 2. Cloudflare Turnstile Element / Top-Level Locator Checkbox Clicker
    if not clicked:
        turnstile_locators = [
            "iframe[src*='challenges.cloudflare.com']",
            "iframe[src*='turnstile']",
            "iframe[src*='cloudflare']",
            "div.cf-turnstile iframe",
            "#turnstile-wrapper iframe",
            "#challenge-stage iframe",
            "div[data-sitekey] iframe",
            "iframe[title*='Cloudflare']",
            "iframe[title*='Turnstile']",
            "#turnstile-wrapper",
            "#challenge-stage",
            ".cf-turnstile"
        ]
        for t_sel in turnstile_locators:
            try:
                loc = page.locator(t_sel).first
                if await loc.count() > 0:
                    box = await loc.bounding_box()
                    if box and box['width'] > 15 and box['height'] > 15:
                        click_x = box['x'] + (28.0 if box['width'] > 60 else box['width'] / 2.0)
                        click_y = box['y'] + (box['height'] / 2.0)
                        logger.info(f"[Turnstile] Detected widget '{t_sel}' at ({click_x:.0f}, {click_y:.0f}). Dispatching human click...")
                        await human_click(page, click_x, click_y)
                        clicked = True
                        break
            except Exception as t_err:
                logger.debug(f"[Turnstile] Locator notice for '{t_sel}': {t_err}")

    # 3. reCAPTCHA v2 / Enterprise Checkbox Locator
    if not clicked:
        try:
            recap_loc = page.locator("iframe[src*='recaptcha/api2/anchor'], iframe[src*='google.com/recaptcha']").first
            if await recap_loc.count() > 0:
                box = await recap_loc.bounding_box()
                if box and box['width'] > 15 and box['height'] > 15:
                    click_x = box['x'] + 28.0
                    click_y = box['y'] + (box['height'] / 2.0)
                    logger.info(f"[reCAPTCHA] Detected anchor at ({click_x:.0f}, {click_y:.0f}). Dispatching human click...")
                    await human_click(page, click_x, click_y)
                    clicked = True
        except Exception as recap_err:
            logger.debug(f"[reCAPTCHA] Notice: {recap_err}")

    # 4. hCaptcha Checkbox Locator
    if not clicked:
        try:
            hcap_loc = page.locator("iframe[src*='hcaptcha.com']").first
            if await hcap_loc.count() > 0:
                box = await hcap_loc.bounding_box()
                if box and box['width'] > 15 and box['height'] > 15:
                    click_x = box['x'] + 28.0
                    click_y = box['y'] + (box['height'] / 2.0)
                    logger.info(f"[hCaptcha] Detected frame at ({click_x:.0f}, {click_y:.0f}). Dispatching human click...")
                    await human_click(page, click_x, click_y)
                    clicked = True
        except Exception as hcap_err:
            logger.debug(f"[hCaptcha] Notice: {hcap_err}")

    # 4.5. Deep Shadow DOM & Web Components Walker Fallback
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

    # 5. Modal Disclaimer / Age Gate Dismissal (Chaturbate, SpankBang, etc.)
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
