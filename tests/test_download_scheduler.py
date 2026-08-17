import os
import threading
import time
import unittest
from unittest.mock import patch

import app


class SchedulerBehaviorTests(unittest.TestCase):
    """Deterministic unit tests for DownloadScheduler using events, no sleeps."""

    def setUp(self):
        self.schedulers = []
        self.release_events = []

    def tearDown(self):
        for event in self.release_events:
            event.set()
        for scheduler in self.schedulers:
            scheduler.shutdown()

    def make_scheduler(self, workers, queue_limit):
        scheduler = app.DownloadScheduler(workers, queue_limit)
        self.schedulers.append(scheduler)
        scheduler.start()
        return scheduler

    def test_direct_construction_clamps_worker_count(self):
        created = []

        def counting_factory(*args, **kwargs):
            thread = threading.Thread(*args, **kwargs)
            created.append(thread)
            return thread

        scheduler = app.DownloadScheduler(10000, 5, thread_factory=counting_factory)
        self.schedulers.append(scheduler)
        self.assertEqual(scheduler.worker_count, app.MAX_CONCURRENT_DOWNLOADS)
        scheduler.start()
        self.assertEqual(len(created), 16)

    def make_gate(self):
        gate = threading.Event()
        self.release_events.append(gate)
        return gate

    def test_running_jobs_never_exceed_worker_count(self):
        scheduler = self.make_scheduler(2, 10)
        gate = self.make_gate()
        started = [threading.Event() for _ in range(3)]

        def make_task(index):
            def task():
                started[index].set()
                gate.wait(timeout=10)
            return task

        for index in range(3):
            self.assertTrue(scheduler.submit(f"job{index}", make_task(index)))
        self.assertTrue(started[0].wait(timeout=5))
        self.assertTrue(started[1].wait(timeout=5))
        # With both workers occupied, the third job is provably still queued.
        self.assertEqual(scheduler.queue_position("job2"), 1)
        gate.set()
        self.assertTrue(started[2].wait(timeout=5))

    def test_jobs_beyond_worker_limit_are_queued_in_fifo_order(self):
        scheduler = self.make_scheduler(1, 10)
        gate = self.make_gate()
        first_started = threading.Event()
        drained = threading.Event()
        order = []

        def blocker():
            first_started.set()
            gate.wait(timeout=10)
            order.append("a")

        def make_task(name, last=False):
            def task():
                order.append(name)
                if last:
                    drained.set()
            return task

        self.assertTrue(scheduler.submit("a", blocker))
        self.assertTrue(first_started.wait(timeout=5))
        self.assertTrue(scheduler.submit("b", make_task("b")))
        self.assertTrue(scheduler.submit("c", make_task("c", last=True)))
        self.assertEqual(scheduler.queue_position("b"), 1)
        self.assertEqual(scheduler.queue_position("c"), 2)
        gate.set()
        self.assertTrue(drained.wait(timeout=5))
        self.assertEqual(order, ["a", "b", "c"])

    def test_full_queue_rejects_submit(self):
        scheduler = self.make_scheduler(1, 1)
        gate = self.make_gate()
        running = threading.Event()

        def blocker():
            running.set()
            gate.wait(timeout=10)

        self.assertTrue(scheduler.submit("running", blocker))
        self.assertTrue(running.wait(timeout=5))
        self.assertTrue(scheduler.submit("waiting", lambda: None))
        self.assertFalse(scheduler.submit("rejected", lambda: None))

    def test_cancel_queued_removes_task_and_frees_capacity(self):
        scheduler = self.make_scheduler(1, 1)
        gate = self.make_gate()
        running = threading.Event()
        drained = threading.Event()
        ran = []

        def blocker():
            running.set()
            gate.wait(timeout=10)

        self.assertTrue(scheduler.submit("running", blocker))
        self.assertTrue(running.wait(timeout=5))
        self.assertTrue(scheduler.submit("victim", lambda: ran.append("victim")))
        self.assertTrue(scheduler.cancel_queued("victim"))
        self.assertIsNone(scheduler.queue_position("victim"))

        def replacement():
            ran.append("replacement")
            drained.set()

        self.assertTrue(scheduler.submit("replacement", replacement))
        gate.set()
        self.assertTrue(drained.wait(timeout=5))
        self.assertEqual(ran, ["replacement"])

    def test_cancel_queued_returns_false_after_dequeue(self):
        scheduler = self.make_scheduler(1, 10)
        gate = self.make_gate()
        running = threading.Event()

        def blocker():
            running.set()
            gate.wait(timeout=10)

        self.assertTrue(scheduler.submit("only", blocker))
        self.assertTrue(running.wait(timeout=5))
        self.assertFalse(scheduler.cancel_queued("only"))

    def test_shutdown_drops_queued_tasks_and_rejects_new_submits(self):
        scheduler = self.make_scheduler(1, 10)
        gate = self.make_gate()
        running = threading.Event()
        ran = []

        def blocker():
            running.set()
            gate.wait(timeout=10)

        self.assertTrue(scheduler.submit("running", blocker))
        self.assertTrue(running.wait(timeout=5))
        self.assertTrue(scheduler.submit("victim", lambda: ran.append("victim")))

        scheduler.shutdown(wait_seconds=0)

        self.assertFalse(scheduler.submit("late", lambda: ran.append("late")))
        self.assertEqual(scheduler.queued_count(), 0)
        gate.set()
        for worker in scheduler._workers:
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(ran, [])

    def test_shutdown_wakes_idle_workers(self):
        scheduler = self.make_scheduler(1, 10)
        done = threading.Event()
        self.assertTrue(scheduler.submit("quick", done.set))
        self.assertTrue(done.wait(timeout=5))
        scheduler.shutdown()
        for worker in scheduler._workers:
            self.assertFalse(worker.is_alive())


class SchedulerApiIntegrationTests(unittest.TestCase):
    def setUp(self):
        app.jobs.clear()
        app.processes.clear()
        app.reset_scheduler()
        self.client = app.app.test_client()

    def tearDown(self):
        app.reset_scheduler()
        app.jobs.clear()
        app.processes.clear()

    def test_post_does_not_create_a_thread_per_request(self):
        class AcceptingScheduler:
            def submit(self, job_id, task):
                return True

        with patch.object(app, "get_scheduler", return_value=AcceptingScheduler()), patch.object(
            app, "runtime_unavailable_response", return_value=None
        ), patch.object(app.threading, "Thread") as thread:
            response = self.client.post("/api/download", json={"url": "https://example.test/video"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("job_id", response.get_json())
        thread.assert_not_called()

    def test_accepted_job_starts_queued_with_created_at(self):
        class AcceptingScheduler:
            def submit(self, job_id, task):
                return True

        with patch.object(app, "get_scheduler", return_value=AcceptingScheduler()), patch.object(
            app, "runtime_unavailable_response", return_value=None
        ):
            response = self.client.post("/api/download", json={"url": "https://example.test/video"})
        job = app.jobs[response.get_json()["job_id"]]
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["phase"], "queued")
        self.assertIsNone(job["started_at"])
        self.assertIsNotNone(job["created_at"])
        self.assertIsNone(job["finished_at"])

    def test_full_queue_returns_429_through_real_scheduler(self):
        """Proves the LINKSIFT_MAX_QUEUED_DOWNLOADS env wiring, not just the handler."""
        release = threading.Event()
        first_started = threading.Event()

        def fake_run_pipeline(job_id, url, title):
            first_started.set()
            release.wait(timeout=10)

        try:
            with patch.dict(os.environ, {
                "LINKSIFT_MAX_CONCURRENT_DOWNLOADS": "1",
                "LINKSIFT_MAX_QUEUED_DOWNLOADS": "1",
            }), patch.object(app, "run_pipeline", side_effect=fake_run_pipeline), patch.object(
                app, "runtime_unavailable_response", return_value=None
            ):
                app.reset_scheduler()
                first = self.client.post("/api/download", json={"url": "https://example.test/v1"})
                self.assertEqual(first.status_code, 200)
                self.assertTrue(first_started.wait(timeout=5))

                second = self.client.post("/api/download", json={"url": "https://example.test/v2"})
                self.assertEqual(second.status_code, 200)

                third = self.client.post("/api/download", json={"url": "https://example.test/v3"})
                self.assertEqual(third.status_code, 429)
                self.assertEqual(third.get_json()["error"], "Download queue is full")
                with app.jobs_lock:
                    self.assertEqual(len(app.jobs), 2)

                release.set()
                app.reset_scheduler()
        finally:
            release.set()

    def test_overflow_job_queues_then_cancels_end_to_end(self):
        release = threading.Event()
        first_started = threading.Event()
        executed = []

        def fake_run_pipeline(job_id, url, title):
            executed.append(job_id)
            with app.jobs_lock:
                job = app.jobs.get(job_id)
                if job:
                    job.update({"status": "downloading", "phase": "starting", "started_at": time.time()})
            first_started.set()
            release.wait(timeout=10)
            with app.jobs_lock:
                job = app.jobs.get(job_id)
                if job:
                    job.update({"status": "done", "phase": "done", "finished_at": time.time()})

        try:
            with patch.dict(os.environ, {"LINKSIFT_MAX_CONCURRENT_DOWNLOADS": "1"}), patch.object(
                app, "run_pipeline", side_effect=fake_run_pipeline
            ), patch.object(app, "runtime_unavailable_response", return_value=None):
                app.reset_scheduler()
                first = self.client.post("/api/download", json={"url": "https://example.test/v1"})
                self.assertEqual(first.status_code, 200)
                first_id = first.get_json()["job_id"]
                self.assertTrue(first_started.wait(timeout=5))

                second = self.client.post("/api/download", json={"url": "https://example.test/v2"})
                self.assertEqual(second.status_code, 200)
                second_id = second.get_json()["job_id"]

                status = self.client.get(f"/api/status/{second_id}").get_json()
                self.assertEqual(status["status"], "queued")
                self.assertEqual(status["phase"], "queued")
                self.assertEqual(status["queue_position"], 1)
                self.assertIsNone(status["started_at"])

                cancel = self.client.delete(f"/api/download/{second_id}")
                self.assertEqual(cancel.status_code, 200)
                self.assertEqual(cancel.get_json()["status"], "cancelled")
                with app.jobs_lock:
                    job = app.jobs[second_id]
                    self.assertEqual(job["status"], "cancelled")
                    self.assertEqual(job["phase"], "cancelled")
                    self.assertIsNotNone(job["finished_at"])

                release.set()
                # Join workers while the patches are still active.
                app.reset_scheduler()
            self.assertEqual(executed, [first_id])
        finally:
            release.set()


if __name__ == "__main__":
    unittest.main()
