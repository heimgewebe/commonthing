"""Regression: Experiment B must warm readiness before shared T048 load."""
from __future__ import annotations

import inspect
import unittest
from unittest import mock

from scripts.platform import experiment_b_runtime as runtime


class ExperimentBT048ReadyStreakTests(unittest.TestCase):
    def test_transient_ready_200_does_not_start_measurement(self) -> None:
        responses = [503, 200, 200, 503, 200, 200, 200]
        calls = []

        def read(url: str, *, timeout: int) -> tuple[int, bytes, float]:
            calls.append((url, timeout))
            return responses.pop(0), b"", 1.0

        process = mock.Mock()
        process.poll.return_value = None
        with (
            mock.patch.object(runtime, "_http_read", side_effect=read),
            mock.patch.object(runtime.time, "sleep") as sleep,
        ):
            runtime._wait_http_200(
                "http://127.0.0.1:8787/health/ready",
                process,
                consecutive=3,
            )
        self.assertEqual(responses, [])
        self.assertEqual(len(calls), 7)
        self.assertTrue(all(timeout == 2 for _, timeout in calls))
        self.assertEqual(sleep.call_count, 6)

    def test_http_body_timeout_resets_streak_then_retries(self) -> None:
        results = [200, 200, TimeoutError('body read timed out'), 200, 200, 200]
        calls = []

        def read(url: str, *, timeout: int) -> tuple[int, bytes, float]:
            calls.append((url, timeout))
            result = results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result, b'', 1.0

        process = mock.Mock()
        process.poll.return_value = None
        with (
            mock.patch.object(runtime, '_http_read', side_effect=read),
            mock.patch.object(runtime.time, 'sleep') as sleep,
        ):
            runtime._wait_http_200(
                'http://127.0.0.1:8787/health/ready', process, consecutive=3
            )
        self.assertEqual(results, [])
        self.assertEqual(len(calls), 6)
        self.assertEqual(sleep.call_count, 5)

    def test_permanent_http_body_timeouts_fail_within_existing_bound(self) -> None:
        with (
            mock.patch.object(runtime, '_http_read', side_effect=TimeoutError('read timeout')) as read,
            mock.patch.object(runtime.time, 'sleep'),
            self.assertRaisesRegex(runtime.RuntimeErrorEB, 'did not become ready'),
        ):
            runtime._wait_http_200('http://127.0.0.1:8787/health/ready', consecutive=3)
        self.assertEqual(read.call_count, 120)

    def test_unexpected_readiness_error_is_not_suppressed(self) -> None:
        with (
            mock.patch.object(runtime, '_http_read', side_effect=ValueError('invalid response')),
            self.assertRaisesRegex(ValueError, 'invalid response'),
        ):
            runtime._wait_http_200('http://127.0.0.1:8787/health/ready', consecutive=3)


    def test_permanently_unready_api_fails_closed(self) -> None:
        with (
            mock.patch.object(runtime, "_http_read", return_value=(503, b"", 1.0)) as read,
            mock.patch.object(runtime.time, "sleep"),
            self.assertRaisesRegex(runtime.RuntimeErrorEB, "did not become ready"),
        ):
            runtime._wait_http_200(
                "http://127.0.0.1:8787/health/ready",
                consecutive=3,
            )
        self.assertEqual(read.call_count, 120)

    def test_experiment_b_drains_full_pipeline_before_t048_measurement(self) -> None:
        source = inspect.getsource(runtime.t048_load_proof)
        fixture = source.index("_validated_t048_fixture_receipt(")
        drain = source.index("_wait_event_pipeline_quiescent(")
        ready = source.index('_wait_http_200(f"{base_url}/health/ready"')
        metrics = source.index('_http_read(f"{base_url}/metrics")')
        load = source.index("load = subprocess.Popen(")
        self.assertLess(fixture, drain)
        self.assertLess(drain, ready)
        self.assertLess(ready, metrics)
        self.assertLess(metrics, load)
        binding = source[drain:ready]
        self.assertIn("source_commit=source_commit", binding)
        self.assertIn("database_identity=database_identity", binding)

    def test_experiment_b_gates_measurement_on_stable_readiness(self) -> None:
        source = inspect.getsource(runtime.t048_load_proof)
        search = source.index("_prime_t048_search_metric(")
        ready = source.index('consecutive=3')
        metrics_before = source.index('_http_read(f"{base_url}/metrics")')
        k6_start = source.index("load = subprocess.Popen(")
        self.assertLess(search, ready)
        self.assertLess(ready, metrics_before)
        self.assertLess(metrics_before, k6_start)


if __name__ == "__main__":
    unittest.main()
