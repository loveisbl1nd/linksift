"""Parent lifecycle: process reaping, timeout cleanup and queued cancellation.

Three behaviours that the rest of the suite only exercises indirectly:

  * ``reap_active_parent_process`` must terminate and unregister the process of
    ONE parent, keyed by parent job id, leaving a sibling parent's process alone.
  * ``unregister_process_if_current`` must be identity-checked, so a slow
    ``finally`` from a finished artifact cannot evict the process that the NEXT
    artifact just registered.
  * A parent that runs out of its shared deadline must delete the files of the
    artifacts that already COMPLETED — the whole parent is worthless once the
    budget is gone — and a cancelled queued parent must mark every artifact
    cancelled, not just the parent.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class FakePopen:
    """A subprocess stand-in that records the lifecycle calls made against it."""

    def __init__(self, pid=1234, alive=True):
        self.pid = pid
        self.terminated = 0
        self.killed = 0
        self.waits = 0
        self._alive = alive
        self.returncode = None if alive else 0

    def poll(self):
        return None if self._alive else self.returncode

    def terminate(self):
        self.terminated += 1
        self._alive = False
        self.returncode = -15

    def kill(self):
        self.killed += 1
        self._alive = False
        self.returncode = -9

    def wait(self, timeout=None):
        self.waits += 1
        if self._alive:
            self._alive = False
            self.returncode = -15
        return self.returncode


class UnregisterIdentityTests(unittest.TestCase):
    """The registry pop is conditional on the entry still being OUR process."""

    def setUp(self):
        app.processes.clear()

    def tearDown(self):
        app.processes.clear()

    def test_matching_process_is_removed(self):
        process = FakePopen()
        app.processes["parentA"] = process
        app.unregister_process_if_current("parentA", process)
        self.assertNotIn("parentA", app.processes)

    def test_stale_process_does_not_evict_a_newer_one(self):
        """A late finally from artifact N must not unregister artifact N+1.

        Both are registered under the same parent id, so an unconditional pop
        would leave DELETE with nothing to signal while yt-dlp keeps running.
        """
        stale = FakePopen(pid=1)
        newer = FakePopen(pid=2)
        app.processes["parentA"] = newer

        app.unregister_process_if_current("parentA", stale)

        self.assertIs(app.processes.get("parentA"), newer)

    def test_unregistering_an_unknown_parent_is_a_noop(self):
        other = FakePopen()
        app.processes["parentB"] = other
        app.unregister_process_if_current("parentA", FakePopen())
        self.assertIs(app.processes.get("parentB"), other)


class ReapActiveParentProcessTests(unittest.TestCase):
    """Terminate + reap + unregister, scoped to a single parent job id."""

    def setUp(self):
        app.processes.clear()

    def tearDown(self):
        app.processes.clear()

    def test_reaps_and_unregisters_the_parents_process(self):
        process = FakePopen()
        app.processes["parentA"] = process

        with patch.object(app, "terminate_process_tree") as terminate_tree:
            app.reap_active_parent_process("parentA")

        terminate_tree.assert_called_once_with(process)
        self.assertGreaterEqual(process.waits, 1, "the process must be reaped")
        self.assertNotIn("parentA", app.processes)

    def test_leaves_a_sibling_parents_process_untouched(self):
        """Reaping one parent must not disturb another parent's download."""
        mine = FakePopen(pid=10)
        theirs = FakePopen(pid=20)
        app.processes["parentA"] = mine
        app.processes["parentB"] = theirs

        with patch.object(app, "terminate_process_tree") as terminate_tree:
            app.reap_active_parent_process("parentA")

        terminate_tree.assert_called_once_with(mine)
        self.assertIs(app.processes.get("parentB"), theirs)
        self.assertEqual(theirs.waits, 0)
        self.assertEqual(theirs.terminated, 0)

    def test_parent_with_no_process_is_a_noop(self):
        theirs = FakePopen()
        app.processes["parentB"] = theirs

        with patch.object(app, "terminate_process_tree") as terminate_tree:
            app.reap_active_parent_process("parentA")

        terminate_tree.assert_not_called()
        self.assertIs(app.processes.get("parentB"), theirs)

    def test_kill_fallback_when_terminate_does_not_reap(self):
        """terminate -> wait times out -> kill -> wait again."""

        class StubbornPopen(FakePopen):
            def __init__(self):
                super().__init__()
                self.wait_calls = 0

            def wait(self, timeout=None):
                self.wait_calls += 1
                if self.wait_calls == 1:
                    raise app.subprocess.TimeoutExpired(cmd="yt-dlp", timeout=timeout)
                return 0

        process = StubbornPopen()
        app.processes["parentA"] = process

        with patch.object(app, "terminate_process_tree"):
            app.reap_active_parent_process("parentA")

        self.assertEqual(process.killed, 1, "kill must follow a timed-out wait")
        self.assertEqual(process.wait_calls, 2, "the kill must itself be reaped")
        self.assertNotIn("parentA", app.processes)


class PipelineTimeoutCleanupTests(unittest.TestCase):
    """A parent that exhausts its shared deadline keeps nothing."""

    def setUp(self):
        app.jobs.clear()
        app.processes.clear()
        self._temp = tempfile.TemporaryDirectory()
        self.temp_dir = self._temp.name
        self._patch = patch.object(app, "DOWNLOAD_DIR", self.temp_dir)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._temp.cleanup()
        app.jobs.clear()
        app.processes.clear()

    def make_parent(self, job_id="parent1"):
        """A parent with one COMPLETED artifact and one still downloading."""
        done_file = Path(self.temp_dir) / f"{job_id}.a000.mp4"
        done_file.write_bytes(b"finished video")
        partial_file = Path(self.temp_dir) / f"{job_id}.a001.mp3.part"
        partial_file.write_bytes(b"half an audio")
        app.jobs[job_id] = {
            "id": job_id,
            "status": "downloading",
            "phase": "downloading",
            "artifacts": [
                {
                    "id": "a000",
                    "status": "done",
                    "phase": "done",
                    "file": str(done_file),
                    "filename": "video.mp4",
                },
                {
                    "id": "a001",
                    "status": "downloading",
                    "phase": "downloading",
                    "file": None,
                    "filename": "audio.mp3",
                },
            ],
        }
        return done_file, partial_file

    def test_timeout_deletes_a_completed_artifacts_final_file(self):
        """The done artifact's file goes too: the parent ran out of budget.

        Leaving it on disk would strand a file that no endpoint will ever serve,
        because the parent's status is terminal-but-not-servable.
        """
        done_file, partial_file = self.make_parent()

        app.set_pipeline_error("parent1", "Download timed out", status="timed_out")

        self.assertFalse(done_file.exists(), "completed artifact file must be removed")
        self.assertFalse(partial_file.exists(), "intermediate file must be removed")

    def test_timeout_marks_the_parent_and_unfinished_artifacts(self):
        self.make_parent()

        app.set_pipeline_error("parent1", "Download timed out", status="timed_out")

        job = app.jobs["parent1"]
        self.assertEqual(job["status"], "timed_out")
        self.assertEqual(job["error"], "Download timed out")
        self.assertIsNone(job["current_artifact_id"])
        # The already-terminal artifact keeps its own status; the running one
        # takes the parent's terminal status.
        self.assertEqual(job["artifacts"][0]["status"], "done")
        self.assertEqual(job["artifacts"][1]["status"], "timed_out")

    def test_timeout_reaps_the_live_subprocess(self):
        self.make_parent()
        process = FakePopen()
        app.processes["parent1"] = process

        with patch.object(app, "terminate_process_tree") as terminate_tree:
            app.set_pipeline_error("parent1", "Download timed out", status="timed_out")

        terminate_tree.assert_called_once_with(process)
        self.assertNotIn("parent1", app.processes)

    def test_cancellation_wins_over_a_concurrent_timeout(self):
        """A user cancel already in flight must not be relabelled 'timed_out'."""
        done_file, partial_file = self.make_parent()
        app.jobs["parent1"]["cancel_requested"] = True

        app.set_pipeline_error("parent1", "Download timed out", status="timed_out")

        job = app.jobs["parent1"]
        self.assertEqual(job["status"], "cancelled")
        self.assertIsNone(job["error"], "a cancel is not an error")
        self.assertEqual(job["artifacts"][1]["status"], "cancelled")
        # Cancellation cleans the parent just as thoroughly.
        self.assertFalse(done_file.exists())
        self.assertFalse(partial_file.exists())

    def test_plain_error_keeps_completed_output(self):
        """A non-timeout failure of one artifact leaves finished ones alone.

        This is what makes the 'partial' status meaningful.
        """
        done_file, _ = self.make_parent()

        app.set_pipeline_error("parent1", "yt-dlp exited 1", status="error")

        self.assertTrue(
            done_file.exists(), "a completed artifact survives a sibling's error"
        )
        self.assertEqual(app.jobs["parent1"]["artifacts"][0]["status"], "done")

    def test_unknown_job_is_a_noop(self):
        app.set_pipeline_error("ghost", "boom", status="timed_out")
        self.assertNotIn("ghost", app.jobs)


class QueuedParentCancellationTests(unittest.TestCase):
    """Cancelling a parent that never started must settle every artifact."""

    def setUp(self):
        app.jobs.clear()
        app.processes.clear()
        self.client = app.app.test_client()

    def tearDown(self):
        app.jobs.clear()
        app.processes.clear()

    def test_cancelling_a_queued_multi_output_parent_marks_all_artifacts(self):
        """Every pending artifact becomes cancelled, not just the parent.

        A queued parent owns no subprocess, so nothing else would ever move the
        artifacts out of 'pending' and the UI would show them stuck forever.
        """
        app.jobs["queued1"] = {
            "id": "queued1",
            "status": "queued",
            "phase": "queued",
            "artifacts": [
                {"id": "a000", "status": "pending", "phase": "pending"},
                {"id": "a001", "status": "pending", "phase": "pending"},
            ],
        }

        with patch.object(app, "get_scheduler") as get_scheduler:
            get_scheduler.return_value.cancel_queued.return_value = True
            response = self.client.delete("/api/download/queued1")

        self.assertEqual(response.status_code, 200)
        job = app.jobs["queued1"]
        self.assertEqual(job["status"], "cancelled")
        for artifact in job["artifacts"]:
            self.assertEqual(artifact["status"], "cancelled", artifact["id"])

    def test_cancelling_a_queued_parent_spawns_nothing(self):
        app.jobs["queued2"] = {
            "id": "queued2",
            "status": "queued",
            "phase": "queued",
            "artifacts": [{"id": "a000", "status": "pending", "phase": "pending"}],
        }

        with patch.object(app, "get_scheduler") as get_scheduler, patch.object(
            app.subprocess, "Popen"
        ) as popen:
            get_scheduler.return_value.cancel_queued.return_value = True
            self.client.delete("/api/download/queued2")

        popen.assert_not_called()
        self.assertNotIn("queued2", app.processes)


if __name__ == "__main__":
    unittest.main()
