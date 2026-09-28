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
            summary = await reopened.summary()
            self.assertEqual(summary["total"], 2)
            self.assertEqual(summary["failed"], 1)
            self.assertEqual(sum(hour["success"] + hour["failed"] for hour in summary["hours"]), 2)
            self.assertEqual(summary["top_failed_domains"], [("example.net", 1)])
            self.assertEqual(summary["tier_counts"]["tier2_cache"], 1)
            self.assertEqual((await reopened.summary(tier="tier2_cache"))["total"], 1)
            self.assertEqual(len((await reopened.summary(hours=168))["hours"]), 24)
            self.assertEqual(len(await reopened.recent(tier="tier2_cache", hours=1)), 1)
            with open(path, "rb") as db:
                contents = db.read()
            self.assertNotIn(b"sensitive", contents)
            self.assertNotIn(b"secret", contents)
