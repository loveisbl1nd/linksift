import itertools
import threading
import unittest
from unittest.mock import patch

import app


def make_failing_factory(fail_on_calls):
    """Return real threads, except creations whose 1-based index is in
    fail_on_calls: those get a start() that raises RuntimeError."""
    counter = itertools.count(1)

    def factory(*args, **kwargs):
        thread = threading.Thread(*args, **kwargs)
        if next(counter) in fail_on_calls:
            def fail_start():
                raise RuntimeError("simulated thread startup failure")

            thread.start = fail_start
        return thread

    return factory


class SchedulerStartupFailureTests(unittest.TestCase):
    def setUp(self):
        app.jobs.clear()
        app.processes.clear()
        app.reset_scheduler()
        self.client = app.app.test_client()

    def tearDown(self):
        app.reset_scheduler()
        app.jobs.clear()
        app.processes.clear()

    @staticmethod
    def _failing_ctor(fail_on_calls):
        real = app.DownloadScheduler

        def ctor(worker_count, queue_limit, **kwargs):
            kwargs["thread_factory"] = make_failing_factory(fail_on_calls)
            return real(worker_count, queue_limit, **kwargs)

        return ctor

    def test_startup_failure_returns_503_without_orphans(self):
        with patch.object(app, "DownloadScheduler", self._failing_ctor({1})), patch.object(
            app, "runtime_unavailable_response", return_value=None
        ), patch.object(app, "run_pipeline") as run:
            response = self.client.post("/api/download", json={"url": "https://example.test/video"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json(), {"error": "Download scheduler is unavailable"})
        self.assertEqual(app.jobs, {})
        with app.scheduler_guard:
            self.assertIsNone(app.scheduler)
        run.assert_not_called()

    def test_partial_startup_failure_shuts_down_started_workers(self):
        scheduler = app.DownloadScheduler(2, 5, thread_factory=make_failing_factory({2}))
        with self.assertRaises(RuntimeError):
            scheduler.start()
        self.assertEqual(len(scheduler._workers), 1)
        for worker in scheduler._workers:
            self.assertFalse(worker.is_alive())
        self.assertFalse(scheduler.submit("late", lambda: None))
        self.assertEqual(scheduler.queued_count(), 0)

    def test_request_after_startup_failure_recovers(self):
        with patch.object(app, "DownloadScheduler", self._failing_ctor({1})), patch.object(
            app, "runtime_unavailable_response", return_value=None
        ):
            first = self.client.post("/api/download", json={"url": "https://example.test/video"})
        self.assertEqual(first.status_code, 503)
        self.assertEqual(app.jobs, {})

        executed = threading.Event()
        with patch.object(
            app, "run_pipeline", side_effect=lambda *args, **kwargs: executed.set()
        ), patch.object(app, "runtime_unavailable_response", return_value=None):
            second = self.client.post("/api/download", json={"url": "https://example.test/video"})
            self.assertEqual(second.status_code, 200)
            self.assertIn("job_id", second.get_json())
            self.assertTrue(executed.wait(timeout=5))
            app.reset_scheduler()


if __name__ == "__main__":
    unittest.main()
