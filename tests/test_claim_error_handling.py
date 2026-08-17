import itertools
import json
import os
import threading
import unittest
from unittest.mock import patch

import app


class BareSchedulerClaimErrorTests(unittest.TestCase):
    def test_claim_error_hook_fires_and_worker_survives(self):
        ran = []
        errors = []
        done = threading.Event()
        calls = itertools.count(1)

        def flaky_claim(job_id):
            if next(calls) == 1:
                raise ValueError("claim exploded")
            return True

        def record_error(job_id, exc):
            errors.append((job_id, type(exc).__name__))

        scheduler = app.DownloadScheduler(1, 5, on_claim=flaky_claim, on_claim_error=record_error)
        try:
            scheduler.start()
            self.assertTrue(scheduler.submit("bad", lambda: ran.append("bad")))
            self.assertTrue(scheduler.submit("good", lambda: (ran.append("good"), done.set())))
            self.assertTrue(done.wait(timeout=5))
            self.assertEqual(ran, ["good"])
            self.assertEqual(errors, [("bad", "ValueError")])
            self.assertTrue(all(worker.is_alive() for worker in scheduler._workers))
            self.assertEqual(scheduler.queued_count(), 0)
        finally:
            scheduler.shutdown()


class ApiClaimErrorTests(unittest.TestCase):
    def setUp(self):
        app.jobs.clear()
        app.processes.clear()
        app.reset_scheduler()
        self.client = app.app.test_client()

    def tearDown(self):
        app.reset_scheduler()
        app.jobs.clear()
        app.processes.clear()

    def test_claim_exception_fails_job_and_same_worker_continues(self):
        release = threading.Event()
        second_running = threading.Event()
        executed = []
        real_claim = app.claim_download_job
        calls = itertools.count(1)

        def flaky_claim(job_id):
            if next(calls) == 1:
                raise RuntimeError("boom-internal-detail")
            return real_claim(job_id)

        def fake_run_pipeline(job_id, url, title):
            executed.append(job_id)
            second_running.set()
            release.wait(timeout=10)

        try:
            with patch.dict(os.environ, {"LINKSIFT_MAX_CONCURRENT_DOWNLOADS": "1"}), patch.object(
                app, "claim_download_job", side_effect=flaky_claim
            ), patch.object(app, "run_pipeline", side_effect=fake_run_pipeline), patch.object(
                app, "runtime_unavailable_response", return_value=None
            ):
                app.reset_scheduler()
                first = self.client.post("/api/download", json={"url": "https://example.test/v1"})
                first_id = first.get_json()["job_id"]
                with app.scheduler_guard:
                    scheduler_before = app.scheduler
                second = self.client.post("/api/download", json={"url": "https://example.test/v2"})
                second_id = second.get_json()["job_id"]

                # The single worker must survive the failed claim and run job 2.
                self.assertTrue(second_running.wait(timeout=5))
                self.assertEqual(executed, [second_id])
                with app.scheduler_guard:
                    self.assertIs(app.scheduler, scheduler_before)

                payload = self.client.get(f"/api/status/{first_id}").get_json()
                self.assertEqual(payload["status"], "error")
                self.assertEqual(payload["phase"], "error")
                self.assertEqual(payload["error"], "Download could not be started")
                self.assertIsNone(payload["queue_position"])
                self.assertIsNone(payload["started_at"])
                self.assertIsNone(payload["speed"])
                self.assertIsNone(payload["eta"])
                self.assertIsNone(payload["percent"])
                self.assertNotIn("boom-internal-detail", json.dumps(payload))
                with app.jobs_lock:
                    self.assertIsNotNone(app.jobs[first_id]["finished_at"])
                self.assertNotIn(first_id, app.processes)

                release.set()
                app.reset_scheduler()
        finally:
            release.set()

    def test_repeated_claim_failures_keep_worker_and_queue_alive(self):
        done = threading.Event()
        executed = []
        real_claim = app.claim_download_job
        calls = itertools.count(1)

        def flaky_claim(job_id):
            if next(calls) <= 3:
                raise RuntimeError("boom")
            return real_claim(job_id)

        def fake_run_pipeline(job_id, url, title):
            executed.append(job_id)
            done.set()

        with patch.dict(os.environ, {"LINKSIFT_MAX_CONCURRENT_DOWNLOADS": "1"}), patch.object(
            app, "claim_download_job", side_effect=flaky_claim
        ), patch.object(app, "run_pipeline", side_effect=fake_run_pipeline), patch.object(
            app, "runtime_unavailable_response", return_value=None
        ):
            app.reset_scheduler()
            job_ids = []
            for index in range(4):
                response = self.client.post("/api/download", json={"url": f"https://example.test/v{index}"})
                self.assertEqual(response.status_code, 200)
                job_ids.append(response.get_json()["job_id"])

            self.assertTrue(done.wait(timeout=5))
            self.assertEqual(executed, [job_ids[3]])
            with app.jobs_lock:
                for job_id in job_ids[:3]:
                    self.assertEqual(app.jobs[job_id]["status"], "error")
                    self.assertEqual(app.jobs[job_id]["error"], "Download could not be started")
            with app.scheduler_guard:
                self.assertTrue(all(worker.is_alive() for worker in app.scheduler._workers))
            app.reset_scheduler()

    def test_claim_error_hook_runs_with_jobs_lock_held(self):
        hook_done = threading.Event()
        lock_was_free = []
        real_fail = app.fail_claimed_job

        def recording_fail(job_id, exc):
            acquired = app.jobs_lock.acquire(blocking=False)
            if acquired:
                app.jobs_lock.release()
            lock_was_free.append(acquired)
            real_fail(job_id, exc)
            hook_done.set()

        def bad_claim(job_id):
            raise RuntimeError("boom")

        with patch.object(app, "claim_download_job", side_effect=bad_claim), patch.object(
            app, "fail_claimed_job", side_effect=recording_fail
        ), patch.object(app, "runtime_unavailable_response", return_value=None):
            app.reset_scheduler()
            response = self.client.post("/api/download", json={"url": "https://example.test/v1"})
            job_id = response.get_json()["job_id"]
            self.assertTrue(hook_done.wait(timeout=5))
            self.assertEqual(lock_was_free, [False])
            with app.jobs_lock:
                self.assertEqual(app.jobs[job_id]["status"], "error")
                self.assertIsNone(app.jobs[job_id]["started_at"])
            app.reset_scheduler()


if __name__ == "__main__":
    unittest.main()
