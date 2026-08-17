"""Regression tests for the consolidated intermediate-file detector.

Before consolidation there were three separate implementations:
  * app.py had a correct digit-checking copy that was dead code,
  * app.py redefined it later with ``".f" in name``, which won at import time
    and misclassified any name containing ``.f``,
  * output_pipeline.py carried a third copy with the same defect.
None of the three recognized the LinkSift ffmpeg scratch file ``.temp.mp3``,
so a cancelled or failed reuse left the temp file behind and
``select_artifact_output_file`` could return it as if it were final output.

There must now be exactly one implementation, and it must never classify a
final artifact as intermediate.
"""
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import app
import output_pipeline as pipeline


JOB = "0123456789"


class SingleImplementationTests(unittest.TestCase):
    def test_app_reexports_the_pipeline_implementation(self):
        """app.is_intermediate_file must BE the pipeline function, not a copy."""
        self.assertIs(app.is_intermediate_file, pipeline.is_intermediate_file)

    def test_no_private_duplicate_left_in_pipeline(self):
        self.assertFalse(
            hasattr(pipeline, "_is_intermediate_file"),
            "output_pipeline._is_intermediate_file must be gone, not shadowed",
        )

    def test_app_defines_the_helper_exactly_once(self):
        """A second `def is_intermediate_file` would silently override the first."""
        source = Path(app.__file__).read_text(encoding="utf-8")
        self.assertEqual(source.count("def is_intermediate_file"), 0)
        self.assertEqual(source.count("is_intermediate_file = pipeline.is_intermediate_file"), 1)

    def test_job_paths_defined_exactly_once(self):
        source = Path(app.__file__).read_text(encoding="utf-8")
        self.assertEqual(source.count("def job_paths"), 1)


class IntermediateClassificationTests(unittest.TestCase):
    """Positive cases: these must all be treated as removable intermediates."""

    def test_part_file_is_intermediate(self):
        self.assertTrue(app.is_intermediate_file(f"{JOB}.a000.mp4.part"))

    def test_fragment_part_file_is_intermediate(self):
        self.assertTrue(app.is_intermediate_file(f"{JOB}.a000.f137.mp4.part"))

    def test_numbered_part_fragment_is_intermediate(self):
        self.assertTrue(app.is_intermediate_file(f"{JOB}.a000.mp4.part-Frag12"))

    def test_ytdl_metadata_is_intermediate(self):
        self.assertTrue(app.is_intermediate_file(f"{JOB}.a000.ytdl"))

    def test_format_stream_is_intermediate(self):
        self.assertTrue(app.is_intermediate_file(f"{JOB}.a000.f137.mp4"))
        self.assertTrue(app.is_intermediate_file(f"{JOB}.a000.f22.webm"))

    def test_ffmpeg_temp_output_is_intermediate(self):
        """The regression that motivated consolidation: .temp.mp3 was missed."""
        self.assertTrue(app.is_intermediate_file(f"{JOB}.a001.temp.mp3"))

    def test_full_paths_are_classified_by_basename(self):
        path = os.path.join("/var/tmp/linksift", f"{JOB}.a000.mp4.part")
        self.assertTrue(app.is_intermediate_file(path))


class FinalArtifactClassificationTests(unittest.TestCase):
    """Negative cases: a final artifact must never be deleted as intermediate."""

    def test_final_video_artifact_is_not_intermediate(self):
        self.assertFalse(app.is_intermediate_file(f"{JOB}.a000.mp4"))

    def test_final_audio_artifact_is_not_intermediate(self):
        self.assertFalse(app.is_intermediate_file(f"{JOB}.a001.mp3"))

    def test_legacy_single_output_names_are_not_intermediate(self):
        self.assertFalse(app.is_intermediate_file(f"{JOB}.mp4"))
        self.assertFalse(app.is_intermediate_file(f"{JOB}.mp3"))

    def test_name_containing_dot_f_without_digits_is_not_intermediate(self):
        """`".f" in name` used to make these false positives."""
        for name in (f"{JOB}.a0af.mp4", f"{JOB}.final.mp4", f"{JOB}.a000.flac"):
            with self.subTest(name=name):
                self.assertFalse(app.is_intermediate_file(name))


class ArtifactCleanupIsolationTests(unittest.TestCase):
    """Cleaning one artifact must not touch its siblings or the parent."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._patch = unittest.mock.patch.object(app, "DOWNLOAD_DIR", self.tmp.name)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _touch(self, name):
        path = Path(self.tmp.name) / name
        path.write_bytes(b"x")
        return path

    def test_cleanup_removes_only_intermediates_by_default(self):
        final = self._touch(f"{JOB}.a000.mp4")
        part = self._touch(f"{JOB}.a000.mp4.part")
        temp = self._touch(f"{JOB}.a000.temp.mp3")

        app.cleanup_artifact_files(JOB, "a000")

        self.assertTrue(final.exists(), "final artifact must survive")
        self.assertFalse(part.exists())
        self.assertFalse(temp.exists(), ".temp.mp3 must now be cleaned")

    def test_cleanup_does_not_touch_sibling_artifact(self):
        mine = self._touch(f"{JOB}.a000.mp4.part")
        sibling_final = self._touch(f"{JOB}.a001.mp3")
        sibling_part = self._touch(f"{JOB}.a001.mp3.part")

        app.cleanup_artifact_files(JOB, "a000", include_final=True)

        self.assertFalse(mine.exists())
        self.assertTrue(sibling_final.exists())
        self.assertTrue(sibling_part.exists())

    def test_cleanup_does_not_touch_another_job(self):
        other = "9876543210"
        mine = self._touch(f"{JOB}.a000.mp4")
        theirs = self._touch(f"{other}.a000.mp4")

        app.cleanup_artifact_files(JOB, "a000", include_final=True)

        self.assertFalse(mine.exists())
        self.assertTrue(theirs.exists())

    def test_job_paths_matches_all_artifacts_of_the_job_only(self):
        self._touch(f"{JOB}.a000.mp4")
        self._touch(f"{JOB}.a001.mp3")
        self._touch(f"{JOB}.a000.mp4.part")
        self._touch("9876543210.a000.mp4")

        found = {os.path.basename(p) for p in app.job_paths(JOB)}

        self.assertEqual(
            found,
            {f"{JOB}.a000.mp4", f"{JOB}.a001.mp3", f"{JOB}.a000.mp4.part"},
        )


class SelectArtifactOutputTests(unittest.TestCase):
    """select_artifact_output_file must never hand back an intermediate."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _touch(self, name):
        path = Path(self.tmp.name) / name
        path.write_bytes(b"x")
        return path

    def test_temp_mp3_is_not_selected_as_final_audio(self):
        """A leftover .temp.mp3 must not be published as the artifact."""
        self._touch(f"{JOB}.a001.temp.mp3")

        selected = pipeline.select_artifact_output_file(
            JOB, "a001", "audio", self.tmp.name
        )

        self.assertIsNone(selected)

    def test_exact_final_name_is_selected(self):
        final = self._touch(f"{JOB}.a001.mp3")
        self._touch(f"{JOB}.a001.temp.mp3")

        selected = pipeline.select_artifact_output_file(
            JOB, "a001", "audio", self.tmp.name
        )

        self.assertEqual(selected, str(final))

    def test_fragment_files_are_not_selected_as_final_video(self):
        self._touch(f"{JOB}.a000.f137.mp4")

        selected = pipeline.select_artifact_output_file(
            JOB, "a000", "video", self.tmp.name
        )

        self.assertIsNone(selected)


if __name__ == "__main__":
    unittest.main()
