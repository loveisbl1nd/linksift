import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import app


class QueueCancellationTests(unittest.TestCase):
    def setUp(self):
        app.jobs.clear()
        app.processes.clear()
        self.client = app.app.test_client()

    def tearDown(self):
        app.jobs.clear()
        app.processes.clear()

    def test_delete_queued_job_cancels_without_spawning(self):
        job_id = "queuedcncl"
        app.jobs[job_id] = {"id": job_id, "status": "queued", "phase": "queued", "started_at": None}
        fake = Mock()
        fake.cancel_queued.return_value = True
        with patch.object(app, "get_scheduler", return_value=fake), patch.object(
            app, "terminate_process_tree"
        ) as terminate:
            response = self.client.delete(f"/api/download/{job_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "cancelled")
        job = app.jobs[job_id]
        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(job["phase"], "cancelled")
        self.assertIsNotNone(job["finished_at"])
        self.assertIsNone(job["started_at"])
        fake.cancel_queued.assert_called_once_with(job_id)
        terminate.assert_not_called()

    def test_delete_after_dequeue_race_falls_back_to_cancelling(self):
        job_id = "dequeuerac"
        app.jobs[job_id] = {"id": job_id, "status": "queued", "phase": "queued"}
        fake = Mock()
        fake.cancel_queued.return_value = False
        with patch.object(app, "get_scheduler", return_value=fake):
            response = self.client.delete(f"/api/download/{job_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "cancelling")
        self.assertTrue(app.jobs[job_id]["cancel_requested"])
        self.assertEqual(app.jobs[job_id]["status"], "cancelling")

    def test_worker_waking_after_cancel_never_spawns_ytdlp(self):
        job_id = "latewaker1"
        app.jobs[job_id] = {"id": job_id, "status": "cancelling", "cancel_requested": True, "title": ""}
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            app, "DOWNLOAD_DIR", temp_dir
        ), patch.object(app, "run_download_command") as run:
            app.run_download(job_id, "https://example.test/video", "video", None)
        run.assert_not_called()
        self.assertEqual(app.jobs[job_id]["status"], "cancelled")
        self.assertIsNone(app.jobs[job_id].get("started_at"))

    def test_worker_skips_job_already_finalized_as_cancelled(self):
        job_id = "finalized1"
        app.jobs[job_id] = {
            "id": job_id,
            "status": "cancelled",
            "phase": "cancelled",
            "cancel_requested": True,
            "finished_at": 123.0,
        }
        with patch.object(app, "run_download_command") as run:
            app.run_download(job_id, "https://example.test/video", "video", None)
        run.assert_not_called()
        self.assertEqual(app.jobs[job_id]["finished_at"], 123.0)
        self.assertEqual(app.jobs[job_id]["status"], "cancelled")

    def test_running_cancellation_still_terminates_process_tree(self):
        job_id = "running001"
        process = Mock()
        app.jobs[job_id] = {"id": job_id, "status": "downloading", "title": ""}
        app.processes[job_id] = process
        with patch.object(app, "terminate_process_tree") as terminate:
            response = self.client.delete(f"/api/download/{job_id}")
        self.assertEqual(response.get_json()["status"], "cancelling")
        terminate.assert_called_once_with(process)

    def test_cancellation_still_beats_timeout(self):
        job_id = "beats00001"
        app.jobs[job_id] = {"id": job_id, "status": "downloading", "title": ""}

        def cancel_then_timeout(cmd, parent_job_id, progress_target, timeout):
            with app.jobs_lock:
                progress_target["cancel_requested"] = True
                progress_target["status"] = "cancelling"
            raise app.subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            app, "DOWNLOAD_DIR", temp_dir
        ), patch.object(app, "run_download_command", side_effect=cancel_then_timeout):
            app.run_download(job_id, "https://example.test/video", "video", None)
        self.assertEqual(app.jobs[job_id]["status"], "cancelled")
        self.assertIsNone(app.jobs[job_id].get("error"))

    def test_ttl_cleanup_keeps_queued_jobs_and_clears_scheduler_refs(self):
        import time

        now = time.time()
        fake = Mock()
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            app, "DOWNLOAD_DIR", temp_dir
        ), patch.object(app, "get_job_ttl", return_value=100), patch.object(
            app, "scheduler", fake
        ):
            app.jobs["queuedjob1"] = {"status": "queued", "created_at": now - 5000, "finished_at": None}
            app.jobs["expiredone"] = {"status": "done", "created_at": now - 5000, "finished_at": now - 500}
            app.run_cleanup(now=now)
            self.assertIn("queuedjob1", app.jobs)
            self.assertNotIn("expiredone", app.jobs)
        fake.cancel_queued.assert_called_once_with("expiredone")


if __name__ == "__main__":
    unittest.main()
