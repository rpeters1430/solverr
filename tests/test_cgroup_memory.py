import io
import unittest
from unittest.mock import patch

from app import config


def _fake_fs(files):
    real_open = open

    def fake_open(path, *args, **kwargs):
        if path in files:
            return io.StringIO(files[path])
        if str(path).startswith("/sys/fs/cgroup"):
            raise FileNotFoundError(path)
        return real_open(path, *args, **kwargs)
    return fake_open


class TestCgroupMemoryUsage(unittest.TestCase):
    def test_v2_working_set_excludes_inactive_file_cache(self):
        files = {
            "/sys/fs/cgroup/memory.max": "2147483648\n",
            "/sys/fs/cgroup/memory.current": "1500000000\n",
            "/sys/fs/cgroup/memory.stat": "anon 900000000\nfile 600000000\ninactive_file 400000000\n",
        }
        with patch("builtins.open", _fake_fs(files)):
            self.assertEqual(config.cgroup_memory_usage(), (1100000000, 2147483648))

    def test_v1_layout(self):
        files = {
            "/sys/fs/cgroup/memory/memory.limit_in_bytes": "4294967296\n",
            "/sys/fs/cgroup/memory/memory.usage_in_bytes": "3000000000\n",
            "/sys/fs/cgroup/memory/memory.stat": "cache 1\ntotal_inactive_file 1000000000\n",
        }
        with patch("builtins.open", _fake_fs(files)):
            self.assertEqual(config.cgroup_memory_usage(), (2000000000, 4294967296))

    def test_unlimited_container_reports_nothing(self):
        files = {"/sys/fs/cgroup/memory.max": "max\n", "/sys/fs/cgroup/memory.current": "123\n"}
        with patch("builtins.open", _fake_fs(files)):
            self.assertIsNone(config.cgroup_memory_usage())


if __name__ == "__main__":
    unittest.main()


class TestUserPrefsParsing(unittest.TestCase):
    def test_valid_prefs(self):
        self.assertEqual(config.parse_user_prefs(""), {})
        self.assertEqual(config.parse_user_prefs(None), {})
        self.assertEqual(
            config.parse_user_prefs('{"a": "x", "b": true, "c": -2147483648}'),
            {"a": "x", "b": True, "c": -2147483648},
        )

    def test_invalid_prefs_fail_loudly(self):
        for bad in ("not json", "[1]", '{"a": 1.5}', '{"a": 2147483648}', '{"a": null}', '{"a": {}}'):
            with self.assertRaises(ValueError, msg=bad):
                config.parse_user_prefs(bad)
