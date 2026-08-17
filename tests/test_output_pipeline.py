"""Tests for the multi-output pipeline logic module."""

import os
import unittest
from unittest.mock import patch

import output_pipeline as op


class MaxOutputsParsingTests(unittest.TestCase):
    def test_default_is_four(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LINKSIFT_MAX_OUTPUTS_PER_JOB", None)
            self.assertEqual(op.get_max_outputs_per_job(), 4)

    def test_valid_values_pass_through(self):
        for value, expected in (("1", 1), ("3", 3), ("4", 4), ("8", 8)):
            with self.subTest(value=value), patch.dict(os.environ, {"LINKSIFT_MAX_OUTPUTS_PER_JOB": value}):
                self.assertEqual(op.get_max_outputs_per_job(), expected)

    def test_values_above_eight_are_clamped(self):
        for value in ("9", "100", "1000"):
            with self.subTest(value=value), patch.dict(os.environ, {"LINKSIFT_MAX_OUTPUTS_PER_JOB": value}):
                self.assertEqual(op.get_max_outputs_per_job(), 8)

    def test_invalid_values_fall_back_to_default(self):
        for value in ("abc", "", "0", "-1", "1.5"):
            with self.subTest(value=value), patch.dict(os.environ, {"LINKSIFT_MAX_OUTPUTS_PER_JOB": value}):
                self.assertEqual(op.get_max_outputs_per_job(), 4)


class NormalizeOutputsTests(unittest.TestCase):
    def test_legacy_video_without_format_id(self):
        result, error = op.normalize_outputs({"format": "video"})
        self.assertIsNone(error)
        self.assertEqual(result, [{"type": "video", "format_id": None}])

    def test_legacy_video_with_format_id(self):
        result, error = op.normalize_outputs({"format": "video", "format_id": "137"})
        self.assertIsNone(error)
        self.assertEqual(result, [{"type": "video", "format_id": "137"}])

    def test_legacy_audio(self):
        result, error = op.normalize_outputs({"format": "audio"})
        self.assertIsNone(error)
        self.assertEqual(result, [{"type": "audio", "format_id": None}])

    def test_legacy_invalid_format(self):
        _, error = op.normalize_outputs({"format": "mp4"})
        self.assertIn("Format must be", error)

    def test_explicit_multi_output(self):
        outputs = [
            {"type": "video", "format_id": "137"},
            {"type": "video", "format_id": "22"},
            {"type": "audio"},
        ]
        result, error = op.normalize_outputs({"outputs": outputs})
        self.assertIsNone(error)
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0], {"type": "video", "format_id": "137"})
        self.assertEqual(result[1], {"type": "video", "format_id": "22"})
        self.assertEqual(result[2], {"type": "audio", "format_id": None})

    def test_empty_outputs_rejected(self):
        _, error = op.normalize_outputs({"outputs": []})
        self.assertIn("must not be empty", error)

    def test_outputs_not_a_list_rejected(self):
        _, error = op.normalize_outputs({"outputs": "not a list"})
        self.assertIn("must be an array", error)

    def test_output_not_an_object_rejected(self):
        _, error = op.normalize_outputs({"outputs": ["not an object"]})
        self.assertIn("must be an object", error)

    def test_unknown_type_rejected(self):
        _, error = op.normalize_outputs({"outputs": [{"type": "unknown"}]})
        self.assertIn("invalid type", error)

    def test_video_missing_format_id_rejected(self):
        _, error = op.normalize_outputs({"outputs": [{"type": "video"}]})
        self.assertIn("requires a 'format_id'", error)

    def test_video_format_id_not_string_rejected(self):
        _, error = op.normalize_outputs({"outputs": [{"type": "video", "format_id": 137}]})
        self.assertIn("must be a non-empty string", error)

    def test_video_format_id_unsafe_chars_rejected(self):
        for bad_id in ("137;rm", "137$HOME", "137\nnewline", "../../etc/passwd", "a" * 200):
            with self.subTest(bad_id=bad_id):
                _, error = op.normalize_outputs({"outputs": [{"type": "video", "format_id": bad_id}]})
                self.assertIsNotNone(error)

    def test_audio_format_id_ignored(self):
        result, error = op.normalize_outputs({"outputs": [{"type": "audio", "format_id": "ignored"}]})
        self.assertIsNone(error)
        self.assertEqual(result, [{"type": "audio", "format_id": None}])

    def test_duplicate_outputs_rejected(self):
        outputs = [
            {"type": "video", "format_id": "137"},
            {"type": "video", "format_id": "137"},
        ]
        _, error = op.normalize_outputs({"outputs": outputs})
        self.assertIn("Duplicate", error)

    def test_duplicate_audio_rejected(self):
        outputs = [{"type": "audio"}, {"type": "audio"}]
        _, error = op.normalize_outputs({"outputs": outputs})
        self.assertIn("Duplicate", error)

    def test_outputs_exceeding_limit_rejected(self):
        with patch.dict(os.environ, {"LINKSIFT_MAX_OUTPUTS_PER_JOB": "2"}):
            outputs = [
                {"type": "video", "format_id": "137"},
                {"type": "video", "format_id": "22"},
                {"type": "audio"},
            ]
            _, error = op.normalize_outputs({"outputs": outputs})
            self.assertIn("Too many outputs", error)
            self.assertIn("maximum is 2", error)

    def test_legacy_and_outputs_together_rejected(self):
        _, error = op.normalize_outputs({"format": "video", "outputs": [{"type": "audio"}]})
        self.assertIn("Cannot use both", error)


class PlanArtifactsTests(unittest.TestCase):
    def test_videos_before_audio_in_execution_order(self):
        outputs = [
            {"type": "audio"},
            {"type": "video", "format_id": "137"},
            {"type": "video", "format_id": "22"},
        ]
        exec_order, display_order = op.plan_artifacts(outputs)
        self.assertEqual(exec_order[0]["type"], "video")
        self.assertEqual(exec_order[1]["type"], "video")
        self.assertEqual(exec_order[2]["type"], "audio")
        # Display order preserves request order.
        self.assertEqual(display_order[0]["type"], "audio")
        self.assertEqual(display_order[1]["type"], "video")
        self.assertEqual(display_order[2]["type"], "video")

    def test_artifact_ids_are_unique(self):
        outputs = [{"type": "video", "format_id": "137"}, {"type": "audio"}]
        exec_order, _ = op.plan_artifacts(outputs)
        ids = [a["id"] for a in exec_order]
        self.assertEqual(len(ids), len(set(ids)))

    def test_labels_are_correct(self):
        outputs = [{"type": "video", "format_id": "137"}, {"type": "audio"}]
        exec_order, _ = op.plan_artifacts(outputs)
        self.assertEqual(exec_order[0]["label"], "MP4 137")
        self.assertEqual(exec_order[1]["label"], "MP3 audio")

    def test_initial_status_is_pending(self):
        exec_order, _ = op.plan_artifacts([{"type": "video", "format_id": "137"}])
        self.assertEqual(exec_order[0]["status"], "pending")
        self.assertEqual(exec_order[0]["phase"], "pending")


class CommandConstructionTests(unittest.TestCase):
    def test_video_command_includes_format_selection(self):
        artifact = {"id": "a000", "type": "video", "format_id": "137"}
        cmd = op.build_ytdlp_command(
            "https://example.test/video", "job123456", artifact, "/tmp",
            "__LINKSIFT_PROGRESS__", "__LINKSIFT_POSTPROCESS__", 4, []
        )
        self.assertIn("-f", cmd)
        idx = cmd.index("-f")
        self.assertEqual(cmd[idx + 1], "137+bestaudio/best")
        self.assertIn("--merge-output-format", cmd)
        self.assertEqual(cmd[-2:], ["--", "https://example.test/video"])

    def test_audio_command_uses_extract_audio(self):
        artifact = {"id": "a000", "type": "audio", "format_id": None}
        cmd = op.build_ytdlp_command(
            "https://example.test/video", "job123456", artifact, "/tmp",
            "__LINKSIFT_PROGRESS__", "__LINKSIFT_POSTPROCESS__", 4, []
        )
        self.assertIn("-x", cmd)
        self.assertIn("--audio-format", cmd)
        self.assertEqual(cmd[cmd.index("--audio-format") + 1], "mp3")

    def test_output_template_includes_artifact_id(self):
        artifact = {"id": "a001", "type": "video", "format_id": "22"}
        cmd = op.build_ytdlp_command(
            "https://example.test/video", "job123456", artifact, "/downloads",
            "__P__", "__PP__", 4, []
        )
        idx = cmd.index("-o")
        self.assertIn("job123456.a001", cmd[idx + 1])

    def test_ffmpeg_command_is_list_not_shell(self):
        cmd = op.build_ffmpeg_extract_command("/tmp/source.mp4", "/tmp/output.mp3")
        self.assertIsInstance(cmd, list)
        self.assertEqual(cmd[0], "ffmpeg")
        self.assertIn("-vn", cmd)
        self.assertIn("libmp3lame", cmd)


class AggregateStatusTests(unittest.TestCase):
    def test_all_done_returns_done(self):
        artifacts = [
            {"status": "done", "percent": 100},
            {"status": "done", "percent": 100},
        ]
        status, phase = op.compute_aggregate_status(artifacts)
        self.assertEqual(status, "done")

    def test_all_error_returns_error(self):
        artifacts = [{"status": "error"}, {"status": "error"}]
        status, _ = op.compute_aggregate_status(artifacts)
        self.assertEqual(status, "error")

    def test_mixed_done_and_error_returns_partial(self):
        artifacts = [{"status": "done"}, {"status": "error"}]
        status, _ = op.compute_aggregate_status(artifacts)
        self.assertEqual(status, "partial")

    def test_mixed_done_and_cancelled_returns_partial(self):
        artifacts = [{"status": "done"}, {"status": "cancelled"}]
        status, _ = op.compute_aggregate_status(artifacts)
        self.assertEqual(status, "partial")

    def test_all_cancelled_returns_cancelled(self):
        artifacts = [{"status": "cancelled"}, {"status": "cancelled"}]
        status, _ = op.compute_aggregate_status(artifacts)
        self.assertEqual(status, "cancelled")

    def test_all_timed_out_returns_timed_out(self):
        artifacts = [{"status": "timed_out"}, {"status": "timed_out"}]
        status, _ = op.compute_aggregate_status(artifacts)
        self.assertEqual(status, "timed_out")

    def test_pending_with_downloading_returns_downloading(self):
        artifacts = [{"status": "pending"}, {"status": "downloading"}]
        status, _ = op.compute_aggregate_status(artifacts)
        self.assertEqual(status, "downloading")

    def test_pending_only_returns_downloading_starting(self):
        artifacts = [{"status": "pending"}, {"status": "pending"}]
        status, phase = op.compute_aggregate_status(artifacts)
        self.assertEqual(status, "downloading")
        self.assertEqual(phase, "starting")

    def test_empty_returns_error(self):
        status, _ = op.compute_aggregate_status([])
        self.assertEqual(status, "error")


class AggregateProgressTests(unittest.TestCase):
    def test_all_done_returns_100(self):
        artifacts = [
            {"id": "a0", "status": "done", "percent": 100},
            {"id": "a1", "status": "done", "percent": 100},
        ]
        pct = op.compute_aggregate_progress(artifacts, None)
        self.assertEqual(pct, 100.0)

    def test_one_done_one_half_returns_75(self):
        artifacts = [
            {"id": "a0", "status": "done", "percent": 100},
            {"id": "a1", "status": "downloading", "percent": 50},
        ]
        pct = op.compute_aggregate_progress(artifacts, "a1")
        self.assertEqual(pct, 75.0)

    def test_no_progress_returns_none(self):
        pct = op.compute_aggregate_progress([], None)
        self.assertIsNone(pct)

    def test_current_artifact_contributes_partial_progress(self):
        artifacts = [
            {"id": "a0", "status": "done", "percent": 100},
            {"id": "a1", "status": "downloading", "percent": 30},
            {"id": "a2", "status": "pending", "percent": None},
        ]
        pct = op.compute_aggregate_progress(artifacts, "a1")
        # (1 + 0.3) / 3 * 100 = 43.3
        self.assertAlmostEqual(pct, 43.3, places=1)


class VideoForReuseTests(unittest.TestCase):
    def test_selects_first_completed_video(self):
        artifacts = [
            {"status": "done", "type": "video", "file": "/tmp/a.mp4"},
            {"status": "done", "type": "video", "file": "/tmp/b.mp4"},
        ]
        with patch("os.path.isfile", return_value=True):
            result = op.select_video_for_reuse(artifacts)
        self.assertIsNotNone(result)
        self.assertEqual(result["file"], "/tmp/a.mp4")

    def test_returns_none_when_no_video_done(self):
        artifacts = [
            {"status": "error", "type": "video", "file": None},
        ]
        result = op.select_video_for_reuse(artifacts)
        self.assertIsNone(result)

    def test_returns_none_when_file_missing(self):
        artifacts = [
            {"status": "done", "type": "video", "file": "/tmp/missing.mp4"},
        ]
        with patch("os.path.isfile", return_value=False):
            result = op.select_video_for_reuse(artifacts)
        self.assertIsNone(result)


class ArtifactFilenameTests(unittest.TestCase):
    def test_generates_safe_filename(self):
        artifact = {"type": "video", "label": "MP4 137"}
        name = op.make_artifact_filename("My Video Title", artifact)
        self.assertTrue(name.endswith(".mp4"))
        self.assertIn("MP4 137", name)
        self.assertIn("My Video Title", name)

    def test_audio_extension(self):
        artifact = {"type": "audio", "label": "MP3 audio"}
        name = op.make_artifact_filename("Title", artifact)
        self.assertTrue(name.endswith(".mp3"))

    def test_sanitizes_unsafe_chars(self):
        artifact = {"type": "video", "label": "MP4 22"}
        name = op.make_artifact_filename('Title with "quotes" and /slashes/', artifact)
        self.assertNotIn('"', name)
        self.assertNotIn('/', name)

    def test_handles_empty_title(self):
        artifact = {"type": "video", "label": "MP4 22"}
        name = op.make_artifact_filename("", artifact)
        self.assertTrue(name.endswith(".mp4"))
        self.assertIn("MP4 22", name)


if __name__ == "__main__":
    unittest.main()
