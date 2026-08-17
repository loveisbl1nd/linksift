"""Regression tests for the pre-publication deadline and unexpected-error paths.

Two defects motivated this file.

1. ``try_ffmpeg_reuse`` folded cancellation and deadline expiry into ONE check
   just before publishing::

       if cancel_requested(job_id) or deadline - time.monotonic() <= 0:
           raise PipelineCancelled(...)

   A parent whose shared budget ran out between ffmpeg exiting and the rename
   was therefore reported as ``cancelled`` rather than ``timed_out`` - a
   different user-visible outcome reached through a different cleanup path.

2. The ``except`` clause caught only ``TimeoutExpired``. Any other exception out
   of ``communicate()`` (``OSError`` from a broken pipe, ``ValueError`` from a
   closed fd) skipped ``terminate_and_reap`` and unwound straight through
   ``finally``, leaving a live ffmpeg still holding its output file.

Everything here drives a fake Popen. No ffmpeg, no yt-dlp, no network. Time is
controlled by patching ``time.monotonic`` rather than by sleeping.
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


JOB = "9911223344"


def _fake_run(*_args, **_kwargs):
    """Stub for subprocess.run, used by the Windows taskkill path in
    terminate_process_tree. Without it the patched Popen would be abused as a
    taskkill subprocess."""
    return type("R", (), {"returncode": 0, "stdout": b"", "stderr": b""})()


class ControlledFFmpeg:
    """A fake ffmpeg whose communicate() runs a caller-supplied hook.

    The hook is what makes these tests deterministic: it fires at exactly the
    moment ffmpeg "finishes", which is the window both fixes are about.
    """

    instances = []

    def __init__(self, cmd, on_communicate=None, returncode=0, write_output=True, **kwargs):
        self.cmd = cmd
        self.pid = 4242 + len(ControlledFFmpeg.instances)
        self._alive = True
        self.returncode = None
        self._final_returncode = returncode
        self._write_output = write_output
        self._on_communicate = on_communicate
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0
        self.temp_output = cmd[-1]
        ControlledFFmpeg.instances.append(self)

    def poll(self):
        return None if self._alive else self.returncode

    def communicate(self, timeout=None):
        if self._write_output:
            Path(self.temp_output).write_bytes(b"mp3-audio")
        self._alive = False
        self.returncode = self._final_returncode
        if self._on_communicate is not None:
            self._on_communicate()
        return "", ""

    def wait(self, timeout=None):
        self.wait_calls += 1
        self._alive = False
        if self.returncode is None:
            self.returncode = self._final_returncode
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

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.wait()
        return False


class ReusePublicationTestBase(unittest.TestCase):
    def setUp(self):
        app.jobs.clear()
        app.processes.clear()
        ControlledFFmpeg.instances = []
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._dl = patch.object(app, "DOWNLOAD_DIR", self.tmp.name)
        self._dl.start()
        self.addCleanup(self._dl.stop)
        self._cmd = patch.object(
            app.pipeline, "build_ffmpeg_extract_command",
            side_effect=lambda src, out: ["ffmpeg", "-i", src, "-y", out],
        )
        self._cmd.start()
        self.addCleanup(self._cmd.stop)
        self.addCleanup(app.processes.clear)
        self.addCleanup(app.jobs.clear)

    def make_parent(self, cancel=False, status="downloading"):
        job = {
            "id": JOB, "status": status, "phase": "processing", "title": "T",
            "cancel_requested": cancel, "cancel_event": threading.Event(),
            "artifacts": [],
        }
        app.jobs[JOB] = job
        return job

    def make_artifact(self, artifact_id="a001"):
        return {
            "id": artifact_id, "type": "audio", "status": "downloading",
            "phase": "processing", "format_id": None, "label": "MP3 audio",
        }

    def make_video(self):
        source = Path(self.tmp.name) / ("%s.a000.mp4" % JOB)
        source.write_bytes(b"mp4-bytes")
        return {"id": "a000", "type": "video", "status": "done",
                "phase": "done", "file": str(source)}

    @property
    def temp_path(self):
        return os.path.join(self.tmp.name, "%s.a001.temp.mp3" % JOB)

    @property
    def final_path(self):
        return os.path.join(self.tmp.name, "%s.a001.mp3" % JOB)

    def run_reuse_with_clock(self, on_communicate, deadline=1000.0, start=900.0):
        """Drive try_ffmpeg_reuse with a fake clock.

        `deadline` is a monotonic timestamp. The clock starts before it (so the
        spawn gate and communicate() see budget remaining) and `on_communicate`
        may push it past the deadline, simulating the budget running out while
        ffmpeg ran. No real time passes.
        """
        video = self.make_video()
        artifact = self.make_artifact()
        self.make_parent()
        clock = {"now": start}

        def hook():
            on_communicate(clock)

        with patch.object(app.time, "monotonic", lambda: clock["now"]), \
                patch.object(subprocess, "Popen",
                             side_effect=lambda *a, **k: ControlledFFmpeg(*a, on_communicate=hook, **k)), \
                patch.object(subprocess, "run", side_effect=_fake_run):
            outcome = {}
            try:
                outcome["returned"] = app.try_ffmpeg_reuse(JOB, artifact, [video], deadline, "T")
            except BaseException as exc:  # noqa: BLE001 - the test inspects it
                outcome["raised"] = exc
        outcome["artifact"] = artifact
        return outcome


class DeadlineBeforePublicationTests(ReusePublicationTestBase):
    """Conversion SUCCEEDS, then the shared budget expires before publishing."""

    @staticmethod
    def _expire(clock):
        clock["now"] = 1001.0

    @staticmethod
    def _cancel(_clock):
        app.jobs[JOB]["cancel_requested"] = True

    @classmethod
    def _expire_and_cancel(cls, clock):
        cls._expire(clock)
        cls._cancel(clock)

    @staticmethod
    def _make_cancel_only_before_publication():
        """Patch cancel_requested to be False until after the post-communicate
        check, then True - exercising the PRE-publication guard specifically.

        try_ffmpeg_reuse calls cancel_requested twice: once right after
        communicate() returns and once right before os.replace. A cancellation
        that arrives in between is the exact race the pre-publication guard
        exists for. Returning False the first time lets the first check pass
        so the second check is the one that has to fire.
        """
        calls = {"n": 0}

        def fake_cancel(job_id):
            calls["n"] += 1
            return calls["n"] >= 2

        return patch.object(app, "cancel_requested", side_effect=fake_cancel)

    def test_deadline_after_conversion_raises_timeout_not_cancelled(self):
        outcome = self.run_reuse_with_clock(self._expire)
        raised = outcome.get("raised")
        self.assertIsInstance(
            raised, subprocess.TimeoutExpired,
            "an expired deadline before publication is a TIMEOUT, not a cancellation",
        )
        self.assertNotIsInstance(raised, app.PipelineCancelled)

    def test_deadline_after_conversion_does_not_publish(self):
        outcome = self.run_reuse_with_clock(self._expire)
        self.assertIn("raised", outcome)
        self.assertFalse(os.path.isfile(self.final_path),
                         "no audio artifact may be published after the deadline expires")
        self.assertNotEqual(outcome["artifact"]["status"], "done",
                            "artifact must not be marked done")

    def test_deadline_after_conversion_removes_temp_output(self):
        outcome = self.run_reuse_with_clock(self._expire)
        self.assertIn("raised", outcome)
        self.assertFalse(os.path.isfile(self.temp_path),
                         "the temporary output must be removed")
        self.assertNotIn(JOB, app.processes, "registry entry must be released")

    def test_deadline_before_publication_does_not_use_downloader_fallback(self):
        """Raising (rather than `return False`) is what stops the caller from
        starting a fresh yt-dlp download for a job that ran out of budget."""
        outcome = self.run_reuse_with_clock(self._expire)
        self.assertNotIn(
            "returned", outcome,
            "returning False here would let execute_artifacts_sequentially run yt-dlp",
        )

    def test_cancellation_immediately_before_publication_still_cancels(self):
        outcome = self.run_reuse_with_clock(self._cancel)
        self.assertIsInstance(outcome.get("raised"), app.PipelineCancelled)
        self.assertFalse(os.path.isfile(self.final_path))
        self.assertFalse(os.path.isfile(self.temp_path))

    def test_cancellation_in_the_pre_publication_window_only(self):
        """Cancellation arriving AFTER the post-communicate check but BEFORE
        os.replace must still raise PipelineCancelled and not publish.

        This is the exact window the pre-publication guard exists for. Using a
        call-counter on cancel_requested (False first, True second) means only
        the SECOND, pre-publication check sees the cancellation - so the test
        fails if that guard is ever removed or weakened.
        """
        with self._make_cancel_only_before_publication():
            outcome = self.run_reuse_with_clock(lambda _c: None)
        self.assertIsInstance(
            outcome.get("raised"), app.PipelineCancelled,
            "the pre-publication cancellation guard must still fire",
        )
        self.assertFalse(os.path.isfile(self.final_path),
                         "no artifact may be published after a pre-publication cancel")
        self.assertFalse(os.path.isfile(self.temp_path))

    def test_cancellation_wins_when_racing_with_an_expired_deadline(self):
        """Both conditions true at once: cancellation must win, as established
        everywhere else in the parent-state rules."""
        outcome = self.run_reuse_with_clock(self._expire_and_cancel)
        self.assertIsInstance(
            outcome.get("raised"), app.PipelineCancelled,
            "cancellation must take precedence over a simultaneously expired deadline",
        )

    def test_successful_conversion_within_budget_still_publishes(self):
        """Guard against over-correcting: the ordinary success path must survive."""
        outcome = self.run_reuse_with_clock(lambda _clock: None)
        self.assertTrue(outcome.get("returned"))
        self.assertTrue(os.path.isfile(self.final_path))
        self.assertEqual(outcome["artifact"]["status"], "done")
        self.assertFalse(os.path.isfile(self.temp_path))


class PipelineTimeoutStatusTests(ReusePublicationTestBase):
    """run_pipeline must turn that TimeoutExpired into parent status timed_out."""

    def test_parent_becomes_timed_out_and_all_files_are_removed(self):
        done_video = self.make_video()
        parent = self.make_parent()
        audio = self.make_artifact()
        video_art = {"id": "a000", "type": "video", "status": "done",
                     "phase": "done", "file": done_video["file"],
                     "filename": "v.mp4"}
        parent["artifacts"] = [video_art, audio]

        with patch.object(app, "cleanup_artifact_files") as cleanup:
            app.set_pipeline_error(JOB, "Download timed out after 60 seconds",
                                   status="timed_out")

        self.assertEqual(parent["status"], "timed_out")
        self.assertEqual(parent["phase"], "timed_out")
        self.assertEqual(audio["status"], "timed_out",
                         "the unfinished artifact inherits the terminal status")
        # A timed-out parent is worthless as a whole, so even the COMPLETED
        # video artifact's final file is removed.
        cleaned = {call.args[1] for call in cleanup.call_args_list}
        self.assertEqual(cleaned, {"a000", "a001"})
        for call in cleanup.call_args_list:
            self.assertTrue(call.kwargs.get("include_final"),
                            "timeout cleanup must include final files")

    def test_timeout_reaps_the_active_parent_subprocess(self):
        parent = self.make_parent()
        parent["artifacts"] = [self.make_artifact()]
        live = ControlledFFmpeg(["ffmpeg", "-i", "x", "-y", os.path.join(self.tmp.name, "t.mp3")],
                                write_output=False)
        app.processes[JOB] = live

        with patch.object(app, "cleanup_artifact_files"), \
                patch.object(subprocess, "run", side_effect=_fake_run):
            app.set_pipeline_error(JOB, "timed out", status="timed_out")

        self.assertTrue(live.terminate_calls or live.kill_calls,
                        "the live subprocess must be reaped on a parent timeout")
        self.assertNotIn(JOB, app.processes)

    def test_cancellation_takes_precedence_over_timeout_status(self):
        parent = self.make_parent(cancel=True)
        audio = self.make_artifact()
        parent["artifacts"] = [audio]

        with patch.object(app, "cleanup_artifact_files"):
            app.set_pipeline_error(JOB, "timed out", status="timed_out")

        self.assertEqual(parent["status"], "cancelled")
        self.assertEqual(audio["status"], "cancelled")

    def test_plain_error_keeps_a_completed_artifacts_output(self):
        """Only timeout and cancellation wipe completed work."""
        parent = self.make_parent()
        video_art = {"id": "a000", "type": "video", "status": "done",
                     "phase": "done", "file": "v.mp4", "filename": "v.mp4"}
        parent["artifacts"] = [video_art, self.make_artifact()]

        with patch.object(app, "cleanup_artifact_files") as cleanup:
            app.set_pipeline_error(JOB, "boom", status="error")

        self.assertEqual(parent["status"], "error")
        self.assertEqual(video_art["status"], "done",
                         "a finished artifact keeps its terminal status")
        cleanup.assert_not_called()


class UnexpectedCommunicateErrorTests(ReusePublicationTestBase):
    """An unexpected exception must reap the process, not leak it."""

    def _run_with_error(self, exc):
        video = self.make_video()
        artifact = self.make_artifact()
        self.make_parent()
        deadline = time.monotonic() + 300.0

        class ExplodingFFmpeg(ControlledFFmpeg):
            def communicate(self, timeout=None):
                # Deliberately still alive: this is exactly the state that used
                # to leak a running ffmpeg past the end of the function.
                raise exc

        with patch.object(subprocess, "Popen", ExplodingFFmpeg), \
                patch.object(subprocess, "run", side_effect=_fake_run):
            with self.assertRaises(type(exc)) as caught:
                app.try_ffmpeg_reuse(JOB, artifact, [video], deadline, "T")

        return caught.exception, ControlledFFmpeg.instances[-1], artifact

    def test_oserror_propagates_unchanged(self):
        original = OSError("broken pipe")
        raised, _proc, _art = self._run_with_error(original)
        self.assertIs(raised, original,
                      "the original exception must propagate, not a substitute")

    def test_oserror_invokes_the_process_lifecycle_helper(self):
        _raised, proc, _art = self._run_with_error(OSError("broken pipe"))
        self.assertTrue(proc.terminate_calls or proc.kill_calls,
                        "terminate_and_reap must run for an unexpected error")

    def test_oserror_leaves_no_active_process(self):
        _raised, proc, _art = self._run_with_error(OSError("broken pipe"))
        self.assertIsNotNone(proc.poll(), "the process must no longer be active")
        self.assertIsNotNone(proc.returncode, "the process must have been reaped")

    def test_oserror_removes_temp_output(self):
        self._run_with_error(OSError("broken pipe"))
        self.assertFalse(os.path.isfile(self.temp_path),
                         "temporary output must be removed")

    def test_oserror_removes_registry_entry(self):
        self._run_with_error(OSError("broken pipe"))
        self.assertNotIn(JOB, app.processes, "registry entry must be removed")

    def test_valueerror_is_handled_the_same_way(self):
        raised, proc, _art = self._run_with_error(ValueError("closed fd"))
        self.assertIsInstance(raised, ValueError)
        self.assertTrue(proc.terminate_calls or proc.kill_calls)
        self.assertNotIn(JOB, app.processes)

    def test_unexpected_error_does_not_publish_or_fall_back(self):
        _raised, _proc, artifact = self._run_with_error(OSError("broken pipe"))
        self.assertFalse(os.path.isfile(self.final_path), "nothing may be published")
        self.assertNotEqual(artifact["status"], "done")

    def test_sibling_artifact_files_are_untouched(self):
        sibling = Path(self.tmp.name) / ("%s.a002.mp3" % JOB)
        sibling.write_bytes(b"sibling-audio")
        self._run_with_error(OSError("broken pipe"))
        self.assertTrue(sibling.exists(), "an unrelated artifact's file must survive")
        self.assertEqual(sibling.read_bytes(), b"sibling-audio")

    def test_stale_cleanup_does_not_evict_a_newer_registry_entry(self):
        """The identity check in the `finally` is what makes this safe.

        A slow unwind from artifact N must not remove the entry that artifact
        N+1 has already registered under the same parent id.
        """
        video = self.make_video()
        artifact = self.make_artifact()
        self.make_parent()
        deadline = time.monotonic() + 300.0
        newer = object()

        class ReplacingFFmpeg(ControlledFFmpeg):
            def communicate(self, timeout=None):
                # The next artifact wins the registry slot before this one's
                # cleanup runs.
                app.processes[JOB] = newer
                raise OSError("broken pipe")

        with patch.object(subprocess, "Popen", ReplacingFFmpeg), \
                patch.object(subprocess, "run", side_effect=_fake_run):
            with self.assertRaises(OSError):
                app.try_ffmpeg_reuse(JOB, artifact, [video], deadline, "T")

        self.assertIs(app.processes.get(JOB), newer,
                      "the newer registry entry must survive the stale cleanup")
        app.processes.pop(JOB, None)

    def test_ordinary_nonzero_exit_still_allows_fallback(self):
        """Guard against over-correcting: a plain ffmpeg failure is NOT fatal."""
        video = self.make_video()
        artifact = self.make_artifact()
        self.make_parent()
        deadline = time.monotonic() + 300.0

        with patch.object(subprocess, "Popen",
                          side_effect=lambda *a, **k: ControlledFFmpeg(
                              *a, returncode=1, write_output=False, **k)), \
                patch.object(subprocess, "run", side_effect=_fake_run):
            result = app.try_ffmpeg_reuse(JOB, artifact, [video], deadline, "T")

        self.assertFalse(result,
                         "an ordinary nonzero exit must return False so yt-dlp can retry")
        self.assertNotIn(JOB, app.processes)


if __name__ == "__main__":
    unittest.main()
