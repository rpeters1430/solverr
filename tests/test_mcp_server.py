import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
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
        cls.client = TestClient(app)
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


if __name__ == "__main__":
    unittest.main()
