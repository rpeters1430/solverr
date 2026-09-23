from typing import Dict, List, Optional

# Keys double as challenge type names in logs and PerformanceMetrics.challenges_solved.
CHALLENGE_MARKERS: Dict[str, List[str]] = {
    "cloudflare_turnstile": [
        "just a moment...",
        "cf-turnstile",
        "cf-challenge",
        "checking your browser",
        "challenges.cloudflare.com",
        "_cf_chl_opt",
    ],
    "cloudflare_5s": ["attention required!", "ddos protection by cloudflare", "please wait 5 seconds"],
    "ddos_guard": ["ddos-guard", "check.ddos-guard.net", "ddg-captcha", "ddos protection by ddos-guard"],
    "recaptcha": ["g-recaptcha", "google.com/recaptcha", "recaptcha/api2"],
    "hcaptcha": ["hcaptcha.com", "h-captcha", "cf-hcaptcha"],
    "geetest": ["geetest", "gt_captcha"],
    "imperva": ["incapsula", "_incapsula_resource", "visid_incap", "sec-cpt"],
    "datadome": ["datadome", "geo.captcha-delivery.com"],
    "akamai": ["ak_bmsc", "akamai-bot-manager", "akamai_bm"],
    # AWS WAF inlines its config as window.gokuProps. The aws-waf-token cookie
    # can't be a marker because detection only sees the title and HTML.
    "aws_waf": ["gokuprops", "awswaf"]
}

AGE_GATE_MARKERS = ["disclaimer-dialog", "close_enter_site_button", "btn-agree"]
CHALLENGE_TITLE_MARKERS = [
    "just a moment",
    "checking your browser",
    "attention required",
    "ddos-guard",
    "ddos protection by cloudflare",
]

BROWSER_ERROR_TITLES = [
    "problem loading page",
    "warning: security risk",
    "server not found",
    "address not found",
    "connection timed out",
    "unable to connect",
    "secure connection failed",
    "potential security risk ahead",
]


def detect_challenge(title: str, content: str, check_content: bool) -> Optional[str]:
    """Match CHALLENGE_MARKERS against the title, and against `content` only when check_content is set."""
    title_lower = title.lower() if title else ""
    content_lower = content.lower() if content else ""
    for ctype, markers in CHALLENGE_MARKERS.items():
        if any(m in title_lower or (check_content and m in content_lower) for m in markers):
            return ctype
    return None


def is_challenge_title(title: str) -> bool:
    title_lower = title.lower() if title else ""
    if any(t in title_lower for t in CHALLENGE_TITLE_MARKERS):
        return True
    # Cloudflare's "Loading https://..." title appears mid-redirect, before the real page arrives.
    if title_lower.startswith("loading ") and "://" in title_lower:
        return True
    return False


def is_browser_error(title: str, url: str = "") -> bool:
    """Detect whether a browser navigation terminated at an internal error page
    (DNS failure, SSL handshake rejection, connection refused, etc.)."""
    if url and (url.startswith("about:neterror") or url.startswith("about:certerror")):
        return True
    title_lower = title.lower() if title else ""
    return any(err in title_lower for err in BROWSER_ERROR_TITLES)


def has_age_gate_marker(content_lower: str, check_content: bool) -> bool:
    return check_content and any(ag in content_lower for ag in AGE_GATE_MARKERS)
