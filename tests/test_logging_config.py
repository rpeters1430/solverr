import io
import logging
import unittest

from app.logging_config import CustomLogFormatter, RequestIdFilter, sanitize_proxy_url


class TestCredentialMasking(unittest.TestCase):
    def test_sanitize_masks_only_the_password(self):
        self.assertEqual(sanitize_proxy_url("http://user:secret@host:8080"), "http://user:***@host:8080")

    def test_free_text_after_a_url_is_not_swallowed(self):
        text = "connect to http://host:8080 failed; mail admin@example.com"
        self.assertEqual(sanitize_proxy_url(text), text)

    def test_formatter_masks_messages_and_tracebacks(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(CustomLogFormatter("%(req_prefix)s%(message)s"))
        handler.addFilter(RequestIdFilter())
        logger = logging.getLogger("solverr.test.masking")
        logger.addHandler(handler)
        logger.propagate = False
        try:
            try:
                raise ConnectionError("proxy http://bob:hunter2@proxy.test:3128 refused")
            except ConnectionError as e:
                logger.error(f"failed: {e}", exc_info=True)
        finally:
            logger.removeHandler(handler)
        output = stream.getvalue()
        self.assertNotIn("hunter2", output)
        self.assertIn("http://bob:***@proxy.test:3128", output)
        self.assertIn("Traceback", output)


if __name__ == "__main__":
    unittest.main()
