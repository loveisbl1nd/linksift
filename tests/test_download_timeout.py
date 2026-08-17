import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class DownloadTimeoutTests(unittest.TestCase):
    def test_default_timeout_allows_large_downloads(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LINKSIFT_DOWNLOAD_TIMEOUT", None)
            self.assertEqual(app.get_download_timeout(), 3600)

    def test_invalid_timeout_override_uses_default(self):
        for value in ("not-a-number", "0", "-1"):
            with self.subTest(value=value), patch.dict(
                os.environ,
                {"LINKSIFT_DOWNLOAD_TIMEOUT": value},
            ):
                self.assertEqual(app.get_download_timeout(), 3600)

    def test_run_download_uses_configured_timeout(self):
        job_id = "timeout-config"
        app.jobs[job_id] = {"status": "downloading", "url": "https://example.com/video", "title": ""}
        try:
            with patch.object(app, "get_download_timeout", return_value=3600), patch.object(
                app.subprocess,
                "run",
                side_effect=AssertionError("legacy download runner used"),
            ), patch.object(
                app,
                "run_download_command",
                side_effect=app.subprocess.TimeoutExpired(cmd=["yt-dlp"], timeout=3600),
            ) as run:
                app.run_download(job_id, "https://example.com/video", "video", None)
            # Signature: run_download_command(cmd, parent_job_id, progress_target, timeout)
            # timeout is at index 3
            self.assertEqual(run.call_args.args[3], 3600)
            command = run.call_args.args[0]
            self.assertIn("--progress-template", command)
            self.assertIn(f"download:{app.PROGRESS_PREFIX}%(progress)j", command)
        finally:
            app.jobs.pop(job_id, None)

    def test_timeout_removes_partial_files(self):
        job_id = "timeout-cleanup"
        app.jobs[job_id] = {"status": "downloading", "url": "https://example.com/video", "title": ""}
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                partial = Path(temp_dir) / f"{job_id}.f11.mp4.part"
                partial.write_bytes(b"partial")
                with patch.object(app, "DOWNLOAD_DIR", temp_dir), patch.object(
                    app,
                    "get_download_timeout",
                    return_value=3600,
                ), patch.object(
                    app,
                    "run_download_command",
                    side_effect=app.subprocess.TimeoutExpired(cmd=["yt-dlp"], timeout=3600),
                ):
                    app.run_download(job_id, "https://example.com/video", "video", None)
                self.assertEqual(list(Path(temp_dir).glob(f"{job_id}.*")), [])
        finally:
            app.jobs.pop(job_id, None)

    def test_cleanup_preserves_completed_file_and_other_jobs(self):
        job_id = "timeout-preserve"
        with tempfile.TemporaryDirectory() as temp_dir:
            completed = Path(temp_dir) / f"{job_id}.mp4"
            other_job = Path(temp_dir) / f"{job_id}-other.f11.mp4.part"
            completed.write_bytes(b"complete")
            other_job.write_bytes(b"other")
            with patch.object(app, "DOWNLOAD_DIR", temp_dir):
                app.cleanup_job_files(job_id)
            self.assertTrue(completed.exists())
            self.assertTrue(other_job.exists())


if __name__ == "__main__":
    unittest.main()
