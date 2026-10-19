import json
import logging
import uuid
from http.cookies import CookieError, SimpleCookie
from typing import Any, Dict, List, Optional, Tuple

from playwright.async_api import Page

from app.solver.captcha_solver import captcha_solver
from app.solver.browser.clearance import host_of as host_of_url

logger = logging.getLogger("solverr.browser")

# Per widget: the CaptchaSolverClient method, sitekey selectors, and token response fields.
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


_WIDGET_ATTRIBUTES = {
    "sitekey": "data-sitekey",
    "callback": "data-callback",
    "action": "data-action",
    "cdata": "data-cdata",
    "size": "data-size",
    "s": "data-s",
}


async def extract_widget_params(page: Page, selectors: List[str]) -> Optional[Dict[str, Optional[str]]]:
    """The first widget's data-* render parameters (sitekey, callback, action, cdata, size, s), or None."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                sitekey = await loc.get_attribute("data-sitekey")
                if sitekey:
                    params: Dict[str, Optional[str]] = {"sitekey": sitekey}
                    for key, attr in _WIDGET_ATTRIBUTES.items():
                        if key != "sitekey":
                            params[key] = await loc.get_attribute(attr)
                    return params
        except Exception as e:
            logger.debug(f"[CaptchaSolver] Sitekey lookup notice for '{sel}': {e}")
    return None


async def extract_sitekey(page: Page, selectors: List[str]) -> Optional[Tuple[str, Optional[str]]]:
    """Return the first widget's (data-sitekey, data-callback), or None."""
    params = await extract_widget_params(page, selectors)
    return (params["sitekey"], params.get("callback")) if params else None


async def uses_recaptcha_enterprise(page: Page) -> bool:
    """reCAPTCHA Enterprise tokens come from a different endpoint; a standard one is rejected."""
    try:
        return bool(await page.evaluate("""() => [...document.scripts].some((s) => /recaptcha\/enterprise\.js/.test(s.src || ''))"""))
    except Exception:
        return False


async def _solver_options(page: Page, challenge_type: str, params: Dict[str, Optional[str]]) -> Dict[str, Any]:
    if challenge_type == "recaptcha":
        return {
            "enterprise": await uses_recaptcha_enterprise(page),
            "invisible": (params.get("size") or "").lower() == "invisible",
            "data_s": params.get("s"),
        }
    if challenge_type == "cloudflare_turnstile":
        return {"action": params.get("action"), "cdata": params.get("cdata")}
    return {}


async def inject_captcha_token(page: Page, response_field_names: List[str], token: str, callback_name: Optional[str]):
    """Write the token into the response fields and call data-callback, as the widget does for a human."""
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
    """Tier 3.5: solve the widget's sitekey via the paid service and inject the token."""
    widget = CAPTCHA_SOLVER_WIDGETS.get(challenge_type)
    if not widget:
        return False

    params = await extract_widget_params(page, widget["selectors"])
    if not params:
        logger.info(f"[CaptchaSolver] No sitekey found for '{challenge_type}' widget - skipping paid solver escalation.")
        return False

    sitekey, callback_name = params["sitekey"], params.get("callback")
    logger.info(f"[CaptchaSolver] Escalating '{challenge_type}' (sitekey='{sitekey[:16]}...') to paid solver service...")

    solve_fn = getattr(captcha_solver, widget["solve_method"])
    token = await solve_fn(sitekey, url, **await _solver_options(page, challenge_type, params))
    if not token:
        logger.warning(f"[CaptchaSolver] Paid solver did not return a token for '{challenge_type}'.")
        return False

    await inject_captcha_token(page, widget["response_fields"], token, callback_name)
    logger.info(f"[CaptchaSolver] Token injected for '{challenge_type}'.")
    return True



# Cloudflare's challenge page has no data-sitekey element: its orchestrator calls turnstile.render()
# with the sitekey, action, cData and chlPageData, and a callback that submits the challenge form.
# This init script (main world, so it sees the page's own `turnstile`) records the parameters of the
# latest render and keeps its callback, so a paid solver's token can be handed to Cloudflare the way
# its widget would. Only installed when the paid solver is configured.
_CLOUDFLARE_CAPTURE_JS = r"""(key) => {
  if (window !== window.top) return;
  let captured;
  const valid = (v, max) => typeof v === 'string' && v.length > 0 && v.length <= max;
  const read = () => captured && !captured.used && captured.render.websiteURL === location.href
    && document.querySelector('#challenge-form') ? Object.assign({}, captured.render) : null;
  Object.defineProperty(window, key, { value: {
    read,
    deliver(expected, token) {
      if (!captured || JSON.stringify(read()) !== JSON.stringify(expected)) return false;
      captured.used = true;
      captured.callback(token);
      return true;
    },
  } });
  const wrapped = new WeakSet();
  const wrap = (sdk) => {
    if (!sdk || typeof sdk !== 'object' || wrapped.has(sdk)) return;
    wrapped.add(sdk);
    const d = Object.getOwnPropertyDescriptor(sdk, 'render');
    if (d && (!d.configurable || d.get || d.set)) return;
    const intercept = (render) => typeof render !== 'function' ? render : function (container, options) {
      captured = undefined;
      if (options && valid(options.sitekey, 512) && valid(options.action, 512) && valid(options.cData, 4096)
          && valid(options.chlPageData, 65536) && typeof options.callback === 'function') {
        const callback = options.callback;
        const current = { callback, used: false, render: {
          websiteURL: location.href, websiteKey: options.sitekey, action: options.action,
          data: options.cData, pagedata: options.chlPageData, userAgent: navigator.userAgent,
        } };
        captured = current;
        options = Object.assign({}, options, {
          callback: function (...args) { current.used = true; return callback.apply(this, args); },
        });
        for (const name of ['expired-callback', 'error-callback']) {
          const handler = options[name];
          options[name] = function (...args) {
            current.used = true;
            if (typeof handler === 'function') return handler.apply(this, args);
          };
        }
      }
      try { return render.call(this, container, options); } catch (e) { captured = undefined; throw e; }
    };
    let render = intercept(sdk.render);
    Object.defineProperty(sdk, 'render', {
      configurable: true, enumerable: d ? d.enumerable : true,
      get: () => render, set: (v) => { captured = undefined; render = intercept(v); },
    });
    for (const name of ['reset', 'remove']) {
      const method = sdk[name];
      if (typeof method === 'function') {
        sdk[name] = function (...args) { captured = undefined; return method.apply(this, args); };
      }
    }
  };
  const d = Object.getOwnPropertyDescriptor(window, 'turnstile');
  if (d && (!d.configurable || d.get || d.set)) return;
  let sdk = window.turnstile;
  wrap(sdk);
  Object.defineProperty(window, 'turnstile', {
    configurable: true, enumerable: d ? d.enumerable : true,
    get: () => sdk, set(v) { if (sdk !== v) captured = undefined; sdk = v; wrap(v); },
  });
}"""

_RENDER_FIELDS = ("websiteURL", "websiteKey", "action", "data", "pagedata", "userAgent")


async def install_cloudflare_capture(context: Any) -> Optional[str]:
    """Install the challenge-page capture into every page of `context`; returns its window key, or None.

    The key is random per solve so the page can't look for a fixed name. Camoufox's "mw:" prefix
    runs it in the page's main world (needs main_world_eval at launch, see pool.py)."""
    key = f"__{uuid.uuid4().hex}"
    try:
        await context.add_init_script(script=f"mw:({_CLOUDFLARE_CAPTURE_JS})({json.dumps(key)})")
        return key
    except Exception as e:
        logger.debug(f"[CaptchaSolver] Cloudflare challenge capture unavailable: {e}")
        return None


async def read_cloudflare_render(page: Page, key: str) -> Optional[Dict[str, str]]:
    """The live challenge's render parameters, or None if none is waiting for a token."""
    try:
        render = await page.evaluate(f"mw:window[{json.dumps(key)}]?.read() ?? null")
    except Exception:
        return None
    if not isinstance(render, dict) or not all(isinstance(render.get(f), str) and render[f] for f in _RENDER_FIELDS):
        return None
    return {f: render[f] for f in _RENDER_FIELDS}


async def try_cloudflare_challenge_solver(
    page: Page, key: str, render: Dict[str, str], proxy_url: Optional[str], timeout: float
) -> bool:
    """Tier 3.5 for Cloudflare's own challenge page: solve its Turnstile with the captured render
    parameters and hand the token to the page's callback, which submits the challenge."""
    logger.info(f"[CaptchaSolver] Escalating Cloudflare challenge page (sitekey='{render['websiteKey'][:16]}...') to paid solver service...")
    token = await captcha_solver.solve_cloudflare_challenge(render, proxy_url=proxy_url, timeout=timeout)
    if not token:
        logger.warning("[CaptchaSolver] Paid solver did not return a token for the Cloudflare challenge page.")
        return False
    if (page.url or "") != render["websiteURL"]:
        # The challenge was replaced while the task ran (a reload, an expiry); the token is for the old one.
        logger.info("[CaptchaSolver] Cloudflare challenge changed while solving; discarding the token.")
        return False
    try:
        delivered = await page.evaluate(
            f"mw:window[{json.dumps(key)}]?.deliver({json.dumps(render)}, {json.dumps(token)}) === true"
        )
    except Exception as e:
        logger.debug(f"[CaptchaSolver] Cloudflare token delivery notice: {e}")
        delivered = False
    if delivered:
        logger.info("[CaptchaSolver] Token delivered to the Cloudflare challenge page.")
    else:
        logger.info("[CaptchaSolver] Cloudflare challenge was no longer waiting for this token.")
    return bool(delivered)


def parse_solver_cookie(set_cookie: str, page_host: str) -> Optional[Dict[str, Any]]:
    """A Playwright cookie from the Set-Cookie string a solver returns (e.g. DataDome's), or None."""
    jar = SimpleCookie()
    try:
        jar.load(set_cookie)
    except CookieError:
        return None
    for name, morsel in jar.items():
        cookie: Dict[str, Any] = {
            "name": name,
            "value": morsel.value,
            "domain": morsel["domain"] or page_host,
            "path": morsel["path"] or "/",
            "secure": bool(morsel["secure"]),
        }
        same_site = (morsel["samesite"] or "").capitalize()
        if same_site in ("Lax", "Strict", "None"):
            cookie["sameSite"] = same_site
        return cookie
    return None


async def try_datadome_slider_solver(
    page: Page, context: Any, url: str, user_agent: str, proxy_url: Optional[str], timeout: float
) -> bool:
    """Tier 3.5 for DataDome's slider: have the paid service solve it through the same proxy and
    install the `datadome` cookie it returns. The caller reloads the target afterwards."""
    if not proxy_url:
        logger.info("[CaptchaSolver] DataDome slider needs a proxy to be solved by the paid service; skipping.")
        return False
    try:
        captcha_url = await page.locator("iframe[src*='captcha-delivery.com/captcha']").first.get_attribute(
            "src", timeout=2000
        )
    except Exception:
        captcha_url = None
    if not captcha_url:
        logger.info("[CaptchaSolver] DataDome slider frame not found; skipping paid solver escalation.")
        return False
    logger.info("[CaptchaSolver] Escalating DataDome slider to paid solver service...")
    set_cookie = await captcha_solver.solve_datadome_slider(captcha_url, url, user_agent, proxy_url, timeout=timeout)
    if not set_cookie:
        return False
    cookie = parse_solver_cookie(set_cookie, host_of_url(page.url or url))
    if not cookie:
        logger.warning("[CaptchaSolver] DataDome solution was not a usable cookie.")
        return False
    try:
        await context.add_cookies([cookie])
    except Exception as e:
        logger.warning(f"[CaptchaSolver] Could not install DataDome cookie: {e}")
        return False
    logger.info("[CaptchaSolver] DataDome cookie installed.")
    return True
