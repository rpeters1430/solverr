"""Loopback-only HTTP egress proxy that connects only to validated numeric addresses."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from http import HTTPStatus
from urllib.parse import urlsplit

from app.config import settings
from app.security import SSRFBlockedError, resolve_target_host_async

logger = logging.getLogger("solverr.egress_proxy")

MAX_HEADER_BYTES = 64 * 1024
HTTP_TOKEN_RE = re.compile(rb"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_HOP_BY_HOP_HEADERS = {
    b"connection", b"proxy-connection", b"proxy-authorization", b"proxy-authenticate",
    b"keep-alive", b"te", b"trailer", b"upgrade",
}


class EgressProxyError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


async def _connect_numeric_address(info):
    """Open a socket from a getaddrinfo record without resolving its hostname again."""
    family, socktype, proto, _canonname, sockaddr = info
    sock = socket.socket(family, socktype, proto)
    sock.setblocking(False)
    try:
        await asyncio.get_running_loop().sock_connect(sock, sockaddr)
        return await asyncio.open_connection(sock=sock)
    except BaseException:
        sock.close()
        raise


class EgressProxyServer:
    """Minimal HTTP/1.1 forward proxy for Solverr's own outbound HTTP stacks.

    HTTP requests use absolute-form and are rewritten to origin-form. HTTPS CONNECT
    is a byte tunnel, so TLS and certificate validation remain end-to-end. The
    server always binds to IPv4 loopback and never forwards credentials in logs.
    """

    def __init__(self, host="127.0.0.1", port=8193, resolver=None, connector=None):
        if host not in ("127.0.0.1", "localhost"):
            raise ValueError("The egress proxy may bind only to loopback")
        self.host = "127.0.0.1"
        self.requested_port = int(port)
        self.port = int(port)
        self._resolver = resolver
        self._connector = connector or _connect_numeric_address
        self._server: asyncio.Server | None = None
        self._clients: set[asyncio.Task] = set()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(
            self._client_connected,
            host=self.host,
            port=self.requested_port,
            limit=MAX_HEADER_BYTES + 1,
        )
        self.port = int(self._server.sockets[0].getsockname()[1])
        logger.info("Policy-enforcing egress proxy listening on 127.0.0.1:%s", self.port)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        pending = [task for task in self._clients if task is not asyncio.current_task()]
        if pending:
            _, still_running = await asyncio.wait(pending, timeout=5)
            for task in still_running:
                task.cancel()
            if still_running:
                await asyncio.gather(*still_running, return_exceptions=True)

    async def _client_connected(self, client_reader, client_writer) -> None:
        task = asyncio.current_task()
        if task:
            self._clients.add(task)
        upstream_writer = None
        try:
            request = await asyncio.wait_for(self._read_request(client_reader), timeout=15)
            method, target, version, headers = request
            if method == b"CONNECT":
                host, port = self._parse_connect_target(target)
                if port != 443:
                    raise EgressProxyError(403, "Only HTTPS CONNECT on port 443 is permitted")
                _upstream_reader, upstream_writer = await self._open_validated(host, port)
                client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await client_writer.drain()
                await self._relay(client_reader, client_writer, _upstream_reader, upstream_writer)
            else:
                host, port, origin_target = self._parse_http_target(target)
                upstream_reader, upstream_writer = await self._open_validated(host, port)
                request_headers = self._origin_headers(headers)
                formatted_host = f"[{host}]" if ":" in host else host
                if port != 80:
                    formatted_host += f":{port}"
                request_headers = [(name, value) for name, value in request_headers if name != b"host"]
                request_headers.append((b"host", formatted_host.encode("ascii")))
                expect_continue = any(
                    name == b"expect" and value.lower() == b"100-continue" for name, value in headers
                )
                request_headers = [(name, value) for name, value in request_headers if name != b"expect"]
                upstream_writer.write(method + b" " + origin_target + b" " + version + b"\r\n")
                for name, value in request_headers:
                    upstream_writer.write(name + b": " + value + b"\r\n")
                upstream_writer.write(b"Connection: close\r\n\r\n")
                await upstream_writer.drain()
                if expect_continue:
                    client_writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
                    await client_writer.drain()
                await self._copy_http_request_body(client_reader, upstream_writer, request_headers)
                await self._relay_http_response(client_reader, client_writer, upstream_reader, upstream_writer)
        except asyncio.IncompleteReadError:
            pass
        except EgressProxyError as exc:
            await self._write_error(client_writer, exc.status, str(exc))
        except SSRFBlockedError:
            await self._write_error(client_writer, 403, "Destination is blocked by Solverr network policy")
        except (asyncio.TimeoutError, OSError, ConnectionError):
            await self._write_error(client_writer, 502, "Unable to connect to the validated destination")
        except Exception as exc:
            logger.warning("Egress proxy request failed: %s", type(exc).__name__)
            await self._write_error(client_writer, 502, "Unable to process the egress proxy request")
        finally:
            if upstream_writer is not None:
                upstream_writer.close()
                try:
                    await upstream_writer.wait_closed()
                except (ConnectionError, OSError):
                    pass
            client_writer.close()
            try:
                await client_writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            if task:
                self._clients.discard(task)

    async def _read_request(self, reader):
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
        except asyncio.LimitOverrunError as exc:
            raise EgressProxyError(431, "Request headers are too large") from exc
        if len(raw) > MAX_HEADER_BYTES:
            raise EgressProxyError(431, "Request headers are too large")
        try:
            lines = raw[:-4].split(b"\r\n")
            method, target, version = lines[0].split(b" ", 2)
        except (ValueError, IndexError) as exc:
            raise EgressProxyError(400, "Malformed HTTP request") from exc
        if not HTTP_TOKEN_RE.fullmatch(method) or version not in (b"HTTP/1.0", b"HTTP/1.1"):
            raise EgressProxyError(400, "Malformed HTTP request line")
        headers = []
        seen_framing = set()
        for line in lines[1:]:
            if line[:1] in (b" ", b"\t") or b":" not in line:
                raise EgressProxyError(400, "Malformed HTTP header")
            name, value = line.split(b":", 1)
            name = name.strip().lower()
            value = value.strip()
            if not HTTP_TOKEN_RE.fullmatch(name) or any(byte < 32 and byte != 9 for byte in value):
                raise EgressProxyError(400, "Invalid HTTP header")
            if name in {b"host", b"content-length", b"transfer-encoding"}:
                if name in seen_framing:
                    raise EgressProxyError(400, "Duplicate HTTP framing header")
                seen_framing.add(name)
            headers.append((name, value))
        if b"content-length" in seen_framing and b"transfer-encoding" in seen_framing:
            raise EgressProxyError(400, "Conflicting HTTP body framing")
        return method.upper(), target, version, headers

    @staticmethod
    def _parse_connect_target(target: bytes) -> tuple[str, int]:
        try:
            authority = target.decode("ascii")
            if authority.startswith("["):
                end = authority.index("]")
                if authority[end + 1:end + 2] != ":":
                    raise ValueError
                host = authority[1:end]
                port_text = authority[end + 2:]
            else:
                host, sep, port_text = authority.rpartition(":")
                if not sep:
                    raise ValueError
            port = int(port_text)
            ipaddress.ip_address(host) if ":" in host else None
        except (UnicodeDecodeError, ValueError, IndexError) as exc:
            raise EgressProxyError(400, "CONNECT target must be host:port") from exc
        if not host or not 1 <= port <= 65535:
            raise EgressProxyError(400, "Invalid CONNECT target")
        return host, port

    @staticmethod
    def _parse_http_target(target: bytes) -> tuple[str, int, bytes]:
        try:
            parsed = urlsplit(target.decode("ascii"))
            port = parsed.port
        except (UnicodeDecodeError, ValueError) as exc:
            raise EgressProxyError(400, "Invalid HTTP proxy target") from exc
        if parsed.scheme.lower() != "http" or not parsed.hostname:
            raise EgressProxyError(400, "HTTP requests must use an absolute http:// target")
        if parsed.username or parsed.password or parsed.fragment:
            raise EgressProxyError(400, "Proxy target must not contain credentials or a fragment")
        port = port if port is not None else 80
        if not 1 <= port <= 65535:
            raise EgressProxyError(400, "Invalid target port")
        target_path = parsed.path or "/"
        if parsed.query:
            target_path += "?" + parsed.query
        return parsed.hostname, port, target_path.encode("ascii")

    @staticmethod
    def _origin_headers(headers):
        connection_tokens = set()
        for name, value in headers:
            if name == b"connection":
                connection_tokens.update(token.strip().lower() for token in value.split(b","))
        strip = _HOP_BY_HOP_HEADERS | connection_tokens
        strip -= {b"host", b"content-length", b"transfer-encoding"}
        return [(name, value) for name, value in headers if name not in strip]

    @staticmethod
    async def _copy_http_request_body(reader, writer, headers):
        framing = {name: value for name, value in headers if name in {b"content-length", b"transfer-encoding"}}
        if b"content-length" in framing:
            try:
                remaining = int(framing[b"content-length"])
            except ValueError as exc:
                raise EgressProxyError(400, "Invalid Content-Length") from exc
            if remaining < 0:
                raise EgressProxyError(400, "Invalid Content-Length")
            while remaining:
                chunk = await reader.readexactly(min(64 * 1024, remaining))
                writer.write(chunk)
                await writer.drain()
                remaining -= len(chunk)
            return

        transfer_encoding = framing.get(b"transfer-encoding")
        if transfer_encoding is None:
            return
        if transfer_encoding.lower() != b"chunked":
            raise EgressProxyError(501, "Unsupported Transfer-Encoding")

        trailer_bytes = 0
        while True:
            line = await reader.readline()
            if not line.endswith(b"\r\n") or len(line) > 8192:
                raise EgressProxyError(400, "Malformed chunked request body")
            try:
                size = int(line[:-2].split(b";", 1)[0], 16)
            except ValueError as exc:
                raise EgressProxyError(400, "Malformed chunk size") from exc
            if size < 0:
                raise EgressProxyError(400, "Malformed chunk size")
            writer.write(line)
            if size == 0:
                while True:
                    trailer = await reader.readline()
                    trailer_bytes += len(trailer)
                    if not trailer.endswith(b"\r\n") or trailer_bytes > MAX_HEADER_BYTES:
                        raise EgressProxyError(400, "Malformed chunk trailers")
                    writer.write(trailer)
                    if trailer == b"\r\n":
                        await writer.drain()
                        return
            else:
                chunk = await reader.readexactly(size + 2)
                if not chunk.endswith(b"\r\n"):
                    raise EgressProxyError(400, "Malformed chunk framing")
                writer.write(chunk)
            await writer.drain()

    @staticmethod
    async def _relay_response(upstream_reader, client_writer):
        while True:
            data = await upstream_reader.read(64 * 1024)
            if not data:
                try:
                    client_writer.write_eof()
                    await client_writer.drain()
                except (AttributeError, ConnectionError, OSError):
                    pass
                return
            client_writer.write(data)
            await client_writer.drain()

    async def _relay_http_response(self, client_reader, client_writer, upstream_reader, upstream_writer):
        async def watch_client_disconnect():
            while await client_reader.read(64 * 1024):
                # This proxy closes the client connection after one response, so pipelined bytes
                # are discarded while an EOF signals cancellation or a disconnected peer.
                continue

        response_task = asyncio.create_task(self._relay_response(upstream_reader, client_writer))
        disconnect_task = asyncio.create_task(watch_client_disconnect())
        try:
            done, pending = await asyncio.wait(
                {response_task, disconnect_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if disconnect_task in done and response_task not in done:
                upstream_writer.close()
                response_task.cancel()
            for task in pending:
                task.cancel()
            await asyncio.gather(*done, *pending, return_exceptions=True)
        finally:
            if not upstream_writer.is_closing():
                upstream_writer.close()

    async def _open_validated(self, host: str, port: int):
        validated = await resolve_target_host_async(
            host, port, resolver=self._resolver, label="Egress destination"
        )
        last_error = None
        for info in validated.addresses:
            try:
                return await asyncio.wait_for(self._connector(info), timeout=15)
            except (OSError, asyncio.TimeoutError) as exc:
                last_error = exc
        raise OSError("All validated destination addresses failed") from last_error

    @staticmethod
    async def _relay(client_reader, client_writer, upstream_reader, upstream_writer):
        async def copy(reader, writer):
            while True:
                data = await reader.read(64 * 1024)
                if not data:
                    try:
                        writer.write_eof()
                        await writer.drain()
                    except (AttributeError, ConnectionError, OSError):
                        pass
                    return
                writer.write(data)
                await writer.drain()

        tasks = {
            asyncio.create_task(copy(client_reader, upstream_writer)),
            asyncio.create_task(copy(upstream_reader, client_writer)),
        }
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*done, *pending, return_exceptions=True)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    async def _write_error(writer, status: int, message: str):
        if writer.is_closing():
            return
        try:
            reason = HTTPStatus(status).phrase
        except ValueError:
            reason = "Proxy Error"
        body = (message + "\n").encode("utf-8")
        writer.write(
            f"HTTP/1.1 {status} {reason}\r\n".encode("ascii")
            + b"Content-Type: text/plain; charset=utf-8\r\n"
            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("ascii")
            + body
        )
        try:
            await writer.drain()
        except (ConnectionError, OSError):
            pass


egress_proxy = EgressProxyServer(port=settings.EGRESS_PROXY_PORT)
