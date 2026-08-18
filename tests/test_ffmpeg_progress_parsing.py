"""Tests for real ffmpeg packaging progress: command contract and pure parsers.

Before this work the MP4 -> MP3 reuse step was a black box. The extract command
was ``["ffmpeg", "-y", "-i", src, "-vn", "-acodec", "libmp3lame", "-b:a",
"320k", dst]`` and it was run with ``communicate()``, so ffmpeg emitted nothing
machine-readable and nothing was read while it worked. The audio artifact kept
``percent = None`` for the whole packaging step, which had two visible effects:

  * the artifact row showed a frozen placeholder instead of real progress, and
  * ``compute_aggregate_progress`` fed ``current_pct = 0`` into its formula, so
    a two-output job sat at exactly 50.0 from the moment the MP4 finished until
    the MP3 flipped to ``done`` and it jumped straight to 100.

The fix adds ``-nostats -progress pipe:1`` to the command and a family of pure
parsers that turn ffmpeg's ``key=value`` stdout blocks into a percent, an ETA
and a speed. This module covers the command contract and the pure functions;
the reader thread and its sticky-terminal rules are covered elsewhere.

Every assertion here fails against the old code: the old command carries
neither ``-progress`` nor ``-nostats``, and none of the parsing helpers existed.
"""

import math
import unittest

import output_pipeline as pipeline


SRC = "/downloads/0123456789.a000.mp4"
DST = "/downloads/0123456789.a001.temp.mp3"


class FfmpegExtractCommandTests(unittest.TestCase):
    """The extract command must ask ffmpeg for machine-readable progress.

    The old command was ``["ffmpeg", "-y", "-i", src, ...]`` with no reporting
    flags at all, so ``assertIn("-progress", cmd)`` and
    ``assertIn("-nostats", cmd)`` both fail against it. Placement matters as
    much as presence: both are global options, and ffmpeg only applies them to
    the run when they appear before ``-i``. After ``-i`` they would be parsed
    as per-output options and either be rejected or silently apply to the MP3.
    """

    def setUp(self):
        self.cmd = pipeline.build_ffmpeg_extract_command(SRC, DST)

    def test_command_is_an_argv_list_not_a_shell_string(self):
        """A string here would mean shell=True and a quoting/injection surface."""
        self.assertIsInstance(self.cmd, list)
        for token in self.cmd:
            self.assertIsInstance(token, str)

    def test_command_requests_progress_on_stdout(self):
        self.assertIn("-progress", self.cmd)
        self.assertEqual(self.cmd[self.cmd.index("-progress") + 1], "pipe:1")

    def test_command_suppresses_the_human_readable_stats_line(self):
        """-nostats keeps the CR-updated status line out of the merged stream."""
        self.assertIn("-nostats", self.cmd)

    def test_progress_flag_is_global_and_precedes_input(self):
        self.assertLess(self.cmd.index("-progress"), self.cmd.index("-i"))

    def test_nostats_flag_is_global_and_precedes_input(self):
        self.assertLess(self.cmd.index("-nostats"), self.cmd.index("-i"))

    def test_pipe_value_is_not_mistaken_for_the_input_path(self):
        """``pipe:1`` must belong to -progress, never be the -i operand."""
        self.assertNotEqual(self.cmd[self.cmd.index("-i") + 1], "pipe:1")

    def test_source_immediately_follows_input_flag(self):
        self.assertEqual(self.cmd[self.cmd.index("-i") + 1], SRC)

    def test_output_path_is_the_last_token(self):
        self.assertEqual(self.cmd[-1], DST)

    def test_encoder_contract_is_unchanged(self):
        """Adding progress flags must not disturb the audio encoding options."""
        self.assertEqual(self.cmd[0], "ffmpeg")
        self.assertIn("-y", self.cmd)
        self.assertIn("-vn", self.cmd)
        self.assertEqual(self.cmd[self.cmd.index("-acodec") + 1], "libmp3lame")
        self.assertEqual(self.cmd[self.cmd.index("-b:a") + 1], "320k")

    def test_video_is_dropped_after_the_input(self):
        """-vn is an output option; before -i it would apply to the source."""
        self.assertGreater(self.cmd.index("-vn"), self.cmd.index("-i"))

    def test_paths_are_passed_through_verbatim(self):
        weird_src = "/downloads/a b & c.mp4"
        weird_dst = "/downloads/a b & c.temp.mp3"
        cmd = pipeline.build_ffmpeg_extract_command(weird_src, weird_dst)
        self.assertEqual(cmd[cmd.index("-i") + 1], weird_src)
        self.assertEqual(cmd[-1], weird_dst)


class ParseFfmpegDurationTests(unittest.TestCase):
    """Total input length comes from ffmpeg's own metadata dump.

    Reading it out of the stream we already have avoids spawning a second
    ffprobe process. An unparsable or absent duration must stay None so the
    caller keeps the artifact indeterminate instead of inventing a denominator.
    """

    def test_parses_a_real_duration_line(self):
        line = "  Duration: 00:01:23.11, start: 0.000000, bitrate: 129 kb/s"
        self.assertAlmostEqual(pipeline.parse_ffmpeg_duration(line), 83.11, places=6)

    def test_parses_multi_digit_hours(self):
        """Hours are not zero-padded to two digits for very long inputs."""
        line = "  Duration: 100:00:00.00, start: 0.000000, bitrate: 129 kb/s"
        self.assertAlmostEqual(pipeline.parse_ffmpeg_duration(line), 360000.0, places=6)

    def test_duration_not_available_is_none(self):
        line = "  Duration: N/A, start: 0.000000, bitrate: N/A"
        self.assertIsNone(pipeline.parse_ffmpeg_duration(line))

    def test_line_without_duration_is_none(self):
        self.assertIsNone(pipeline.parse_ffmpeg_duration("frame=  120 fps=0.0 q=-1.0"))

    def test_zero_duration_is_none(self):
        """A zero denominator would make every percent computation meaningless."""
        line = "  Duration: 00:00:00.00, start: 0.000000, bitrate: 0 kb/s"
        self.assertIsNone(pipeline.parse_ffmpeg_duration(line))

    def test_non_string_input_is_none(self):
        for value in (None, 123, 12.5, b"Duration: 00:01:23.11", ["Duration: 00:01:23.11"]):
            with self.subTest(value=value):
                self.assertIsNone(pipeline.parse_ffmpeg_duration(value))


class ParseFfmpegProgressLineTests(unittest.TestCase):
    """Only unindented ``key=value`` lines are progress records.

    stdout and stderr are merged into one stream, so the parser sees ffmpeg's
    banner and input dump alongside the -progress block. Metadata lines are
    indented and use ``key : value``; treating one of them as a progress record
    would publish garbage into the artifact.
    """

    def test_parses_out_time(self):
        self.assertEqual(
            pipeline.parse_ffmpeg_progress_line("out_time=00:00:07.50"),
            ("out_time", "00:00:07.50"),
        )

    def test_parses_speed(self):
        self.assertEqual(
            pipeline.parse_ffmpeg_progress_line("speed=12.3x"),
            ("speed", "12.3x"),
        )

    def test_parses_the_block_terminator(self):
        """``progress=continue``/``end`` is what triggers a publish."""
        self.assertEqual(
            pipeline.parse_ffmpeg_progress_line("progress=continue"),
            ("progress", "continue"),
        )
        self.assertEqual(
            pipeline.parse_ffmpeg_progress_line("progress=end"),
            ("progress", "end"),
        )

    def test_indented_duration_line_is_not_a_progress_record(self):
        line = "  Duration: 00:01:23.11, start: 0.000000, bitrate: 129 kb/s"
        self.assertIsNone(pipeline.parse_ffmpeg_progress_line(line))

    def test_stream_banner_is_not_a_progress_record(self):
        self.assertIsNone(
            pipeline.parse_ffmpeg_progress_line("Stream #0:0: Audio: mp3")
        )

    def test_indented_metadata_pair_is_not_a_progress_record(self):
        """``key : value`` metadata must not be mistaken for ``key=value``."""
        self.assertIsNone(
            pipeline.parse_ffmpeg_progress_line("  encoder         : Lavf")
        )

    def test_indentation_alone_disqualifies_a_progress_record(self):
        """The leading anchor, not the punctuation, is what rejects metadata.

        The other negative cases here would be rejected anyway (they use
        ``key : value`` or carry no ``=`` at all), so none of them notices if
        the pattern stops being anchored at the start of the line. ffmpeg's
        input dump does contain indented ``key=value`` pairs, and a tag such as
        an indented ``out_time=`` would then be published as a real position.
        """
        self.assertIsNone(
            pipeline.parse_ffmpeg_progress_line("    out_time=00:00:99.00")
        )
        self.assertIsNone(pipeline.parse_ffmpeg_progress_line("  speed=999x"))
        self.assertIsNone(pipeline.parse_ffmpeg_progress_line("\tprogress=end"))

    def test_empty_line_is_none(self):
        self.assertIsNone(pipeline.parse_ffmpeg_progress_line(""))

    def test_non_string_input_is_none(self):
        for value in (None, 0, 7.5, b"out_time=00:00:07.50", ("out_time", "1")):
            with self.subTest(value=value):
                self.assertIsNone(pipeline.parse_ffmpeg_progress_line(value))

    def test_trailing_whitespace_is_stripped_from_the_value(self):
        """Line endings must not leak into the value fed to the converters."""
        self.assertEqual(
            pipeline.parse_ffmpeg_progress_line("speed=12.3x  "),
            ("speed", "12.3x"),
        )


class FfmpegProgressSecondsTests(unittest.TestCase):
    """Position-in-input conversion for the ``out_time*`` family."""

    def test_out_time_timestamp(self):
        self.assertAlmostEqual(
            pipeline.ffmpeg_progress_seconds("out_time", "00:00:07.50"), 7.5, places=6
        )

    def test_out_time_us_is_microseconds(self):
        self.assertAlmostEqual(
            pipeline.ffmpeg_progress_seconds("out_time_us", "7500000"), 7.5, places=6
        )

    def test_out_time_ms_is_also_read_as_microseconds(self):
        """ffmpeg emits MICROseconds under the ``out_time_ms`` name.

        This is a long-standing upstream quirk, not a bug here: the key is
        named milliseconds but the value has always been microseconds. Dividing
        it by 1_000 as the name suggests would report a position 1000x too
        large, which would peg the artifact at 100% on the first block. The
        1_000_000 divisor is what matches ffmpeg's actual output.
        """
        self.assertAlmostEqual(
            pipeline.ffmpeg_progress_seconds("out_time_ms", "7500000"), 7.5, places=6
        )

    def test_all_declared_time_keys_convert_consistently(self):
        self.assertEqual(
            pipeline.FFMPEG_TIME_KEYS, ("out_time", "out_time_us", "out_time_ms")
        )
        values = {
            "out_time": "00:00:07.50",
            "out_time_us": "7500000",
            "out_time_ms": "7500000",
        }
        for key in pipeline.FFMPEG_TIME_KEYS:
            with self.subTest(key=key):
                self.assertAlmostEqual(
                    pipeline.ffmpeg_progress_seconds(key, values[key]), 7.5, places=6
                )

    def test_not_available_is_none(self):
        for key in pipeline.FFMPEG_TIME_KEYS:
            with self.subTest(key=key):
                self.assertIsNone(pipeline.ffmpeg_progress_seconds(key, "N/A"))

    def test_empty_value_is_none(self):
        for key in pipeline.FFMPEG_TIME_KEYS:
            with self.subTest(key=key):
                self.assertIsNone(pipeline.ffmpeg_progress_seconds(key, ""))
                self.assertIsNone(pipeline.ffmpeg_progress_seconds(key, "   "))

    def test_negative_sentinel_timestamp_is_rejected(self):
        """ffmpeg emits -577014:32:22.000000 before the first frame lands.

        Rejecting it keeps the position unknown. Clamping it to zero would
        instead publish a real 0% and reset a bar that had already moved.
        """
        self.assertIsNone(
            pipeline.ffmpeg_progress_seconds("out_time", "-577014:32:22.000000")
        )

    def test_negative_microseconds_are_rejected(self):
        self.assertIsNone(pipeline.ffmpeg_progress_seconds("out_time_us", "-1"))
        self.assertIsNone(pipeline.ffmpeg_progress_seconds("out_time_ms", "-1"))

    def test_nan_and_inf_are_rejected(self):
        for key in ("out_time_us", "out_time_ms"):
            for value in ("nan", "inf", "-inf", "Infinity"):
                with self.subTest(key=key, value=value):
                    self.assertIsNone(pipeline.ffmpeg_progress_seconds(key, value))

    def test_unrelated_keys_are_rejected(self):
        for key in ("bitrate", "total_size", "speed", "frame", "out_time_s", ""):
            with self.subTest(key=key):
                self.assertIsNone(pipeline.ffmpeg_progress_seconds(key, "7500000"))

    def test_non_string_value_is_none(self):
        for value in (None, 7500000, 7.5, b"7500000"):
            with self.subTest(value=value):
                self.assertIsNone(pipeline.ffmpeg_progress_seconds("out_time_us", value))
                self.assertIsNone(pipeline.ffmpeg_progress_seconds("out_time", value))

    def test_malformed_timestamp_is_none(self):
        for value in ("00:07.50", "7.5", "aa:bb:cc.dd", "00:0:07.50"):
            with self.subTest(value=value):
                self.assertIsNone(pipeline.ffmpeg_progress_seconds("out_time", value))

    def test_trailing_garbage_after_a_timestamp_is_none(self):
        """The timestamp must consume the whole value, not just a prefix.

        Every other malformed case above fails at the start or in the middle of
        the pattern, so a lost closing anchor goes unnoticed. A truncated or
        interleaved line then yields a plausible-looking position built from a
        prefix of a value ffmpeg never meant as a timestamp.
        """
        for value in ("00:00:07.50abc", "00:00:07.50:99", "00:00:07.5x", "00:00:07.50 junk"):
            with self.subTest(value=value):
                self.assertIsNone(pipeline.ffmpeg_progress_seconds("out_time", value))


class ParseFfmpegSpeedTests(unittest.TestCase):
    """``speed=12.3x`` is media seconds produced per wall-clock second."""

    def test_decimal_speed(self):
        self.assertAlmostEqual(pipeline.parse_ffmpeg_speed("12.3x"), 12.3, places=6)

    def test_integer_speed(self):
        self.assertAlmostEqual(pipeline.parse_ffmpeg_speed("1x"), 1.0, places=6)

    def test_scientific_notation_is_accepted(self):
        """ffmpeg prints very fast copies in exponent form; 1.0e1x is 10x."""
        self.assertAlmostEqual(pipeline.parse_ffmpeg_speed("1.0e1x"), 10.0, places=6)

    def test_not_available_is_none(self):
        self.assertIsNone(pipeline.parse_ffmpeg_speed("N/A"))

    def test_zero_speed_is_none(self):
        """0x would divide by zero in the ETA computation."""
        self.assertIsNone(pipeline.parse_ffmpeg_speed("0x"))

    def test_negative_speed_is_none(self):
        self.assertIsNone(pipeline.parse_ffmpeg_speed("-2x"))

    def test_missing_suffix_is_none(self):
        self.assertIsNone(pipeline.parse_ffmpeg_speed("12.3"))

    def test_trailing_garbage_after_the_suffix_is_none(self):
        """``x`` must end the value, not merely appear in it.

        ``"12.3"`` is rejected for lacking the suffix entirely, so it never
        exercises the closing anchor. Without that anchor a corrupted or
        partially-flushed line like ``12.3xyz`` parses as a clean 12.3 and a
        fabricated speed feeds straight into the ETA.
        """
        for value in ("12.3xyz", "1xJUNK", "12.3x garbage", "12.3x x"):
            with self.subTest(value=value):
                self.assertIsNone(pipeline.parse_ffmpeg_speed(value))

    def test_empty_value_is_none(self):
        self.assertIsNone(pipeline.parse_ffmpeg_speed(""))

    def test_non_string_value_is_none(self):
        for value in (None, 12.3, 12, b"12.3x"):
            with self.subTest(value=value):
                self.assertIsNone(pipeline.parse_ffmpeg_speed(value))

    def test_surrounding_whitespace_is_tolerated(self):
        self.assertAlmostEqual(pipeline.parse_ffmpeg_speed(" 12.3x "), 12.3, places=6)


class ComputeProcessingPercentTests(unittest.TestCase):
    """Percent of the input processed: clamped, rounded, and never decreasing."""

    def test_basic_ratio(self):
        self.assertEqual(pipeline.compute_processing_percent(7.5, 15, None), 50.0)

    def test_result_is_rounded_to_one_decimal(self):
        self.assertEqual(pipeline.compute_processing_percent(1, 3, None), 33.3)

    def test_overshoot_is_clamped_to_one_hundred(self):
        """ffmpeg can report a position past the advertised duration."""
        self.assertEqual(pipeline.compute_processing_percent(200, 100, None), 100.0)
        self.assertEqual(pipeline.compute_processing_percent(15.4, 15, 50.0), 100.0)

    def test_percent_never_walks_backwards(self):
        """A dip between blocks must not rewind a bar the user already saw."""
        self.assertEqual(pipeline.compute_processing_percent(5, 100, 40.0), 40.0)

    def test_forward_movement_still_wins(self):
        self.assertEqual(pipeline.compute_processing_percent(60, 100, 40.0), 60.0)

    def test_missing_duration_returns_previous(self):
        self.assertEqual(pipeline.compute_processing_percent(5, None, 40.0), 40.0)
        self.assertIsNone(pipeline.compute_processing_percent(5, None, None))

    def test_zero_duration_returns_previous(self):
        self.assertEqual(pipeline.compute_processing_percent(5, 0, 40.0), 40.0)
        self.assertIsNone(pipeline.compute_processing_percent(5, 0, None))

    def test_negative_duration_returns_previous(self):
        self.assertEqual(pipeline.compute_processing_percent(5, -10, 40.0), 40.0)

    def test_missing_processed_returns_previous(self):
        self.assertEqual(pipeline.compute_processing_percent(None, 100, 40.0), 40.0)
        self.assertIsNone(pipeline.compute_processing_percent(None, 100, None))

    def test_negative_processed_returns_previous(self):
        self.assertEqual(pipeline.compute_processing_percent(-5, 100, 40.0), 40.0)
        self.assertIsNone(pipeline.compute_processing_percent(-5, 100, None))

    def test_everything_unknown_is_none(self):
        """No inputs means no number: the artifact stays indeterminate."""
        self.assertIsNone(pipeline.compute_processing_percent(None, None, None))

    def test_previous_above_one_hundred_is_clamped_first(self):
        self.assertEqual(pipeline.compute_processing_percent(5, None, 140.0), 100.0)
        self.assertEqual(pipeline.compute_processing_percent(5, 100, 140.0), 100.0)

    def test_previous_below_zero_is_clamped_first(self):
        self.assertEqual(pipeline.compute_processing_percent(None, None, -20.0), 0.0)
        self.assertEqual(pipeline.compute_processing_percent(5, 100, -20.0), 5.0)

    def test_nan_arguments_never_produce_a_number(self):
        """float('nan') compares false against everything; it must be filtered."""
        nan = float("nan")
        self.assertEqual(pipeline.compute_processing_percent(nan, 100, 40.0), 40.0)
        self.assertEqual(pipeline.compute_processing_percent(5, nan, 40.0), 40.0)
        self.assertEqual(pipeline.compute_processing_percent(5, 100, nan), 5.0)
        self.assertIsNone(pipeline.compute_processing_percent(nan, nan, nan))

    def test_infinite_arguments_never_produce_a_number(self):
        for infinite in (float("inf"), float("-inf")):
            with self.subTest(value=infinite):
                self.assertEqual(
                    pipeline.compute_processing_percent(infinite, 100, 40.0), 40.0
                )
                self.assertEqual(
                    pipeline.compute_processing_percent(5, infinite, 40.0), 40.0
                )
                self.assertEqual(
                    pipeline.compute_processing_percent(5, 100, infinite), 5.0
                )

    def test_booleans_are_not_treated_as_numbers(self):
        """True would otherwise become 1.0 and fabricate a 1-second duration."""
        self.assertEqual(pipeline.compute_processing_percent(True, 100, 40.0), 40.0)
        self.assertEqual(pipeline.compute_processing_percent(5, True, 40.0), 40.0)
        self.assertEqual(pipeline.compute_processing_percent(5, 100, True), 5.0)

    def test_unparsable_arguments_return_previous(self):
        self.assertEqual(pipeline.compute_processing_percent("abc", 100, 40.0), 40.0)
        self.assertEqual(pipeline.compute_processing_percent(5, [], 40.0), 40.0)

    def test_every_result_stays_within_bounds(self):
        samples = (None, -5, 0, 1, 7.5, 100, 1e9, float("nan"), float("inf"), True, "x")
        for processed in samples:
            for duration in samples:
                for previous in (None, 0.0, 40.0, 100.0, 140.0, -20.0):
                    result = pipeline.compute_processing_percent(
                        processed, duration, previous
                    )
                    if result is None:
                        continue
                    self.assertTrue(
                        math.isfinite(result) and 0.0 <= result <= 100.0,
                        f"out of range: {result!r} from "
                        f"({processed!r}, {duration!r}, {previous!r})",
                    )


class ComputeProcessingEtaTests(unittest.TestCase):
    """Wall-clock seconds left = remaining media seconds / ffmpeg's speed."""

    def test_basic_estimate(self):
        """7.5s of 15s done at 2x leaves 7.5 media seconds -> 3.75s -> 4."""
        self.assertEqual(pipeline.compute_processing_eta(7.5, 15, 2.0), 4)

    def test_result_is_a_whole_number_of_seconds(self):
        self.assertIsInstance(pipeline.compute_processing_eta(7.5, 15, 2.0), int)

    def test_exactly_finished_is_zero(self):
        self.assertEqual(pipeline.compute_processing_eta(15, 15, 2.0), 0)

    def test_overshoot_is_zero_not_negative(self):
        """A countdown must never show a negative number of seconds left."""
        self.assertEqual(pipeline.compute_processing_eta(20, 15, 2.0), 0)

    def test_missing_speed_is_none(self):
        self.assertIsNone(pipeline.compute_processing_eta(7.5, 15, None))

    def test_zero_speed_is_none(self):
        """Guards the division; 0x would otherwise raise ZeroDivisionError."""
        self.assertIsNone(pipeline.compute_processing_eta(7.5, 15, 0))

    def test_negative_speed_is_none(self):
        self.assertIsNone(pipeline.compute_processing_eta(7.5, 15, -1))

    def test_missing_duration_is_none(self):
        self.assertIsNone(pipeline.compute_processing_eta(7.5, None, 2.0))

    def test_zero_duration_is_none(self):
        self.assertIsNone(pipeline.compute_processing_eta(7.5, 0, 2.0))

    def test_missing_processed_is_none(self):
        self.assertIsNone(pipeline.compute_processing_eta(None, 15, 2.0))

    def test_negative_processed_is_none(self):
        self.assertIsNone(pipeline.compute_processing_eta(-1, 15, 2.0))

    def test_nan_and_inf_arguments_are_none(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=bad):
                self.assertIsNone(pipeline.compute_processing_eta(bad, 15, 2.0))
                self.assertIsNone(pipeline.compute_processing_eta(7.5, bad, 2.0))
                self.assertIsNone(pipeline.compute_processing_eta(7.5, 15, bad))

    def test_unparsable_arguments_are_none(self):
        self.assertIsNone(pipeline.compute_processing_eta("abc", 15, 2.0))
        self.assertIsNone(pipeline.compute_processing_eta(7.5, 15, "fast"))

    def test_slow_speed_produces_a_larger_estimate(self):
        """A 0.5x pass over the same remainder takes four times as long as 2x."""
        self.assertEqual(pipeline.compute_processing_eta(0, 100, 2.0), 50)
        self.assertEqual(pipeline.compute_processing_eta(0, 100, 0.5), 200)


class PlanArtifactsPackagingFieldsTests(unittest.TestCase):
    """Every artifact carries the packaging fields from the moment it is planned.

    /api/status reads these keys unconditionally. If plan_artifacts omitted them
    a video-only job (which never reaches the ffmpeg step) would raise KeyError
    on the first poll, so they must exist and start as None for every artifact,
    not just for audio ones.
    """

    PACKAGING_FIELDS = ("processed_seconds", "duration_seconds", "processing_speed")

    def _plan(self, outputs):
        execution_order, display_order = pipeline.plan_artifacts(outputs)
        return execution_order, display_order

    def test_fields_present_and_none_for_every_artifact(self):
        outputs = [
            {"type": "video", "format_id": "137"},
            {"type": "audio", "format_id": None},
            {"type": "video", "format_id": "22"},
        ]
        _, display_order = self._plan(outputs)
        self.assertEqual(len(display_order), 3)
        for artifact in display_order:
            for field in self.PACKAGING_FIELDS:
                with self.subTest(artifact=artifact["id"], field=field):
                    self.assertIn(field, artifact)
                    self.assertIsNone(artifact[field])

    def test_video_only_job_also_gets_the_fields(self):
        _, display_order = self._plan([{"type": "video", "format_id": "137"}])
        for field in self.PACKAGING_FIELDS:
            with self.subTest(field=field):
                self.assertIn(field, display_order[0])
                self.assertIsNone(display_order[0][field])

    def test_execution_and_display_order_share_the_same_dicts(self):
        """Both views must alias one dict, or a publish would update only one."""
        execution_order, display_order = self._plan(
            [{"type": "audio", "format_id": None}, {"type": "video", "format_id": "137"}]
        )
        self.assertEqual(len(execution_order), len(display_order))
        for artifact in display_order:
            self.assertTrue(any(artifact is other for other in execution_order))

    def test_existing_progress_fields_are_untouched(self):
        """The new keys are additions; nothing older may have been dropped."""
        _, display_order = self._plan([{"type": "audio", "format_id": None}])
        artifact = display_order[0]
        for field in ("percent", "eta", "speed", "status", "phase", "downloaded_bytes"):
            with self.subTest(field=field):
                self.assertIn(field, artifact)


class AggregateProgressWithRealPackagingPercentTests(unittest.TestCase):
    """The parent bar can only leave 50% once the audio artifact has a percent.

    ``compute_aggregate_progress`` computes ``(completed + current_pct/100) /
    total * 100``. Its arithmetic is unchanged, but its input is not: before
    this work the audio artifact's ``percent`` stayed None for the entire
    packaging step, so ``current_pct`` was always 0 and a two-output job was
    pinned at exactly 50.0 until the MP3 finished and the value jumped to 100.

    70.0 is therefore only reachable when the audio artifact carries a real
    percent produced by the new parsers. The chain test below derives that 40.0
    from raw ffmpeg output rather than hard-coding it, so it fails if any link
    in the parse -> seconds -> percent path is missing.
    """

    def _artifacts(self, audio_percent):
        _, display_order = pipeline.plan_artifacts(
            [{"type": "video", "format_id": "137"}, {"type": "audio", "format_id": None}]
        )
        video, audio = display_order
        video["status"] = "done"
        video["percent"] = 100.0
        audio["status"] = "processing"
        audio["percent"] = audio_percent
        return display_order, audio["id"]

    def test_audio_at_forty_percent_puts_the_parent_at_seventy(self):
        artifacts, current_id = self._artifacts(40.0)
        self.assertEqual(
            pipeline.compute_aggregate_progress(artifacts, current_id), 70.0
        )

    def test_audio_at_zero_percent_keeps_the_parent_at_fifty(self):
        artifacts, current_id = self._artifacts(0.0)
        self.assertEqual(
            pipeline.compute_aggregate_progress(artifacts, current_id), 50.0
        )

    def test_audio_at_one_hundred_percent_puts_the_parent_at_one_hundred(self):
        artifacts, current_id = self._artifacts(100.0)
        self.assertEqual(
            pipeline.compute_aggregate_progress(artifacts, current_id), 100.0
        )

    def test_audio_without_a_percent_is_the_old_stuck_at_fifty_behaviour(self):
        """This is exactly what the user saw before: no percent, no movement."""
        artifacts, current_id = self._artifacts(None)
        self.assertEqual(
            pipeline.compute_aggregate_progress(artifacts, current_id), 50.0
        )

    def test_parent_moves_monotonically_as_packaging_advances(self):
        seen = []
        for percent in (0.0, 10.0, 40.0, 75.0, 100.0):
            artifacts, current_id = self._artifacts(percent)
            seen.append(pipeline.compute_aggregate_progress(artifacts, current_id))
        self.assertEqual(seen, [50.0, 55.0, 70.0, 87.5, 100.0])
        self.assertEqual(seen, sorted(seen))

    def test_full_chain_from_raw_ffmpeg_output_to_parent_percent(self):
        """Feed a real -progress block through every helper end to end.

        A 15s input, 6s processed at 12.3x: percent 40.0, ETA 1s, and a parent
        that reads 70.0. Against the old code there is no block to parse (no
        ``-progress`` flag) and none of these helpers exist.
        """
        stream = [
            "  Duration: 00:00:15.00, start: 0.000000, bitrate: 129 kb/s",
            "  encoder         : Lavf60.16.100",
            "Stream #0:0: Audio: mp3, 44100 Hz, stereo",
            "frame=    0 fps=0.0",
            "out_time_ms=6000000",
            "speed=12.3x",
            "progress=continue",
        ]

        duration = None
        processed = None
        speed = None
        for line in stream:
            if duration is None:
                duration = pipeline.parse_ffmpeg_duration(line)
            parsed = pipeline.parse_ffmpeg_progress_line(line)
            if parsed is None:
                continue
            key, value = parsed
            if key in pipeline.FFMPEG_TIME_KEYS:
                seconds = pipeline.ffmpeg_progress_seconds(key, value)
                if seconds is not None:
                    processed = seconds
            elif key == "speed":
                parsed_speed = pipeline.parse_ffmpeg_speed(value)
                if parsed_speed is not None:
                    speed = parsed_speed

        self.assertAlmostEqual(duration, 15.0, places=6)
        self.assertAlmostEqual(processed, 6.0, places=6)
        self.assertAlmostEqual(speed, 12.3, places=6)

        percent = pipeline.compute_processing_percent(processed, duration, None)
        self.assertEqual(percent, 40.0)
        self.assertEqual(pipeline.compute_processing_eta(processed, duration, speed), 1)

        artifacts, current_id = self._artifacts(percent)
        self.assertEqual(
            pipeline.compute_aggregate_progress(artifacts, current_id), 70.0
        )


if __name__ == "__main__":
    unittest.main()
