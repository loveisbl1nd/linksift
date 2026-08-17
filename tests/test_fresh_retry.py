import itertools
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class RetrySettingsParsingTests(unittest.TestCase):
    def test_job_retries_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LINKSIFT_JOB_RETRIES", None)
            self.assertEqual(app.get_job_retries(), 2)

    def test_job_retries_valid_values(self):
        for value, expected in (("0", 0), ("1", 1), ("3", 3), ("5", 5)):
            with self.subTest(value=value), patch.dict(os.environ, {"LINKSIFT_JOB_RETRIES": value}):
                self.assertEqual(app.get_job_retries(), expected)

    def test_job_retries_invalid_values_fall_back(self):
        for value in ("abc", "", "-1", "6", "100", "1.5"):
            with self.subTest(value=value), patch.dict(os.environ, {"LINKSIFT_JOB_RETRIES": value}):
                self.assertEqual(app.get_job_retries(), 2)

    def test_retry_base_delay_default_and_validation(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LINKSIFT_RETRY_BASE_DELAY", None)
            self.assertEqual(app.get_retry_base_delay(), 2)
        for value, expected in (("0", 0.0), ("1.5", 1.5), ("4", 4.0)):
            with self.subTest(value=value), patch.dict(os.environ, {"LINKSIFT_RETRY_BASE_DELAY": value}):
                self.assertEqual(app.get_retry_base_delay(), expected)
        for value in ("abc", "", "-3", "nan", "inf"):
            with self.subTest(value=value), patch.dict(os.environ, {"LINKSIFT_RETRY_BASE_DELAY": value}):
                self.assertEqual(app.get_retry_base_delay(), 2)

    def test_backoff_is_exponential_and_capped(self):
        self.assertEqual(app.retry_backoff_delay(1, 2), 2)
        self.assertEqual(app.retry_backoff_delay(2, 2), 4)
        self.assertEqual(app.retry_backoff_delay(3, 2), 8)
        self.assertEqual(app.retry_backoff_delay(4, 2), app.MAX_RETRY_BACKOFF_SECONDS)
        self.assertEqual(app.retry_backoff_delay(1, 100), app.MAX_RETRY_BACKOFF_SECONDS)


class TransientErrorClassifierTests(unittest.TestCase):
    def test_transient_errors_are_retryable(self):
        for message in (
            "ERROR: unable to download video data: HTTP Error 403: Forbidden",
            "ERROR: HTTP Error 429: Too Many Requests",
            "ERROR: HTTP Error 500: Internal Server Error",
            "ERROR: HTTP Error 502: Bad Gateway",
            "ERROR: HTTP Error 503: Service Unavailable",
            "ERROR: HTTP Error 504: Gateway Timeout",
            "ERROR: Connection reset by peer",
            "ERROR: Remote end closed connection without response",
            "ERROR: Temporary failure in name resolution",
            "ERROR: getaddrinfo failed",
            "ERROR: unable to download video data: The read operation timed out",
        ):
            with self.subTest(message=message):
                self.assertTrue(app.is_transient_download_error(message))

    def test_permanent_errors_are_not_retryable(self):
        for message in (
            "ERROR: Unsupported URL: https://example.test/x",
            "ERROR: Video unavailable",
            "ERROR: Private video. Sign in if you've been granted access",
            "ERROR: This video has been removed by the uploader",
            "ERROR: Requested format is not available",
            "ERROR: Sign in to confirm you're not a bot. Use --cookies for authentication",
            "ERROR: This video is only available to Music Premium members",
        ):
            with self.subTest(message=message):
                self.assertFalse(app.is_transient_download_error(message))

    def test_unknown_and_empty_errors_are_not_retryable(self):
        self.assertFalse(app.is_transient_download_error("something odd happened"))
        self.assertFalse(app.is_transient_download_error(""))
        self.assertFalse(app.is_transient_download_error(None))

    def test_permanent_marker_wins_over_transient_marker(self):
        combined = "ERROR: HTTP Error 403: Forbidden. Sign in to confirm you're not a bot"
        self.assertFalse(app.is_transient_download_error(combined))


class WaitBeforeRetryTests(unittest.TestCase):
    def test_cancelled_job_returns_false_immediately(self):
        job = {"cancel_requested": True}
        self.assertFalse(app.wait_before_retry(job, 5, time.monotonic() + 100))

    def test_expired_deadline_returns_false_immediately(self):
        job = {"cancel_requested": False}
        self.assertFalse(app.wait_before_retry(job, 5, time.monotonic() - 1))

    def test_zero_delay_returns_true_immediately(self):
        job = {"cancel_requested": False}
        self.assertTrue(app.wait_before_retry(job, 0, time.monotonic() + 100))

    def test_cancel_event_wakes_the_wait(self):
        event = threading.Event()
        event.set()
        job = {"cancel_requested": False, "cancel_event": event}
        self.assertFalse(app.wait_before_retry(job, 5, time.monotonic() + 100))


class FreshRetryDownloadTests(unittest.TestCase):
    def setUp(self):
        app.jobs.clear()
        app.processes.clear()
        self.client = app.app.test_client()

    def tearDown(self):
        app.jobs.clear()
        app.processes.clear()

    def test_transient_403_retries_and_succeeds(self):
        job_id = "retryok001"
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            app, "DOWNLOAD_DIR", temp_dir
        ), patch.dict(os.environ, {"LINKSIFT_JOB_RETRIES": "2", "LINKSIFT_RETRY_BASE_DELAY": "0"}):
            partial = Path(temp_dir) / f"{job_id}.f137.mp4.part"
            partial.write_bytes(b"partial")
            output = Path(temp_dir) / f"{job_id}.mp4"
            calls = []

            def flaky(cmd, parent_job_id, job, timeout):
                calls.append(list(cmd))
                if len(calls) == 1:
                    return subprocess.CompletedProcess(cmd, 1, "", "ERROR: HTTP Error 403: Forbidden")
                # The partial file must survive between attempts so yt-dlp can resume.
                self.assertTrue(partial.exists())
                # The transient error from attempt 1 must have been cleared.
                self.assertIsNone(job.get("error"))
                output.write_bytes(b"final")
                return subprocess.CompletedProcess(cmd, 0, "", "")

            app.jobs[job_id] = {"status": "downloading", "title": "", "error": "previous failure"}
            with patch.object(app, "run_download_command", side_effect=flaky):
                app.run_download(job_id, "https://example.test/v", "video", None)

            job = app.jobs[job_id]
            self.assertEqual(job["status"], "done")
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0], calls[1])  # same command, fresh extraction
            self.assertEqual(job["attempt"], 2)
            self.assertEqual(job["max_attempts"], 3)

    def test_transient_error_exhausts_retries_then_errors(self):
        job_id = "retryexh01"
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            app, "DOWNLOAD_DIR", temp_dir
        ), patch.dict(os.environ, {"LINKSIFT_JOB_RETRIES": "1", "LINKSIFT_RETRY_BASE_DELAY": "0"}):
            partial = Path(temp_dir) / f"{job_id}.f137.mp4.part"
            partial.write_bytes(b"partial")
            calls = []

            def always_403(cmd, parent_job_id, job, timeout):
                calls.append(timeout)
                return subprocess.CompletedProcess(cmd, 1, "", "ERROR: HTTP Error 403: Forbidden")

            app.jobs[job_id] = {"status": "downloading", "title": ""}
            with patch.object(app, "run_download_command", side_effect=always_403):
                app.run_download(job_id, "https://example.test/v", "video", None)

            job = app.jobs[job_id]
            self.assertEqual(job["status"], "error")
            self.assertEqual(len(calls), 2)
            self.assertIn("403", job["error"])
            self.assertFalse(partial.exists())  # intermediates cleaned after final failure

    def test_429_and_5xx_are_retried_by_the_engine(self):
        job_id = "retry429x1"
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            app, "DOWNLOAD_DIR", temp_dir
        ), patch.dict(os.environ, {"LINKSIFT_JOB_RETRIES": "2", "LINKSIFT_RETRY_BASE_DELAY": "0"}):
            output = Path(temp_dir) / f"{job_id}.mp4"
            responses = [
                subprocess.CompletedProcess([], 1, "", "ERROR: HTTP Error 429: Too Many Requests"),
                subprocess.CompletedProcess([], 1, "", "ERROR: HTTP Error 502: Bad Gateway"),
            ]

            def flaky(cmd, parent_job_id, job, timeout):
                if responses:
                    return responses.pop(0)
                output.write_bytes(b"final")
                return subprocess.CompletedProcess(cmd, 0, "", "")

            app.jobs[job_id] = {"status": "downloading", "title": ""}
            with patch.object(app, "run_download_command", side_effect=flaky):
                app.run_download(job_id, "https://example.test/v", "video", None)

            self.assertEqual(app.jobs[job_id]["status"], "done")
            self.assertEqual(app.jobs[job_id]["attempt"], 3)

    def test_permanent_errors_do_not_retry(self):
        for message in ("ERROR: Requested format is not available", "ERROR: Video unavailable"):
            with self.subTest(message=message):
                job_id = "noretry001"
                app.jobs.clear()
                calls = []

                def permanent(cmd, parent_job_id, job, timeout):
                    calls.append(1)
                    return subprocess.CompletedProcess(cmd, 1, "", message)

                with tempfile.TemporaryDirectory() as temp_dir, patch.object(
                    app, "DOWNLOAD_DIR", temp_dir
                ), patch.dict(os.environ, {"LINKSIFT_JOB_RETRIES": "3", "LINKSIFT_RETRY_BASE_DELAY": "0"}):
                    app.jobs[job_id] = {"status": "downloading", "title": ""}
                    with patch.object(app, "run_download_command", side_effect=permanent):
                        app.run_download(job_id, "https://example.test/v", "video", None)
                self.assertEqual(len(calls), 1)
                self.assertEqual(app.jobs[job_id]["status"], "error")

    def test_cancellation_during_backoff_prevents_next_attempt(self):
        job_id = "cancelbkof"
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def fake_wait(job, delay, deadline):
            entered.set()
            release.wait(timeout=10)
            return not job.get("cancel_requested")

        def always_403(cmd, parent_job_id, job, timeout):
            calls.append(1)
            return subprocess.CompletedProcess(cmd, 1, "", "ERROR: HTTP Error 403: Forbidden")

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            app, "DOWNLOAD_DIR", temp_dir
        ), patch.dict(os.environ, {"LINKSIFT_JOB_RETRIES": "3"}), patch.object(
            app, "run_download_command", side_effect=always_403
        ), patch.object(app, "wait_before_retry", side_effect=fake_wait):
            final = Path(temp_dir) / f"{job_id}.mp4"
            final.write_bytes(b"stale")
            app.jobs[job_id] = {
                "id": job_id,
                "status": "downloading",
                "title": "",
                "cancel_requested": False,
                "cancel_event": threading.Event(),
            }
            worker = threading.Thread(
                target=app.run_download, args=(job_id, "https://example.test/v", "video", None)
            )
            worker.start()
            try:
                self.assertTrue(entered.wait(timeout=5))
                response = self.client.delete(f"/api/download/{job_id}")
                self.assertEqual(response.status_code, 200)
            finally:
                release.set()
                worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(calls), 1)  # no second attempt after cancel
            self.assertEqual(app.jobs[job_id]["status"], "cancelled")
            self.assertFalse(final.exists())  # cancelled jobs clean all files

    def test_total_timeout_is_not_reset_between_attempts(self):
        job_id = "deadline01"
        timeouts = []
        # Fake monotonic clock advancing per call: deterministic on every
        # platform regardless of the real clock's resolution.
        ticks = itertools.count()

        def flaky(cmd, parent_job_id, job, timeout):
            timeouts.append(timeout)
            if len(timeouts) == 1:
                return subprocess.CompletedProcess(cmd, 1, "", "ERROR: HTTP Error 403: Forbidden")
            return subprocess.CompletedProcess(cmd, 1, "", "ERROR: Video unavailable")

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            app, "DOWNLOAD_DIR", temp_dir
        ), patch.dict(os.environ, {"LINKSIFT_JOB_RETRIES": "2", "LINKSIFT_RETRY_BASE_DELAY": "0"}), patch.object(
            app, "get_download_timeout", return_value=3600
        ), patch.object(app, "run_download_command", side_effect=flaky), patch.object(
            app.time, "monotonic", side_effect=lambda: next(ticks) * 0.05
        ):
            app.jobs[job_id] = {"status": "downloading", "title": ""}
            app.run_download(job_id, "https://example.test/v", "video", None)

        self.assertEqual(len(timeouts), 2)
        self.assertEqual(timeouts[0], 3600)
        self.assertLess(timeouts[1], 3600)

    def test_deadline_expiry_during_backoff_becomes_timed_out(self):
        job_id = "deadline02"

        def always_403(cmd, parent_job_id, job, timeout):
            return subprocess.CompletedProcess(cmd, 1, "", "ERROR: HTTP Error 403: Forbidden")

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            app, "DOWNLOAD_DIR", temp_dir
        ), patch.dict(os.environ, {"LINKSIFT_JOB_RETRIES": "2"}), patch.object(
            app, "get_download_timeout", return_value=3600
        ), patch.object(app, "run_download_command", side_effect=always_403), patch.object(
            app, "wait_before_retry", return_value=False
        ):
            partial = Path(temp_dir) / f"{job_id}.f1.mp4.part"
            partial.write_bytes(b"partial")
            app.jobs[job_id] = {"status": "downloading", "title": "", "cancel_requested": False}
            app.run_download(job_id, "https://example.test/v", "video", None)

        job = app.jobs[job_id]
        self.assertEqual(job["status"], "timed_out")
        self.assertEqual(job["error"], "Download timed out after 3600 seconds")
        self.assertFalse(partial.exists())

    def test_cancel_after_backoff_never_spawns_second_attempt(self):
        """Reproduces the review race: cancellation lands after the backoff
        completes but before attempt 2 — no second command may run."""
        job_id = "racecncl01"
        calls = []

        def first_403(cmd, parent_job_id, job, timeout):
            calls.append(1)
            return subprocess.CompletedProcess(cmd, 1, "", "ERROR: HTTP Error 403: Forbidden")

        def wait_then_cancel(job, delay, deadline):
            with app.jobs_lock:
                job["cancel_requested"] = True
                job["status"] = "cancelling"
                job["phase"] = "cancelling"
            return True  # the backoff itself "completed" before noticing the cancel

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            app, "DOWNLOAD_DIR", temp_dir
        ), patch.dict(os.environ, {"LINKSIFT_JOB_RETRIES": "3", "LINKSIFT_RETRY_BASE_DELAY": "0"}), patch.object(
            app, "run_download_command", side_effect=first_403
        ), patch.object(app, "wait_before_retry", side_effect=wait_then_cancel):
            app.jobs[job_id] = {"id": job_id, "status": "downloading", "title": "", "cancel_requested": False}
            app.run_download(job_id, "https://example.test/v", "video", None)

        self.assertEqual(len(calls), 1)
        self.assertEqual(app.jobs[job_id]["status"], "cancelled")

    def test_pre_spawn_gate_blocks_popen_when_cancelled(self):
        parent_job_id = "parent123"
        job_id = "gated00001"
        job = {"id": job_id, "status": "cancelling", "cancel_requested": True}
        app.jobs[parent_job_id] = job
        with patch.object(app.subprocess, "Popen") as popen:
            result = app.run_download_command(["yt-dlp", "url"], parent_job_id, job, 60)
        popen.assert_not_called()
        self.assertIsNone(result)
        self.assertNotIn(parent_job_id, app.processes)

    def test_delete_after_spawn_sees_registered_process(self):
        parent_job_id = "parent123"
        job_id = "spawnrace1"
        job = {"id": job_id, "status": "downloading", "title": "", "cancel_requested": False}
        app.jobs[parent_job_id] = job
        started = threading.Event()
        release = threading.Event()

        class FakeProcess:
            def __init__(self):
                self.stdout = iter([])
                self.returncode = 1

            def wait(self, timeout):
                started.set()
                release.wait(timeout=10)
                return 1

            def poll(self):
                return None

        fake = FakeProcess()
        try:
            with patch.object(app.subprocess, "Popen", return_value=fake), patch.object(
                app, "terminate_process_tree"
            ) as terminate:
                worker = threading.Thread(
                    target=app.run_download_command, args=(["yt-dlp", "url"], parent_job_id, job, 60)
                )
                worker.start()
                self.assertTrue(started.wait(timeout=5))
                with app.jobs_lock:
                    self.assertIs(app.processes.get(parent_job_id), fake)
                response = self.client.delete(f"/api/download/{parent_job_id}")
                self.assertEqual(response.get_json()["status"], "cancelling")
                terminate.assert_called_once_with(fake)
                release.set()
                worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            self.assertNotIn(job_id, app.processes)  # no orphan reference
        finally:
            release.set()

    def test_retry_keeps_scheduler_slot_and_reports_attempt_fields(self):
        entered_backoff = threading.Event()
        release = threading.Event()

        def fake_wait(job, delay, deadline):
            entered_backoff.set()
            release.wait(timeout=10)
            return not job.get("cancel_requested")

        def always_403(cmd, parent_job_id, job, timeout):
            return subprocess.CompletedProcess(cmd, 1, "", "ERROR: HTTP Error 403: Forbidden")

        try:
            with patch.dict(os.environ, {
                "LINKSIFT_MAX_CONCURRENT_DOWNLOADS": "1",
                "LINKSIFT_JOB_RETRIES": "1",
            }), patch.object(app, "run_download_command", side_effect=always_403), patch.object(
                app, "wait_before_retry", side_effect=fake_wait
            ), patch.object(app, "runtime_unavailable_response", return_value=None):
                app.reset_scheduler()
                first = self.client.post("/api/download", json={"url": "https://example.test/v1"})
                first_id = first.get_json()["job_id"]
                self.assertTrue(entered_backoff.wait(timeout=5))

                # While job 1 is backing off it must keep the only worker slot.
                second = self.client.post("/api/download", json={"url": "https://example.test/v2"})
                second_id = second.get_json()["job_id"]
                waiting = self.client.get(f"/api/status/{second_id}").get_json()
                self.assertEqual(waiting["status"], "queued")
                self.assertEqual(waiting["queue_position"], 1)

                retrying = self.client.get(f"/api/status/{first_id}").get_json()
                self.assertEqual(retrying["status"], "downloading")
                self.assertEqual(retrying["phase"], "retrying")
                self.assertEqual(retrying["attempt"], 2)
                self.assertEqual(retrying["max_attempts"], 2)
                self.assertIsNone(retrying["error"])

                release.set()
                app.reset_scheduler()
        finally:
            release.set()
            app.reset_scheduler()


if __name__ == "__main__":
    unittest.main()
