import unittest
from app.models.flaresolverr import V1Request, ProxyConfig, ScrapeRequest, ScrapeResponse, SolutionModel

class TestV1RequestModels(unittest.TestCase):
    def test_proxy_string_normalization(self):
        req = V1Request(cmd="request.get", url="http://example.com", proxy="http://user:pass@proxy.com:8080")
        self.assertEqual(req.get_proxy_url(), "http://user:pass@proxy.com:8080")

    def test_proxy_dict_normalization(self):
        req1 = V1Request(cmd="request.get", url="http://example.com", proxy={"url": "http://proxy.com:8080"})
        self.assertEqual(req1.get_proxy_url(), "http://proxy.com:8080")

        req2 = V1Request(cmd="request.get", url="http://example.com", proxy={"host": "1.2.3.4", "port": 8080, "username": "usr", "password": "pwd"})
        self.assertEqual(req2.get_proxy_url(), "http://usr:pwd@1.2.3.4:8080")

        req3 = V1Request(cmd="request.get", url="http://example.com", proxy={"url": "http://proxy.com:8080", "username": "usr", "password": "pwd"})
        self.assertEqual(req3.get_proxy_url(), "http://usr:pwd@proxy.com:8080")

        req4 = V1Request(cmd="request.get", url="http://example.com", proxy={"server": "http://proxy.com:8080", "username": "usr", "password": "pwd"})
        self.assertEqual(req4.get_proxy_url(), "http://usr:pwd@proxy.com:8080")

    def test_proxy_object_normalization(self):
        req = V1Request(cmd="request.get", url="http://example.com", proxy=ProxyConfig(url="http://proxy.com:8080"))
        self.assertEqual(req.get_proxy_url(), "http://proxy.com:8080")

    def test_custom_user_agent_and_headers(self):
        req = V1Request(
            cmd="request.get",
            url="http://example.com",
            userAgent="CustomUA/1.0",
            headers={"X-Custom-Header": "value123"},
            fastTlsOnly=True
        )
        self.assertEqual(req.userAgent, "CustomUA/1.0")
        self.assertEqual(req.headers, {"X-Custom-Header": "value123"})
        self.assertTrue(req.fastTlsOnly)

    def test_scrape_request_conversion(self):
        scrape_req = ScrapeRequest(
            url="https://nowsecure.nl",
            method="POST",
            postData='{"test": 1}',
            tier="tier3_browser",
            screenshot=True,
            wait_selector="#ready"
        )
        v1_req = scrape_req.to_v1_request()
        self.assertEqual(v1_req.cmd, "request.post")
        self.assertEqual(v1_req.url, "https://nowsecure.nl")
        self.assertTrue(v1_req.forceBrowser)
        self.assertTrue(v1_req.screenshot)
        self.assertEqual(v1_req.wait_selector, "#ready")

if __name__ == "__main__":
    unittest.main()
