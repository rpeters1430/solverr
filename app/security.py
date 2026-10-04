import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

from app.config import settings


class SSRFBlockedError(Exception):
    """Raised when a target URL resolves to a disallowed network."""


# The metadata IP is link-local and already blocked; these are its DNS aliases.
_METADATA_HOSTNAMES = {"metadata.google.internal", "metadata.goog"}

_ALLOWED_TARGET_SCHEMES = {"http", "https"}
_ALLOWED_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h", "socks4", "socks4a"}
_SAFE_BROWSER_PSEUDO_SCHEMES = {"data", "blob", "about"}


def _is_blocked_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        return True
    if getattr(ip, "ipv4_mapped", None):
        v4 = ip.ipv4_mapped
        if (
            v4.is_private
            or v4.is_loopback
            or v4.is_link_local
            or v4.is_reserved
            or v4.is_multicast
            or v4.is_unspecified
        ):
            return True
    return False


def _validated_host(url: str, label: str) -> str | None:
    if not url or not url.strip():
        raise SSRFBlockedError(f"{label} URL cannot be empty")

    url_lower = url.lower()
    if label == "Browser request" and (
        url_lower.startswith("data:")
        or url_lower.startswith("blob:")
        or url_lower.startswith("about:")
    ):
        return None

    parse_target = url if "://" in url else f"//{url}"
    try:
        parsed = urlparse(parse_target)
        host = parsed.hostname
        scheme = parsed.scheme.lower() if parsed.scheme else ""
    except Exception as exc:
        raise SSRFBlockedError(f"{label} URL '{url}' is invalid") from exc

    if label == "Browser request":
        if scheme and scheme not in _ALLOWED_TARGET_SCHEMES:
            raise SSRFBlockedError(f"{label} scheme '{scheme}' is not allowed")
    elif label in ("Proxy", "Fallback proxy"):
        if scheme and scheme not in _ALLOWED_PROXY_SCHEMES:
            raise SSRFBlockedError(
                f"{label} scheme '{scheme}' is not allowed (only http, https, socks4, socks5 are permitted)"
            )
    else:
        if scheme and scheme not in _ALLOWED_TARGET_SCHEMES:
            raise SSRFBlockedError(
                f"{label} scheme '{scheme}' is not allowed (only http and https are permitted)"
            )

    if not host:
        raise SSRFBlockedError(f"{label} URL '{url}' is missing a valid hostname")

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
