import unittest
from unittest.mock import AsyncMock, patch

from app.forward_proxy import ForwardProxyServer
from app.models.flaresolverr import SolutionModel


class Writer:
    def __init__(self):
        self.data = bytearray()
    def is_closing(self):
        return False
    def write(self, data):
        self.data.extend(data)
    async def drain(self):
        pass


class ForwardProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.proxy = ForwardProxyServer()
        self.addAsyncCleanup(self.proxy.close)

    async def test_generated_failures_have_proxy_status(self):
        writer = Writer()
        await self.proxy._write_error(writer, 502, "Upstream failed")
        self.assertIn(b"Proxy-Status: Solverr; error=http_request_error\r\n", writer.data)

    async def test_cookie_header_is_imported_for_browser_escalation(self):
        captured = []
        async def solve(req):
            captured.extend(req.cookies or [])
            return SolutionModel(url=req.url, status=200, response="ok")
        with patch("app.forward_proxy.solver_engine.process_request", side_effect=solve):
            await self.proxy._solve_and_write("GET", "https://forum.test/topic", {"cookie": "login=private; preference=dark"}, None, Writer())
        self.assertEqual({c.name: c.value for c in captured}, {"login": "private", "preference": "dark"})
        self.assertTrue(all(c.domain == "forum.test" for c in captured))

    async def test_origin_errors_keep_their_status_without_proxy_error_marker(self):
        writer = Writer()
        with patch("app.forward_proxy.solver_engine.process_request", new=AsyncMock(return_value=SolutionModel(url="https://forum.test", status=503, response="maintenance"))):
            await self.proxy._solve_and_write("GET", "https://forum.test", {}, None, writer)
        self.assertTrue(writer.data.startswith(b"HTTP/1.1 503"))
        self.assertNotIn(b"Proxy-Status:", writer.data)
        self.assertTrue(writer.data.endswith(b"maintenance"))

    async def test_head_and_bodyless_statuses_keep_http_semantics(self):
        for method, status in (("HEAD", 200), ("GET", 204), ("GET", 304)):
            writer = Writer()
            with patch("app.forward_proxy.solver_engine.process_request", new=AsyncMock(return_value=SolutionModel(url="https://forum.test", status=status, response="must not be sent", headers={"content-length": "123"}))):
                await self.proxy._solve_and_write(method, "https://forum.test", {}, None, writer)
            head, body = bytes(writer.data).split(b"\r\n\r\n", 1)
            self.assertEqual(body, b"", (method, status))
            if status == 204:
                self.assertNotIn(b"content-length:", head.lower())
            else:
                self.assertIn(b"content-length: 123", head.lower())

    async def test_raw_upstream_does_not_inherit_other_clients_cookies(self):
        import httpx
        requests = []
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"ok"
        async def origin(request):
            requests.append(request)
            return httpx.Response(200, headers={"content-type": "text/plain", "set-cookie": "login=other; Path=/"}, stream=Stream())
        await self.proxy._upstream.aclose()
        self.proxy._upstream = httpx.AsyncClient(transport=httpx.MockTransport(origin))
        with patch("app.forward_proxy.check_target_url_async", new=AsyncMock()):
            for _ in range(2):
                await self.proxy._stream_upstream("GET", "https://forum.test/file", {}, None, Writer())
        self.assertNotIn("cookie", requests[1].headers)

    async def test_cookies_the_client_sent_are_not_echoed_back_as_set_cookie(self):
        from tests.test_engine import _cookie
        writer = Writer()
        solution = SolutionModel(url="https://forum.test", status=200, response="ok", headers={"content-type": "text/html"}, cookies=[
            _cookie("SID", "private", "forum.test"),
            _cookie("cf_clearance", "new", "forum.test"),
        ])
        with patch("app.forward_proxy.solver_engine.process_request", new=AsyncMock(return_value=solution)):
            await self.proxy._solve_and_write("GET", "https://forum.test", {"cookie": "SID=private"}, None, writer)
        head = bytes(writer.data).split(b"\r\n\r\n", 1)[0].lower()
        self.assertIn(b"set-cookie: cf_clearance=new", head)
        self.assertNotIn(b"set-cookie: sid=", head)

    def test_google_and_youtube_are_passthrough_hosts_by_default(self):
        for host in ("www.youtube.com", "youtube.com", "rr3---sn-abc.googlevideo.com", "accounts.google.com", "i.ytimg.com", "YOUTU.BE"):
            self.assertTrue(self.proxy._is_passthrough_host(host), host)
        for host in ("forum.test", "notyoutube.com", "youtube.com.evil.test"):
            self.assertFalse(self.proxy._is_passthrough_host(host), host)

    async def test_passthrough_connect_is_tunnelled_without_interception(self):
        import asyncio
        from app.config import settings

        async def origin(reader, writer):
            writer.write(await reader.readexactly(5))
            await writer.drain()
            writer.close()
        origin_server = await asyncio.start_server(origin, "127.0.0.1", 0)
        self.addAsyncCleanup(origin_server.wait_closed)
        self.addCleanup(origin_server.close)
        origin_port = origin_server.sockets[0].getsockname()[1]

        async def open_validated(host, port):
            self.assertEqual((host, port), ("www.youtube.com", 443))
            return await asyncio.open_connection("127.0.0.1", origin_port)

        with patch.object(settings, "MITM_PROXY_USERNAME", "u"), patch.object(settings, "MITM_PROXY_PASSWORD", "p"), \
             patch("app.forward_proxy.egress_proxy._open_validated", side_effect=open_validated), \
             patch.object(self.proxy.certificates, "issue_server_context") as issue:
            self.proxy._expected_auth = b"u:p"
            server = await asyncio.start_server(self.proxy._client_connected, "127.0.0.1", 0)
            self.addAsyncCleanup(server.wait_closed)
            self.addCleanup(server.close)
            reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
            writer.write(b"CONNECT www.youtube.com:443 HTTP/1.1\r\nHost: www.youtube.com:443\r\n"
                         b"Proxy-Authorization: Basic dTpw\r\n\r\n")
            await writer.drain()
            status = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            self.assertTrue(status.startswith(b"HTTP/1.1 200"))
            writer.write(b"hello")
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.readexactly(5), 5), b"hello")
            writer.close()
        issue.assert_not_called()
