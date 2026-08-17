import json
import unittest
from unittest.mock import patch

import app


class PostprocessProgressTests(unittest.TestCase):
    def test_postprocess_payload_switches_phase_to_processing(self):
        job = {"status": "downloading", "phase": "downloading", "speed": 1024.0, "eta": 12, "percent": 88.0}
        line = app.POSTPROCESS_PREFIX + json.dumps({"status": "started"})
        self.assertTrue(app.update_job_postprocess(job, line))
        self.assertEqual(job["phase"], "processing")
        self.assertIsNone(job["speed"])
        self.assertIsNone(job["eta"])
        # No fabricated ffmpeg percentage: the last known value stays as-is.
        self.assertEqual(job["percent"], 88.0)

    def test_postprocess_malformed_payloads_are_ignored(self):
        for payload in ("[]", "42", "not-json", ""):
            with self.subTest(payload=payload):
                job = {"status": "downloading", "phase": "downloading"}
                self.assertFalse(app.update_job_postprocess(job, app.POSTPROCESS_PREFIX + payload))
                self.assertEqual(job, {"status": "downloading", "phase": "downloading"})

    def test_non_postprocess_lines_are_not_consumed(self):
        job = {"status": "downloading"}
        self.assertFalse(app.update_job_postprocess(job, "ordinary diagnostics line"))
        self.assertEqual(job, {"status": "downloading"})

    def test_late_download_progress_cannot_change_terminal_job(self):
        line = app.PROGRESS_PREFIX + json.dumps({"downloaded_bytes": 10, "total_bytes": 100})
        for status in ("done", "error", "cancelled", "timed_out"):
            with self.subTest(status=status):
                job = {"status": status, "phase": status}
                self.assertTrue(app.update_job_progress(job, line))
                self.assertEqual(job, {"status": status, "phase": status})

    def test_late_postprocess_progress_cannot_change_terminal_job(self):
        line = app.POSTPROCESS_PREFIX + json.dumps({"status": "started"})
        for status in ("done", "error", "cancelled", "timed_out"):
            with self.subTest(status=status):
                job = {"status": status, "phase": status}
                self.assertTrue(app.update_job_postprocess(job, line))
                self.assertEqual(job, {"status": status, "phase": status})

    def test_reader_routes_progress_lines_away_from_diagnostics(self):
        job_id = "ppreader01"
        job = {"id": job_id, "status": "downloading"}
        app.jobs[job_id] = job
        lines = [
            app.PROGRESS_PREFIX + json.dumps({"downloaded_bytes": 10, "total_bytes": 100}) + "\n",
            app.POSTPROCESS_PREFIX + json.dumps({"status": "started"}) + "\n",
            "some warning\n",
        ]

        class FakeProcess:
            def __init__(self):
                self.stdout = iter(lines)
                self.returncode = 0

            def wait(self, timeout):
                return self.returncode

        try:
            with patch.object(app.subprocess, "Popen", return_value=FakeProcess()):
                result = app.run_download_command(["yt-dlp", "url"], job_id, job, 60)

            self.assertEqual(result.stderr, "some warning")
            self.assertEqual(job["phase"], "processing")
            self.assertIsNone(job["speed"])
            self.assertIsNone(job["eta"])
        finally:
            app.jobs.pop(job_id, None)


if __name__ == "__main__":
    unittest.main()
