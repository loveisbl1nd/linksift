import os
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import app


class ClaimHookTests(unittest.TestCase):
    def setUp(self):
        app.jobs.clear()
        app.processes.clear()

    def tearDown(self):
        app.jobs.clear()
        app.processes.clear()

    def test_claim_transitions_queued_job_to_downloading(self):
        app.jobs["claimjob01"] = {"status": "queued", "phase": "queued", "started_at": None}
        self.assertTrue(app.claim_download_job("claimjob01"))
        job = app.jobs["claimjob01"]
        self.assertEqual(job["status"], "downloading")
        self.assertEqual(job["phase"], "starting")
        self.assertIsInstance(job["started_at"], float)

    def test_claim_refuses_terminal_or_missing_job(self):
        app.jobs["claimdone1"] = {"status": "done"}
        self.assertFalse(app.claim_download_job("claimdone1"))
        self.assertNotIn("started_at", app.jobs["claimdone1"])
        self.assertFalse(app.claim_download_job("missing000"))

    def test_claim_never_leaves_queued_state_after_cancel_request(self):
        app.jobs["claimcanc1"] = {
            "status": "queued",
            "phase": "queued",
            "cancel_requested": True,
            "started_at": None,
        }
        self.assertTrue(app.claim_download_job("claimcanc1"))
        job = app.jobs["claimcanc1"]
        self.assertEqual(job["status"], "cancelling")
        self.assertIsNone(job["started_at"])


class DequeueStatusRaceTests(unittest.TestCase):
    """The claim step is atomic with the dequeue under jobs_lock, so a status
    reader can never observe status="queued" with queue_position=null."""

    def setUp(self):
        app.jobs.clear()
        app.processes.clear()
        app.reset_scheduler()
        self.client = app.app.test_client()

    def tearDown(self):
        app.reset_scheduler()
        app.jobs.clear()
        app.processes.clear()

    def test_status_contract_at_claim_boundary(self):
        release = threading.Event()
        first_running = threading.Event()
        executed = []

        def fake_run_pipeline(job_id, url, title):
            executed.append(job_id)
            first_running.set()
            release.wait(timeout=10)

        try:
            with patch.dict(os.environ, {"LINKSIFT_MAX_CONCURRENT_DOWNLOADS": "1"}), patch.object(
                app, "run_pipeline", side_effect=fake_run_pipeline
            ), patch.object(app, "runtime_unavailable_response", return_value=None):
                app.reset_scheduler()
                first = self.client.post("/api/download", json={"url": "https://example.test/v1"})
                self.assertEqual(first.status_code, 200)
                first_id = first.get_json()["job_id"]
                # Sync point: the worker has claimed the job and entered the task.
                self.assertTrue(first_running.wait(timeout=5))

                claimed = self.client.get(f"/api/status/{first_id}").get_json()
                self.assertEqual(claimed["status"], "downloading")
                self.assertEqual(claimed["phase"], "starting")
                self.assertIsNone(claimed["queue_position"])
                self.assertIsInstance(claimed["started_at"], float)

                second = self.client.post("/api/download", json={"url": "https://example.test/v2"})
                second_id = second.get_json()["job_id"]
                waiting = self.client.get(f"/api/status/{second_id}").get_json()
                self.assertEqual(waiting["status"], "queued")
                self.assertEqual(waiting["queue_position"], 1)
                self.assertIsNone(waiting["started_at"])

                cancel = self.client.delete(f"/api/download/{second_id}")
                self.assertEqual(cancel.get_json()["status"], "cancelled")

                release.set()
                app.reset_scheduler()
            self.assertEqual(executed, [first_id])
        finally:
            release.set()

    def test_claim_hook_runs_with_jobs_lock_held(self):
        """Kills the race-reintroducing mutations: claim_lock unwired, or the
        claim hook moved outside the locks. The hook must observe jobs_lock
        as already held by the claiming worker."""
        release = threading.Event()
        running = threading.Event()
        lock_was_free = []
        real_claim = app.claim_download_job

        def recording_claim(job_id):
            acquired = app.jobs_lock.acquire(blocking=False)
            if acquired:
                app.jobs_lock.release()
            lock_was_free.append(acquired)
            return real_claim(job_id)

        def fake_run_pipeline(job_id, url, title):
            running.set()
            release.wait(timeout=10)

        try:
            with patch.object(app, "claim_download_job", side_effect=recording_claim), patch.object(
                app, "run_pipeline", side_effect=fake_run_pipeline
            ), patch.object(app, "runtime_unavailable_response", return_value=None):
                app.reset_scheduler()
                self.assertIs(app.get_scheduler()._claim_lock, app.jobs_lock)
                response = self.client.post("/api/download", json={"url": "https://example.test/v1"})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(running.wait(timeout=5))
                # The claim hook must run with jobs_lock already held by the worker.
                self.assertEqual(lock_was_free, [False])
                release.set()
                app.reset_scheduler()
        finally:
            release.set()

    def test_cancel_after_claim_goes_cancelling_then_cancelled(self):
        in_command = threading.Event()
        release = threading.Event()

        def blocked_command(cmd, parent_job_id, job, timeout):
            in_command.set()
            release.wait(timeout=10)
            return subprocess.CompletedProcess(cmd, 1, "", "")

        try:
            with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
                os.environ, {"LINKSIFT_MAX_CONCURRENT_DOWNLOADS": "1"}
            ), patch.object(app, "DOWNLOAD_DIR", temp_dir), patch.object(
                app, "run_download_command", side_effect=blocked_command
            ), patch.object(app, "runtime_unavailable_response", return_value=None):
                app.reset_scheduler()
                response = self.client.post("/api/download", json={"url": "https://example.test/v1"})
                self.assertEqual(response.status_code, 200)
                job_id = response.get_json()["job_id"]
                self.assertTrue(in_command.wait(timeout=5))

                before = self.client.get(f"/api/status/{job_id}").get_json()
                self.assertEqual(before["status"], "downloading")
                self.assertIsNone(before["queue_position"])
                self.assertIsInstance(before["started_at"], float)

                cancel = self.client.delete(f"/api/download/{job_id}")
                self.assertEqual(cancel.get_json()["status"], "cancelling")
                during = self.client.get(f"/api/status/{job_id}").get_json()
                self.assertEqual(during["status"], "cancelling")

                release.set()
                app.reset_scheduler()  # joins the worker: run_download has finalized

                final = self.client.get(f"/api/status/{job_id}").get_json()
                self.assertEqual(final["status"], "cancelled")
                self.assertIsNone(final["error"])
        finally:
            release.set()


if __name__ == "__main__":
    unittest.main()
