"""Opt-in HTTP/1.1 forward proxy which sends requests through Solverr's tiered engine."""

from __future__ import annotations

import asyncio
import base64
import hmac
import logging
import re
import ssl
from email.utils import formatdate
from http import HTTPStatus
from http.cookies import SimpleCookie
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.config import settings
from app.models.flaresolverr import V1Request
from app.proxy_certificates import ProxyCertificateAuthority
from app.security import check_target_url_async
from app.egress_proxy import egress_proxy
from app.solver.engine import solver_engine

logger = logging.getLogger("solverr.forward_proxy")

MAX_HEADER_BYTES = 64 * 1024
MAX_REQUESTS_PER_CONNECTION = 100
MAX_ACTIVE_PROXY_CLIENTS = 64
HTTP_TOKEN_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
PROXY_HOP_HEADERS = {
    "connection", "proxy-connection", "proxy-authenticate", "proxy-authorization",
    "keep-alive", "te", "trailer", "transfer-encoding", "upgrade", "content-length",
}
RESPONSE_HOP_HEADERS = {
    "connection", "proxy-connection", "proxy-authenticate", "proxy-authorization",
    "keep-alive", "te", "trailer", "transfer-encoding", "upgrade", "content-length",
    "content-encoding", "content-md5", "digest",
}
STREAMABLE_CONTENT_TYPES = (
    "text/html", "application/xhtml+xml",
)


class ProxyProtocolError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class ForwardProxyServer:
    def __init__(self):
        self._server: asyncio.Server | None = None
        self._clients: set[asyncio.Task] = set()
        self._client_slots = asyncio.Semaphore(MAX_ACTIVE_PROXY_CLIENTS)
        self.certificates = ProxyCertificateAuthority(settings.MITM_CA_DIR)
        self._expected_auth = f"{settings.MITM_PROXY_USERNAME or ''}:{settings.MITM_PROXY_PASSWORD or ''}".encode()
        self._upstream = httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(connect=15, read=120, write=30, pool=15),
            proxy=egress_proxy.url,
            trust_env=False,
        )

    async def start(self) -> None:
        if not settings.MITM_ENABLED:
            return
        if not settings.MITM_PROXY_USERNAME or not settings.MITM_PROXY_PASSWORD:
            raise RuntimeError("MITM proxy requires both MITM_PROXY_USERNAME and MITM_PROXY_PASSWORD")
        if not 1 <= settings.MITM_PORT <= 65535:
            raise RuntimeError("MITM_PORT must be between 1 and 65535")
        self.certificates.initialize()
        logger.warning("Forward-proxy CA SHA-256 PEM fingerprint: %s", self.certificates.public_fingerprint())
        self._server = await asyncio.start_server(
            self._client_connected,
            host=settings.MITM_HOST,
            port=settings.MITM_PORT,
            limit=MAX_HEADER_BYTES + 1,
        )
        logger.warning(
            "Forward proxy enabled on %s:%s; HTTPS is intercepted using the persisted Solverr CA. "
            "Keep this listener on a trusted private network.",
            settings.MITM_HOST,
            settings.MITM_PORT,
        )

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._clients:
            _, pending = await asyncio.wait(self._clients, timeout=10)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        await self._upstream.aclose()

    async def _client_connected(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._clients.add(task)
        peer = writer.get_extra_info("peername")
        slot_acquired = False
        try:
            if self._client_slots.locked():
                await self._write_error(writer, 503, "Forward proxy is at its connection limit")
                return
            await self._client_slots.acquire()
            slot_acquired = True
            first = await asyncio.wait_for(self._read_request_headers(reader), timeout=15)
            method, target, headers = first
            if method.upper() == "CONNECT":
                self._require_auth(headers)
                hostname, port = self._parse_connect_target(target)
                if port != 443:
                    raise ProxyProtocolError(403, "Only HTTPS CONNECT on port 443 is supported")
                context = self.certificates.issue_server_context(hostname)
                writer.write(b"HTTP/1.1 200 Connection Established\r\nProxy-Agent: Solverr\r\n\r\n")
                await writer.drain()
                await asyncio.wait_for(writer.start_tls(context, ssl_handshake_timeout=15), timeout=20)
                await self._serve_http_requests(reader, writer, secure_host=hostname, first=None, already_authenticated=True)
            else:
                await self._serve_http_requests(reader, writer, secure_host=None, first=first, already_authenticated=False)
        except asyncio.IncompleteReadError:
            pass
        except (asyncio.TimeoutError, ConnectionError, ssl.SSLError):
            logger.debug("Forward proxy client disconnected or timed out: %s", peer)
        except ProxyProtocolError as exc:
            await self._write_error(writer, exc.status, str(exc))
        except Exception as exc:
            logger.warning("Forward proxy request failed from %s: %s", peer, type(exc).__name__)
            await self._write_error(writer, 502, "Solverr could not process this proxy request")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, ssl.SSLError):
                pass
            if task is not None:
                self._clients.discard(task)
            if slot_acquired:
                self._client_slots.release()

    async def _serve_http_requests(self, reader, writer, secure_host: Optional[str], first, already_authenticated: bool):
        current = first
        for _ in range(MAX_REQUESTS_PER_CONNECTION):
            if current is None:
                try:
                    current = await asyncio.wait_for(self._read_request_headers(reader), timeout=120)
                except asyncio.IncompleteReadError:
                    return
                except asyncio.TimeoutError:
                    return
            method, target, headers = current
            current = None
            try:
                if method.upper() not in {"GET", "HEAD", "POST"}:
                    raise ProxyProtocolError(501, "Only GET, HEAD, and POST are supported by the solver-backed proxy")
                if not already_authenticated:
                    self._require_auth(headers)
                url = self._resolve_target(target, headers, secure_host)
                body = await self._read_request_body(reader, writer, headers)
                if self._should_try_stream(method, url, headers):
                    if await self._stream_upstream(method, url, headers, body, writer):
                        # The streamed response is deliberately close-delimited.
                        return
                await self._solve_and_write(method, url, headers, body, writer)
            except ProxyProtocolError as exc:
                await self._write_error(writer, exc.status, str(exc))
                return
            except Exception as exc:
                logger.warning("Forward proxy solve failed for request: %s", type(exc).__name__)
                await self._write_error(writer, 502, "Solverr could not process this proxy request")
                return
            if headers.get("connection", "").lower() == "close":
                return

    async def _read_request_headers(self, reader):
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
        except asyncio.LimitOverrunError as exc:
            raise ProxyProtocolError(431, "Request headers are too large") from exc
        if len(raw) > MAX_HEADER_BYTES:
            raise ProxyProtocolError(431, "Request headers are too large")
        try:
            lines = raw[:-4].decode("iso-8859-1").split("\r\n")
            method, target, version = lines[0].split(" ", 2)
        except (UnicodeDecodeError, ValueError, IndexError) as exc:
            raise ProxyProtocolError(400, "Malformed HTTP request") from exc
        if version not in ("HTTP/1.0", "HTTP/1.1"):
            raise ProxyProtocolError(505, "Only HTTP/1.0 and HTTP/1.1 are supported")
        if not HTTP_TOKEN_RE.fullmatch(method) or any(ord(ch) < 32 for ch in target):
            raise ProxyProtocolError(400, "Malformed HTTP request line")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if line[:1] in (" ", "\t") or ":" not in line:
                raise ProxyProtocolError(400, "Malformed HTTP header")
            name, value = line.split(":", 1)
            key = name.strip().lower()
            if not HTTP_TOKEN_RE.fullmatch(key) or key in headers:
                raise ProxyProtocolError(400, "Invalid or duplicate HTTP header")
            value = value.strip()
            if any(ord(ch) < 32 and ch != "\t" for ch in value):
                raise ProxyProtocolError(400, "Invalid control character in HTTP header")
            headers[key] = value
        return method, target, headers

    def _require_auth(self, headers: dict[str, str]) -> None:
        value = headers.get("proxy-authorization", "")
        scheme, _, token = value.partition(" ")
        if scheme.lower() != "basic" or not token:
            raise ProxyProtocolError(407, "Proxy authentication required")
        try:
            supplied = base64.b64decode(token, validate=True)
        except Exception as exc:
            raise ProxyProtocolError(407, "Invalid proxy credentials") from exc
        if not hmac.compare_digest(supplied, self._expected_auth):
            raise ProxyProtocolError(407, "Invalid proxy credentials")

    @staticmethod
    def _parse_connect_target(target: str) -> tuple[str, int]:
        host, sep, port_text = target.rpartition(":")
        if not sep or not host or not port_text.isdigit():
            raise ProxyProtocolError(400, "CONNECT target must be host:port")
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        port = int(port_text)
        if not host or port != 443:
            raise ProxyProtocolError(403, "Only HTTPS CONNECT on port 443 is supported")
        return host, port

    @staticmethod
    def _resolve_target(target: str, headers: dict[str, str], secure_host: Optional[str]) -> str:
        parsed = urlsplit(target)
        if parsed.scheme in ("http", "https") and parsed.hostname:
            try:
                parsed.port
            except ValueError as exc:
                raise ProxyProtocolError(400, "Invalid target port") from exc
            if parsed.username or parsed.password or parsed.fragment:
                raise ProxyProtocolError(400, "Proxy target must not contain credentials or a fragment")
            if parsed.scheme == "https" and not secure_host:
                raise ProxyProtocolError(400, "HTTPS proxy requests must use CONNECT")
            if secure_host and parsed.hostname.lower() != secure_host.lower():
                raise ProxyProtocolError(400, "Request host does not match CONNECT host")
            return target
        if secure_host:
            if not target.startswith("/"):
                raise ProxyProtocolError(400, "HTTPS request target must be origin-form")
            path = target if target.startswith("/") else "/" + target
            netloc = f"[{secure_host}]" if ":" in secure_host else secure_host
            return urlunsplit(("https", netloc, path.split("?", 1)[0], path.split("?", 1)[1] if "?" in path else "", ""))
        raise ProxyProtocolError(400, "HTTP proxy requests must use an absolute http:// URL")

    async def _read_request_body(self, reader, writer, headers: dict[str, str]) -> Optional[str]:
        if "transfer-encoding" in headers:
            raise ProxyProtocolError(501, "Chunked request bodies are not supported yet")
        length_text = headers.get("content-length", "0")
        try:
            length = int(length_text)
        except ValueError as exc:
            raise ProxyProtocolError(400, "Invalid Content-Length") from exc
        if length < 0:
            raise ProxyProtocolError(400, "Invalid Content-Length")
        limit = int(settings.MAX_REQUEST_BODY_MB * 1024 * 1024) if settings.MAX_REQUEST_BODY_MB > 0 else 10 * 1024 * 1024
        if length > limit:
            raise ProxyProtocolError(413, "Request body exceeds Solverr's configured limit")
        if headers.get("expect", "").lower() == "100-continue":
            writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
            await writer.drain()
        raw = await reader.readexactly(length) if length else b""
        try:
            return raw.decode("utf-8") if raw else None
        except UnicodeDecodeError as exc:
            raise ProxyProtocolError(415, "Binary request bodies are not supported by Solverr's text request API") from exc

    async def _solve_and_write(self, method, url, inbound_headers, body, writer):
        forward_headers = {
            key: value for key, value in inbound_headers.items()
            if key not in PROXY_HOP_HEADERS and key not in {"host", "expect"}
        }
        request = V1Request(
            cmd=f"request.{method.lower()}",
            url=url,
            postData=body,
            headers=forward_headers,
        )
        solution = await solver_engine.process_request(request)
        status = solution.status if isinstance(solution.status, int) and 100 <= solution.status <= 599 else 502
        response_headers = {
            str(key).lower(): str(value)
            for key, value in (solution.headers or {}).items()
            if str(key).lower() not in RESPONSE_HOP_HEADERS
        }
        content_type = response_headers.get("content-type", "text/html")
        if not re.search(r"(?:text/|json|xml|javascript|x-www-form-urlencoded|form-data)", content_type, re.I):
            raise ProxyProtocolError(501, "Byte-transparent binary and media responses are not supported yet")
        payload = solution.response or ""
        charset = re.search(r"charset\s*=\s*[\"']?([^;\"']+)", content_type, re.I)
        encoding = charset.group(1).strip() if charset else "utf-8"
        try:
            body_bytes = payload.encode(encoding, errors="replace")
        except LookupError:
            body_bytes = payload.encode("utf-8")
        if method.upper() == "HEAD":
            body_bytes = b""

        headers_out = []
        for name, value in response_headers.items():
            if re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-z-]+", name, re.I) and "\r" not in value and "\n" not in value:
                headers_out.append((name, value))
        for cookie in solution.cookies or []:
            if any(ch in f"{cookie.name}{cookie.value}{cookie.domain or ''}{cookie.path or ''}" for ch in "\r\n"):
                continue
            morsels = SimpleCookie()
            try:
                morsels[cookie.name] = cookie.value
            except Exception:
                continue
            morsel = morsels[cookie.name]
            morsel["path"] = cookie.path or "/"
            if cookie.domain:
                morsel["domain"] = cookie.domain.lstrip(".")
            if cookie.secure:
                morsel["secure"] = True
            if cookie.httpOnly:
                morsel["httponly"] = True
            if cookie.expires and cookie.expires > 0:
                morsel["expires"] = formatdate(cookie.expires, usegmt=True)
            if cookie.sameSite in {"Strict", "Lax", "None"}:
                morsel["samesite"] = cookie.sameSite
            value = morsel.OutputString()
            headers_out.append(("set-cookie", value))
        headers_out.append(("content-length", str(len(body_bytes))))
        headers_out.append(("connection", "close"))
        try:
            reason = HTTPStatus(status).phrase
        except ValueError:
            reason = "Upstream Response"
        writer.write(f"HTTP/1.1 {status} {reason}\r\n".encode("ascii"))
        for name, value in headers_out:
            writer.write(f"{name}: {value}\r\n".encode("iso-8859-1", errors="replace"))
        writer.write(b"\r\n")
        if body_bytes:
            writer.write(body_bytes)
        await writer.drain()

    @staticmethod
    def _should_try_stream(method: str, url: str, headers: dict[str, str]) -> bool:
        """Route downloader-shaped requests through a byte-preserving upstream path.

        Solverr's solve API intentionally returns text. yt-dlp's media and range
        requests need their original bytes, so only requests that look like a
        download are probed here. HTML responses still go through Solverr below.
        """
        if method.upper() not in {"GET", "HEAD"}:
            return False
        accept = headers.get("accept", "")
        if "range" in headers or "text/html" not in accept.lower():
            return True
        path = urlsplit(url).path.lower()
        return path.endswith((
            ".mp4", ".m4v", ".mkv", ".webm", ".mov", ".mp3", ".m4a",
            ".opus", ".ogg", ".aac", ".wav", ".flac", ".ts", ".m3u8",
            ".mpd", ".vtt", ".srt", ".jpg", ".jpeg", ".png", ".webp",
        ))

    async def _stream_upstream(self, method, url, inbound_headers, body, writer) -> bool:
        """Stream non-HTML responses without decoding or buffering media bytes.

        Returns False for HTML so the normal tiered solver can process challenges.
        """
        await check_target_url_async(url)
        request_headers = {
            key: value for key, value in inbound_headers.items()
            if key not in PROXY_HOP_HEADERS and key not in {"host", "expect"}
        }
        # Keep byte ranges stable and avoid a decompression/recompression cycle.
        request_headers.setdefault("accept-encoding", "identity")
        request = self._upstream.build_request(method, url, headers=request_headers, content=body)
        upstream_response = await self._upstream.send(request, stream=True)
        content_type = upstream_response.headers.get("content-type", "").lower()
        if any(content_type.startswith(value) for value in STREAMABLE_CONTENT_TYPES):
            await upstream_response.aclose()
            return False

        try:
            status = upstream_response.status_code
            try:
                reason = HTTPStatus(status).phrase
            except ValueError:
                reason = "Upstream Response"
            writer.write(f"HTTP/1.1 {status} {reason}\r\n".encode("ascii"))
            for name, value in upstream_response.headers.multi_items():
                lname = name.lower()
                if lname in {
                    "connection", "proxy-connection", "proxy-authenticate", "proxy-authorization",
                    "keep-alive", "te", "trailer", "transfer-encoding", "upgrade",
                }:
                    continue
                if "\r" not in name + value and "\n" not in name + value:
                    writer.write(f"{name}: {value}\r\n".encode("iso-8859-1", errors="replace"))
            # Preserve Content-Length when upstream supplied it. Chunked transfer
            # framing is removed by httpx, so those responses end at connection close.
            writer.write(b"Connection: close\r\n\r\n")
            await writer.drain()
            if method.upper() != "HEAD":
                try:
                    async for chunk in upstream_response.aiter_raw():
                        if chunk:
                            writer.write(chunk)
                            await writer.drain()
                except (httpx.HTTPError, ConnectionError, OSError) as exc:
                    # Headers may already be on the wire; close the truncated
                    # response instead of appending a second HTTP error message.
                    logger.debug("Raw proxy stream ended early: %s", type(exc).__name__)
        finally:
            await upstream_response.aclose()
        return True

    @staticmethod
    async def _write_error(writer, status: int, message: str) -> None:
        if writer.is_closing():
            return
        try:
            reason = HTTPStatus(status).phrase
        except ValueError:
            reason = "Proxy Error"
        body = (message + "\n").encode("utf-8")
        headers = [
            f"HTTP/1.1 {status} {reason}\r\n",
            "Content-Type: text/plain; charset=utf-8\r\n",
            "Proxy-Authenticate: Basic realm=\"Solverr\"\r\n" if status == 407 else "",
            f"Content-Length: {len(body)}\r\n",
            "Connection: close\r\n\r\n",
        ]
        writer.write("".join(headers).encode("ascii") + body)
        try:
            await writer.drain()
        except (ConnectionError, ssl.SSLError):
            pass


forward_proxy = ForwardProxyServer()
