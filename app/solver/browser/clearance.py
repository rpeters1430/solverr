"""Sensor cookies: the proof that a WAF's in-page check passed. Pure functions, no page dependency."""
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urlparse

# Cookie each provider issues once its check passes. DDoS-Guard's are matched as name prefixes.
SENSOR_COOKIES: Dict[str, Tuple[str, ...]] = {
    "cloudflare_turnstile": ("cf_clearance",),
    "cloudflare_5s": ("cf_clearance",),
    "imperva": ("reese84", "___utmvc"),
    "akamai": ("_abck",),
    "datadome": ("datadome",),
    "aws_waf": ("aws-waf-token",),
    "ddos_guard": ("__ddg2_", "__ddg5_"),
}

# DataDome and AWS WAF set their cookie on the wall's own response, so finding one proves
# nothing; only a value that wasn't there when the wall was first seen does.
BLOCK_PAGE_COOKIES = frozenset({"datadome", "aws-waf-token"})

_ALL_SENSOR_NAMES = tuple({name for names in SENSOR_COOKIES.values() for name in names})


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def cookie_applies_to_host(cookie_domain: Optional[str], host: str) -> bool:
    domain = (cookie_domain or "").lstrip(".").lower()
    return bool(domain and host) and (host == domain or host.endswith("." + domain))


def _is_sensor_name(cookie_name: str, sensor_names: Iterable[str]) -> bool:
    return any(cookie_name == n or (n.startswith("__ddg") and cookie_name.startswith(n)) for n in sensor_names)


def abck_is_valid(value: str) -> bool:
    """Akamai sets `_abck` on the first response with -1 in its second `~` field; the sensor
    post rewrites it once the browser is accepted."""
    parts = (value or "").split("~")
    return len(parts) > 1 and parts[1] != "-1"


def sensor_snapshot(
    raw_cookies: List[Dict[str, Any]], host: str, names: Optional[Iterable[str]] = None
) -> Set[Tuple[str, str]]:
    """(name, value) of every sensor cookie that applies to `host`, limited to `names` when given."""
    wanted = tuple(names) if names is not None else _ALL_SENSOR_NAMES
    return {
        (str(c.get("name", "")), str(c.get("value", "")))
        for c in raw_cookies
        if _is_sensor_name(str(c.get("name", "")), wanted) and cookie_applies_to_host(c.get("domain"), host)
    }


def has_earned_sensor_cookie(
    challenge: Optional[str], raw_cookies: List[Dict[str, Any]], host: str, baseline: Set[Tuple[str, str]]
) -> bool:
    """Whether `challenge`'s provider issued its sensor cookie for `host` during this solve.

    Values already in `baseline` (cached cookies replayed into the context, or the one a wall
    hands out with its own response) don't count: the site is still challenging despite them."""
    sensor_names = SENSOR_COOKIES.get(challenge or "", ())
    if not sensor_names:
        return False
    for name, value in sensor_snapshot(raw_cookies, host, sensor_names):
        if (name, value) in baseline:
            continue
        if name == "_abck" and not abck_is_valid(value):
            continue
        return True
    return False
