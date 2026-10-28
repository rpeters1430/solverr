import asyncio
import inspect
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

from app.config import settings


class SSRFBlockedError(Exception):
    """Raised when a target URL resolves to a disallowed network."""


# The metadata IP is link-local and already blocked; these are its DNS aliases.
_METADATA_HOSTNAMES = {"metadata.google.internal", "metadata.goog"}

_ALLOWED_TARGET_SCHEMES = {"http", "https"}
_ALLOWED_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h", "socks4", "socks4a"}
_SAFE_BROWSER_PSEUDO_SCHEMES = {"data", "blob", "about"}


@dataclass(frozen=True)
class ResolvedTarget:
    """A policy-approved DNS result. `sockaddr` contains only numeric IPs."""

    host: str
    port: int
    addresses: tuple


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


def _host_policy_allows_private(host: str, label: str) -> bool:
    host_lower = host.rstrip(".").lower()
    if host_lower in settings.DENIED_HOSTS:
        raise SSRFBlockedError(f"{label} host '{host}' is explicitly denied by DENIED_HOSTS")
    if settings.ALLOW_PRIVATE_NETWORKS or host_lower in settings.ALLOWED_HOSTS:
        return True
    if host_lower == "localhost" or host_lower in _METADATA_HOSTNAMES:
        raise SSRFBlockedError(f"{label} host '{host}' is not allowed (blocked hostname)")
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

    if _host_policy_allows_private(host, label):
        return None
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


def _resolve_sync(host: str, port: int, label: str, resolver=None) -> ResolvedTarget:
    allow_private = _host_policy_allows_private(host, label)
    resolve = resolver or socket.getaddrinfo
    try:
        infos = resolve(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SSRFBlockedError(f"{label} host '{host}' could not be resolved safely") from exc
    infos = tuple(info for info in infos if info[0] in (socket.AF_INET, socket.AF_INET6))
    if not infos:
        raise SSRFBlockedError(f"{label} host '{host}' did not resolve to a usable address")
    if not allow_private:
        _reject_blocked_addresses(host, infos, label)
    return ResolvedTarget(host=host, port=port, addresses=infos)


async def resolve_target_host_async(host: str, port: int, label: str = "Target", resolver=None) -> ResolvedTarget:
    """Resolve once, apply the SSRF policy to every answer, and retain numeric socket addresses.

    The caller must connect using one of ``addresses`` directly. Passing ``host`` to a second
    resolver or to ``open_connection(host=...)`` would reopen the DNS rebinding gap.
    """
    allow_private = _host_policy_allows_private(host, label)
    if not 1 <= int(port) <= 65535:
        raise SSRFBlockedError(f"{label} port must be between 1 and 65535")
    loop = asyncio.get_running_loop()
    try:
        if resolver is None:
            infos = await loop.getaddrinfo(host, int(port), type=socket.SOCK_STREAM)
        else:
            result = resolver(host, int(port), type=socket.SOCK_STREAM)
            infos = await result if inspect.isawaitable(result) else result
    except OSError as exc:
        raise SSRFBlockedError(f"{label} host '{host}' could not be resolved safely") from exc
    infos = tuple(info for info in infos if info[0] in (socket.AF_INET, socket.AF_INET6))
    if not infos:
        raise SSRFBlockedError(f"{label} host '{host}' did not resolve to a usable address")
    if not allow_private:
        _reject_blocked_addresses(host, infos, label)
    return ResolvedTarget(host=host, port=int(port), addresses=infos)


def check_target_url(url: str, label: str = "Target") -> None:
    """Validate a target synchronously for compatibility with existing callers."""
    host = _validated_host(url, label)
    if not host:
        return
    _resolve_sync(host, 443, label)


async def check_target_url_async(url: str, label: str = "Target") -> None:
    """Validate without blocking Uvicorn's event loop during DNS lookup."""
    host = _validated_host(url, label)
    if not host:
        return
    await resolve_target_host_async(host, 443, label)
