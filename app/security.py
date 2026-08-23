import ipaddress
import socket
from urllib.parse import urlparse

from app.config import settings


class SSRFBlockedError(Exception):
    """Raised when a target URL resolves to a disallowed network."""


# Cloud metadata endpoints aren't caught by the private-IP ranges below (they
# live at a link-local address, which IS covered - but DNS names some clouds
# accept for the same endpoint are listed explicitly for clarity/robustness).
_METADATA_HOSTNAMES = {"metadata.google.internal", "metadata.goog"}


def _is_blocked_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def check_target_url(url: str) -> None:
    """Raise SSRFBlockedError if `url`'s host is disallowed by policy.

    Only validates the initial request target - it does not follow
    redirects, so a target that redirects to an internal address only after
    this check runs is not caught here.
    """
    if settings.ALLOW_PRIVATE_NETWORKS or not url:
        return

    try:
        host = urlparse(url).hostname
    except Exception:
        return
    if not host:
        return
    host_lower = host.lower()

    if host_lower in settings.ALLOWED_HOSTS:
        return
    if host_lower in settings.DENIED_HOSTS:
        raise SSRFBlockedError(f"Target host '{host}' is explicitly denied by DENIED_HOSTS")
    if host_lower == "localhost" or host_lower in _METADATA_HOSTNAMES:
        raise SSRFBlockedError(f"Target host '{host}' is not allowed (blocked hostname)")

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        # Can't resolve - let the real request fail naturally downstream
        # rather than raising a confusing SSRF error for a typo'd hostname.
        return

    for info in infos:
        ip_str = info[4][0]
        if _is_blocked_ip(ip_str):
            raise SSRFBlockedError(
                f"Target host '{host}' resolves to a private/internal address "
                f"({ip_str}) - set ALLOW_PRIVATE_NETWORKS=true or add it to "
                f"ALLOWED_HOSTS to permit this"
            )
