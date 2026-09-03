"""Tier 3 stealth-browser engine, split by responsibility:

- `pool` - Camoufox process lifecycle (CamoufoxPool)
- `models` - small shared dataclasses (_PooledCamoufox)
- `challenges` - pure challenge/age-gate detection (no page/browser dependency)
- `captcha` - paid captcha-solver escalation (sitekey extraction, token injection)
- `cookies` - Playwright <-> CookieModel conversion
- `navigation` - page navigation (GET/POST) and media-blocking setup
- `interactions` - human-like challenge-widget click dispatch
- `browser` - BrowserPool: ties the above into the actual solve workflow

Everything below is re-exported here so existing call sites
(`from app.solver.browser import browser_pool`, etc.) keep working unchanged.
"""

from app.solver.browser.models import _PooledCamoufox
from app.solver.browser.pool import (
    CamoufoxPool,
    CAMOUFOX_AVAILABLE,
    CAMOUFOX_LAUNCH_TIMEOUT_SECONDS,
)
from app.solver.browser.challenges import (
    CHALLENGE_MARKERS,
    AGE_GATE_MARKERS,
    CHALLENGE_TITLE_MARKERS,
    detect_challenge,
    is_challenge_title,
    has_age_gate_marker,
)
from app.solver.browser.captcha import (
    CAPTCHA_SOLVER_WIDGETS,
    extract_sitekey,
    inject_captcha_token,
    try_captcha_solver_escalation,
)
from app.solver.browser.browser import (
    BrowserPool,
    browser_pool,
    SOLVE_WALLCLOCK_GRACE_SECONDS,
)

__all__ = [
    "_PooledCamoufox",
    "CamoufoxPool",
    "CAMOUFOX_AVAILABLE",
    "CAMOUFOX_LAUNCH_TIMEOUT_SECONDS",
    "CHALLENGE_MARKERS",
    "AGE_GATE_MARKERS",
    "CHALLENGE_TITLE_MARKERS",
    "detect_challenge",
    "is_challenge_title",
    "has_age_gate_marker",
    "CAPTCHA_SOLVER_WIDGETS",
    "extract_sitekey",
    "inject_captcha_token",
    "try_captcha_solver_escalation",
    "BrowserPool",
    "browser_pool",
    "SOLVE_WALLCLOCK_GRACE_SECONDS",
]
