import unittest
from contextlib import contextmanager
from unittest.mock import patch
import httpx
from app.solver.captcha_solver import CaptchaSolverClient


@contextmanager
def mocked_http(handler):
    """Patch httpx.AsyncClient so every instantiation routes through a
    MockTransport - no real network calls, fully offline test."""
    real_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_async_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    with patch("httpx.AsyncClient", side_effect=factory):
        yield


def make_client() -> CaptchaSolverClient:
    client = CaptchaSolverClient()
    client.api_key = "test-api-key"
    client.base_url = "https://fake-solver.test"
    client.poll_interval = 0  # no real waiting in tests
    client.timeout = 5
    return client


class TestCaptchaSolverClient(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_without_api_key_returns_none_immediately(self):
        client = CaptchaSolverClient()
        client.api_key = None
        result = await client.solve_recaptcha_v2("sitekey123", "https://example.com")
        self.assertIsNone(result)

    async def test_successful_submit_and_poll_returns_token(self):
        calls = {"in": 0, "res": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/in.php":
                calls["in"] += 1
                return httpx.Response(200, json={"status": 1, "request": "task-42"})
            calls["res"] += 1
            return httpx.Response(200, json={"status": 1, "request": "SOLVED_TOKEN"})

        client = make_client()
        with mocked_http(handler):
            token = await client.solve_recaptcha_v2("sitekey123", "https://example.com")
        self.assertEqual(token, "SOLVED_TOKEN")
        self.assertEqual(calls["in"], 1)
        self.assertGreaterEqual(calls["res"], 1)

    async def test_submit_rejected_returns_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": 0, "request": "ERROR_WRONG_USER_KEY"})

        client = make_client()
        with mocked_http(handler):
            token = await client.solve_hcaptcha("sitekey123", "https://example.com")
        self.assertIsNone(token)

    async def test_poll_failure_returns_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/in.php":
                return httpx.Response(200, json={"status": 1, "request": "task-99"})
            return httpx.Response(200, json={"status": 0, "request": "ERROR_CAPTCHA_UNSOLVABLE"})

        client = make_client()
        with mocked_http(handler):
            token = await client.solve_turnstile("sitekey123", "https://example.com")
        self.assertIsNone(token)

    async def test_not_ready_then_solved(self):
        state = {"polls": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/in.php":
                return httpx.Response(200, json={"status": 1, "request": "task-7"})
            state["polls"] += 1
            if state["polls"] < 3:
                return httpx.Response(200, json={"status": 0, "request": "CAPCHA_NOT_READY"})
            return httpx.Response(200, json={"status": 1, "request": "FINAL_TOKEN"})

        client = make_client()
        with mocked_http(handler):
            token = await client.solve_recaptcha_v2("sitekey123", "https://example.com")
        self.assertEqual(token, "FINAL_TOKEN")
        self.assertEqual(state["polls"], 3)

    async def test_enabled_property_reflects_api_key(self):
        client = CaptchaSolverClient()
        client.api_key = None
        self.assertFalse(client.enabled)
        client.api_key = "abc"
        self.assertTrue(client.enabled)


if __name__ == "__main__":
    unittest.main()
