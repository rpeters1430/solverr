import asyncio
import socket
import unittest

import httpx

from app.egress_proxy import EgressProxyError, EgressProxyServer


class TestEgressProxyDNSPinning(unittest.IsolatedAsyncioTestCase):
    def test_rejects_zero_port_in_absolute_http_target(self):
        with self.assertRaises(EgressProxyError):
            EgressProxyServer._parse_http_target(b"http://example.test:0/path")

    def test_connect_ipv6_authority_requires_a_colon_before_the_port(self):
        with self.assertRaises(EgressProxyError):
            EgressProxyServer._parse_connect_target(b"[2001:db8::1]443")

    async def test_redirect_to_private_host_is_revalidated_by_the_proxy(self):
        resolved_hosts = []
        connected = 0

        async def origin(reader, writer):
            request = await reader.readuntil(b"\r\n\r\n")
            if b"GET /redirect " in request:
                response = (
                    b"HTTP/1.1 302 Found\r\nLocation: http://private.test/secret\r\n"
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
            else:
                response = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
            writer.write(response)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        origin_server = await asyncio.start_server(origin, "127.0.0.1", 0)
        origin_port = origin_server.sockets[0].getsockname()[1]

        async def resolver(host, port, **kwargs):
            resolved_hosts.append(host)
            ip = "93.184.216.34" if host == "redirect.test" else "127.0.0.1"
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))]

        async def connector(_info):
            nonlocal connected
            connected += 1
            return await asyncio.open_connection("127.0.0.1", origin_port)

        proxy = EgressProxyServer(port=0, resolver=resolver, connector=connector)
        await proxy.start()
        try:
            async with httpx.AsyncClient(proxy=proxy.url, trust_env=False, follow_redirects=True, timeout=1) as client:
                response = await client.get("http://redirect.test/redirect")
            self.assertEqual(response.status_code, 403)
            self.assertEqual(resolved_hosts, ["redirect.test", "private.test"])
            self.assertEqual(connected, 1)
        finally:
            await proxy.close()
            origin_server.close()
            await origin_server.wait_closed()

    async def test_concurrent_same_host_requests_each_use_a_validated_resolution(self):
        resolutions = 0
        connections = 0
        lock = asyncio.Lock()

        async def origin(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        origin_server = await asyncio.start_server(origin, "127.0.0.1", 0)
        origin_port = origin_server.sockets[0].getsockname()[1]

        async def resolver(host, port, **kwargs):
            nonlocal resolutions
            async with lock:
                resolutions += 1
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port))]

        async def connector(_info):
            nonlocal connections
            async with lock:
                connections += 1
            return await asyncio.open_connection("127.0.0.1", origin_port)

        proxy = EgressProxyServer(port=0, resolver=resolver, connector=connector)
        await proxy.start()
        try:
            async with httpx.AsyncClient(
                proxy=proxy.url, trust_env=False, timeout=2,
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=0),
            ) as client:
                responses = await asyncio.gather(*(
                    client.get(f"http://same.test/{index}") for index in range(4)
                ))
            self.assertEqual([response.status_code for response in responses], [200] * 4)
            self.assertEqual(resolutions, 4)
            self.assertEqual(connections, 4)
        finally:
            await proxy.close()
            origin_server.close()
            await origin_server.wait_closed()

    async def test_connect_establishes_an_end_to_end_tunnel(self):
        async def echo(reader, writer):
            data = await reader.read(4)
            writer.write(data)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        origin = await asyncio.start_server(echo, "127.0.0.1", 0)
        origin_port = origin.sockets[0].getsockname()[1]

        async def resolver(host, port, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port))]

        async def connector(_info):
            return await asyncio.open_connection("127.0.0.1", origin_port)

        proxy = EgressProxyServer(port=0, resolver=resolver, connector=connector)
        await proxy.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            writer.write(b"CONNECT example.test:443 HTTP/1.1\r\nHost: example.test:443\r\n\r\n")
            await writer.drain()
            response = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=1)
            self.assertIn(b"200 Connection Established", response)
            writer.write(b"ping")
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.readexactly(4), timeout=1), b"ping")
            writer.close()
            await writer.wait_closed()
        finally:
            await proxy.close()
            origin.close()
            await origin.wait_closed()

    async def test_http_absolute_form_is_forwarded_to_the_pinned_socket(self):
        received = asyncio.Future()
        origin_port = 0

        async def origin(reader, writer):
            request = await reader.readuntil(b"\r\n\r\n")
            received.set_result(request)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        origin_server = await asyncio.start_server(origin, "127.0.0.1", 0)
        origin_port = origin_server.sockets[0].getsockname()[1]

        async def resolver(host, port, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port))]

        async def connector(_info):
            return await asyncio.open_connection("127.0.0.1", origin_port)

        proxy = EgressProxyServer(port=0, resolver=resolver, connector=connector)
        await proxy.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            writer.write(
                b"GET http://example.test:8080/a?q=1 HTTP/1.1\r\n"
                b"Host: wrong.test\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), timeout=1)
            forwarded = await asyncio.wait_for(received, timeout=1)
            await asyncio.sleep(0.01)
            self.assertEqual(len(proxy._clients), 0)
            writer.close()
            await writer.wait_closed()

            self.assertIn(b"200 OK", response)
            self.assertTrue(response.endswith(b"ok"))
            self.assertTrue(forwarded.startswith(b"GET /a?q=1 HTTP/1.1\r\n"))
            self.assertIn(b"host: example.test:8080\r\n", forwarded.lower())
        finally:
            await proxy.close()
            origin_server.close()
            await origin_server.wait_closed()

    async def test_cancelled_http_client_closes_the_pinned_upstream(self):
        upstream_closed = asyncio.Event()
        upstream_started = asyncio.Event()

        async def origin(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            upstream_started.set()
            await reader.read()
            upstream_closed.set()
            writer.close()
            await writer.wait_closed()

        origin_server = await asyncio.start_server(origin, "127.0.0.1", 0)
        origin_port = origin_server.sockets[0].getsockname()[1]

        async def resolver(host, port, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port))]

        async def connector(_info):
            return await asyncio.open_connection("127.0.0.1", origin_port)

        proxy = EgressProxyServer(port=0, resolver=resolver, connector=connector)
        await proxy.start()
        try:
            _reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            writer.write(b"GET http://example.test/slow HTTP/1.1\r\nHost: example.test\r\n\r\n")
            await writer.drain()
            await asyncio.wait_for(upstream_started.wait(), timeout=0.5)
            writer.close()
            await writer.wait_closed()
            await asyncio.wait_for(upstream_closed.wait(), timeout=0.5)
            for _ in range(100):
                if not proxy._clients:
                    break
                await asyncio.sleep(0.005)
            self.assertEqual(len(proxy._clients), 0)
        finally:
            await proxy.close()
            origin_server.close()
            await origin_server.wait_closed()

    async def test_dns_change_to_loopback_does_not_reach_loopback_listener(self):
        accepted = asyncio.Event()
        local_server = await asyncio.start_server(
            lambda _reader, _writer: accepted.set(), "127.0.0.1", 0
        )
        local_port = local_server.sockets[0].getsockname()[1]
        resolver_calls = 0

        async def changing_resolver(host, port, **kwargs):
            nonlocal resolver_calls
            resolver_calls += 1
            ip = "93.184.216.34" if resolver_calls == 1 else "127.0.0.1"
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))]

        async def connector(info):
            address = info[4]
            if address[0] == "127.0.0.1":
                return await asyncio.open_connection(address[0], address[1])
            raise OSError("simulated unreachable public address")

        proxy = EgressProxyServer(port=0, resolver=changing_resolver, connector=connector)
        await proxy.start()
        proxy_port = proxy.port
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
            writer.write(
                f"GET http://rebind.test:{local_port}/private HTTP/1.1\r\n"
                "Host: rebind.test\r\nConnection: close\r\n\r\n".encode()
            )
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), timeout=1)
            writer.close()
            await writer.wait_closed()

            self.assertIn(b"502 Bad Gateway", response)
            self.assertEqual(resolver_calls, 1)
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(accepted.wait(), timeout=0.05)
        finally:
            await proxy.close()
            local_server.close()
            await local_server.wait_closed()

    async def test_resolution_error_fails_closed_before_connecting(self):
        async def resolver(host, port, **kwargs):
            raise socket.gaierror("controlled resolver failure")

        async def connector(_info):
            self.fail("connector must not run after resolution failure")

        proxy = EgressProxyServer(port=0, resolver=resolver, connector=connector)
        await proxy.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            writer.write(b"GET http://missing.test/ HTTP/1.1\r\nHost: missing.test\r\n\r\n")
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), timeout=1)
            writer.close()
            await writer.wait_closed()
            self.assertIn(b"403 Forbidden", response)
        finally:
            await proxy.close()
