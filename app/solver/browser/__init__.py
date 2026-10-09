"""Tier 3 stealth-browser engine, split by responsibility:

- `pool` - Camoufox process lifecycle (CamoufoxPool)
- `models` - small shared dataclasses (_PooledCamoufox)
- `challenges` - pure challenge/age-gate detection (no page/browser dependency)
- `clearance` - pure sensor-cookie checks (which cookie proves which provider's check passed)
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
    BROWSER_ERROR_TITLES,
    WALL_MARKERS,
    WIDGET_CHALLENGES,
    ChallengeNotSolvedError,
    ANUBIS_REJECTED_REASON,
    IP_BLOCKED_REASON,
    anubis_state,
    challenge_from_headers,
    datadome_action,
    is_anubis_verification_url,
    is_google_sorry_url,
    detect_challenge,
    ip_block_provider,
    is_challenge_title,
    is_challenge_wall,
    is_browser_error,
    has_age_gate_marker,
    needs_unsolvable_captcha,
)
from app.solver.browser.clearance import (
    SENSOR_COOKIES,
    has_earned_sensor_cookie,
    sensor_snapshot,
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
    "BROWSER_ERROR_TITLES",
    "WALL_MARKERS",
    "WIDGET_CHALLENGES",
    "ChallengeNotSolvedError",
    "ANUBIS_REJECTED_REASON",
    "IP_BLOCKED_REASON",
    "anubis_state",
    "challenge_from_headers",
    "is_anubis_verification_url",
    "is_google_sorry_url",
    "datadome_action",
    "needs_unsolvable_captcha",
    "SENSOR_COOKIES",
    "has_earned_sensor_cookie",
    "sensor_snapshot",
    "detect_challenge",
    "ip_block_provider",
    "is_challenge_title",
    "is_challenge_wall",
    "is_browser_error",
    "has_age_gate_marker",
    "CAPTCHA_SOLVER_WIDGETS",
    "extract_sitekey",
    "inject_captcha_token",
    "try_captcha_solver_escalation",
    "BrowserPool",
    "browser_pool",
    "SOLVE_WALLCLOCK_GRACE_SECONDS",
]
