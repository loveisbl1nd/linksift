import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import app


class CancelEndpointTests(unittest.TestCase):
    def setUp(self):
        app.jobs.clear()
        app.processes.clear()
        self.client = app.app.test_client()

    def tearDown(self):
        app.jobs.clear()
        app.processes.clear()

    def test_cancel_unknown_job_returns_404(self):
        response = self.client.delete("/api/download/nosuchjob1")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["error"], "Job not found")

    def test_cancel_before_subprocess_starts(self):
        job_id = "cancelear1"
        app.jobs[job_id] = {"status": "downloading", "phase": "starting", "title": ""}

        response = self.client.delete(f"/api/download/{job_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "cancelling")
        self.assertEqual(app.jobs[job_id]["status"], "cancelling")

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            app, "DOWNLOAD_DIR", temp_dir
        ), patch.object(app, "run_download_command") as run:
            app.run_download(job_id, "https://example.test/video", "video", None)

        run.assert_not_called()
        self.assertEqual(app.jobs[job_id]["status"], "cancelled")
        self.assertIsNotNone(app.jobs[job_id].get("finished_at"))

    def test_cancel_while_subprocess_running_terminates_process_tree(self):
        job_id = "cancelrun1"
        process = Mock()
        app.jobs[job_id] = {"status": "downloading", "id": job_id, "title": ""}
        app.processes[job_id] = process

        with patch.object(app, "terminate_process_tree") as terminate:
            response = self.client.delete(f"/api/download/{job_id}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "cancelling")
        terminate.assert_called_once_with(process)
        self.assertTrue(app.jobs[job_id]["cancel_requested"])
        self.assertEqual(app.jobs[job_id]["status"], "cancelling")

    def test_cancelled_job_does_not_become_error(self):
        job_id = "cancelrun2"
        app.jobs[job_id] = {"status": "downloading", "title": ""}

        def killed_mid_run(cmd, parent_job_id, job, timeout):
            with app.jobs_lock:
                job["cancel_requested"] = True
                job["status"] = "cancelling"
            return subprocess.CompletedProcess(cmd, 1, "", "Killed")

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            app, "DOWNLOAD_DIR", temp_dir
        ), patch.object(app, "run_download_command", side_effect=killed_mid_run):
            app.run_download(job_id, "https://example.test/video", "video", None)

        self.assertEqual(app.jobs[job_id]["status"], "cancelled")
        self.assertIsNone(app.jobs[job_id].get("error"))

    def test_cancel_terminal_job_is_idempotent(self):
        for status in ("done", "error", "cancelled"):
            with self.subTest(status=status):
                job_id = f"term{status}"[:10]
                app.jobs[job_id] = {"status": status}
                with patch.object(app, "terminate_process_tree") as terminate:
                    response = self.client.delete(f"/api/download/{job_id}")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json()["status"], status)
                self.assertEqual(app.jobs[job_id]["status"], status)
                terminate.assert_not_called()

    def test_cancel_removes_partial_and_final_files(self):
        job_id = "cancelfile"
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(app, "DOWNLOAD_DIR", temp_dir):
            final = Path(temp_dir) / f"{job_id}.mp4"
            partial = Path(temp_dir) / f"{job_id}.f137.mp4.part"
            final.write_bytes(b"final")
            partial.write_bytes(b"partial")
            app.jobs[job_id] = {"status": "downloading", "title": ""}

            def cancel_mid_run(cmd, parent_job_id, job, timeout):
                with app.jobs_lock:
                    job["cancel_requested"] = True
                    job["status"] = "cancelling"
                return subprocess.CompletedProcess(cmd, 1, "", "")

            with patch.object(app, "run_download_command", side_effect=cancel_mid_run):
                app.run_download(job_id, "https://example.test/video", "video", None)

            self.assertEqual(app.jobs[job_id]["status"], "cancelled")
            self.assertFalse(final.exists())
            self.assertFalse(partial.exists())

    def test_process_registry_cleared_after_completion(self):
        job_id = "registryjb"
        parent_job_id = "parent123"
        job = {"id": parent_job_id, "status": "downloading"}
        app.jobs[parent_job_id] = job
        observed = {}

        class FakeProcess:
            def __init__(self):
                self.stdout = iter([])
                self.returncode = 0

            def wait(self, timeout):
                observed["registered"] = app.processes.get(parent_job_id)
                return self.returncode

        fake = FakeProcess()
        with patch.object(app.subprocess, "Popen", return_value=fake):
            result = app.run_download_command(["yt-dlp", "url"], parent_job_id, job, 60)

        self.assertEqual(result.returncode, 0)
        self.assertIs(observed["registered"], fake)
        self.assertEqual(app.processes, {})


class CancelFrontendContractTests(unittest.TestCase):
    def test_frontend_sends_delete_for_active_downloads(self):
        template = (Path(app.__file__).parent / "templates" / "index.html").read_text(encoding="utf-8")
        for token in (
            "function cancelActiveDownloads()",
            "cancelActiveDownloads();",
            "method: 'DELETE'",
            "/api/download/${jobId}",
            "data-cancel",
            "'cancelled'",
            "Cancelled",
        ):
            with self.subTest(token=token):
                self.assertIn(token, template)


if __name__ == "__main__":
    unittest.main()
