import os
import tempfile
import unittest

from app.history import RequestHistory


class TestRequestHistory(unittest.IsolatedAsyncioTestCase):
    async def test_persists_bounded_sanitized_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "history.sqlite")
            history = RequestHistory(path, limit=2)
            await history.record("https://user:secret@example.com/private?token=sensitive", "tier1_fast_tls", "success", 12, 200)
            await history.record("https://example.net/a", "tier3_stealth_browser", "failed", 30,
                                 failure_type="TimeoutError")
            await history.record("https://example.org/b", "tier2_cache", "success", 4, 200)
            reopened = RequestHistory(path)
            records = await reopened.recent()
            self.assertEqual([row["domain"] for row in records], ["example.org", "example.net"])
            self.assertEqual((await reopened.recent(outcome="failed"))[0]["failure_type"], "TimeoutError")
            with open(path, "rb") as db:
                contents = db.read()
            self.assertNotIn(b"sensitive", contents)
            self.assertNotIn(b"secret", contents)

