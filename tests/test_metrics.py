import unittest
from unittest.mock import patch

from app.metrics import generate_prometheus_metrics
from app.solver.engine import FAILURE_REASONS, PerformanceMetrics


class TestPerformanceMetrics(unittest.TestCase):
    def test_success_and_failure_outcomes_have_end_to_end_latency(self):
        subject = PerformanceMetrics()
        subject.record_fast(400.0)
        subject.record_browser(2400.0)
        subject.record_failure(61000.0, "timeout")

        data = subject.to_dict()
        self.assertEqual(data["total_requests"], 3)
        self.assertEqual(data["successful_requests"], 2)
        self.assertEqual(data["failed_requests"], 1)
        self.assertEqual(data["success_rate_pct"], 66.7)
        self.assertEqual(data["failure_rate_pct"], 33.3)
        self.assertEqual(data["avg_end_to_end_success_ms"], 1400.0)
        self.assertEqual(data["avg_end_to_end_failure_ms"], 61000.0)
        self.assertEqual(subject.outcome_duration_histograms["success"].count, 2)
        self.assertEqual(subject.outcome_duration_histograms["failure"].count, 1)

    def test_failure_reason_is_bounded_and_unknown_is_safe_default(self):
        subject = PerformanceMetrics()
        subject.record_failure(1.0, "raw exception containing https://secret.example")

        self.assertEqual(subject.failure_reasons["unknown"], 1)
        self.assertEqual(set(subject.failure_reasons), set(FAILURE_REASONS))
        self.assertNotIn("raw exception containing https://secret.example", subject.failure_reasons)

    def test_new_request_metrics_are_exposed(self):
        subject = PerformanceMetrics()
        subject.record_fast(400.0)
        subject.record_failure(61000.0, "timeout")
        with patch("app.metrics.metrics", subject):
            body = generate_prometheus_metrics()
        self.assertIn('solverr_request_failures_total{reason="timeout"} 1', body)
        self.assertIn('solverr_request_failures_total{reason="unknown"} 0', body)
        self.assertIn('solverr_end_to_end_request_duration_seconds_count{outcome="success"} 1', body)
        self.assertIn('solverr_end_to_end_request_duration_seconds_count{outcome="failure"} 1', body)


if __name__ == "__main__":
    unittest.main()
