import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import app


EXPECTED_STATUS_FIELDS = {
    "status",
    "phase",
    "downloaded_bytes",
    "total_bytes",
    "speed",
    "eta",
    "percent",
    "error",
    "filename",
    "queue_position",
    "started_at",
    "attempt",
    "max_attempts",
    # Packaging (ffmpeg) progress, added additively. They mirror the current
    # artifact while it is being packaged and are null otherwise, so a client
    # that ignores them sees exactly the response it saw before.
    "processed_seconds",
    "duration_seconds",
    "processing_speed",
}

# The pre-packaging-progress contract. Every one of these must survive, which is
# what makes the addition above additive rather than breaking.
LEGACY_STATUS_FIELDS = EXPECTED_STATUS_FIELDS - {
    "processed_seconds",
    "duration_seconds",
    "processing_speed",
}


class StatusQueueFieldsTests(unittest.TestCase):
    def setUp(self):
        app.jobs.clear()
        app.processes.clear()
        self.client = app.app.test_client()

    def tearDown(self):
        app.jobs.clear()
        app.processes.clear()

    def test_queued_job_reports_one_based_queue_position(self):
        job_id = "queuedpos1"
        app.jobs[job_id] = {"id": job_id, "status": "queued", "phase": "queued", "started_at": None}
        fake = Mock()
        fake.queue_position.return_value = 2
        with patch.object(app, "get_scheduler", return_value=fake):
            payload = self.client.get(f"/api/status/{job_id}").get_json()
        self.assertEqual(payload["status"], "queued")
        self.assertEqual(payload["queue_position"], 2)
        self.assertIsNone(payload["started_at"])
        fake.queue_position.assert_called_once_with(job_id)

    def test_non_queued_jobs_have_null_queue_position(self):
        fake = Mock()
        for status in ("downloading", "cancelling", "done", "error", "cancelled", "timed_out"):
            with self.subTest(status=status):
                job_id = f"st{status}"[:10]
                app.jobs[job_id] = {"id": job_id, "status": status, "started_at": 111.0}
                with patch.object(app, "get_scheduler", return_value=fake):
                    payload = self.client.get(f"/api/status/{job_id}").get_json()
                self.assertIsNone(payload["queue_position"])
                self.assertEqual(payload["started_at"], 111.0)
        fake.queue_position.assert_not_called()

    def test_status_response_keeps_existing_fields_and_adds_new_ones(self):
        job_id = "fieldcheck"
        app.jobs[job_id] = {"id": job_id, "status": "downloading", "phase": "downloading", "started_at": 5.0}
        payload = self.client.get(f"/api/status/{job_id}").get_json()
        self.assertEqual(set(payload), EXPECTED_STATUS_FIELDS)
        # Additive, not breaking: nothing a pre-packaging client read went away.
        self.assertTrue(LEGACY_STATUS_FIELDS.issubset(set(payload)))
        # A job that never packages reports the new fields as null rather than
        # omitting them, so the response shape stays constant across phases.
        for field in ("processed_seconds", "duration_seconds", "processing_speed"):
            with self.subTest(field=field):
                self.assertIsNone(payload[field])

    def test_started_at_transitions_from_null_to_timestamp(self):
        job_id = "startstamp"
        app.jobs[job_id] = {
            "id": job_id,
            "status": "queued",
            "phase": "queued",
            "started_at": None,
            "title": "",
        }
        fake = Mock()
        fake.queue_position.return_value = 1
        with patch.object(app, "get_scheduler", return_value=fake):
            before = self.client.get(f"/api/status/{job_id}").get_json()
        self.assertIsNone(before["started_at"])

        import os

        failed = subprocess.CompletedProcess([], 1, "", "boom")
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            app, "DOWNLOAD_DIR", temp_dir
        ), patch.dict(os.environ, {"LINKSIFT_JOB_RETRIES": "0"}), patch.object(
            app, "run_download_command", return_value=failed
        ):
            app.run_download(job_id, "https://example.test/video", "video", None)

        after = self.client.get(f"/api/status/{job_id}").get_json()
        self.assertIsInstance(after["started_at"], float)
        self.assertEqual(after["status"], "error")


if __name__ == "__main__":
    unittest.main()
