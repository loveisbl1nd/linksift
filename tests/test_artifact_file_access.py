"""Access rules for the artifact and legacy file endpoints.

Serving a finished file is gated twice: the PARENT job must be in a state that
permits serving output (done or partial), and the individual artifact must be
done. A cancelled, timed-out or errored parent has had its files cleaned, so a
stray file left on disk must never be served. Paths are additionally required to
resolve inside DOWNLOAD_DIR.

The happy paths live in tests/test_multi_output_pipeline.py; this module covers
the rejection paths, which are what actually protect the endpoint.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class ArtifactFileAccessTests(unittest.TestCase):
    def setUp(self):
        app.jobs.clear()
        self.client = app.app.test_client()
        self._temp = tempfile.TemporaryDirectory()
        self.temp_dir = self._temp.name
        self._patch = patch.object(app, "DOWNLOAD_DIR", self.temp_dir)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._temp.cleanup()
        app.jobs.clear()

    def make_job(self, job_id, *, parent_status, artifact_status, filename="video.mp4"):
        """Register a parent with one real on-disk artifact file."""
        artifact_file = Path(self.temp_dir) / f"{job_id}.a000.mp4"
        artifact_file.write_bytes(b"payload")
        app.jobs[job_id] = {
            "id": job_id,
            "status": parent_status,
            "phase": parent_status,
            "artifacts": [{
                "id": "a000",
                "status": artifact_status,
                "file": str(artifact_file),
                "filename": filename,
            }],
        }
        return artifact_file

    def test_unknown_job_is_404(self):
        response = self.client.get("/api/file/nosuchjob/a000")
        self.assertEqual(response.status_code, 404)

    def test_parent_still_running_is_404(self):
        """A done artifact under a still-downloading parent is not servable."""
        self.make_job("job1", parent_status="downloading", artifact_status="done")
        response = self.client.get("/api/file/job1/a000")
        self.assertEqual(response.status_code, 404)

    def test_cancelled_parent_never_serves_a_lingering_file(self):
        """Cancellation cleans files; a stray one must still be refused.

        The parent-status gate has to reject before the file-existence check,
        otherwise a file that survived cleanup would be handed out.
        """
        artifact_file = self.make_job(
            "job2", parent_status="cancelled", artifact_status="done"
        )
        self.assertTrue(artifact_file.exists(), "file deliberately left on disk")
        response = self.client.get("/api/file/job2/a000")
        self.assertEqual(response.status_code, 404)

    def test_timed_out_parent_is_404(self):
        self.make_job("job3", parent_status="timed_out", artifact_status="done")
        response = self.client.get("/api/file/job3/a000")
        self.assertEqual(response.status_code, 404)

    def test_errored_parent_is_404(self):
        self.make_job("job4", parent_status="error", artifact_status="done")
        response = self.client.get("/api/file/job4/a000")
        self.assertEqual(response.status_code, 404)

    def test_partial_parent_serves_its_done_artifact(self):
        """Partial is a serving state: the outputs that succeeded are available."""
        self.make_job("job5", parent_status="partial", artifact_status="done")
        response = self.client.get("/api/file/job5/a000")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"payload")
        response.close()

    def test_unfinished_artifact_under_done_parent_is_404(self):
        """Parent done (others succeeded) but this artifact is not."""
        self.make_job("job6", parent_status="partial", artifact_status="error")
        response = self.client.get("/api/file/job6/a000")
        self.assertEqual(response.status_code, 404)

    def test_downloading_artifact_is_404(self):
        self.make_job("job7", parent_status="partial", artifact_status="downloading")
        response = self.client.get("/api/file/job7/a000")
        self.assertEqual(response.status_code, 404)

    def test_unknown_artifact_id_is_404(self):
        self.make_job("job8", parent_status="done", artifact_status="done")
        response = self.client.get("/api/file/job8/a999")
        self.assertEqual(response.status_code, 404)

    def test_path_outside_download_dir_is_404(self):
        """A file that resolves outside DOWNLOAD_DIR is refused even if it exists."""
        with tempfile.TemporaryDirectory() as outside_dir:
            outside = Path(outside_dir) / "secret.mp4"
            outside.write_bytes(b"not yours")
            app.jobs["job9"] = {
                "id": "job9",
                "status": "done",
                "phase": "done",
                "artifacts": [{
                    "id": "a000",
                    "status": "done",
                    "file": str(outside),
                    "filename": "secret.mp4",
                }],
            }
            response = self.client.get("/api/file/job9/a000")
            self.assertEqual(response.status_code, 404)

    def test_traversal_path_is_404(self):
        """A ../ path escaping DOWNLOAD_DIR is refused."""
        with tempfile.TemporaryDirectory() as outside_dir:
            outside = Path(outside_dir) / "escape.mp4"
            outside.write_bytes(b"nope")
            traversal = os.path.join(
                self.temp_dir, "..", os.path.basename(outside_dir), "escape.mp4"
            )
            app.jobs["job10"] = {
                "id": "job10",
                "status": "done",
                "phase": "done",
                "artifacts": [{
                    "id": "a000",
                    "status": "done",
                    "file": traversal,
                    "filename": "escape.mp4",
                }],
            }
            response = self.client.get("/api/file/job10/a000")
            self.assertEqual(response.status_code, 404)

    def test_missing_file_on_disk_is_404(self):
        artifact_file = self.make_job(
            "job11", parent_status="done", artifact_status="done"
        )
        artifact_file.unlink()
        response = self.client.get("/api/file/job11/a000")
        self.assertEqual(response.status_code, 404)


class LegacyFileEndpointAccessTests(unittest.TestCase):
    """The single-output endpoint applies the same gates as the artifact one."""

    def setUp(self):
        app.jobs.clear()
        self.client = app.app.test_client()
        self._temp = tempfile.TemporaryDirectory()
        self.temp_dir = self._temp.name
        self._patch = patch.object(app, "DOWNLOAD_DIR", self.temp_dir)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._temp.cleanup()
        app.jobs.clear()

    def make_single_output_job(self, job_id, *, parent_status, artifact_status):
        artifact_file = Path(self.temp_dir) / f"{job_id}.a000.mp4"
        artifact_file.write_bytes(b"single payload")
        app.jobs[job_id] = {
            "id": job_id,
            "status": parent_status,
            "phase": parent_status,
            "artifacts": [{
                "id": "a000",
                "status": artifact_status,
                "file": str(artifact_file),
                "filename": "movie.mp4",
            }],
        }
        return artifact_file

    def test_single_artifact_job_serves_through_legacy_endpoint(self):
        """One artifact is a single-output job: /api/file/<job> still works.

        This is the backward-compatibility contract the frontend relies on for
        its auto-save path.
        """
        self.make_single_output_job("solo1", parent_status="done", artifact_status="done")
        response = self.client.get("/api/file/solo1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"single payload")
        response.close()

    def test_legacy_endpoint_rejects_cancelled_parent(self):
        artifact_file = self.make_single_output_job(
            "solo2", parent_status="cancelled", artifact_status="done"
        )
        self.assertTrue(artifact_file.exists())
        response = self.client.get("/api/file/solo2")
        self.assertEqual(response.status_code, 404)

    def test_legacy_endpoint_rejects_running_parent(self):
        self.make_single_output_job(
            "solo3", parent_status="downloading", artifact_status="done"
        )
        response = self.client.get("/api/file/solo3")
        self.assertEqual(response.status_code, 404)

    def test_legacy_endpoint_rejects_unfinished_artifact(self):
        self.make_single_output_job(
            "solo4", parent_status="partial", artifact_status="error"
        )
        response = self.client.get("/api/file/solo4")
        self.assertEqual(response.status_code, 404)

    def test_legacy_endpoint_rejects_path_outside_download_dir(self):
        with tempfile.TemporaryDirectory() as outside_dir:
            outside = Path(outside_dir) / "secret.mp4"
            outside.write_bytes(b"not yours")
            app.jobs["solo5"] = {
                "id": "solo5",
                "status": "done",
                "phase": "done",
                "artifacts": [{
                    "id": "a000",
                    "status": "done",
                    "file": str(outside),
                    "filename": "secret.mp4",
                }],
            }
            response = self.client.get("/api/file/solo5")
            self.assertEqual(response.status_code, 404)

    def test_multi_output_job_is_409_with_artifact_ids(self):
        """The 409 must tell the client which artifact ids to ask for."""
        app.jobs["multi1"] = {
            "id": "multi1",
            "status": "done",
            "phase": "done",
            "artifacts": [
                {"id": "a000", "status": "done"},
                {"id": "a001", "status": "done"},
            ],
        }
        response = self.client.get("/api/file/multi1")
        self.assertEqual(response.status_code, 409)
        payload = response.get_json()
        self.assertEqual(payload["artifacts"], ["a000", "a001"])


if __name__ == "__main__":
    unittest.main()
