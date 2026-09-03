from typing import Dict, List, Optional

# Multi-WAF challenge marker table, keyed by the challenge type name used
# both in logs and in PerformanceMetrics.challenges_solved (engine.py).
CHALLENGE_MARKERS: Dict[str, List[str]] = {
    "cloudflare_turnstile": ["just a moment...", "turnstile", "cf-challenge", "checking your browser"],
    "cloudflare_5s": ["attention required!", "ddos protection by cloudflare", "please wait 5 seconds"],
    "ddos_guard": ["ddos-guard", "check.ddos-guard.net", "ddg-captcha", "ddos protection by ddos-guard"],
    "recaptcha": ["g-recaptcha", "google.com/recaptcha", "recaptcha/api2"],
    "hcaptcha": ["hcaptcha.com", "h-captcha", "cf-hcaptcha"],
    "geetest": ["geetest", "gt_captcha"],
    "imperva": ["incapsula", "_incapsula_resource", "visid_incap", "sec-cpt"],
    "datadome": ["datadome", "geo.captcha-delivery.com"],
    "akamai": ["akamai", "ak_bmsc"]
}

AGE_GATE_MARKERS = ["disclaimer-dialog", "close_enter_site_button", "btn-agree"]
CHALLENGE_TITLE_MARKERS = ["just a moment", "checking your browser", "attention required", "ddos-guard", "cloudflare"]


def detect_challenge(title: str, content: str, check_content: bool) -> Optional[str]:
    """Pure lookup over CHALLENGE_MARKERS - no page/browser dependency, so
    it's directly unit-testable. `content` is only consulted when
    check_content is True (the loop only re-fetches page.content() every
    few iterations to save time)."""
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
    # Cloudflare's own transitional title ("Loading https://...") shown
    # while its challenge JS finishes and window.location redirects to the
    # real page - not a genuinely cleared page yet. Treating this as clean
    # makes the loop break and snapshot the page mid-transition, which can
    # capture a stale/incomplete response (and its real status code, now
    # that status is tracked accurately - see the response listener above).
    if title_lower.startswith("loading ") and "://" in title_lower:
        return True
    return False


def has_age_gate_marker(content_lower: str, check_content: bool) -> bool:
    return check_content and any(ag in content_lower for ag in AGE_GATE_MARKERS)
