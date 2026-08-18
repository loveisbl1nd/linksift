"""Tests for v0.3 multi-output download pipeline.

Covers:
- Parent job structure (one queue slot, one worker slot)
- Sequential artifact execution
- Aggregate status (done/partial/error)
- Shared deadline across artifacts
- MP4→MP3 reuse and fallback
- Cancellation in yt-dlp and ffmpeg
- Artifact file endpoint and legacy compatibility
- Full parent cleanup
"""
import io
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import app


# What a real ffmpeg writes on the MERGED stream try_ffmpeg_reuse reads: the
# input dump carrying `Duration:` plus the unindented key=value blocks of
# `-progress pipe:1`. Kept short so the reader thread reaches EOF at once.
FFMPEG_TRANSCRIPT = (
    "ffmpeg version 6.0\n"
    "  Duration: 00:00:15.00, start: 0.000000, bitrate: 129 kb/s\n"
    "out_time=00:00:07.50\n"
    "speed=12.3x\n"
    "progress=continue\n"
    "out_time=00:00:15.00\n"
    "speed=12.5x\n"
    "progress=end\n"
)

# A failing run emits its diagnostics on the same merged stream and never
# reaches a progress block.
FFMPEG_FAILURE_TRANSCRIPT = (
    "ffmpeg version 6.0\n"
    "ffmpeg error\n"
)


class ParentJobStructureTests(unittest.TestCase):
    """Verify one parent job = one queue slot = one worker slot."""

    def setUp(self):
        app.jobs.clear()
        app.processes.clear()
        app.reset_scheduler()
        self.client = app.app.test_client()

    def tearDown(self):
        app.reset_scheduler()
        app.jobs.clear()
        app.processes.clear()

    def test_multi_output_uses_one_queue_slot(self):
        """Multi-output request creates exactly one parent job."""
        with patch.object(app, "runtime_unavailable_response", return_value=None):
            response = self.client.post("/api/download", json={
                "url": "https://example.test/video",
                "outputs": [
                    {"type": "video", "format_id": "22"},
                    {"type": "video", "format_id": "136"},
                    {"type": "audio"}
                ]
            })

        self.assertEqual(response.status_code, 200)
        job_id = response.get_json()["job_id"]

        with app.jobs_lock:
            # Exactly one parent job
            self.assertEqual(len(app.jobs), 1)
            job = app.jobs[job_id]
            # Parent has 3 artifacts
            self.assertEqual(len(job["artifacts"]), 3)

    def test_multi_output_uses_one_worker_slot(self):
        """Multi-output job occupies exactly one worker slot."""
        release = threading.Event()
        job_started = threading.Event()

        def fake_pipeline(job_id, url, title):
            job_started.set()
            release.wait(timeout=10)

        try:
            with patch.dict(os.environ, {"LINKSIFT_MAX_CONCURRENT_DOWNLOADS": "1"}), patch.object(
                app, "run_pipeline", side_effect=fake_pipeline
            ), patch.object(app, "runtime_unavailable_response", return_value=None):
                app.reset_scheduler()

                # Submit multi-output job
                first = self.client.post("/api/download", json={
                    "url": "https://example.test/video",
                    "outputs": [
                        {"type": "video", "format_id": "22"},
                        {"type": "audio"}
                    ]
                })
                self.assertEqual(first.status_code, 200)
                self.assertTrue(job_started.wait(timeout=5))

                # Second job should be queued (worker slot occupied)
                second = self.client.post("/api/download", json={"url": "https://example.test/v2"})
                self.assertEqual(second.status_code, 200)

                # Verify second job is queued
                status = self.client.get(f"/api/status/{second.get_json()['job_id']}").get_json()
                self.assertEqual(status["status"], "queued")
                self.assertEqual(status["queue_position"], 1)

                release.set()
                app.reset_scheduler()
        finally:
            release.set()


class SequentialArtifactExecutionTests(unittest.TestCase):
    """Verify artifacts execute sequentially in correct order."""

    def setUp(self):
        app.jobs.clear()
        app.processes.clear()

    def tearDown(self):
        app.jobs.clear()
        app.processes.clear()

    def test_videos_execute_before_audio(self):
        """Video artifacts must complete before audio reuse attempt."""
        job_id = "testjob123"
        url = "https://example.test/video"
        title = "Test Video"

        execution_order_log = []

        def track_execution(cmd, parent_job_id, progress_target, timeout):
            # Track which artifact is being processed
            if "-x" in cmd:  # Audio extraction
                execution_order_log.append("audio")
            else:  # Video download
                # Check format_id in -f argument (e.g., "22+bestaudio/best")
                if any("22+bestaudio" in arg for arg in cmd):
                    execution_order_log.append("video_720p")
                else:
                    execution_order_log.append("video_1080p")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            app, "DOWNLOAD_DIR", temp_dir
        ), patch.object(app, "run_download_command", side_effect=track_execution), patch.object(
            app, "get_concurrent_fragments", return_value=1
        ), patch.object(app, "ytdlp_runtime_args", return_value=[]):
            # Create job with properly initialized artifacts (like start_download does)
            from output_pipeline import plan_artifacts
            outputs = [
                {"type": "audio"},  # Listed first
                {"type": "video", "format_id": "22"},
                {"type": "video", "format_id": "136"}
            ]
            execution_order, display_order = plan_artifacts(outputs)

            # Initialize runtime fields
            for art in execution_order:
                art["status"] = "pending"
                art["phase"] = "pending"
                art["downloaded_bytes"] = 0
                art["total_bytes"] = None
                art["speed"] = None
                art["eta"] = None
                art["percent"] = None
                art["error"] = None
                art["attempt"] = 0
                art["max_attempts"] = app.get_job_retries() + 1

            app.jobs[job_id] = {
                "id": job_id,
                "status": "queued",
                "phase": "queued",
                "url": url,
                "title": title,
                "artifacts": display_order,
                "execution_order": execution_order,
                "current_artifact_id": None,
                "cancel_requested": False,
                "cancel_event": threading.Event(),
                "created_at": time.time(),
                "started_at": None,
                "finished_at": None,
            }

            app.run_pipeline(job_id, url, title)

            # Videos must execute before audio
            self.assertIn("video_720p", execution_order_log[:2])
            self.assertIn("video_1080p", execution_order_log[:2])
            self.assertEqual(execution_order_log[2], "audio")


class AggregateStatusTests(unittest.TestCase):
    """Verify parent status reflects artifact outcomes."""

    def setUp(self):
        app.jobs.clear()
        app.processes.clear()

    def tearDown(self):
        app.jobs.clear()
        app.processes.clear()

    def _initialize_artifacts(self, execution_order):
        """Helper to initialize runtime fields for artifacts."""
        for art in execution_order:
            art["status"] = "pending"
            art["phase"] = "pending"
            art["downloaded_bytes"] = 0
            art["total_bytes"] = None
            art["speed"] = None
            art["eta"] = None
            art["percent"] = None
            art["error"] = None
            art["attempt"] = 0
            art["max_attempts"] = app.get_job_retries() + 1

    def test_all_success_returns_done(self):
        """All artifacts succeed → parent status = done."""
        job_id = "testjob123"

        class MockFFmpegProcess:
            """A working ffmpeg for the audio artifact's MP4->MP3 reuse.

            try_ffmpeg_reuse spawns via subprocess.Popen. Without this stub the
            test would invoke the host's real ffmpeg, so it would pass only on
            machines that happen to have it installed. It drains a merged
            stdout pipe on a reader thread, so `stdout` must be an iterable,
            closeable stream that reaches EOF.
            """

            def __init__(self, cmd, **kwargs):
                Path(cmd[-1]).write_bytes(b"fake audio")
                self.returncode = 0
                self.stdout = io.StringIO(FFMPEG_TRANSCRIPT)

            def wait(self, timeout=None):
                return self.returncode

            def poll(self):
                return self.returncode

        def always_success(cmd, parent_job_id, progress_target, timeout):
            # Create actual output file when download succeeds
            if "-o" in cmd:
                output_template = cmd[cmd.index("-o") + 1]
                # Replace %(ext)s with appropriate extension
                if "-x" in cmd:  # Audio extraction
                    output_file = output_template.replace("%(ext)s", "mp3")
                else:  # Video
                    output_file = output_template.replace("%(ext)s", "mp4")
                Path(output_file).write_bytes(b"fake content")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            app, "DOWNLOAD_DIR", temp_dir
        ), patch.object(app, "run_download_command", side_effect=always_success), patch.object(
            subprocess, "Popen", MockFFmpegProcess
        ), patch.object(
            app, "get_concurrent_fragments", return_value=1
        ), patch.object(app, "ytdlp_runtime_args", return_value=[]):
            # Initialize artifacts properly
            from output_pipeline import plan_artifacts
            outputs = [
                {"type": "video", "format_id": "22"},
                {"type": "audio"}
            ]
            execution_order, display_order = plan_artifacts(outputs)
            self._initialize_artifacts(execution_order)

            app.jobs[job_id] = {
                "id": job_id,
                "status": "queued",
                "phase": "queued",
                "url": "https://example.test/video",
                "title": "Test",
                "artifacts": display_order,
                "execution_order": execution_order,
                "current_artifact_id": None,
                "cancel_requested": False,
                "cancel_event": threading.Event(),
                "created_at": time.time(),
                "started_at": None,
                "finished_at": None,
            }

            app.run_pipeline(job_id, "https://example.test/video", "Test")

            job = app.jobs[job_id]
            self.assertEqual(job["status"], "done")

    def test_partial_success_returns_partial(self):
        """Some artifacts succeed, some fail → parent status = partial."""
        job_id = "testjob123"
        call_count = [0]

        def mixed_results(cmd, parent_job_id, progress_target, timeout):
            call_count[0] += 1
            if call_count[0] == 1:  # First artifact succeeds
                # Create actual output file
                if "-o" in cmd:
                    output_template = cmd[cmd.index("-o") + 1]
                    output_file = output_template.replace("%(ext)s", "mp4")
                    Path(output_file).write_bytes(b"fake content")
                return subprocess.CompletedProcess(cmd, 0, "", "")
            else:  # Second artifact fails
                return subprocess.CompletedProcess(cmd, 1, "", "ERROR: Download failed")

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            app, "DOWNLOAD_DIR", temp_dir
        ), patch.object(app, "run_download_command", side_effect=mixed_results), patch.object(
            app, "get_concurrent_fragments", return_value=1
        ), patch.object(app, "ytdlp_runtime_args", return_value=[]):
            # Initialize artifacts properly
            from output_pipeline import plan_artifacts
            outputs = [
                {"type": "video", "format_id": "22"},
                {"type": "video", "format_id": "136"}
            ]
            execution_order, display_order = plan_artifacts(outputs)
            self._initialize_artifacts(execution_order)

            app.jobs[job_id] = {
                "id": job_id,
                "status": "queued",
                "phase": "queued",
                "url": "https://example.test/video",
                "title": "Test",
                "artifacts": display_order,
                "execution_order": execution_order,
                "current_artifact_id": None,
                "cancel_requested": False,
                "cancel_event": threading.Event(),
                "created_at": time.time(),
                "started_at": None,
                "finished_at": None,
            }

            app.run_pipeline(job_id, "https://example.test/video", "Test")

            job = app.jobs[job_id]
            self.assertEqual(job["status"], "partial")

    def test_all_fail_returns_error(self):
        """All artifacts fail → parent status = error."""
        job_id = "testjob123"

        def always_fail(cmd, parent_job_id, progress_target, timeout):
            return subprocess.CompletedProcess(cmd, 1, "", "ERROR: Download failed")

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            app, "DOWNLOAD_DIR", temp_dir
        ), patch.object(app, "run_download_command", side_effect=always_fail), patch.object(
            app, "get_concurrent_fragments", return_value=1
        ), patch.object(app, "ytdlp_runtime_args", return_value=[]):
            # Initialize artifacts properly
            from output_pipeline import plan_artifacts
            outputs = [
                {"type": "video", "format_id": "22"},
                {"type": "audio"}
            ]
            execution_order, display_order = plan_artifacts(outputs)

            # Initialize runtime fields
            for art in execution_order:
                art["status"] = "pending"
                art["phase"] = "pending"
                art["downloaded_bytes"] = 0
                art["total_bytes"] = None
                art["speed"] = None
                art["eta"] = None
                art["percent"] = None
                art["error"] = None
                art["attempt"] = 0
                art["max_attempts"] = app.get_job_retries() + 1

            app.jobs[job_id] = {
                "id": job_id,
                "status": "queued",
                "phase": "queued",
                "url": "https://example.test/video",
                "title": "Test",
                "artifacts": display_order,
                "execution_order": execution_order,
                "current_artifact_id": None,
                "cancel_requested": False,
                "cancel_event": threading.Event(),
                "created_at": time.time(),
                "started_at": None,
                "finished_at": None,
            }

            app.run_pipeline(job_id, "https://example.test/video", "Test")

            job = app.jobs[job_id]
            self.assertEqual(job["status"], "error")

    def test_deadline_shared_across_artifacts(self):
        """Timeout budget is shared, not reset per artifact."""
        job_id = "testjob123"
        call_count = [0]
        deadlines = []

        def track_deadline(cmd, parent_job_id, progress_target, timeout):
            call_count[0] += 1
            deadlines.append(timeout)
            # First artifact uses most of the budget
            if call_count[0] == 1:
                time.sleep(0.1)
            # Create actual output file when download succeeds
            if "-o" in cmd:
                output_template = cmd[cmd.index("-o") + 1]
                output_file = output_template.replace("%(ext)s", "mp4")
                Path(output_file).write_bytes(b"fake content")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            app, "DOWNLOAD_DIR", temp_dir
        ), patch.object(app, "run_download_command", side_effect=track_deadline), patch.object(
            app, "get_download_timeout", return_value=3600
        ), patch.object(app, "get_concurrent_fragments", return_value=1
        ), patch.object(app, "ytdlp_runtime_args", return_value=[]):
            # Initialize artifacts properly
            from output_pipeline import plan_artifacts
            outputs = [
                {"type": "video", "format_id": "22"},
                {"type": "video", "format_id": "136"}
            ]
            execution_order, display_order = plan_artifacts(outputs)

            # Initialize runtime fields
            for art in execution_order:
                art["status"] = "pending"
                art["phase"] = "pending"
                art["downloaded_bytes"] = 0
                art["total_bytes"] = None
                art["speed"] = None
                art["eta"] = None
                art["percent"] = None
                art["error"] = None
                art["attempt"] = 0
                art["max_attempts"] = app.get_job_retries() + 1

            app.jobs[job_id] = {
                "id": job_id,
                "status": "queued",
                "phase": "queued",
                "url": "https://example.test/video",
                "title": "Test",
                "artifacts": display_order,
                "execution_order": execution_order,
                "current_artifact_id": None,
                "cancel_requested": False,
                "cancel_event": threading.Event(),
                "created_at": time.time(),
                "started_at": None,
                "finished_at": None,
            }

            app.run_pipeline(job_id, "https://example.test/video", "Test")

            # Verify deadlines decrease (shared budget)
            self.assertEqual(len(deadlines), 2)
            self.assertLess(deadlines[1], deadlines[0])


class MP4MP3ReuseTests(unittest.TestCase):
    """Verify MP4→MP3 reuse and fallback behavior."""

    def setUp(self):
        app.jobs.clear()
        app.processes.clear()

    def tearDown(self):
        app.jobs.clear()
        app.processes.clear()

    def test_mp3_reuses_mp4_when_available(self):
        """Audio artifact reuses MP4 via ffmpeg when video succeeded."""
        job_id = "testjob123"
        ffmpeg_called = [False]

        class MockFFmpegProcess:
            def __init__(self, cmd, **kwargs):
                if "ffmpeg" in cmd[0]:
                    ffmpeg_called[0] = True
                    # Create output file
                    output_file = cmd[-1]
                    Path(output_file).write_bytes(b"fake audio")
                self.returncode = 0
                self.stdout = io.StringIO(FFMPEG_TRANSCRIPT)

            def wait(self, timeout=None):
                return self.returncode

        def video_success(cmd, parent_job_id, progress_target, timeout):
            # Create MP4 file
            output_pattern = cmd[cmd.index("-o") + 1]
            output_file = output_pattern.replace("%(ext)s", "mp4")
            Path(output_file).write_bytes(b"fake video")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            app, "DOWNLOAD_DIR", temp_dir
        ), patch.object(app, "run_download_command", side_effect=video_success), patch.object(
            subprocess, "Popen", MockFFmpegProcess
        ), patch.object(app, "get_concurrent_fragments", return_value=1
        ), patch.object(app, "ytdlp_runtime_args", return_value=[]):
            # Initialize artifacts properly
            from output_pipeline import plan_artifacts
            outputs = [
                {"type": "video", "format_id": "22"},
                {"type": "audio"}
            ]
            execution_order, display_order = plan_artifacts(outputs)

            # Initialize runtime fields for each artifact
            for art in execution_order:
                art["max_attempts"] = 1  # No retries for test simplicity
                art["status"] = "queued"
                art["phase"] = "queued"

            app.jobs[job_id] = {
                "id": job_id,
                "status": "queued",
                "phase": "queued",
                "url": "https://example.test/video",
                "title": "Test",
                "artifacts": display_order,
                "execution_order": execution_order,
                "current_artifact_id": None,
                "cancel_requested": False,
                "cancel_event": threading.Event(),
                "created_at": time.time(),
                "started_at": None,
                "finished_at": None,
            }

            app.run_pipeline(job_id, "https://example.test/video", "Test")

            # FFmpeg must have been called for audio extraction
            self.assertTrue(ffmpeg_called[0])

    def test_mp3_fallback_to_ytdlp_on_ffmpeg_failure(self):
        """Audio falls back to yt-dlp if ffmpeg fails."""
        job_id = "testjob123"
        ytdlp_calls = [0]

        class MockFailingFFmpeg:
            """An ffmpeg that exits non-zero and writes no output file.

            try_ffmpeg_reuse spawns via subprocess.Popen, so patching
            subprocess.run here would be a no-op and the test would silently
            depend on a real ffmpeg being installed on the host. It drains a
            merged stdout pipe on a reader thread, so `stdout` must be an
            iterable, closeable stream that reaches EOF.
            """

            def __init__(self, cmd, **kwargs):
                self.cmd = cmd
                self.returncode = 1
                self.stdout = io.StringIO(FFMPEG_FAILURE_TRANSCRIPT)

            def wait(self, timeout=None):
                return self.returncode

            def poll(self):
                return self.returncode

        def track_ytdlp(cmd, parent_job_id, progress_target, timeout):
            ytdlp_calls[0] += 1
            # Create output file
            output_pattern = cmd[cmd.index("-o") + 1]
            ext = "mp4" if "-x" not in cmd else "mp3"
            output_file = output_pattern.replace("%(ext)s", ext)
            Path(output_file).write_bytes(b"fake content")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            app, "DOWNLOAD_DIR", temp_dir
        ), patch.object(app, "run_download_command", side_effect=track_ytdlp), patch.object(
            subprocess, "Popen", MockFailingFFmpeg
        ), patch.object(app, "get_concurrent_fragments", return_value=1
        ), patch.object(app, "ytdlp_runtime_args", return_value=[]):
            # Initialize artifacts properly
            from output_pipeline import plan_artifacts
            outputs = [
                {"type": "video", "format_id": "22"},
                {"type": "audio"}
            ]
            execution_order, display_order = plan_artifacts(outputs)

            # Initialize runtime fields for each artifact
            for art in execution_order:
                art["max_attempts"] = 1  # No retries for test simplicity
                art["status"] = "queued"
                art["phase"] = "queued"

            app.jobs[job_id] = {
                "id": job_id,
                "status": "queued",
                "phase": "queued",
                "url": "https://example.test/video",
                "title": "Test",
                "artifacts": display_order,
                "execution_order": execution_order,
                "current_artifact_id": None,
                "cancel_requested": False,
                "cancel_event": threading.Event(),
                "created_at": time.time(),
                "started_at": None,
                "finished_at": None,
            }

            app.run_pipeline(job_id, "https://example.test/video", "Test")

            # yt-dlp called twice: once for video, once for audio fallback
            self.assertEqual(ytdlp_calls[0], 2)


class ArtifactEndpointTests(unittest.TestCase):
    """Verify artifact file endpoint and legacy compatibility."""

    def setUp(self):
        app.jobs.clear()
        self.client = app.app.test_client()

    def tearDown(self):
        app.jobs.clear()

    def test_artifact_endpoint_serves_file(self):
        """GET /api/file/<job_id>/<artifact_id> serves artifact file."""
        job_id = "testjob123"
        artifact_id = "testart001"

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            app, "DOWNLOAD_DIR", temp_dir
        ):
            # Create artifact file
            artifact_file = Path(temp_dir) / f"{job_id}.{artifact_id}.mp4"
            artifact_file.write_bytes(b"fake video content")

            app.jobs[job_id] = {
                "id": job_id,
                "status": "done",
                "phase": "done",
                "artifacts": [{
                    "id": artifact_id,
                    "status": "done",
                    "file": str(artifact_file),
                    "filename": "video.mp4"
                }]
            }

            response = self.client.get(f"/api/file/{job_id}/{artifact_id}")
            self.assertEqual(response.status_code, 200)
            # Read data before closing response
            self.assertEqual(response.data, b"fake video content")
            response.close()

    def test_legacy_endpoint_409_for_multi_output(self):
        """GET /api/file/<job_id> returns 409 for multi-output jobs."""
        job_id = "testjob123"

        app.jobs[job_id] = {
            "id": job_id,
            "status": "done",
            "phase": "done",
            "artifacts": [
                {"id": "art1", "status": "done"},
                {"id": "art2", "status": "done"}
            ]
        }

        response = self.client.get(f"/api/file/{job_id}")
        self.assertEqual(response.status_code, 409)


class ParentCleanupTests(unittest.TestCase):
    """Verify full parent cleanup on cancel/timeout."""

    def setUp(self):
        app.jobs.clear()
        app.processes.clear()

    def tearDown(self):
        app.jobs.clear()
        app.processes.clear()

    def test_cancel_cleans_all_artifacts(self):
        """Cancellation cleans all artifact files including completed ones."""
        job_id = "testjob123"

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            app, "DOWNLOAD_DIR", temp_dir
        ):
            # Create some artifact files
            (Path(temp_dir) / f"{job_id}.art1.mp4").write_bytes(b"video1")
            (Path(temp_dir) / f"{job_id}.art2.mp3").write_bytes(b"audio")

            app.jobs[job_id] = {
                "id": job_id,
                "status": "downloading",
                "phase": "downloading",
                "cancel_requested": False,
                "cancel_event": threading.Event(),
                "artifacts": [
                    {"id": "art1", "status": "done"},
                    {"id": "art2", "status": "done"}
                ]
            }

            # Trigger cancellation
            app.jobs[job_id]["cancel_requested"] = True
            app.finish_pipeline_cancelled(job_id)

            # All files must be cleaned
            self.assertFalse((Path(temp_dir) / f"{job_id}.art1.mp4").exists())
            self.assertFalse((Path(temp_dir) / f"{job_id}.art2.mp3").exists())


if __name__ == "__main__":
    unittest.main()
