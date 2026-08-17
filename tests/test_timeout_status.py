import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class TimeoutStatusTests(unittest.TestCase):
    def setUp(self):
        app.jobs.clear()
        app.processes.clear()
        self.client = app.app.test_client()

    def tearDown(self):
        app.jobs.clear()
        app.processes.clear()

    def _run_with_timeout(self, job_id, temp_dir, side_effect=None):
        if side_effect is None:
            side_effect = app.subprocess.TimeoutExpired(cmd=["yt-dlp"], timeout=3600)
        with patch.object(app, "DOWNLOAD_DIR", temp_dir), patch.object(
            app, "get_download_timeout", return_value=3600
        ), patch.object(app, "run_download_command", side_effect=side_effect):
            app.run_download(job_id, "https://example.test/video", "video", None)

    def test_timeout_sets_timed_out_status_not_error(self):
        job_id = "timeoutjb1"
        app.jobs[job_id] = {"status": "downloading", "title": ""}
        with tempfile.TemporaryDirectory() as temp_dir:
            self._run_with_timeout(job_id, temp_dir)
        job = app.jobs[job_id]
        self.assertEqual(job["status"], "timed_out")
        self.assertEqual(job["phase"], "timed_out")
        self.assertEqual(job["error"], "Download timed out after 3600 seconds")
        self.assertIsNone(job["speed"])
        self.assertIsNone(job["eta"])

    def test_timeout_sets_finished_at(self):
        job_id = "timeoutjb2"
        app.jobs[job_id] = {"status": "downloading", "title": ""}
        with tempfile.TemporaryDirectory() as temp_dir:
            self._run_with_timeout(job_id, temp_dir)
        self.assertIsNotNone(app.jobs[job_id].get("finished_at"))

    def test_timeout_cleans_partial_files(self):
        job_id = "timeoutjb3"
        app.jobs[job_id] = {"status": "downloading", "title": ""}
        with tempfile.TemporaryDirectory() as temp_dir:
            partial = Path(temp_dir) / f"{job_id}.f137.mp4.part"
            partial.write_bytes(b"partial")
            self._run_with_timeout(job_id, temp_dir)
            self.assertFalse(partial.exists())
        self.assertEqual(app.jobs[job_id]["status"], "timed_out")

    def test_cancel_wins_over_timeout(self):
        job_id = "timeoutjb4"
        app.jobs[job_id] = {"status": "downloading", "title": ""}

        def cancel_then_timeout(cmd, parent_job_id, progress_target, timeout):
            with app.jobs_lock:
                progress_target["cancel_requested"] = True
                progress_target["status"] = "cancelling"
            raise app.subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

        with tempfile.TemporaryDirectory() as temp_dir:
            self._run_with_timeout(job_id, temp_dir, side_effect=cancel_then_timeout)
        job = app.jobs[job_id]
        self.assertEqual(job["status"], "cancelled")
        self.assertIsNone(job.get("error"))

    def test_delete_timed_out_job_is_idempotent(self):
        job_id = "timeoutjb5"
        app.jobs[job_id] = {"status": "timed_out"}
        with patch.object(app, "terminate_process_tree") as terminate:
            response = self.client.delete(f"/api/download/{job_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "timed_out")
        self.assertEqual(app.jobs[job_id]["status"], "timed_out")
        terminate.assert_not_called()

    def test_ttl_cleanup_removes_expired_timed_out_job(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            app, "DOWNLOAD_DIR", temp_dir
        ), patch.object(app, "get_job_ttl", return_value=100):
            job_id = "aaaa111100"
            (Path(temp_dir) / f"{job_id}.mp4").write_bytes(b"final")
            (Path(temp_dir) / f"{job_id}.f137.mp4.part").write_bytes(b"partial")
            app.jobs[job_id] = {"status": "timed_out", "created_at": now - 500, "finished_at": now - 200}
            app.run_cleanup(now=now)
            self.assertNotIn(job_id, app.jobs)
            self.assertEqual(list(Path(temp_dir).iterdir()), [])

    def test_status_endpoint_reports_timed_out(self):
        job_id = "timeoutjb6"
        app.jobs[job_id] = {"status": "timed_out", "error": "Download timed out after 3600 seconds"}
        response = self.client.get(f"/api/status/{job_id}")
        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["status"], "timed_out")
        self.assertEqual(payload["error"], "Download timed out after 3600 seconds")


class TimedOutFrontendContractTests(unittest.TestCase):
    def test_frontend_handles_timed_out(self):
        template = (Path(app.__file__).parent / "templates" / "index.html").read_text(encoding="utf-8")
        for token in (
            "if (data.status === 'timed_out') {",
            "card.status === 'info-error' || card.status === 'timed_out'",
            "Retry download",
            "data-status=\"timed_out\"",
        ):
            with self.subTest(token=token):
                self.assertIn(token, template)


if __name__ == "__main__":
    unittest.main()
