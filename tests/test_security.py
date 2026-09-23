import unittest
from unittest.mock import patch
from app.config import settings
from app.security import check_target_url, check_target_url_async, SSRFBlockedError


class TestSSRFProtection(unittest.TestCase):
    def test_public_host_allowed(self):
        check_target_url("https://example.com/path")  # must not raise

    def test_loopback_blocked(self):
        with self.assertRaises(SSRFBlockedError):
            check_target_url("http://127.0.0.1:8000/")

    def test_localhost_hostname_blocked(self):
        with self.assertRaises(SSRFBlockedError):
            check_target_url("http://localhost/")

    def test_private_rfc1918_blocked(self):
        with self.assertRaises(SSRFBlockedError):
            check_target_url("http://192.168.1.10/")

    def test_link_local_metadata_ip_blocked(self):
        with self.assertRaises(SSRFBlockedError):
            check_target_url("http://169.254.169.254/latest/meta-data/")

    def test_allow_private_networks_override(self):
        with patch.object(settings, "ALLOW_PRIVATE_NETWORKS", True):
            check_target_url("http://127.0.0.1/")  # must not raise

    def test_unresolvable_host_blocked_sync(self):
        # A DNS lookup failure must fail closed, not be treated as "no
        # resolvable IP to check, so allow it through".
        with self.assertRaises(SSRFBlockedError):
            check_target_url("http://this-host-does-not-exist.invalid/")

    def test_unresolvable_host_blocked_async(self):
        import asyncio
        with self.assertRaises(SSRFBlockedError):
            asyncio.run(check_target_url_async("http://this-host-does-not-exist.invalid/"))

    def test_denied_hosts_wins_over_private_network_override(self):
        with patch.object(settings, "ALLOW_PRIVATE_NETWORKS", True), \
             patch.object(settings, "DENIED_HOSTS", {"example.com"}):
            with self.assertRaises(SSRFBlockedError):
                check_target_url("https://example.com/")

    def test_allowed_hosts_overrides_block(self):
        with patch.object(settings, "ALLOWED_HOSTS", {"127.0.0.1"}):
            check_target_url("http://127.0.0.1/")  # must not raise

    def test_denied_hosts_blocks_public_host(self):
        with patch.object(settings, "DENIED_HOSTS", {"example.com"}):
            with self.assertRaises(SSRFBlockedError):
                check_target_url("https://example.com/")

    def test_unresolvable_host_is_rejected(self):
        with self.assertRaises(SSRFBlockedError):
            check_target_url("http://this-host-does-not-exist.invalid/")

    def test_scheme_less_loopback_blocked(self):
        # urlparse() leaves .hostname empty without "//", so bare host:port proxies need their own check.
        with self.assertRaises(SSRFBlockedError):
            check_target_url("127.0.0.1:8080")

    def test_scheme_less_public_host_allowed(self):
        check_target_url("example.com:8080")  # must not raise


class TestAsyncSSRFProtection(unittest.IsolatedAsyncioTestCase):
    async def test_async_loopback_blocked(self):
        with self.assertRaises(SSRFBlockedError):
            await check_target_url_async("http://127.0.0.1/private", label="Redirect target")

    async def test_async_public_resolution_allowed(self):
        with patch("asyncio.BaseEventLoop.getaddrinfo", return_value=[
            (2, 1, 6, "", ("93.184.216.34", 0))
        ]):
            await check_target_url_async("https://example.com/")


class TestProxySSRFProtection(unittest.IsolatedAsyncioTestCase):
    """The `proxy` field is just as capable of reaching internal targets as
    `url` (it becomes the actual solve egress point), so it must be checked
    too - see HybridSolverEngine.process_request."""

    async def test_internal_proxy_blocked(self):
        from unittest.mock import AsyncMock, patch
        from app.models.flaresolverr import V1Request
        from app.solver.engine import HybridSolverEngine

        engine = HybridSolverEngine()
        req = V1Request(cmd="request.get", url="https://example.com", proxy={"url": "http://127.0.0.1:6379"})
        with patch("app.solver.engine.fast_tls_engine.request", new=AsyncMock()) as fast_mock:
            with self.assertRaises(SSRFBlockedError):
                await engine.process_request(req)
            fast_mock.assert_not_called()

    async def test_internal_proxy_via_metadata_ip_blocked(self):
        from unittest.mock import AsyncMock, patch
        from app.models.flaresolverr import V1Request
        from app.solver.engine import HybridSolverEngine

        engine = HybridSolverEngine()
        req = V1Request(cmd="request.get", url="https://example.com", proxy="http://169.254.169.254/")
        with patch("app.solver.engine.fast_tls_engine.request", new=AsyncMock()) as fast_mock:
            with self.assertRaises(SSRFBlockedError):
                await engine.process_request(req)
            fast_mock.assert_not_called()

    async def test_scheme_less_internal_proxy_blocked(self):
        from unittest.mock import AsyncMock, patch
        from app.models.flaresolverr import V1Request
        from app.solver.engine import HybridSolverEngine

        engine = HybridSolverEngine()
        req = V1Request(cmd="request.get", url="https://example.com", proxy="127.0.0.1:6379")
        with patch("app.solver.engine.fast_tls_engine.request", new=AsyncMock()) as fast_mock:
            with self.assertRaises(SSRFBlockedError):
                await engine.process_request(req)
            fast_mock.assert_not_called()

    async def test_public_proxy_allowed(self):
        from unittest.mock import AsyncMock, patch
        from app.models.flaresolverr import V1Request
        from app.solver.engine import HybridSolverEngine

        engine = HybridSolverEngine()
        # NXDOMAIN counts as blocked, so this needs a host that really resolves.
        req = V1Request(cmd="request.get", url="https://example.com", proxy="http://example.com:8080")
        # Stub every tier so only the SSRF check is under test.
        with patch("app.solver.engine.fast_tls_engine.request", new=AsyncMock(return_value=(False, None))) as fast_mock, \
             patch("app.solver.engine.browser_pool.solve", new=AsyncMock(side_effect=RuntimeError("browser should not be reached in this test"))) as browser_mock, \
             patch.object(settings, "FALLBACK_PROXY_URL", None):
            with self.assertRaises(RuntimeError):
                await engine.process_request(req)
            fast_mock.assert_called_once()
            browser_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
