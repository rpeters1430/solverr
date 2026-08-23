import unittest
from fastapi.testclient import TestClient
from app.main import app
from app.solver.human_cursor import generate_bezier_path

class TestAPIEndpoints(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_health_check(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        json_data = response.json()
        self.assertEqual(json_data.get("status"), "ok")
        self.assertIn("stealth_engine", json_data)

    def test_prometheus_metrics(self):
        response = self.client.get("/metrics")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/plain", response.headers.get("content-type", ""))
        body = response.text
        self.assertIn("solverr_requests_total", body)
        self.assertIn("solverr_active_workers", body)
        self.assertIn("solverr_memory_bytes", body)
        self.assertIn("solverr_fast_hit_rate_percent", body)

    def test_flaresolverr_sessions_flow(self):
        # 1. Create session
        res_create = self.client.post("/v1", json={
            "cmd": "sessions.create",
            "proxy": "http://user:secret@proxy.com:8080"
        })
        self.assertEqual(res_create.status_code, 200)
        data_create = res_create.json()
        self.assertEqual(data_create.get("status"), "ok")
        session_id = data_create.get("session")
        self.assertIsNotNone(session_id)

        # 2. List sessions
        res_list = self.client.post("/v1", json={"cmd": "sessions.list"})
        self.assertEqual(res_list.status_code, 200)
        data_list = res_list.json()
        self.assertIn(session_id, data_list.get("sessions", []))

        # 3. Destroy session
        res_destroy = self.client.post("/v1", json={
            "cmd": "sessions.destroy",
            "session": session_id
        })
        self.assertEqual(res_destroy.status_code, 200)
        self.assertEqual(res_destroy.json().get("status"), "ok")

    def test_dashboard_api(self):
        res_stats = self.client.get("/api/stats")
        self.assertEqual(res_stats.status_code, 200)
        json_stats = res_stats.json()
        self.assertIn("ram_usage_mb", json_stats)
        self.assertIn("tier1_fast_tls_hits", json_stats)
        self.assertIn("cache_backend", json_stats)

        res_cookies = self.client.get("/api/cookies")
        self.assertEqual(res_cookies.status_code, 200)

        res_sessions = self.client.get("/api/sessions")
        self.assertEqual(res_sessions.status_code, 200)

    def test_dashboard_delete_session(self):
        res_create = self.client.post("/v1", json={"cmd": "sessions.create"})
        sid = res_create.json().get("session")
        
        res_del = self.client.delete(f"/api/sessions/{sid}")
        self.assertEqual(res_del.status_code, 200)
        self.assertEqual(res_del.json().get("status"), "ok")

    def test_flaresolverr_v2_endpoint(self):
        res_list = self.client.post("/v2", json={"cmd": "sessions.list"})
        self.assertEqual(res_list.status_code, 200)
        self.assertEqual(res_list.json().get("status"), "ok")

    def test_human_cursor_bezier_path(self):
        path = generate_bezier_path((100, 100), (400, 300), steps=20)
        self.assertGreaterEqual(len(path), 20)
        self.assertEqual(path[-1], (400, 300))

    def test_api_key_auth_modes(self):
        from unittest.mock import patch
        from app.config import settings
        with patch.object(settings, "API_KEY", "secret123"):
            # Missing key -> 401
            res_unauth = self.client.get("/api/stats")
            self.assertEqual(res_unauth.status_code, 401)

            # Header x-api-key -> 200
            res_hdr = self.client.get("/api/stats", headers={"x-api-key": "secret123"})
            self.assertEqual(res_hdr.status_code, 200)

            # Bearer token -> 200
            res_bearer = self.client.get("/api/stats", headers={"authorization": "Bearer secret123"})
            self.assertEqual(res_bearer.status_code, 200)

            # Query param api_key -> 200
            res_param = self.client.get("/api/stats?api_key=secret123")
            self.assertEqual(res_param.status_code, 200)

            # Query param key -> 200
            res_key = self.client.get("/api/stats?key=secret123")
            self.assertEqual(res_key.status_code, 200)

            # Health and metrics remain unauthenticated
            res_health = self.client.get("/health")
            self.assertEqual(res_health.status_code, 200)

    def test_transparent_proxy_query_handling(self):
        from unittest.mock import patch, AsyncMock
        from app.models.flaresolverr import SolutionModel
        fake_sol = SolutionModel(
            url="https://example.com/api?a=1&b=2",
            status=200,
            headers={"content-type": "application/json; charset=utf-8"},
            response='{"ok": true}'
        )
        with patch("app.api.flaresolverr.solver_engine.process_request", new=AsyncMock(return_value=fake_sol)) as mock_proc:
            res = self.client.get("/proxy?url=https://example.com/api?a=1&b=2")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.text, '{"ok": true}')
            self.assertIn("application/json", res.headers.get("content-type", ""))
            # Verify the full target URL with all params was passed to solver_engine
            called_req = mock_proc.call_args[0][0]
            self.assertEqual(called_req.url, "https://example.com/api?a=1&b=2")

    def test_export_cookies_endpoint(self):
        # Test Netscape export
        res_netscape = self.client.get("/api/cookies/export?format=netscape")
        self.assertEqual(res_netscape.status_code, 200)
        self.assertIn("text/plain", res_netscape.headers.get("content-type", ""))
        self.assertIn("# Netscape HTTP Cookie File", res_netscape.text)

        # Test JSON export
        res_json = self.client.get("/api/cookies/export?format=json")
        self.assertEqual(res_json.status_code, 200)
        self.assertIn("domains", res_json.json())


if __name__ == "__main__":
    unittest.main()
