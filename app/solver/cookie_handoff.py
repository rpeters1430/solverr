"""Cookie handoff validation for FlareSolverr consumers such as Prowlarr."""
import time
from urllib.parse import urlsplit


def usable_cookies(cookies, url: str) -> bool:
    target = urlsplit(url)
    host = (target.hostname or "").lower()
    path = target.path or "/"
    now = time.time()
    for cookie in cookies or []:
        domain = (cookie.domain or host).lstrip(".").lower()
        scope = cookie.path or "/"
        if not cookie.name or not cookie.value or (cookie.expires and 0 < cookie.expires <= now):
            continue
        if cookie.secure and target.scheme != "https":
            continue
        if host != domain and not host.endswith("." + domain):
            continue
        if path != scope and not path.startswith(scope if scope.endswith("/") else scope + "/"):
            continue
        return True
    return False


def is_prowlarr_ping(url: str, solution) -> bool:
    def exact_ping(value):
        target = urlsplit(value)
        return (target.scheme.lower() == "https" and (target.hostname or "").lower() == "prowlarr.servarr.com"
                and target.port in (None, 443) and not target.username and not target.password
                and target.path == "/v1/ping" and not target.query and not target.fragment)
    return exact_ping(url) and exact_ping(solution.url) and (solution.response or "").strip() == "Pong"
