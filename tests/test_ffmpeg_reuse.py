"""Regression tests for the ffmpeg reuse step.

Three outcomes must stay distinct:
  * ``True``  - success, artifact published
  * ``False`` - ordinary failure, yt-dlp fallback allowed
  * raise     - cancellation or deadline, NO fallback

The old implementation wrapped the body in ``except Exception: return False``,
which swallowed the re-raised TimeoutExpired and downgraded a cancellation into
an ordinary fallback — i.e. it re-ran yt-dlp for a job the user had stopped.

These tests drive ``try_ffmpeg_reuse`` with a fake Popen so they never invoke
ffmpeg, yt-dlp, or the network.
"""
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import app


JOB = "0123456789"


class FakeFFmpegProcess:
    """Full Popen surface for ffmpeg: pid/poll/communicate/wait/terminate/kill."""

    def __init__(self, cmd, fail=False, fast=True, **kwargs):
        self.cmd = cmd
        self.pid = 7000 + FakeFFmpegProcess.counter
        FakeFFmpegProcess.counter += 1
        self._fail = fail
        self._fast = fast
        self._alive = True
        self.returncode = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self.communicate_calls = 0
        # A real ffmpeg writes the temp output file just before finishing.
        self.temp_output = cmd[-1]
        if not self._fail and self._fast:
            # Success: write the temp file so the publish path is exercised.
            Path(self.temp_output).write_bytes(b"mp3-audio")

    counter = 0

    def poll(self):
        return None if self._alive else self.returncode

    def communicate(self, timeout=None):
        self.communicate_calls += 1
        if self._fast:
            self._alive = False
            self.returncode = 1 if self._fail else 0
            return "", ""
        # Slow: simulate the timeout expiring with no returncode.
        raise subprocess.TimeoutExpired(cmd=self.cmd, timeout=timeout)

    def wait(self, timeout=None):
        if self._alive:
            self._alive = False
            self.returncode = self.returncode if self.returncode is not None else 0
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1
        self._alive = False
        if self.returncode is None:
            self.returncode = -15

    def kill(self):
        self.kill_calls += 1
        self._alive = False
        if self.returncode is None:
            self.returncode = -9

    # subprocess.run uses Popen as a context manager; even though only the
    # ffmpeg spawn is real, the Windows taskkill path reuses the patched
    # Popen, so the fake must support `with ... as p`.
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.wait()
        return False


class FFmpegReuseTestBase(unittest.TestCase):
    def setUp(self):
        app.jobs.clear()
        app.processes.clear()
        FakeFFmpegProcess.counter = 0
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._dl = patch.object(app, "DOWNLOAD_DIR", self.tmp.name)
        self._dl.start()
        self.addCleanup(self._dl.stop)
        # build_ffmpeg_extract_command produces [ffmpeg, -i, src, -y, out]; the
        # temp output is the last argv element, which FakeFFmpegProcess writes.
        self._cmd = patch.object(
            app.pipeline,
            "build_ffmpeg_extract_command",
            side_effect=self._ffmpeg_cmd,
        )
        self._cmd.start()
        self.addCleanup(self._cmd.stop)
        self.addCleanup(app.processes.clear)
        self.addCleanup(app.jobs.clear)

    def _ffmpeg_cmd(self, source, temp_output):
        return ["ffmpeg", "-i", source, "-y", temp_output]

    def make_artifact(self, artifact_id="a001", status="downloading"):
        return {
            "id": artifact_id,
            "type": "audio",
            "status": status,
            "phase": "processing",
            "format_id": None,
            "label": "audio",
        }

    def make_parent(self, deadline_margin=60.0, cancel=False, status="downloading"):
        job = {
            "id": JOB,
            "status": status,
            "phase": "processing",
            "title": "Test",
            "cancel_requested": cancel,
            "cancel_event": threading.Event(),
            "artifacts": [],
        }
        app.jobs[JOB] = job
        deadline = time.monotonic() + deadline_margin
        return job, deadline

    def make_video(self):
        source = Path(self.tmp.name) / f"{JOB}.a000.mp4"
        source.write_bytes(b"mp4")
        return {"id": "a000", "type": "video", "status": "done",
                "phase": "done", "file": str(source)}


class SuccessAndFallbackTests(FFmpegReuseTestBase):
    def test_success_publishes_artifact(self):
        video = self.make_video()
        artifact = self.make_artifact()
        parent, deadline = self.make_parent()

        with patch.object(subprocess, "Popen", FakeFFmpegProcess):
            result = app.try_ffmpeg_reuse(JOB, artifact, [video], deadline, "Test")

        self.assertTrue(result)
        self.assertEqual(artifact["status"], "done")
        self.assertEqual(artifact["percent"], 100.0)
        final = os.path.join(self.tmp.name, f"{JOB}.a001.mp3")
        self.assertEqual(artifact["file"], final)
        self.assertTrue(os.path.isfile(final), "final file must exist")
        self.assertFalse(
            os.path.isfile(os.path.join(self.tmp.name, f"{JOB}.a001.temp.mp3")),
            "temp file must be cleaned after publish",
        )
        # Registry must not leak a finished process.
        self.assertNotIn(JOB, app.processes)

    def test_ordinary_failure_allows_fallback(self):
        video = self.make_video()
        artifact = self.make_artifact()
        parent, deadline = self.make_parent()

        with patch.object(subprocess, "Popen",
                          side_effect=lambda *a, **k: FakeFFmpegProcess(*a, fail=True, **k)):
            result = app.try_ffmpeg_reuse(JOB, artifact, [video], deadline, "Test")

        self.assertFalse(result, "ordinary ffmpeg failure must return False for fallback")
        self.assertEqual(artifact["status"], "processing")
        self.assertFalse(
            os.path.isfile(os.path.join(self.tmp.name, f"{JOB}.a001.temp.mp3")),
        )
        self.assertNotIn(JOB, app.processes)


class FatalNoFallbackTests(FFmpegReuseTestBase):
    """Cancellation and deadline must propagate, never fall back to yt-dlp."""

    def test_timeout_propagates_and_reaps(self):
        video = self.make_video()
        artifact = self.make_artifact()
        # Deadline stays positive at the gate so the process actually spawns,
        # then communicate(timeout=remaining) raises to simulate the budget
        # expiring while ffmpeg runs.
        deadline = time.monotonic() + 5.0
        app.jobs[JOB] = {
            "id": JOB, "status": "downloading", "phase": "processing",
            "title": "Test", "cancel_requested": False,
            "cancel_event": threading.Event(), "artifacts": [],
        }

        captured = {}

        class HangingFFmpeg(FakeFFmpegProcess):
            _instance_ref = None

            def __init__(self, cmd, **kwargs):
                super().__init__(cmd, **kwargs)
                type(self)._instance_ref = self

            def communicate(self, timeout=None):
                captured["seen"] = timeout
                raise subprocess.TimeoutExpired(cmd=self.cmd, timeout=timeout)

        # subprocess.run is used by terminate_process_tree's Windows taskkill;
        # stub it so the patched Popen is not abused as a taskkill subprocess.
        def fake_run(cmd, **kwargs):
            class R:
                returncode = 0
                stdout = b""
                stderr = b""
            return R()

        with patch.object(subprocess, "Popen", HangingFFmpeg), \
                patch.object(subprocess, "run", side_effect=fake_run):
            with self.assertRaises(subprocess.TimeoutExpired):
                app.try_ffmpeg_reuse(JOB, artifact, [video], deadline, "Test")

        inst = HangingFFmpeg._instance_ref

        self._assert_temps_cleaned()
        self.assertNotIn(JOB, app.processes, "registry must be cleared on timeout")
        self.assertIsNotNone(captured.get("seen"), "communicate(timeout=) must run")
        self.assertIsNotNone(inst, "a process must have spawned")
        self.assertTrue(
            inst.kill_calls or inst.terminate_calls,
            "process must be terminated on timeout",
        )

    def test_cancelled_parent_raises_pipeline_cancelled(self):
        video = self.make_video()
        artifact = self.make_artifact()
        deadline = time.monotonic() + 60.0
        app.jobs[JOB] = {
            "id": JOB, "status": "downloading", "phase": "processing",
            "cancel_requested": False, "cancel_event": threading.Event(),
            "artifacts": [],
        }

        class CancelAfterFFmpeg(FakeFFmpegProcess):
            def communicate(self, timeout=None):
                # ffmpeg finishes, but the parent was cancelled during the run.
                self._alive = False
                self.returncode = 0
                Path(self.temp_output).write_bytes(b"mp3")
                app.jobs[JOB]["cancel_requested"] = True
                return "", ""

        with patch.object(subprocess, "Popen", CancelAfterFFmpeg):
            with self.assertRaises(app.PipelineCancelled):
                app.try_ffmpeg_reuse(JOB, artifact, [video], deadline, "Test")

        self._assert_temps_cleaned()
        self.assertEqual(artifact["status"], "processing",
                         "cancelled job must not become done")
        self.assertNotIn(JOB, app.processes)

    def test_cancelled_before_spawn_raises_without_spawning(self):
        video = self.make_video()
        artifact = self.make_artifact()
        _, deadline = self.make_parent(cancel=True)

        with patch.object(subprocess, "Popen", side_effect=AssertionError("must not spawn")):
            with self.assertRaises(app.PipelineCancelled):
                app.try_ffmpeg_reuse(JOB, artifact, [video], deadline, "Test")

        self.assertEqual(FakeFFmpegProcess.counter, 0, "no subprocess was spawned")

    def test_expired_deadline_before_spawn_raises_timeout(self):
        video = self.make_video()
        artifact = self.make_artifact()
        _, deadline = self.make_parent(deadline_margin=-1.0)

        with patch.object(subprocess, "Popen", side_effect=AssertionError("must not spawn")):
            with self.assertRaises(subprocess.TimeoutExpired):
                app.try_ffmpeg_reuse(JOB, artifact, [video], deadline, "Test")

        self.assertEqual(FakeFFmpegProcess.counter, 0)

    def test_terminal_parent_raises_without_spawning(self):
        video = self.make_video()
        artifact = self.make_artifact()
        _, deadline = self.make_parent(status="cancelled")

        with patch.object(subprocess, "Popen", side_effect=AssertionError("must not spawn")):
            with self.assertRaises(app.PipelineCancelled):
                app.try_ffmpeg_reuse(JOB, artifact, [video], deadline, "Test")

    def test_publish_window_cancel_raises_and_does_not_publish(self):
        """If the parent is cancelled between ffmpeg exiting and os.replace, no artifact."""
        video = self.make_video()
        artifact = self.make_artifact()
        _, deadline = self.make_parent()
        final = os.path.join(self.tmp.name, f"{JOB}.a001.mp3")

        class CancelBeforePublish(FakeFFmpegProcess):
            def communicate(self, timeout=None):
                self._alive = False
                self.returncode = 0
                # Cancel arrives after ffmpeg finished but before the function
                # gets a chance to publish.
                app.jobs[JOB]["cancel_requested"] = True
                return "", ""

        with patch.object(subprocess, "Popen", CancelBeforePublish):
            with self.assertRaises(app.PipelineCancelled):
                app.try_ffmpeg_reuse(JOB, artifact, [video], deadline, "Test")

        self._assert_temps_cleaned()
        self.assertEqual(artifact["status"], "processing",
                         "must not transition to done after a post-ffmpeg cancel")
        self.assertFalse(os.path.isfile(final), "no artifact must be published")

    # --- helpers -------------------------------------------------------
    def _assert_temps_cleaned(self):
        self.assertFalse(
            os.path.isfile(os.path.join(self.tmp.name, f"{JOB}.a001.temp.mp3")),
            "temp file must be cleaned on a fatal outcome",
        )


class NoOtherArtifactTouchedTests(FFmpegReuseTestBase):
    def test_success_does_not_touch_sibling_artifact_file(self):
        video = self.make_video()
        artifact = self.make_artifact(artifact_id="a001")
        sibling = Path(self.tmp.name) / f"{JOB}.a002.mp3"
        sibling.write_bytes(b"sibling-audio")
        _, deadline = self.make_parent()

        with patch.object(subprocess, "Popen", FakeFFmpegProcess):
            result = app.try_ffmpeg_reuse(JOB, artifact, [video], deadline, "Test")

        self.assertTrue(result)
        self.assertTrue(sibling.exists(), "a002's final file must be untouched")


class NoVideoSourceTests(FFmpegReuseTestBase):
    def test_no_completed_video_returns_false_no_spawn(self):
        artifact = self.make_artifact()
        _, deadline = self.make_parent()

        with patch.object(subprocess, "Popen", side_effect=AssertionError("no ffmpeg")):
            result = app.try_ffmpeg_reuse(JOB, artifact, [], deadline, "Test")

        self.assertFalse(result, "no source video means no reuse attempt")
        self.assertEqual(FakeFFmpegProcess.counter, 0)


def _last_popen():
    """Hand back the most recent FakeFFmpegProcess for inspection (unused)."""
    return None


if __name__ == "__main__":
    unittest.main()
