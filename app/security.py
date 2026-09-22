import asyncio
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


def _validated_host(url: str, label: str) -> str | None:
    if not url:
        return None
    parse_target = url if "://" in url else f"//{url}"
    try:
        host = urlparse(parse_target).hostname
    except Exception:
        return None
    if not host:
        return None
    host_lower = host.lower()
    if host_lower in settings.DENIED_HOSTS:
        raise SSRFBlockedError(f"{label} host '{host}' is explicitly denied by DENIED_HOSTS")
    if settings.ALLOW_PRIVATE_NETWORKS or host_lower in settings.ALLOWED_HOSTS:
        return None
    if host_lower == "localhost" or host_lower in _METADATA_HOSTNAMES:
        raise SSRFBlockedError(f"{label} host '{host}' is not allowed (blocked hostname)")
    return host


def _reject_blocked_addresses(host: str, infos, label: str) -> None:
    for info in infos:
        ip_str = info[4][0]
        if _is_blocked_ip(ip_str):
            raise SSRFBlockedError(
                f"{label} host '{host}' resolves to a private/internal address "
                f"({ip_str}) - set ALLOW_PRIVATE_NETWORKS=true or add it to "
                f"ALLOWED_HOSTS to permit this"
            )


def check_target_url(url: str, label: str = "Target") -> None:
    """Validate a target synchronously for compatibility with existing callers."""
    host = _validated_host(url, label)
    if not host:
        return
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise SSRFBlockedError(
            f"{label} host '{host}' could not be resolved safely"
        ) from exc
    _reject_blocked_addresses(host, infos, label)


async def check_target_url_async(url: str, label: str = "Target") -> None:
    """Validate without blocking Uvicorn's event loop during DNS lookup."""
    host = _validated_host(url, label)
    if not host:
        return
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise SSRFBlockedError(
            f"{label} host '{host}' could not be resolved safely"
        ) from exc
    _reject_blocked_addresses(host, infos, label)
