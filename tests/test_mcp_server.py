import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.mcp_server import _mcp_transport_security
from app.solver.cache import cookie_cache

MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _rpc(client, method, params=None, req_id=1, headers=None):
    payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
    return client.post("/mcp/", json=payload, headers=headers or MCP_HEADERS)


class TestMCPServer(unittest.TestCase):
    # A shared class-level client: the MCP session manager's run() context
    # (entered by app/main.py's lifespan) can only be entered once per
    # process, so the app's lifespan must start/stop exactly once across
    # this whole test class rather than per test method.
    @classmethod
    def setUpClass(cls):
        # base_url matters here: with no API_KEY configured (the default in
        # this test environment), app/mcp_server.py's DNS-rebinding
        # protection stays on and localhost-only by default (see
        # _mcp_transport_security()) - the default TestClient Host header
        # ("testserver") would otherwise get a 421.
        cls.client = TestClient(app, base_url="http://localhost:8191")
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def test_tools_are_registered(self):
        resp = _rpc(self.client, "tools/list")
        self.assertEqual(resp.status_code, 200)
        names = {t["name"] for t in resp.json()["result"]["tools"]}
        self.assertEqual(
            names,
            {"solverr_scrape", "solverr_screenshot", "solverr_get_cookies", "solverr_get_stats"},
        )

    def test_get_cookies_reads_the_shared_cache_without_a_network_request(self):
        from app.models.flaresolverr import CookieModel

        cookie_cache.set_cookies(
            "https://mcp-test.example.com",
            [CookieModel(name="cf_clearance", value="mcp_test_val", domain=".mcp-test.example.com")],
        )
        try:
            resp = _rpc(
                self.client,
                "tools/call",
                {"name": "solverr_get_cookies", "arguments": {"domain": "mcp-test.example.com"}},
            )
            self.assertEqual(resp.status_code, 200)
            content = resp.json()["result"]["content"][0]["text"]
            self.assertIn("cf_clearance", content)
            self.assertIn("mcp_test_val", content)
        finally:
            cookie_cache._store.pop("mcp-test.example.com", None)

    def test_get_stats_reports_engine_and_pool_health(self):
        resp = _rpc(self.client, "tools/call", {"name": "solverr_get_stats", "arguments": {}})
        self.assertEqual(resp.status_code, 200)
        stats = json.loads(resp.json()["result"]["content"][0]["text"])
        self.assertIn("total_requests", stats)
        self.assertIn("challenges_solved", stats)
        self.assertIn("aws_waf", stats["challenges_solved"])
        self.assertIn("browser_pool", stats)

    def test_endpoint_is_gated_by_api_key_like_every_other_route(self):
        with patch.object(settings, "API_KEY", "test-mcp-key"):
            unauthenticated = _rpc(self.client, "tools/list")
            self.assertEqual(unauthenticated.status_code, 401)

            authenticated = _rpc(
                self.client, "tools/list", headers={**MCP_HEADERS, "x-api-key": "test-mcp-key"}
            )
            self.assertEqual(authenticated.status_code, 200)

    def test_scrape_rejects_unsupported_methods_instead_of_silently_using_get(self):
        resp = _rpc(
            self.client,
            "tools/call",
            {"name": "solverr_scrape", "arguments": {"url": "https://example.com", "method": "PUT"}},
        )
        self.assertEqual(resp.status_code, 200)
        result = resp.json()["result"]
        self.assertTrue(result.get("isError"))
        self.assertIn("Unsupported method", result["content"][0]["text"])

    def test_scrape_failure_does_not_leak_the_raw_exception_to_the_caller(self):
        # Mirrors app/main.py's own no-raw-exception policy: a solver
        # exception can carry internal paths or proxy credentials, so only
        # a sanitized message may reach the MCP client.
        secret = "proxy://user:hunter2@internal-proxy.corp:8080 /etc/shadow"
        with patch(
            "app.mcp_server.solver_engine.process_request",
            new=AsyncMock(side_effect=RuntimeError(secret)),
        ):
            resp = _rpc(
                self.client,
                "tools/call",
                {"name": "solverr_scrape", "arguments": {"url": "https://example.com"}},
            )
        self.assertEqual(resp.status_code, 200)
        result = resp.json()["result"]
        self.assertTrue(result.get("isError"))
        text = result["content"][0]["text"]
        self.assertNotIn(secret, text)
        self.assertIn("Scrape failed", text)

    def test_untrusted_host_is_rejected_without_an_api_key(self):
        # No API_KEY is configured in this test process, so
        # _mcp_transport_security() keeps DNS-rebinding/Host-header
        # protection on and localhost-only (see app/mcp_server.py) - a
        # non-local Host must be rejected even though the request is
        # otherwise well-formed and reaches the class's own localhost:8191
        # client fine (see test_tools_are_registered).
        resp = _rpc(self.client, "tools/list", headers={**MCP_HEADERS, "host": "evil.example.com"})
        self.assertEqual(resp.status_code, 421)


class TestMCPTransportSecurityDecision(unittest.TestCase):
    """Unit-tests _mcp_transport_security()'s branching directly, since the
    mounted ASGI app (tested above) bakes in whatever settings.API_KEY was
    at process/import time and can't be rebuilt per-test."""

    def test_disabled_when_api_key_is_set(self):
        with patch.object(settings, "API_KEY", "some-key"):
            ts = _mcp_transport_security()
        self.assertFalse(ts.enable_dns_rebinding_protection)

    def test_local_only_by_default_without_an_api_key(self):
        with patch.object(settings, "API_KEY", None), \
             patch.object(settings, "MCP_ALLOWED_HOSTS", []), \
             patch.object(settings, "MCP_ALLOWED_ORIGINS", []):
            ts = _mcp_transport_security()
        self.assertTrue(ts.enable_dns_rebinding_protection)
        self.assertIn("localhost:*", ts.allowed_hosts)
        self.assertIn("127.0.0.1:*", ts.allowed_hosts)

    def test_explicit_allowed_hosts_extend_rather_than_replace_localhost_defaults(self):
        # An operator adding a real deployment hostname still expects
        # localhost to keep working for local testing/debugging.
        with patch.object(settings, "API_KEY", None), \
             patch.object(settings, "MCP_ALLOWED_HOSTS", ["my-nas.local:8191"]), \
             patch.object(settings, "MCP_ALLOWED_ORIGINS", []):
            ts = _mcp_transport_security()
        self.assertIn("my-nas.local:8191", ts.allowed_hosts)
        self.assertIn("localhost:*", ts.allowed_hosts)
        self.assertIn("127.0.0.1:*", ts.allowed_hosts)


if __name__ == "__main__":
    unittest.main()
