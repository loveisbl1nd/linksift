"""Regression tests for the live ffmpeg packaging progress reader.

Two defects motivated this file.

1. ``try_ffmpeg_reuse`` ran ffmpeg through ``communicate()``. ``communicate()``
   does not return until the child exits, so nothing on the merged pipe could be
   applied to the artifact WHILE ffmpeg worked: the audio artifact held
   ``percent = None`` for the entire packaging step and then jumped to 100. The
   fix spawns with ``stdout=PIPE, stderr=STDOUT``, starts
   ``read_ffmpeg_progress`` on a daemon thread and waits with
   ``process.wait(timeout=...)``, so progress is published as it is produced.

2. A reader that publishes blindly resurrects finished work. ffmpeg keeps
   emitting blocks for a short while after a DELETE, so a late block could
   rewrite a ``cancelled`` artifact back to ``processing`` with a fresh percent.
   The reader therefore re-reads ``jobs[job_id]`` under ``jobs_lock`` on every
   publish and drops the update when the job is gone or terminal, or when the
   artifact itself is terminal -- the same sticky-terminal rule
   ``update_job_progress`` follows. It still drains to EOF in that case, because
   an unread pipe blocks ffmpeg's next write and a blocked ffmpeg cannot be
   reaped.

Everything here drives fakes: an in-memory stream for the reader tests and a
fake Popen for the lifecycle tests. No ffmpeg, no yt-dlp, no network, no
``time.sleep`` -- threads are sequenced with ``threading.Event`` so the tests
are deterministic rather than timing-dependent.
"""
import io
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import app
import output_pipeline as pipeline


JOB = "5566778899"

# Every Event.wait() in this module uses this bound. A deadlock in the code
# under test must fail the assertion that follows, never hang the suite.
WAIT_TIMEOUT = 5.0


def _fake_run(*_args, **_kwargs):
    """Stub for subprocess.run, used by terminate_process_tree's Windows
    taskkill path. Without it the patched Popen would be abused as a taskkill
    subprocess."""
    return type("R", (), {"returncode": 0, "stdout": b"", "stderr": b""})()


# --- transcripts -------------------------------------------------------
# Shaped like real ffmpeg output on the merged stream: an indented input dump
# carrying the Duration line, then unindented `key=value` progress blocks each
# terminated by `progress=continue` (or `progress=end` for the last one).

HEADER = [
    "ffmpeg version 6.1.1 Copyright (c) 2000-2023 the FFmpeg developers",
    "Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'source.mp4':",
    "  Metadata:",
    "    major_brand     : isom",
    "  Duration: 00:01:40.00, start: 0.000000, bitrate: 1132 kb/s",
    "Stream mapping:",
    "  Stream #0:1 -> #0:0 (aac (native) -> mp3 (libmp3lame))",
    "Output #0, mp3, to 'out.temp.mp3':",
]


def progress_block(out_time, speed="10.0x", terminator="continue", bitrate="320.0kbits/s"):
    """One ffmpeg ``-progress`` block, ending with the ``progress=`` key."""
    return [
        "bitrate=%s" % bitrate,
        "total_size=1024",
        "out_time_us=%d" % int(round(_hms_to_seconds(out_time) * 1_000_000)),
        "out_time=%s" % out_time,
        "speed=%s" % speed,
        "progress=%s" % terminator,
    ]


def _hms_to_seconds(stamp):
    hours, minutes, seconds = stamp.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


class RecordingStream:
    """An iterable stream that records what happened to it.

    ``exhausted`` proves the reader drained all the way to EOF (the property
    that keeps ffmpeg from blocking on a full pipe), ``closed`` proves the
    caller closed the pipe, and ``on_line`` fires as each line is handed over so
    a test can observe the artifact mid-stream without sleeping.
    """

    def __init__(self, lines, on_line=None):
        self._lines = list(lines)
        self._on_line = on_line
        self.exhausted = False
        self.closed = False
        self.delivered = []

    def __iter__(self):
        for index, line in enumerate(self._lines):
            self.delivered.append(line)
            if self._on_line is not None:
                self._on_line(index, line)
            yield line + "\n"
        self.exhausted = True

    def close(self):
        self.closed = True


class ExplodingStream:
    """Yields a few lines, then raises as a torn-down pipe would."""

    def __init__(self, lines, error):
        self._lines = list(lines)
        self._error = error
        self.closed = False

    def __iter__(self):
        for line in self._lines:
            yield line + "\n"
        raise self._error

    def close(self):
        self.closed = True


class ReaderTestBase(unittest.TestCase):
    def setUp(self):
        app.jobs.clear()
        app.processes.clear()
        self.addCleanup(app.jobs.clear)
        self.addCleanup(app.processes.clear)

    def make_artifact(self, artifact_id="a001", status="processing"):
        """An artifact shaped like plan_artifacts output, so the packaging
        fields exist before the reader ever touches them."""
        return {
            "id": artifact_id,
            "type": "audio",
            "format_id": None,
            "label": "MP3 audio",
            "status": status,
            "phase": "processing",
            "filename": None,
            "file": None,
            "downloaded_bytes": 0,
            "total_bytes": None,
            "speed": None,
            "eta": None,
            "percent": None,
            "error": None,
            "attempt": None,
            "max_attempts": None,
            "source": None,
            "processed_seconds": None,
            "duration_seconds": None,
            "processing_speed": None,
        }

    def make_parent(self, status="downloading", cancel=False, artifacts=None):
        job = {
            "id": JOB,
            "status": status,
            "phase": "processing",
            "title": "T",
            "cancel_requested": cancel,
            "cancel_event": threading.Event(),
            "artifacts": artifacts if artifacts is not None else [],
        }
        app.jobs[JOB] = job
        return job

    def read(self, lines, artifact=None, stream=None, diagnostics=None):
        """Run the reader synchronously over a transcript."""
        artifact = artifact if artifact is not None else self.make_artifact()
        stream = stream if stream is not None else RecordingStream(lines)
        diagnostics = diagnostics if diagnostics is not None else []
        app.read_ffmpeg_progress(JOB, artifact, stream, diagnostics)
        return artifact, stream, diagnostics


class LiveTranscriptTests(ReaderTestBase):
    """A well-formed transcript must land real numbers on the artifact.

    Against the old code there is no ``read_ffmpeg_progress`` at all and the
    artifact carries no ``processed_seconds``/``duration_seconds``/
    ``processing_speed`` keys, so every assertion here fails.
    """

    def setUp(self):
        super().setUp()
        self.make_parent()
        lines = list(HEADER)
        lines += progress_block("00:00:20.00", speed="10.0x")
        lines += progress_block("00:00:50.00", speed="12.5x")
        lines += progress_block("00:01:20.00", speed="8.0x", terminator="end")
        self.artifact, self.stream, self.diagnostics = self.read(lines)

    def test_duration_comes_from_the_input_dump(self):
        """No ffprobe is spawned: the length is read off ffmpeg's own header."""
        self.assertEqual(self.artifact["duration_seconds"], 100.0)

    def test_processed_seconds_track_the_last_block(self):
        self.assertEqual(self.artifact["processed_seconds"], 80.0)

    def test_percent_is_a_real_fraction_of_the_input(self):
        self.assertEqual(self.artifact["percent"], 80.0)

    def test_percent_stays_inside_the_expected_range(self):
        """The reader must never publish the 100 that only publication earns."""
        self.assertGreater(self.artifact["percent"], 0)
        self.assertLessEqual(self.artifact["percent"], 100)

    def test_speed_is_the_ffmpeg_multiplier_from_the_last_block(self):
        self.assertEqual(self.artifact["processing_speed"], 8.0)

    def test_eta_uses_remaining_media_time_over_speed(self):
        """(100 - 80) media seconds at 8x is 2.5 wall-clock seconds -> 2."""
        self.assertEqual(self.artifact["eta"], 2)

    def test_artifact_is_marked_processing(self):
        self.assertEqual(self.artifact["status"], "processing")
        self.assertEqual(self.artifact["phase"], "processing")

    def test_stream_is_drained_to_eof(self):
        self.assertTrue(self.stream.exhausted, "an unread pipe blocks ffmpeg")

    def test_non_progress_output_is_captured_as_diagnostics(self):
        """Human-readable lines are kept for the failure message, not applied."""
        self.assertIn("Stream mapping:", self.diagnostics)

    def test_progress_keys_never_leak_into_diagnostics(self):
        for entry in self.diagnostics:
            self.assertNotIn("out_time=", entry)
            self.assertNotIn("progress=", entry)


class MonotonicPercentTests(ReaderTestBase):
    """A published percent must never walk backwards.

    ffmpeg's reported position dips between blocks (it reports the position of
    the last flushed packet, not a monotonic clock). Publishing that dip renders
    as a progress bar sliding left, which reads as a bug. ``publish()`` feeds the
    previously published value back into ``compute_processing_percent`` as
    ``previous_percent`` precisely so a lower number can never be written.
    """

    def _run_and_snapshot(self, lines):
        """Record artifact["percent"] as each block terminator is consumed."""
        artifact = self.make_artifact()
        snapshots = []

        def on_line(_index, line):
            # Sampled as the terminator is handed over, i.e. the state the
            # PREVIOUS block published; the final value is appended after EOF.
            if line.startswith("progress="):
                snapshots.append(artifact["percent"])

        stream = RecordingStream(lines, on_line=on_line)
        app.read_ffmpeg_progress(JOB, artifact, stream, [])
        snapshots.append(artifact["percent"])
        return artifact, [s for s in snapshots if s is not None]

    def test_a_backwards_block_does_not_lower_the_published_percent(self):
        self.make_parent()
        lines = list(HEADER)
        lines += progress_block("00:00:60.00")
        lines += progress_block("00:00:30.00")  # ffmpeg dips backwards
        lines += progress_block("00:01:30.00", terminator="end")
        artifact, snapshots = self._run_and_snapshot(lines)

        self.assertEqual(artifact["percent"], 90.0)
        self.assertGreaterEqual(len(snapshots), 2)
        for earlier, later in zip(snapshots, snapshots[1:]):
            self.assertGreaterEqual(
                later, earlier,
                "published percent sequence must never decrease: %r" % (snapshots,),
            )

    def test_the_dip_is_hidden_by_the_guard_not_by_luck(self):
        """The trailing block really is lower; the guard is what hides it."""
        self.make_parent()
        lines = list(HEADER)
        lines += progress_block("00:00:60.00")
        lines += progress_block("00:00:30.00", terminator="end")
        artifact, _snapshots = self._run_and_snapshot(lines)

        self.assertEqual(artifact["percent"], 60.0,
                         "the 30s block must not lower the 60s percent")

    def test_a_trailing_dip_does_not_move_processed_seconds_backwards(self):
        """processed_seconds feeds the '0:42 / 1:23 processed' label, so it is
        held at the high-water mark for the same reason the percent is."""
        self.make_parent()
        lines = list(HEADER)
        lines += progress_block("00:00:60.00")
        lines += progress_block("00:00:30.00", terminator="end")
        artifact, _ = self._run_and_snapshot(lines)

        self.assertEqual(artifact["processed_seconds"], 60.0)


class HostileTranscriptTests(ReaderTestBase):
    """Junk ffmpeg really emits must not raise, and must not invent a percent."""

    def setUp(self):
        super().setUp()
        self.make_parent()

    def test_junk_values_leave_the_last_good_reading_intact(self):
        lines = list(HEADER)
        lines += progress_block("00:00:40.00", speed="5.0x")
        lines += [
            "",
            "   ",
            "out_time=N/A",
            "out_time_us=nan",
            "out_time_ms=N/A",
            "speed=N/A",
            "size=N/A time=N/A bitrate=N/A speed=N/A",
            "progress=continue",
            "out_time=-577014:32:22.000000",
            "speed=-3.0x",
            "progress=end",
        ]

        artifact, stream, _ = self.read(lines)

        self.assertEqual(artifact["processed_seconds"], 40.0)
        self.assertEqual(artifact["duration_seconds"], 100.0)
        self.assertEqual(artifact["percent"], 40.0)
        self.assertEqual(artifact["processing_speed"], 5.0)
        self.assertTrue(stream.exhausted)

    def test_a_negative_first_position_never_becomes_progress(self):
        """ffmpeg emits -577014:32:22 before the first frame is written.
        Clamping it to zero would publish 0.0 as if it were a measurement."""
        lines = list(HEADER)
        lines += ["out_time=-577014:32:22.000000", "speed=N/A", "progress=continue"]

        artifact, _stream, _ = self.read(lines)

        self.assertIsNone(artifact["processed_seconds"])
        self.assertIsNone(artifact["percent"],
                          "a negative position must stay indeterminate, not 0.0")

    def test_indented_metadata_is_never_parsed_as_progress(self):
        """An indented ``out_time`` belongs to the input dump, not -progress."""
        lines = list(HEADER)
        lines += ["    out_time=00:00:99.00", "  speed=99.0x"]
        lines += progress_block("00:00:10.00", speed="2.0x", terminator="end")

        artifact, _stream, _ = self.read(lines)

        self.assertEqual(artifact["processed_seconds"], 10.0)
        self.assertEqual(artifact["processing_speed"], 2.0)

    def test_a_transcript_with_no_progress_at_all_does_not_raise(self):
        artifact, stream, _ = self.read(list(HEADER))
        self.assertEqual(artifact["duration_seconds"], 100.0)
        self.assertIsNone(artifact["processed_seconds"])
        self.assertTrue(stream.exhausted)

    def test_a_plain_text_stream_is_read_the_same_way(self):
        """The reader must only assume "iterates lines" of its stream, since the
        real one is a text-mode pipe, not this module's instrumented fake."""
        artifact = self.make_artifact()
        lines = list(HEADER) + progress_block("00:00:55.00", speed="3.0x",
                                              terminator="end")
        stream = io.StringIO("\n".join(lines) + "\n")

        app.read_ffmpeg_progress(JOB, artifact, stream, [])

        self.assertEqual(artifact["duration_seconds"], 100.0)
        self.assertEqual(artifact["processed_seconds"], 55.0)
        self.assertEqual(artifact["percent"], 55.0)
        self.assertEqual(artifact["processing_speed"], 3.0)
        self.assertEqual(stream.read(), "", "the stream must be read to EOF")

    def test_crlf_line_endings_are_stripped(self):
        """ffmpeg on Windows terminates its progress lines with CRLF; a stray
        \\r would make "progress=continue\\r" miss the block terminator."""
        artifact = self.make_artifact()
        lines = list(HEADER) + progress_block("00:00:55.00", speed="3.0x",
                                              terminator="end")
        stream = io.StringIO("\r\n".join(lines) + "\r\n")

        app.read_ffmpeg_progress(JOB, artifact, stream, [])

        self.assertEqual(artifact["percent"], 55.0)
        self.assertEqual(artifact["processing_speed"], 3.0)


class IndeterminateDurationTests(ReaderTestBase):
    """No duration means no percent -- an unknown length is not a zero length."""

    def setUp(self):
        super().setUp()
        self.make_parent()

    def _run_without_duration(self, header):
        lines = list(header)
        lines += progress_block("00:00:20.00", speed="4.0x")
        lines += progress_block("00:00:45.00", speed="4.0x", terminator="end")
        return self.read(lines)

    def test_missing_duration_line_leaves_percent_none(self):
        header = [line for line in HEADER if "Duration:" not in line]
        artifact, _stream, _ = self._run_without_duration(header)

        self.assertIsNone(artifact["duration_seconds"])
        self.assertIsNone(artifact["percent"],
                          "an unknown duration must not produce a fabricated percent")

    def test_missing_duration_still_reports_processed_seconds(self):
        """The UI can still show '0:45 processed' with no determinate bar."""
        header = [line for line in HEADER if "Duration:" not in line]
        artifact, _stream, _ = self._run_without_duration(header)

        self.assertEqual(artifact["processed_seconds"], 45.0)
        self.assertEqual(artifact["status"], "processing")

    def test_duration_na_is_treated_as_unknown(self):
        header = [
            line.replace("Duration: 00:01:40.00", "Duration: N/A")
            for line in HEADER
        ]
        artifact, _stream, _ = self._run_without_duration(header)

        self.assertIsNone(artifact["duration_seconds"])
        self.assertIsNone(artifact["percent"])
        self.assertEqual(artifact["processed_seconds"], 45.0)

    def test_indeterminate_run_publishes_no_eta(self):
        header = [line for line in HEADER if "Duration:" not in line]
        artifact, _stream, _ = self._run_without_duration(header)
        self.assertIsNone(artifact["eta"])


FULL_TRANSCRIPT = (
    list(HEADER)
    + progress_block("00:00:30.00", speed="6.0x")
    + progress_block("00:00:70.00", speed="6.0x", terminator="end")
)


class StickyTerminalTests(ReaderTestBase):
    """A terminal job or artifact is never rewritten, but is still drained.

    Both halves matter and they pull in opposite directions. Applying a late
    block resurrects a cancelled artifact into ``processing`` with a fresh
    percent; NOT draining the pipe blocks ffmpeg on its next write, and a
    blocked ffmpeg cannot be reaped -- which is how a cancelled job leaves a
    live process behind. The reader consumes every line and discards the ones it
    must not apply.
    """

    def assert_untouched(self, artifact, expected_status):
        self.assertEqual(artifact["status"], expected_status)
        self.assertIsNone(artifact["percent"])
        self.assertIsNone(artifact["processed_seconds"])
        self.assertIsNone(artifact["duration_seconds"])
        self.assertIsNone(artifact["processing_speed"])
        self.assertIsNone(artifact["eta"])

    def test_cancelled_job_blocks_every_write(self):
        self.make_parent(status="cancelled")
        artifact = self.make_artifact()
        artifact, stream, _ = self.read(FULL_TRANSCRIPT, artifact=artifact)

        self.assert_untouched(artifact, "processing")
        self.assertTrue(stream.exhausted,
                        "the pipe must still be drained or ffmpeg blocks")

    def test_done_job_blocks_every_write(self):
        self.make_parent(status="done")
        artifact, stream, _ = self.read(FULL_TRANSCRIPT)

        self.assert_untouched(artifact, "processing")
        self.assertTrue(stream.exhausted)

    def test_every_terminal_parent_status_is_sticky(self):
        for status in sorted(app.TERMINAL_STATUSES):
            with self.subTest(status=status):
                app.jobs.clear()
                self.make_parent(status=status)
                artifact, stream, _ = self.read(FULL_TRANSCRIPT)
                self.assert_untouched(artifact, "processing")
                self.assertTrue(stream.exhausted)

    def test_every_terminal_artifact_status_is_sticky(self):
        """An artifact cancelled on its own (its parent still running) must not
        be revived either -- the guard checks the artifact separately."""
        for status in sorted(pipeline.ARTIFACT_TERMINAL):
            with self.subTest(status=status):
                app.jobs.clear()
                self.make_parent(status="downloading")
                artifact = self.make_artifact(status=status)
                artifact, stream, _ = self.read(FULL_TRANSCRIPT, artifact=artifact)
                self.assert_untouched(artifact, status)
                self.assertTrue(stream.exhausted)

    def test_a_job_that_goes_terminal_mid_stream_freezes_the_artifact(self):
        """The realistic shape: progress lands, DELETE arrives, later blocks are
        dropped. The frozen values must be the ones from before the flip."""
        self.make_parent(status="downloading")
        artifact = self.make_artifact()
        lines = list(HEADER) + progress_block("00:00:30.00", speed="6.0x")
        lines += progress_block("00:00:90.00", speed="6.0x", terminator="end")

        def on_line(_index, line):
            # Flip the parent terminal right after the first block published.
            if line == "bitrate=320.0kbits/s" and artifact["percent"] == 30.0:
                app.jobs[JOB]["status"] = "cancelled"

        stream = RecordingStream(lines, on_line=on_line)
        app.read_ffmpeg_progress(JOB, artifact, stream, [])

        self.assertEqual(artifact["percent"], 30.0,
                         "the post-cancel block must not advance the percent")
        self.assertEqual(artifact["processed_seconds"], 30.0)
        self.assertTrue(stream.exhausted)

    def test_a_vanished_job_is_not_an_error(self):
        """run_cleanup can drop the job out from under a still-running reader."""
        self.make_parent(status="downloading")
        artifact = self.make_artifact()
        lines = list(FULL_TRANSCRIPT)

        def on_line(index, _line):
            if index == 0:
                app.jobs.pop(JOB, None)

        stream = RecordingStream(lines, on_line=on_line)
        app.read_ffmpeg_progress(JOB, artifact, stream, [])

        self.assert_untouched(artifact, "processing")
        self.assertTrue(stream.exhausted)

    def test_a_job_that_vanishes_mid_stream_freezes_the_artifact(self):
        self.make_parent(status="downloading")
        artifact = self.make_artifact()
        lines = list(HEADER) + progress_block("00:00:30.00", speed="6.0x")
        lines += progress_block("00:00:90.00", speed="6.0x", terminator="end")

        def on_line(_index, line):
            if line == "bitrate=320.0kbits/s" and artifact["percent"] == 30.0:
                app.jobs.pop(JOB, None)

        stream = RecordingStream(lines, on_line=on_line)
        app.read_ffmpeg_progress(JOB, artifact, stream, [])

        self.assertEqual(artifact["percent"], 30.0)
        self.assertTrue(stream.exhausted)


class TornPipeTests(ReaderTestBase):
    """A pipe closed under the reader is an expected end, not a failure.

    ``terminate_and_reap`` closes the pipe from the waiting thread; iterating a
    closed text pipe raises ``ValueError: I/O operation on closed file`` and a
    dead one raises ``OSError``. The reader owns neither the outcome nor the
    error reporting, so it stops silently -- an escaping exception would print a
    thread traceback on every cancellation.
    """

    def setUp(self):
        super().setUp()
        self.make_parent()

    def _run_until(self, error):
        artifact = self.make_artifact()
        lines = list(HEADER) + progress_block("00:00:30.00", speed="6.0x")
        stream = ExplodingStream(lines, error)
        app.read_ffmpeg_progress(JOB, artifact, stream, [])
        return artifact

    def test_valueerror_from_a_closed_pipe_does_not_propagate(self):
        artifact = self._run_until(ValueError("I/O operation on closed file"))
        self.assertEqual(artifact["percent"], 30.0,
                         "work done before the tear-down must survive")

    def test_oserror_from_a_dead_pipe_does_not_propagate(self):
        artifact = self._run_until(OSError("broken pipe"))
        self.assertEqual(artifact["percent"], 30.0)

    def test_a_pipe_that_dies_before_any_output_leaves_no_percent(self):
        artifact = self.make_artifact()
        stream = ExplodingStream([], ValueError("closed"))
        app.read_ffmpeg_progress(JOB, artifact, stream, [])
        self.assertIsNone(artifact["percent"])

    def test_an_unrelated_exception_is_not_swallowed(self):
        """Guard against over-correcting into a bare ``except Exception``: a
        genuine bug in the reader must surface, not be silently discarded."""
        artifact = self.make_artifact()
        stream = ExplodingStream(list(HEADER), RuntimeError("bug in the reader"))
        with self.assertRaises(RuntimeError):
            app.read_ffmpeg_progress(JOB, artifact, stream, [])


# --- lifecycle: try_ffmpeg_reuse driving a real reader thread ----------


class ScriptedStream:
    """The fake process's stdout pipe, iterated by the real reader thread.

    ``on_line`` runs on the reader thread just before each line is handed over,
    which is a precise sequencing point: line *i* is only offered once line
    *i-1* has been fully processed and (for a block terminator) published.
    """

    def __init__(self, lines, on_line=None, on_eof=None):
        self._lines = list(lines)
        self._on_line = on_line
        self._on_eof = on_eof
        self.closed = False
        self.exhausted = False

    def __iter__(self):
        previous = None
        for index, line in enumerate(self._lines):
            if self._on_line is not None:
                self._on_line(index, line, previous)
            previous = line
            yield line + "\n"
        self.exhausted = True
        if self._on_eof is not None:
            self._on_eof()

    def close(self):
        self.closed = True


class ScriptedFFmpeg:
    """A fake ffmpeg that streams a transcript and exits only when told.

    ``wait()`` blocks on ``exit_gate``, which the transcript sets at EOF. That
    inverts the old ``communicate()`` shape on purpose: any progress the reader
    publishes necessarily happens while the caller is still inside ``wait()``,
    so a test can prove the value was visible BEFORE the process exited.
    """

    instances = []

    def __init__(self, cmd, transcript=None, on_line=None, returncode=0,
                 write_output=True, wait_error=None, **kwargs):
        self.cmd = list(cmd)
        self.kwargs = kwargs
        self.pid = 8100 + len(ScriptedFFmpeg.instances)
        self.temp_output = cmd[-1]
        self._alive = True
        self.returncode = None
        self._final_returncode = returncode
        self._write_output = write_output
        self._wait_error = wait_error
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0
        self.wait_timeouts = []
        self.wait_returned = False
        self.exit_gate = threading.Event()
        transcript = transcript if transcript is not None else []
        self.stdout = ScriptedStream(
            transcript, on_line=self._wrap(on_line), on_eof=self.exit_gate.set
        )
        ScriptedFFmpeg.instances.append(self)

    def _wrap(self, on_line):
        def hook(index, line, previous):
            if on_line is not None:
                on_line(self, index, line, previous)
        return hook

    def poll(self):
        return None if self._alive else self.returncode

    def communicate(self, timeout=None):  # pragma: no cover - must not be used
        raise AssertionError(
            "communicate() buffers until exit; the reader owns the pipe now"
        )

    def wait(self, timeout=None):
        self.wait_calls += 1
        self.wait_timeouts.append(timeout)
        if not self._alive:
            return self.returncode
        # Released by the transcript reaching EOF (or by a hook standing in for
        # a DELETE). Bounded so a regression fails the test instead of hanging.
        self.exit_gate.wait(WAIT_TIMEOUT)
        if self._wait_error is not None:
            raise self._wait_error(cmd=self.cmd, timeout=timeout)
        self._alive = False
        if self.returncode is None:
            self.returncode = self._final_returncode
        if self._write_output and self.returncode == 0:
            Path(self.temp_output).write_bytes(b"mp3-audio")
        self.wait_returned = True
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1
        self._alive = False
        self.exit_gate.set()
        if self.returncode is None:
            self.returncode = -15

    def kill(self):
        self.kill_calls += 1
        self._alive = False
        self.exit_gate.set()
        if self.returncode is None:
            self.returncode = -9

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.wait()
        return False


def reader_threads_alive():
    """Live threads still running ``read_ffmpeg_progress``.

    The reader is a daemon, so an orphan would not keep the interpreter alive
    and would go unnoticed without an explicit check -- it would just keep
    holding a pipe and writing to an artifact nobody owns any more.
    """
    return [
        thread for thread in threading.enumerate()
        if getattr(thread, "_target", None) is app.read_ffmpeg_progress
        and thread.is_alive()
    ]


class ReuseLifecycleTestBase(unittest.TestCase):
    def setUp(self):
        app.jobs.clear()
        app.processes.clear()
        ScriptedFFmpeg.instances = []
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._dl = patch.object(app, "DOWNLOAD_DIR", self.tmp.name)
        self._dl.start()
        self.addCleanup(self._dl.stop)
        self.addCleanup(app.processes.clear)
        self.addCleanup(app.jobs.clear)
        self.addCleanup(self.assert_no_orphan_reader)

    def assert_no_orphan_reader(self):
        for _ in range(50):
            if not reader_threads_alive():
                return
            # join(), not sleep(): waits on the thread itself and returns the
            # instant it finishes.
            reader_threads_alive()[0].join(timeout=0.1)
        self.fail("a read_ffmpeg_progress thread outlived try_ffmpeg_reuse")

    def make_parent(self, status="downloading", cancel=False):
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
            "phase": "queued", "format_id": None, "label": "MP3 audio",
            "percent": 100.0, "speed": 1234.0, "eta": 7,
            "processed_seconds": None, "duration_seconds": None,
            "processing_speed": None,
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

    def run_reuse(self, transcript=None, on_line=None, returncode=0,
                  write_output=True, wait_error=None, deadline_margin=60.0):
        """Drive try_ffmpeg_reuse against a scripted ffmpeg.

        The real ``build_ffmpeg_extract_command`` is used, so the argv the fake
        receives is the one production builds.
        """
        video = self.make_video()
        artifact = self.make_artifact()
        self.make_parent()
        deadline = time.monotonic() + deadline_margin
        # Exposed so an on_line hook can inspect the artifact mid-run.
        self.artifact_under_test = artifact

        def factory(cmd, **kwargs):
            return ScriptedFFmpeg(
                cmd, transcript=transcript, on_line=on_line,
                returncode=returncode, write_output=write_output,
                wait_error=wait_error, **kwargs
            )

        with patch.object(subprocess, "Popen", side_effect=factory), \
                patch.object(subprocess, "run", side_effect=_fake_run):
            outcome = self._invoke(JOB, artifact, [video], deadline, "T")
        return outcome, artifact

    def _invoke(self, *args):
        try:
            return ("returned", app.try_ffmpeg_reuse(*args))
        except BaseException as exc:  # noqa: BLE001 - the test inspects it
            return ("raised", exc)


class ProgressIsLiveTests(ReuseLifecycleTestBase):
    """The headline regression: progress must be visible BEFORE ffmpeg exits.

    The old implementation called ``communicate()``, which returns only after
    the child exits. Every byte of progress therefore arrived at once, after the
    fact, and the artifact went straight from ``percent = None`` to 100. This
    test snapshots ``artifact["percent"]`` from the reader thread as each block
    terminator is consumed, while the calling thread is still parked inside
    ``wait()`` -- against ``communicate()`` no snapshot can exist at all,
    because nothing reads the pipe until the process is already gone.
    """

    def setUp(self):
        super().setUp()
        self.snapshots = []
        self.transcript = (
            list(HEADER)
            + progress_block("00:00:25.00", speed="5.0x")
            + progress_block("00:00:60.00", speed="5.0x")
            + progress_block("00:01:35.00", speed="5.0x", terminator="end")
        )

    def _record(self, artifact):
        def on_line(process, _index, line, previous):
            # Sampled as the line AFTER a terminator is offered, i.e. once that
            # block's publish() has completed. wait() is still blocked here.
            if previous is not None and previous.startswith("progress="):
                self.snapshots.append({
                    "percent": artifact["percent"],
                    "wait_returned": process.wait_returned,
                    "status": artifact["status"],
                    "eta": artifact["eta"],
                })
            del line
        return on_line

    def test_progress_lands_while_the_process_is_still_running(self):
        video = self.make_video()
        artifact = self.make_artifact()
        self.make_parent()
        deadline = time.monotonic() + 60.0
        on_line = self._record(artifact)

        def factory(cmd, **kwargs):
            return ScriptedFFmpeg(cmd, transcript=self.transcript,
                                  on_line=on_line, **kwargs)

        with patch.object(subprocess, "Popen", side_effect=factory), \
                patch.object(subprocess, "run", side_effect=_fake_run):
            result = app.try_ffmpeg_reuse(JOB, artifact, [video], deadline, "T")

        self.assertTrue(result)
        self.assertTrue(self.snapshots,
                        "no progress was observed while ffmpeg ran")
        live = [
            snap for snap in self.snapshots
            if snap["percent"] is not None
            and 0 < snap["percent"] < 100
            and not snap["wait_returned"]
        ]
        self.assertTrue(
            live,
            "no intermediate percent was published before the process exited; "
            "observed %r" % (self.snapshots,),
        )

    def test_the_intermediate_percents_are_the_real_fractions(self):
        video = self.make_video()
        artifact = self.make_artifact()
        self.make_parent()
        deadline = time.monotonic() + 60.0
        on_line = self._record(artifact)

        def factory(cmd, **kwargs):
            return ScriptedFFmpeg(cmd, transcript=self.transcript,
                                  on_line=on_line, **kwargs)

        with patch.object(subprocess, "Popen", side_effect=factory), \
                patch.object(subprocess, "run", side_effect=_fake_run):
            app.try_ffmpeg_reuse(JOB, artifact, [video], deadline, "T")

        percents = [s["percent"] for s in self.snapshots if s["percent"] is not None]
        self.assertIn(25.0, percents, "25s of a 100s input is 25%%: %r" % (percents,))
        self.assertIn(60.0, percents)

    def test_live_snapshots_are_marked_processing_with_an_eta(self):
        video = self.make_video()
        artifact = self.make_artifact()
        self.make_parent()
        deadline = time.monotonic() + 60.0
        on_line = self._record(artifact)

        def factory(cmd, **kwargs):
            return ScriptedFFmpeg(cmd, transcript=self.transcript,
                                  on_line=on_line, **kwargs)

        with patch.object(subprocess, "Popen", side_effect=factory), \
                patch.object(subprocess, "run", side_effect=_fake_run):
            app.try_ffmpeg_reuse(JOB, artifact, [video], deadline, "T")

        mid = [s for s in self.snapshots if s["percent"] == 25.0]
        self.assertTrue(mid)
        self.assertEqual(mid[0]["status"], "processing")
        # (100 - 25) media seconds at 5x is 15 wall-clock seconds.
        self.assertEqual(mid[0]["eta"], 15)

    def test_communicate_is_never_called(self):
        """communicate() cannot coexist with a reader thread: both would drain
        the same pipe. The fake raises if production reverts to it."""
        outcome, _artifact = self.run_reuse(transcript=self.transcript)
        self.assertEqual(outcome, ("returned", True))

    def test_the_spawn_requests_a_merged_readable_pipe(self):
        """Progress goes to stdout, the Duration line to stderr. Two pipes with
        one reader deadlocks, so stderr must be redirected onto stdout."""
        self.run_reuse(transcript=self.transcript)
        proc = ScriptedFFmpeg.instances[-1]
        self.assertEqual(proc.kwargs.get("stdout"), subprocess.PIPE)
        self.assertEqual(proc.kwargs.get("stderr"), subprocess.STDOUT)
        self.assertTrue(proc.kwargs.get("text"),
                        "the reader iterates lines, so the pipe must be text")


class SuccessfulPublicationTests(ReuseLifecycleTestBase):
    """Publication overwrites the live fields; it does not merge with them.

    ffmpeg's last block routinely stops a few frames short of the duration, so
    a finished artifact that kept its live values would read
    "1:38 / 1:40 processed - 5x" forever next to a 100% bar. ``duration_seconds``
    is the exception: it describes the media, not the run.
    """

    def setUp(self):
        super().setUp()
        transcript = (
            list(HEADER)
            + progress_block("00:00:40.00", speed="5.0x")
            + progress_block("00:01:38.00", speed="5.0x", terminator="end")
        )
        # Captured mid-run so the "cleared on publication" assertions have a
        # real before/after: a field that was never set would pass them
        # trivially.
        self.live = {}
        self.outcome, self.artifact = self.run_reuse(
            transcript=transcript, on_line=self._capture,
        )

    def _capture(self, _process, _index, line, previous):
        del line
        if previous is not None and previous.startswith("progress="):
            artifact = self.artifact_under_test
            self.live = {
                "processed_seconds": artifact["processed_seconds"],
                "processing_speed": artifact["processing_speed"],
                "duration_seconds": artifact["duration_seconds"],
            }

    def test_reuse_reports_success(self):
        self.assertEqual(self.outcome, ("returned", True))

    def test_the_live_fields_really_were_populated_during_the_run(self):
        """Anchors the two 'cleared' assertions below: without this, they would
        pass against a build that never published anything at all."""
        self.assertEqual(self.live.get("processed_seconds"), 40.0)
        self.assertEqual(self.live.get("processing_speed"), 5.0)
        self.assertEqual(self.live.get("duration_seconds"), 100.0)

    def test_artifact_is_done_at_one_hundred_percent(self):
        self.assertEqual(self.artifact["status"], "done")
        self.assertEqual(self.artifact["phase"], "done")
        self.assertEqual(self.artifact["percent"], 100.0)

    def test_processed_seconds_are_cleared_on_publication(self):
        self.assertEqual(self.live.get("processed_seconds"), 40.0)
        self.assertIsNone(
            self.artifact["processed_seconds"],
            "a 98/100 reading must not outlive the run and contradict 100%",
        )

    def test_processing_speed_is_cleared_on_publication(self):
        self.assertEqual(self.live.get("processing_speed"), 5.0)
        self.assertIsNone(self.artifact["processing_speed"])

    def test_duration_survives_publication(self):
        """It came from the media, so it stays valid after the run ends."""
        self.assertEqual(self.artifact["duration_seconds"], 100.0)

    def test_byte_counters_and_file_are_published(self):
        self.assertEqual(self.artifact["file"], self.final_path)
        self.assertTrue(os.path.isfile(self.final_path))
        self.assertGreater(self.artifact["downloaded_bytes"], 0)

    def test_download_speed_and_eta_are_cleared(self):
        self.assertIsNone(self.artifact["speed"])
        self.assertIsNone(self.artifact["eta"])

    def test_temp_file_and_registry_are_clean(self):
        self.assertFalse(os.path.isfile(self.temp_path))
        self.assertNotIn(JOB, app.processes)

    def test_the_pipe_is_closed(self):
        """wait() does not close the pipes communicate() would have, so every
        packaged artifact would otherwise leak one file descriptor."""
        self.assertTrue(ScriptedFFmpeg.instances[-1].stdout.closed)


class PreSpawnResetTests(ReuseLifecycleTestBase):
    """The packaging fields start empty, even if the download step left values.

    ``speed`` and ``eta`` from the download describe bytes per second, not media
    seconds per wall-clock second. Carried into the packaging phase they would
    render as the packaging speed and ETA until the first ffmpeg block arrived.
    """

    def test_stale_download_fields_are_cleared_before_the_first_block(self):
        video = self.make_video()
        artifact = self.make_artifact()
        artifact.update({
            "percent": 100.0, "speed": 1234.0, "eta": 7,
            "processed_seconds": 999.0, "duration_seconds": 999.0,
            "processing_speed": 99.0,
        })
        self.make_parent()
        deadline = time.monotonic() + 60.0
        seen = {}

        def on_line(_process, index, _line, _previous):
            if index == 0:
                seen.update({k: artifact.get(k) for k in (
                    "percent", "speed", "eta", "processed_seconds",
                    "duration_seconds", "processing_speed", "status", "phase",
                )})

        def factory(cmd, **kwargs):
            return ScriptedFFmpeg(cmd, transcript=list(HEADER), on_line=on_line,
                                  **kwargs)

        with patch.object(subprocess, "Popen", side_effect=factory), \
                patch.object(subprocess, "run", side_effect=_fake_run):
            app.try_ffmpeg_reuse(JOB, artifact, [video], deadline, "T")

        self.assertTrue(seen, "the reader never saw the transcript")
        for field in ("percent", "speed", "eta", "processed_seconds",
                      "duration_seconds", "processing_speed"):
            self.assertIsNone(seen[field],
                              "%s must be cleared before ffmpeg spawns" % field)
        self.assertEqual(seen["status"], "processing")
        self.assertEqual(seen["phase"], "processing")


class CancelDuringPackagingTests(ReuseLifecycleTestBase):
    """A DELETE mid-packaging is fatal: raise, never ``return False``.

    ``return False`` is the caller's licence to fall back to a fresh yt-dlp
    audio download -- for a job the user just stopped. The old
    ``except Exception: return False`` wrapper did exactly that. Modelled the way
    it really happens: the cancel arrives while ffmpeg is mid-transcript and the
    DELETE handler terminates the registered process.
    """

    def setUp(self):
        super().setUp()
        video = self.make_video()
        self.artifact = self.make_artifact()
        parent = self.make_parent()
        deadline = time.monotonic() + 60.0
        transcript = (
            list(HEADER)
            + progress_block("00:00:30.00", speed="5.0x")
            + progress_block("00:00:70.00", speed="5.0x", terminator="end")
        )
        self.cancelled_at = threading.Event()

        def on_line(process, _index, line, previous):
            del line
            # Fires once the first block has published, i.e. genuinely mid-run.
            if previous is not None and previous == "progress=continue" \
                    and not self.cancelled_at.is_set():
                # A partial temp file exists on disk at cancel time.
                Path(process.temp_output).write_bytes(b"partial-mp3")
                with app.jobs_lock:
                    parent["cancel_requested"] = True
                    parent["status"] = "cancelling"
                # What DELETE does to the registered subprocess.
                app.terminate_process_tree(process)
                self.cancelled_at.set()

        def factory(cmd, **kwargs):
            return ScriptedFFmpeg(cmd, transcript=transcript, on_line=on_line,
                                  **kwargs)

        with patch.object(subprocess, "Popen", side_effect=factory), \
                patch.object(subprocess, "run", side_effect=_fake_run):
            self.outcome = self._invoke(JOB, self.artifact, [video], deadline, "T")
        self.process = ScriptedFFmpeg.instances[-1]

    def test_cancellation_raises_pipeline_cancelled(self):
        self.assertEqual(self.outcome[0], "raised",
                         "a cancel must not be reported as an ordinary failure")
        self.assertIsInstance(self.outcome[1], app.PipelineCancelled)

    def test_cancellation_never_returns_false(self):
        """False would let the caller start a fresh yt-dlp download."""
        self.assertNotEqual(self.outcome, ("returned", False))

    def test_the_cancel_really_landed_mid_run(self):
        self.assertTrue(self.cancelled_at.is_set(),
                        "the test must cancel while ffmpeg is still streaming")

    def test_the_process_is_terminated(self):
        self.assertTrue(self.process.kill_calls or self.process.terminate_calls,
                        "a cancelled packaging step must not leave ffmpeg alive")

    def test_the_registry_entry_is_gone(self):
        self.assertNotIn(JOB, app.processes)

    def test_the_partial_temp_file_is_removed(self):
        self.assertFalse(os.path.isfile(self.temp_path),
                         "a partial mp3 must never survive a cancel")

    def test_no_final_artifact_is_published(self):
        self.assertFalse(os.path.isfile(self.final_path))
        self.assertNotEqual(self.artifact["status"], "done")
        self.assertNotEqual(self.artifact["percent"], 100.0)

    def test_the_pipe_is_closed(self):
        self.assertTrue(self.process.stdout.closed)


class TimeoutDuringPackagingTests(ReuseLifecycleTestBase):
    """The shared budget expiring mid-packaging is fatal, not a fallback."""

    def setUp(self):
        super().setUp()
        transcript = (
            list(HEADER)
            + progress_block("00:00:30.00", speed="0.5x")
            + progress_block("00:00:35.00", speed="0.5x", terminator="end")
        )

        def on_line(process, _index, line, previous):
            del line, previous
            # A slow ffmpeg has already written a partial file when the budget
            # runs out.
            Path(process.temp_output).write_bytes(b"partial-mp3")

        self.outcome, self.artifact = self.run_reuse(
            transcript=transcript, on_line=on_line,
            wait_error=subprocess.TimeoutExpired,
        )
        self.process = ScriptedFFmpeg.instances[-1]

    def test_timeout_propagates_unchanged(self):
        self.assertEqual(self.outcome[0], "raised")
        self.assertIsInstance(self.outcome[1], subprocess.TimeoutExpired)

    def test_timeout_never_returns_false(self):
        self.assertNotEqual(self.outcome, ("returned", False))

    def test_the_hung_process_is_reaped(self):
        self.assertTrue(self.process.kill_calls or self.process.terminate_calls)

    def test_the_registry_is_clean(self):
        self.assertNotIn(JOB, app.processes)

    def test_the_partial_temp_file_is_removed(self):
        self.assertFalse(os.path.isfile(self.temp_path))

    def test_nothing_is_published(self):
        self.assertFalse(os.path.isfile(self.final_path))
        self.assertNotEqual(self.artifact["status"], "done")

    def test_the_pipe_is_closed(self):
        self.assertTrue(self.process.stdout.closed)

    def test_the_wait_deadline_is_bounded_by_the_parent_budget(self):
        """wait(timeout=remaining), not wait() -- an unbounded wait would park
        the worker on a hung ffmpeg forever, past the parent's budget."""
        first = self.process.wait_timeouts[0]
        self.assertIsNotNone(first, "the first wait must carry a timeout")
        self.assertGreater(first, 0)
        self.assertLessEqual(first, 60.0,
                             "the timeout must be the remaining budget")


class OrdinaryFailureStillFallsBackTests(ReuseLifecycleTestBase):
    """Guard against over-correcting: a plain ffmpeg failure is NOT fatal.

    Adding a reader thread must not change the three-outcome contract. A nonzero
    exit with no output file is the ONE case allowed to return False so the
    caller can retry the audio with yt-dlp.
    """

    def setUp(self):
        super().setUp()
        transcript = list(HEADER) + [
            "Error opening output file out.temp.mp3.",
            "Conversion failed!",
        ]
        self.outcome, self.artifact = self.run_reuse(
            transcript=transcript, returncode=1, write_output=False,
        )
        self.process = ScriptedFFmpeg.instances[-1]

    def test_a_nonzero_exit_returns_false(self):
        self.assertEqual(self.outcome, ("returned", False))

    def test_the_artifact_is_left_for_the_fallback_to_finish(self):
        self.assertEqual(self.artifact["status"], "processing")
        self.assertNotEqual(self.artifact["percent"], 100.0)

    def test_no_temp_or_final_file_survives(self):
        self.assertFalse(os.path.isfile(self.temp_path))
        self.assertFalse(os.path.isfile(self.final_path))

    def test_the_registry_is_clean(self):
        self.assertNotIn(JOB, app.processes)

    def test_the_pipe_is_closed_on_the_failure_path_too(self):
        self.assertTrue(self.process.stdout.closed)

    def test_a_missing_output_file_after_a_clean_exit_also_falls_back(self):
        """returncode 0 but no file is still an ordinary failure."""
        outcome, _artifact = self.run_reuse(
            transcript=list(HEADER), returncode=0, write_output=False,
        )
        self.assertEqual(outcome, ("returned", False))


class ReaderIsNeverOrphanedTests(ReuseLifecycleTestBase):
    """Every exit path must join the reader and close the pipe.

    The reader is a daemon thread, so an orphan would not block interpreter
    shutdown and would go unnoticed: it would simply keep holding a pipe and
    writing into an artifact the pipeline has already finished with. The
    ``finally`` block joins it and closes the pipe on success, on failure and
    while an exception is unwinding -- the base class asserts the join for every
    test in this module.
    """

    def _transcript(self):
        return (
            list(HEADER)
            + progress_block("00:00:30.00", speed="5.0x")
            + progress_block("00:00:80.00", speed="5.0x", terminator="end")
        )

    def test_no_reader_survives_a_successful_run(self):
        outcome, _artifact = self.run_reuse(transcript=self._transcript())
        self.assertEqual(outcome, ("returned", True))
        self.assertEqual(reader_threads_alive(), [])
        self.assertTrue(ScriptedFFmpeg.instances[-1].stdout.closed)

    def test_no_reader_survives_an_ordinary_failure(self):
        outcome, _artifact = self.run_reuse(
            transcript=self._transcript(), returncode=1, write_output=False,
        )
        self.assertEqual(outcome, ("returned", False))
        self.assertEqual(reader_threads_alive(), [])
        self.assertTrue(ScriptedFFmpeg.instances[-1].stdout.closed)

    def test_no_reader_survives_a_raised_timeout(self):
        outcome, _artifact = self.run_reuse(
            transcript=self._transcript(), wait_error=subprocess.TimeoutExpired,
        )
        self.assertEqual(outcome[0], "raised")
        self.assertEqual(reader_threads_alive(), [])
        self.assertTrue(ScriptedFFmpeg.instances[-1].stdout.closed)

    def test_the_reader_reached_eof_on_every_path(self):
        """Draining to EOF is what lets ffmpeg finish writing and be reaped."""
        for kwargs in (
            {},
            {"returncode": 1, "write_output": False},
            {"wait_error": subprocess.TimeoutExpired},
        ):
            with self.subTest(**kwargs):
                app.jobs.clear()
                app.processes.clear()
                ScriptedFFmpeg.instances = []
                self.run_reuse(transcript=self._transcript(), **kwargs)
                self.assertTrue(ScriptedFFmpeg.instances[-1].stdout.exhausted)


class LateProgressDoesNotResurrectTests(ReuseLifecycleTestBase):
    """A block published after the job went terminal must change nothing.

    ffmpeg keeps emitting for a short while after a DELETE, and the reader
    outlives nothing but the pipe. Without the sticky-terminal re-check the last
    straggling block would rewrite a ``cancelled`` artifact back to
    ``processing`` with a live percent -- a stopped download that keeps ticking.
    """

    def test_a_block_after_a_terminal_finish_is_dropped(self):
        artifact = self.make_artifact()
        parent = self.make_parent()
        parent["artifacts"] = [artifact]

        # The reader is handed a stream that only produces its last block once
        # the pipeline has finished and marked everything cancelled.
        released = threading.Event()
        published = threading.Event()

        class LateStream:
            exhausted = False
            closed = False
            released_in_time = False

            def __iter__(self):
                for line in HEADER + progress_block("00:00:20.00", speed="5.0x"):
                    yield line + "\n"
                published.set()
                # Parks the reader until the pipeline has finished, which is
                # what makes this block genuinely "late" without any sleeping.
                LateStream.released_in_time = released.wait(WAIT_TIMEOUT)
                for line in progress_block("00:01:30.00", speed="5.0x",
                                           terminator="end"):
                    yield line + "\n"
                LateStream.exhausted = True

            def close(self):
                LateStream.closed = True

        stream = LateStream()
        reader = threading.Thread(
            target=app.read_ffmpeg_progress,
            args=(JOB, artifact, stream, []),
            daemon=True,
        )
        reader.start()
        self.assertTrue(published.wait(WAIT_TIMEOUT), "no early block arrived")
        self.assertEqual(artifact["percent"], 20.0)

        # The pipeline finishes and the job is finalized as cancelled.
        with app.jobs_lock:
            parent.update({"status": "cancelled", "phase": "cancelled"})
            artifact.update({"status": "cancelled", "phase": "cancelled"})

        released.set()
        reader.join(timeout=WAIT_TIMEOUT)
        self.assertFalse(reader.is_alive(), "the reader must finish at EOF")
        self.assertTrue(LateStream.released_in_time,
                        "the late block must be produced after the finish, "
                        "not after a wait timeout")

        self.assertEqual(artifact["status"], "cancelled",
                         "a late block must not revive a cancelled artifact")
        self.assertEqual(artifact["phase"], "cancelled")
        self.assertEqual(artifact["percent"], 20.0,
                         "the frozen percent must not advance after cancel")
        self.assertTrue(stream.exhausted, "the pipe must still be drained")


if __name__ == "__main__":
    unittest.main()
