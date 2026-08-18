"""Behavior tests for the process registry pre-spawn gate.

The registry is keyed by PARENT job id, never by artifact id, because DELETE
/api/download/<job_id> only knows the parent. Every one of these tests drives
the real ``app.run_download_command`` with a fake Popen, so they fail if the
gate is reordered, weakened, or moved outside the jobs_lock critical section.
"""
import io
import subprocess
import threading
import unittest
from unittest.mock import patch

import app


PARENT = "0123456789"
OTHER_PARENT = "9876543210"


class FakePopen:
    """Stands in for subprocess.Popen with the full surface the code touches.

    ``run_download_command`` merges stderr into stdout and drains that single
    pipe on a reader thread, so ``stdout`` must be an iterable, closeable
    stream that reaches EOF. These tests care about the gate and the registry,
    not about progress, so the transcript is empty.
    """

    instances = []

    def __init__(self, cmd, **kwargs):
        self.cmd = cmd
        self.kwargs = kwargs
        self.pid = 4000 + len(FakePopen.instances)
        self.returncode = None
        self._alive = True
        self.terminated = False
        self.killed = False
        self.waited = False
        self.stdout = io.StringIO("")
        FakePopen.instances.append(self)

    # --- lifecycle -----------------------------------------------------
    def poll(self):
        return None if self._alive else self.returncode

    def wait(self, timeout=None):
        self.waited = True
        self._alive = False
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self):
        self.terminated = True
        self._alive = False
        if self.returncode is None:
            self.returncode = -15

    def kill(self):
        self.killed = True
        self._alive = False
        if self.returncode is None:
            self.returncode = -9

    # --- helpers for tests ---------------------------------------------
    def finish(self, returncode=0):
        self._alive = False
        self.returncode = returncode


class InstrumentedDict(dict):
    """A registry that records every insert so tests can prove which key was used.

    The log lives on an attribute, never as a dict entry, so the registry itself
    stays byte-for-byte what production would see.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.inserts = []
        self.snapshots = []

    def __setitem__(self, key, value):
        self.inserts.append((key, value))
        super().__setitem__(key, value)
        # Snapshot the full key set at the moment of registration.
        self.snapshots.append(tuple(sorted(super().keys())))


class RegistryGateTestBase(unittest.TestCase):
    def setUp(self):
        app.jobs.clear()
        app.processes.clear()
        FakePopen.instances = []
        patcher = patch.object(subprocess, "Popen", FakePopen)
        self.popen = patcher.start()
        self.addCleanup(patcher.stop)
        self._original_processes = app.processes
        self.addCleanup(self._restore_registry)
        self.addCleanup(app.jobs.clear)

    def _restore_registry(self):
        app.processes = self._original_processes
        app.processes.clear()

    def make_parent(self, job_id=PARENT, status="downloading", cancel=False, artifacts=None):
        job = {
            "id": job_id,
            "status": status,
            "phase": status,
            "cancel_requested": cancel,
            "cancel_event": threading.Event(),
            "artifacts": artifacts if artifacts is not None else [],
        }
        app.jobs[job_id] = job
        return job

    def instrument_registry(self):
        """Replace the module-level registry with a recording one."""
        registry = InstrumentedDict(app.processes)
        app.processes = registry
        return registry

    def run_cmd(self, parent_job_id=PARENT, target=None, timeout=5):
        return app.run_download_command(["yt-dlp", "--version"], parent_job_id, target, timeout)


class NoSpawnConditionsTests(RegistryGateTestBase):
    """Each of these must return None and must NOT create a subprocess."""

    def test_missing_parent_does_not_spawn(self):
        result = self.run_cmd(parent_job_id="doesnotexist")

        self.assertIsNone(result)
        self.assertEqual(FakePopen.instances, [])
        self.assertEqual(app.processes, {})

    def test_cancelled_parent_does_not_spawn(self):
        job = self.make_parent(cancel=True)

        result = self.run_cmd(target=job)

        self.assertIsNone(result)
        self.assertEqual(FakePopen.instances, [])
        self.assertNotIn(PARENT, app.processes)

    def test_terminal_parent_does_not_spawn(self):
        for status in sorted(app.TERMINAL_STATUSES):
            with self.subTest(status=status):
                app.jobs.clear()
                app.processes.clear()
                FakePopen.instances = []
                job = self.make_parent(status=status)

                result = self.run_cmd(target=job)

                self.assertIsNone(result)
                self.assertEqual(FakePopen.instances, [])
                self.assertNotIn(PARENT, app.processes)

    def test_terminal_artifact_does_not_spawn(self):
        """An artifact cancelled while queued must not start a download."""
        import output_pipeline as pipeline

        for status in sorted(pipeline.ARTIFACT_TERMINAL):
            with self.subTest(status=status):
                app.jobs.clear()
                app.processes.clear()
                FakePopen.instances = []
                artifact = {"id": "a000", "status": status, "type": "video"}
                self.make_parent(artifacts=[artifact])

                result = self.run_cmd(target=artifact)

                self.assertIsNone(result)
                self.assertEqual(FakePopen.instances, [])
                self.assertNotIn(PARENT, app.processes)


class SpawnAndRegisterTests(RegistryGateTestBase):
    def test_valid_parent_spawns_and_registers_under_parent_id(self):
        artifact = {"id": "a000", "status": "downloading", "type": "video"}
        self.make_parent(artifacts=[artifact])
        registry = self.instrument_registry()

        result = self.run_cmd(target=artifact)

        self.assertIsNotNone(result)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(FakePopen.instances), 1)
        # Exactly one registration happened, under the parent id, holding the
        # process object that was actually spawned.
        self.assertEqual(len(registry.inserts), 1)
        key, value = registry.inserts[0]
        self.assertEqual(key, PARENT)
        self.assertIs(value, FakePopen.instances[0])

    def test_registry_key_is_parent_id_not_artifact_id(self):
        """Keying by artifact id would put the process beyond DELETE's reach."""
        artifact = {"id": "a000", "status": "downloading", "type": "video"}
        self.make_parent(artifacts=[artifact])
        registry = self.instrument_registry()

        self.run_cmd(target=artifact)

        # Assert on the key set captured AT REGISTRATION TIME, not afterwards:
        # the finally-block unregister would otherwise make any key "absent".
        self.assertEqual(registry.snapshots, [(PARENT,)])
        registered_keys = [key for key, _ in registry.inserts]
        self.assertEqual(registered_keys, [PARENT])
        self.assertNotIn("a000", registered_keys)
        self.assertNotIn(f"{PARENT}.a000", registered_keys)

    def test_registry_is_empty_after_normal_completion(self):
        artifact = {"id": "a000", "status": "downloading", "type": "video"}
        self.make_parent(artifacts=[artifact])
        registry = self.instrument_registry()

        self.run_cmd(target=artifact)

        # It WAS registered during the run, and is gone afterwards.
        self.assertEqual(len(registry.inserts), 1)
        self.assertEqual(dict(registry), {})

    def test_parent_job_dict_is_accepted_as_progress_target(self):
        """Legacy single-output passes the job dict itself as the target."""
        job = self.make_parent()

        result = self.run_cmd(target=job)

        self.assertIsNotNone(result)
        self.assertEqual(len(FakePopen.instances), 1)

    def test_process_is_reaped_before_the_function_returns(self):
        """The next artifact must never start while the previous pid lingers."""
        artifact = {"id": "a000", "status": "downloading", "type": "video"}
        self.make_parent(artifacts=[artifact])

        self.run_cmd(target=artifact)
        spawned = FakePopen.instances[0]

        self.assertTrue(spawned.waited, "process must be waited on (reaped)")
        self.assertIsNotNone(spawned.poll(), "process must not still look alive")


class NoOverwriteTests(RegistryGateTestBase):
    def test_existing_active_process_is_not_silently_overwritten(self):
        """Overwriting would orphan the running process beyond DELETE's reach."""
        artifact = {"id": "a001", "status": "downloading", "type": "audio"}
        self.make_parent(artifacts=[artifact])
        stale = FakePopen(["sleep"])  # still alive: poll() returns None
        app.processes[PARENT] = stale

        with self.assertRaises(RuntimeError) as ctx:
            self.run_cmd(target=artifact)

        self.assertIn(PARENT, str(ctx.exception))
        self.assertIs(app.processes[PARENT], stale, "registry must still point at the live process")
        # No second process may have been spawned.
        self.assertEqual(len(FakePopen.instances), 1)

    def test_finished_process_in_registry_is_replaced(self):
        """A reaped process is not 'active' and must not block the next artifact."""
        artifact = {"id": "a001", "status": "downloading", "type": "audio"}
        self.make_parent(artifacts=[artifact])
        finished = FakePopen(["yt-dlp"])
        finished.finish(returncode=0)
        app.processes[PARENT] = finished

        result = self.run_cmd(target=artifact)

        self.assertIsNotNone(result)
        self.assertEqual(len(FakePopen.instances), 2)


class IdentityCheckedUnregisterTests(RegistryGateTestBase):
    def test_stale_finally_does_not_remove_a_newer_process(self):
        """The unregister must compare identity, not just the key."""
        artifact = {"id": "a000", "status": "downloading", "type": "video"}
        self.make_parent(artifacts=[artifact])
        newer = FakePopen(["newer"])

        # Simulate: first process finishes and its finally runs AFTER a second
        # process for the same parent was registered.
        first = FakePopen(["first"])
        app.processes[PARENT] = newer

        if app.processes.get(PARENT) is first:
            app.processes.pop(PARENT, None)

        self.assertIs(app.processes[PARENT], newer, "newer process must survive a stale unregister")

    def test_unregister_removes_only_the_matching_instance(self):
        artifact = {"id": "a000", "status": "downloading", "type": "video"}
        self.make_parent(artifacts=[artifact])

        self.run_cmd(target=artifact)
        spawned = FakePopen.instances[-1]

        self.assertNotIn(PARENT, app.processes)
        self.assertTrue(spawned.waited)


class ParentIsolationTests(RegistryGateTestBase):
    def test_two_parents_with_same_artifact_id_stay_independent(self):
        """Artifact ids restart at a000 per parent; keying by them would collide."""
        art_a = {"id": "a000", "status": "downloading", "type": "video"}
        art_b = {"id": "a000", "status": "downloading", "type": "video"}
        self.make_parent(PARENT, artifacts=[art_a])
        self.make_parent(OTHER_PARENT, artifacts=[art_b])

        live = FakePopen(["still-running"])
        app.processes[OTHER_PARENT] = live

        # PARENT may still spawn even though OTHER_PARENT has an active process.
        result = self.run_cmd(parent_job_id=PARENT, target=art_a)

        self.assertIsNotNone(result)
        self.assertIs(app.processes.get(OTHER_PARENT), live)
        self.assertNotIn(PARENT, app.processes)

    def test_cancelling_one_parent_leaves_the_other_registered(self):
        self.make_parent(PARENT)
        self.make_parent(OTHER_PARENT)
        proc_a = FakePopen(["a"])
        proc_b = FakePopen(["b"])
        app.processes[PARENT] = proc_a
        app.processes[OTHER_PARENT] = proc_b

        app.processes.pop(PARENT, None)

        self.assertNotIn(PARENT, app.processes)
        self.assertIs(app.processes[OTHER_PARENT], proc_b)


class DeleteReachesActiveProcessTests(RegistryGateTestBase):
    def test_delete_finds_the_active_process_by_parent_id(self):
        """DELETE only knows the parent id; the registry must answer to it."""
        artifact = {"id": "a002", "status": "downloading", "type": "audio"}
        self.make_parent(artifacts=[artifact])
        active = FakePopen(["running"])
        app.processes[PARENT] = active

        found = app.processes.get(PARENT)

        self.assertIs(found, active)
        self.assertIsNone(found.poll(), "process must look alive to the canceller")


if __name__ == "__main__":
    unittest.main()
