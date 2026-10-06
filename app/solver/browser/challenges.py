import re
from typing import Dict, List, Mapping, Optional

# Keys double as challenge type names in logs and PerformanceMetrics.challenges_solved.
# Markers are structural (element classes, script paths, provider-owned hosts), never a bare
# brand name: "datadome" or "geetest" alone also appears in telemetry tags and ordinary prose.
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
    "recaptcha": ["g-recaptcha", "google.com/recaptcha", "recaptcha.net/recaptcha", "recaptcha/api2"],
    "hcaptcha": ["h-captcha", "cf-hcaptcha", "hcaptcha.com/1/api", "js.hcaptcha.com", "newassets.hcaptcha.com"],
    "geetest": ["gt_captcha", "initgeetest", "geetest_", "static.geetest.com", "gcaptcha4.geetest.com"],
    "imperva": ["_incapsula_resource", "visid_incap", "incapsula incident id"],
    # js.datadome.co/tags.js is passive telemetry on every page of a protected site; only
    # captcha-delivery.com serves the walls.
    "datadome": ["captcha-delivery.com"],
    "akamai": ["ak_bmsc", "akamai-bot-manager", "akamai_bm", "sec-if-cpt-container", "/_sec/cp_challenge/"],
    # AWS WAF inlines its config as window.gokuProps. The aws-waf-token cookie
    # can't be a marker because detection only sees the title and HTML.
    "aws_waf": ["gokuprops", "awswaf"]
}

# Captcha widgets a site can embed in its own pages (a login form, a comment box). Finding one
# doesn't mean the page is blocked; every other type exists only as a provider-served wall.
WIDGET_CHALLENGES = frozenset({"cloudflare_turnstile", "recaptcha", "hcaptcha", "geetest"})

# Content that only exists while the provider is serving its own interstitial, as opposed to the
# markers above, some of which (telemetry scripts, embedded widgets) also ride on real pages.
WALL_MARKERS: Dict[str, List[str]] = {
    "cloudflare_turnstile": [
        "_cf_chl_opt",
        "challenge-running",
        "challenge-form",
        "orchestrate/chl_page",
        "enable javascript and cookies to continue",
        "cf-error-details",
    ],
    "cloudflare_5s": ["attention required!", "ddos protection by cloudflare", "please wait 5 seconds"],
    "ddos_guard": ["/.well-known/ddos-guard/js-challenge", "check.ddos-guard.net/check.js", "ddg-captcha", "ddg-l10n-title"],
    "imperva": ["incapsula incident id", "_incapsula_resource?swudnsai"],
    "datadome": [
        "captcha-delivery.com/captcha",
        "captcha-delivery.com/interstitial",
        "captcha-delivery.com/c.js",
        "captcha-delivery.com/i.js",
    ],
    "akamai": ["sec-if-cpt-container", "/_sec/cp_challenge/", "behavioral-content"],
    "aws_waf": ["gokuprops"],
}

# Imperva's JS-challenge stub is nothing but its bootstrap script, the same one it injects into
# every real page as telemetry, so only the body size tells the two apart. Kept small on purpose:
# calling a short real page a wall fails the request, while missing a stub only returns it.
LEAN_WALL_BODY_CHARS: Dict[str, int] = {"imperva": 1500}

WALL_STATUS_CODES = frozenset({403, 429, 503})

# Cloudflare firewall verdicts on the egress IP/ASN/country, or its rate limit. 1010 (browser
# signature banned) is left out: a fresh fingerprint can still clear that one.
_CLOUDFLARE_IP_BLOCK_RE = re.compile(
    r"(?:error(?:\s+code)?[:\s]+|cf-error-code[^>]*>\s*)(?:1005|1006|1007|1008|1009|1015|1020)\b"
)
# DataDome's inline `dd` object declares the wall itself: `rt` is the variant ('i' device check,
# 'c' slider captcha) and `t` the verdict ('bv' is the hard block). Both sit in its first few
# fields, so they are read from a window after the opening brace rather than up to the first `}`,
# which a nested object would cut short.
_DATADOME_OBJECT_RE = re.compile(r"\bdd\s*=\s*\{")
_DATADOME_OBJECT_WINDOW = 600
_DATADOME_RT_RE = re.compile(r"""["']rt["']\s*:\s*["']([^"']*)["']""")
_DATADOME_T_RE = re.compile(r"""["']t["']\s*:\s*["']([^"']*)["']""")
_DATADOME_BLOCK_QUERY_RE = re.compile(r"[?&]t=bv\b")

IP_BLOCKED_REASON = "the site refused this IP address outright"
UNSOLVABLE_CAPTCHA_REASON = "it demands an interactive captcha that Solverr cannot solve"

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


class ChallengeNotSolvedError(RuntimeError):
    """A challenge wall was still up when the solve attempt ended.

    `ip_blocked` means the provider refused this egress IP outright, so retrying from the same
    IP is pointless. The message holds only the challenge type and reason, so it is safe to
    return to API clients."""

    def __init__(self, challenge: str, reason: str, ip_blocked: bool = False):
        super().__init__(f"{challenge} challenge not solved: {reason}")
        self.challenge = challenge
        self.reason = reason
        self.ip_blocked = ip_blocked


def _header(headers: Optional[Mapping[str, str]], name: str) -> str:
    if not headers:
        return ""
    for key, value in headers.items():
        if str(key).lower() == name:
            return str(value).strip().lower()
    return ""


def challenge_from_headers(headers: Optional[Mapping[str, str]], status: Optional[int] = None) -> Optional[str]:
    """Challenge type the provider itself declares on the response, or None.

    Only headers that are sent with walls and never with ordinary pages count here."""
    if not headers:
        return None
    if _header(headers, "cf-mitigated") == "challenge":
        return "cloudflare_turnstile"
    aws_action = _header(headers, "x-amzn-waf-action")
    if (aws_action == "challenge" and status in (None, 202)) or (aws_action == "captcha" and status in (None, 405)):
        return "aws_waf"
    # x-datadome reads "protected" on every page of a protected site; x-dd-b is block-only.
    if _header(headers, "x-dd-b"):
        return "datadome"
    return None


def detect_challenge(
    title: str,
    content: str,
    check_content: bool,
    headers: Optional[Mapping[str, str]] = None,
    status: Optional[int] = None,
) -> Optional[str]:
    """Challenge type named by the response headers, the title, or (when check_content is set) `content`.

    A type alone doesn't mean the page is blocked - see is_challenge_wall()."""
    declared = challenge_from_headers(headers, status)
    if declared:
        return declared
    title_lower = title.lower() if title else ""
    content_lower = content.lower() if content else ""
    for ctype, markers in CHALLENGE_MARKERS.items():
        if any(m in title_lower or (check_content and m in content_lower) for m in markers):
            return ctype
    return None


def is_challenge_wall(
    challenge: Optional[str],
    title: str,
    content: str,
    status: Optional[int] = None,
    headers: Optional[Mapping[str, str]] = None,
) -> bool:
    """Whether the page is the provider's own interstitial rather than the page that was asked for.

    False for a real page that only embeds a captcha widget or carries a provider's telemetry."""
    if is_challenge_title(title):
        return True
    if not challenge:
        return False
    if challenge_from_headers(headers, status):
        return True
    if status in WALL_STATUS_CODES:
        return True
    content_lower = content.lower() if content else ""
    if challenge == "datadome" and _DATADOME_OBJECT_RE.search(content_lower):
        return True
    if any(m in content_lower for m in WALL_MARKERS.get(challenge, ())):
        return True
    lean_limit = LEAN_WALL_BODY_CHARS.get(challenge)
    # An empty body means the content wasn't read, not that the page is a bare stub.
    return bool(lean_limit and content_lower and len(content_lower) < lean_limit)


def ip_block_provider(content: str, url: str = "") -> Optional[str]:
    """The provider that refused the egress IP outright ("cloudflare" or "datadome"), or None.

    No amount of waiting or clicking clears these; only a different IP does."""
    if url and "/cdn-cgi/error/" in url:
        return "cloudflare"
    content_lower = content.lower() if content else ""
    if not content_lower:
        return None
    if "cf-error-details" in content_lower or "cf-error-code" in content_lower:
        if "you have been blocked" in content_lower or _CLOUDFLARE_IP_BLOCK_RE.search(content_lower):
            return "cloudflare"
    if datadome_action(content_lower) == "blocked":
        return "datadome"
    return None


def datadome_action(content: str, headers: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Which DataDome wall this is: "interstitial" (the device check, which clears on its own),
    "captcha" (the slider), "blocked" (the hard block), or None when it isn't one."""
    content_lower = content.lower() if content else ""
    if "captcha-delivery.com" in content_lower:
        match = _DATADOME_OBJECT_RE.search(content_lower)
        dd = content_lower[match.start():match.start() + _DATADOME_OBJECT_WINDOW] if match else ""
        verdict = _DATADOME_T_RE.search(dd)
        if (verdict and verdict.group(1) == "bv") or _DATADOME_BLOCK_QUERY_RE.search(content_lower):
            return "blocked"
        # `rt` is the variant DataDome declares itself, so it outranks the path guesses below.
        variant = _DATADOME_RT_RE.search(dd)
        if variant and variant.group(1) == "i":
            return "interstitial"
        if variant and variant.group(1) == "c":
            return "captcha"
        if "captcha-delivery.com/interstitial" in content_lower or "captcha-delivery.com/i.js" in content_lower:
            return "interstitial"
        if "captcha-delivery.com/captcha" in content_lower or "captcha-delivery.com/c.js" in content_lower:
            return "captcha"
    # The header says a wall was served but not which one; the page reclassifies it once read.
    if _header(headers, "x-dd-b"):
        return "interstitial"
    return None


def needs_unsolvable_captcha(
    challenge: Optional[str],
    content: str,
    headers: Optional[Mapping[str, str]] = None,
    status: Optional[int] = None,
) -> bool:
    """Whether the wall is a puzzle neither the click loop nor the paid solver handles (DataDome's
    slider, AWS WAF's image captcha), so waiting on it only burns the budget."""
    if challenge == "datadome":
        return datadome_action(content, headers) == "captcha"
    if challenge == "aws_waf":
        if _header(headers, "x-amzn-waf-action") == "captcha" and status in (None, 405):
            return True
        content_lower = content.lower() if content else ""
        return "gokuprops" in content_lower and "captcha.js" in content_lower
    return False


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
