import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.resource_metrics import collect_resource_snapshot


class TestResourceMetrics(unittest.TestCase):
    def test_process_tree_rss_includes_parent_and_recursive_children(self):
        parent = Mock()
        parent.memory_info.return_value = SimpleNamespace(rss=100)
        parent.cpu_percent.return_value = 4.5
        child_a = Mock()
        child_a.memory_info.return_value = SimpleNamespace(rss=200)
        child_b = Mock()
        child_b.memory_info.return_value = SimpleNamespace(rss=300)
        parent.children.return_value = [child_a, child_b]

        with patch("app.resource_metrics.psutil.cpu_percent", return_value=12.0):
            result = collect_resource_snapshot(parent)

        self.assertEqual(result.parent_rss_bytes, 100)
        self.assertEqual(result.tree_rss_bytes, 600)
        self.assertEqual(result.process_cpu_percent, 4.5)
        self.assertEqual(result.host_cpu_percent, 12.0)
        parent.children.assert_called_once_with(recursive=True)

    def test_disappearing_child_does_not_break_collection(self):
        import psutil
        parent = Mock()
        parent.memory_info.return_value = SimpleNamespace(rss=100)
        parent.cpu_percent.return_value = 1.0
        vanished = Mock()
        vanished.memory_info.side_effect = psutil.NoSuchProcess(123)
        parent.children.return_value = [vanished]

        result = collect_resource_snapshot(parent)

        self.assertEqual(result.tree_rss_bytes, 100)


if __name__ == "__main__":
    unittest.main()
