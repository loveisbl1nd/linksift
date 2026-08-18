"""Contract tests for the conditional YouTube HTTP-403 player-client fallback.

YouTube's default player client hands out media URLs its CDN then rejects with
403 for the whole stream, while the embedded client's URLs download fine. The
fallback must therefore be *conditional*: default client first (some videos
disable embedding), embedded client only after a genuine 403 on a URL whose
hostname really is YouTube, and only within the existing retry budget.
"""

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import app
import output_pipeline as op


FORCED_CLIENT_ARG = "youtube:player_client=web_embedded"
PROVIDER_ARG = "youtubepot-bgutilhttp:base_url=http://pot:4416"
YT_URL = "https://www.youtube.com/watch?v=0aQ1HDPcnb0"
ERR_403 = "ERROR: unable to download video data: HTTP Error 403: Forbidden"


def forced_client_values(cmd):
    """Every player client pinned via --extractor-args youtube:player_client=."""
    prefix = "youtube:player_client="
    return [
        cmd[i + 1][len(prefix):]
        for i, token in enumerate(cmd)
        if token == "--extractor-args"
        and i + 1 < len(cmd)
        and cmd[i + 1].startswith(prefix)
    ]


class YoutubeUrlIdentificationTests(unittest.TestCase):
    """Hostname parsing, not substring matching."""

    def test_real_youtube_hosts_are_recognised(self):
        for url in (
            "https://www.youtube.com/watch?v=abc",
            "https://youtube.com/watch?v=abc",
            "https://m.youtube.com/watch?v=abc",
            "https://music.youtube.com/watch?v=abc",
            "http://YouTube.COM/watch?v=abc",
            "https://youtu.be/abc",
            "https://www.youtu.be/abc",
        ):
            with self.subTest(url=url):
                self.assertTrue(app.is_youtube_url(url))

    def test_lookalike_and_malformed_urls_are_rejected(self):
        # A substring check would wrongly accept the first three of these, which
        # would let an attacker-controlled host steer our extractor arguments.
        for url in (
            "https://youtube.com.evil.example/watch?v=abc",
            "https://notyoutube.com/watch?v=abc",
            "https://evil.example/?ref=youtube.com",
            "https://vimeo.com/12345",
            "not a url at all",
            "",
            None,
            "http://[::1",
        ):
            with self.subTest(url=url):
                self.assertFalse(app.is_youtube_url(url))


class Http403DetectionTests(unittest.TestCase):
    """Only a real 403 may switch the client."""

    def test_403_messages_are_detected(self):
        for text in (
            ERR_403,
            "ERROR: HTTP Error 403: Forbidden",
            "error: http error 403: forbidden",
        ):
            with self.subTest(text=text):
                self.assertTrue(app.is_http_403_error(text))

    def test_other_errors_are_not_403(self):
        for text in (
            "ERROR: HTTP Error 429: Too Many Requests",
            "ERROR: HTTP Error 503: Service Unavailable",
            "ERROR: Connection reset by peer",
            "ERROR: Requested format is not available",
            "ERROR: Video unavailable",
            "",
            None,
        ):
            with self.subTest(text=text):
                self.assertFalse(app.is_http_403_error(text))


class BuildCommandFallbackTests(unittest.TestCase):
    """The shared builder is what both download paths use."""

    def build(self, artifact, **kwargs):
        return op.build_ytdlp_command(
            YT_URL, "job123456", artifact, "/downloads",
            "__P__", "__PP__", 4, ["--extractor-args", PROVIDER_ARG], **kwargs
        )

    def test_default_build_does_not_force_a_client(self):
        cmd = self.build({"id": "a001", "type": "video", "format_id": "137"})
        self.assertEqual(forced_client_values(cmd), [])

    def test_fallback_build_forces_embedded_client(self):
        cmd = self.build({"id": "a001", "type": "video", "format_id": "137"},
                         youtube_client="web_embedded")
        self.assertEqual(forced_client_values(cmd), ["web_embedded"])

    def test_fallback_keeps_po_token_provider_args(self):
        # The two --extractor-args flags are namespaced to different extractors,
        # so adding the youtube one must not displace the provider one.
        cmd = self.build({"id": "a001", "type": "video", "format_id": "137"},
                         youtube_client="web_embedded")
        self.assertIn(PROVIDER_ARG, cmd)
        self.assertEqual(cmd.count("--extractor-args"), 2)

    def test_fallback_preserves_video_format_and_output_contract(self):
        cmd = self.build({"id": "a001", "type": "video", "format_id": "137"},
                         youtube_client="web_embedded")
        self.assertEqual(cmd[cmd.index("-f") + 1], "137+bestaudio/best")
        self.assertIn("job123456.a001", cmd[cmd.index("-o") + 1])
        self.assertEqual(cmd[cmd.index("--merge-output-format") + 1], "mp4")
        self.assertEqual(cmd[-2:], ["--", YT_URL])
        for flag in ("--no-playlist", "--continue", "--part", "--retries",
                     "--fragment-retries", "--extractor-retries"):
            self.assertIn(flag, cmd)
        self.assertEqual(cmd.count("--progress-template"), 2)

    def test_fallback_preserves_audio_flags(self):
        cmd = self.build({"id": "a002", "type": "audio", "format_id": None},
                         youtube_client="web_embedded")
        self.assertIn("-x", cmd)
        self.assertEqual(cmd[cmd.index("--audio-format") + 1], "mp3")
        self.assertEqual(forced_client_values(cmd), ["web_embedded"])

    def test_each_call_returns_a_fresh_list(self):
        # A shared/mutated argv would make the "fresh extraction" retry a lie.
        artifact = {"id": "a001", "type": "video", "format_id": "137"}
        first = self.build(artifact)
        second = self.build(artifact, youtube_client="web_embedded")
        self.assertIsNot(first, second)
        self.assertEqual(forced_client_values(first), [])
        self.assertEqual(forced_client_values(second), ["web_embedded"])

    def test_output_template_override_keeps_legacy_filename_contract(self):
        cmd = self.build({"id": "job123456", "type": "video", "format_id": None},
                         output_template="/downloads/job123456.%(ext)s")
        self.assertEqual(cmd[cmd.index("-o") + 1], "/downloads/job123456.%(ext)s")


class FallbackDecisionTests(unittest.TestCase):
    """Guards re-checked after the subprocess returned."""

    def setUp(self):
        app.jobs.clear()
        self.job_id = "decide0001"
        app.jobs[self.job_id] = {"status": "downloading", "title": ""}
        self.deadline = time.monotonic() + 300

    def tearDown(self):
        app.jobs.clear()

    def decide(self, url=YT_URL, stderr=ERR_403, already_used=False, deadline=None):
        return app.should_retry_with_youtube_fallback(
            url, stderr, self.job_id,
            self.deadline if deadline is None else deadline,
            already_used,
        )

    def test_youtube_403_triggers_fallback(self):
        self.assertTrue(self.decide())

    def test_non_youtube_403_does_not_trigger(self):
        self.assertFalse(self.decide(url="https://vimeo.com/12345"))

    def test_lookalike_host_403_does_not_trigger(self):
        self.assertFalse(self.decide(url="https://youtube.com.evil.example/watch?v=x"))

    def test_youtube_non_403_does_not_trigger(self):
        self.assertFalse(self.decide(stderr="ERROR: HTTP Error 429: Too Many Requests"))
        self.assertFalse(self.decide(stderr="ERROR: Requested format is not available"))

    def test_fallback_is_used_at_most_once(self):
        self.assertFalse(self.decide(already_used=True))

    def test_cancelled_job_does_not_trigger(self):
        app.jobs[self.job_id]["cancel_requested"] = True
        self.assertFalse(self.decide())

    def test_terminal_job_does_not_trigger(self):
        for status in sorted(app.TERMINAL_STATUSES):
            with self.subTest(status=status):
                app.jobs[self.job_id]["status"] = status
                self.assertFalse(self.decide())

    def test_missing_job_does_not_trigger(self):
        app.jobs.clear()
        self.assertFalse(self.decide())

    def test_expired_deadline_does_not_trigger(self):
        self.assertFalse(self.decide(deadline=time.monotonic() - 1))


class ArtifactPipelineFallbackTests(unittest.TestCase):
    """The live multi-output path."""

    def setUp(self):
        app.jobs.clear()

    def tearDown(self):
        app.jobs.clear()

    def run_artifact(self, side_effect, url=YT_URL, retries="2", artifact=None,
                     job_extra=None):
        job_id = "artifact01"
        calls = []

        def recorder(cmd, parent_job_id, target, timeout):
            calls.append(list(cmd))
            return side_effect(cmd, len(calls))

        artifact = artifact or {
            "id": "a001", "type": "video", "format_id": "137", "label": "1080p",
            "status": "downloading", "phase": "starting",
        }
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(app, "DOWNLOAD_DIR", temp_dir), \
                patch.dict(os.environ, {"LINKSIFT_JOB_RETRIES": retries,
                                        "LINKSIFT_RETRY_BASE_DELAY": "0"}), \
                patch.object(app, "ytdlp_runtime_args",
                             return_value=["--extractor-args", PROVIDER_ARG]), \
                patch.object(app, "run_download_command", side_effect=recorder):
            job = {"status": "downloading", "title": ""}
            job.update(job_extra or {})
            app.jobs[job_id] = job
            self.temp_dir = temp_dir
            ok = app.execute_single_artifact(
                job_id, url, artifact, time.monotonic() + 300, "title"
            )
        return calls, ok, artifact

    def test_youtube_403_switches_client_on_the_next_attempt_only(self):
        def responses(cmd, n):
            if n == 1:
                return subprocess.CompletedProcess(cmd, 1, "", ERR_403)
            Path(self.temp_dir, "artifact01.a001.mp4").write_bytes(b"final")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        calls, ok, _ = self.run_artifact(responses)
        self.assertTrue(ok)
        self.assertEqual(len(calls), 2)
        self.assertEqual(forced_client_values(calls[0]), [])
        self.assertEqual(forced_client_values(calls[1]), ["web_embedded"])

    def test_fallback_command_keeps_provider_args_and_format(self):
        def responses(cmd, n):
            if n == 1:
                return subprocess.CompletedProcess(cmd, 1, "", ERR_403)
            Path(self.temp_dir, "artifact01.a001.mp4").write_bytes(b"final")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        calls, _, _ = self.run_artifact(responses)
        fallback = calls[1]
        self.assertIn(PROVIDER_ARG, fallback)
        self.assertEqual(fallback[fallback.index("-f") + 1], "137+bestaudio/best")
        self.assertEqual(fallback[-2:], ["--", YT_URL])

    def test_audio_artifact_fallback_keeps_audio_flags(self):
        def responses(cmd, n):
            if n == 1:
                return subprocess.CompletedProcess(cmd, 1, "", ERR_403)
            Path(self.temp_dir, "artifact01.a002.mp3").write_bytes(b"final")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        calls, ok, _ = self.run_artifact(responses, artifact={
            "id": "a002", "type": "audio", "format_id": None, "label": "MP3",
            "status": "downloading", "phase": "starting",
        })
        self.assertTrue(ok)
        self.assertEqual(forced_client_values(calls[1]), ["web_embedded"])
        self.assertIn("-x", calls[1])

    def test_non_youtube_403_never_forces_a_client(self):
        def always_403(cmd, n):
            return subprocess.CompletedProcess(cmd, 1, "", ERR_403)

        calls, ok, _ = self.run_artifact(always_403, url="https://vimeo.com/12345")
        self.assertFalse(ok)
        for cmd in calls:
            self.assertEqual(forced_client_values(cmd), [])

    def test_youtube_non_403_never_forces_a_client(self):
        def always_429(cmd, n):
            return subprocess.CompletedProcess(cmd, 1, "", "ERROR: HTTP Error 429: Too Many Requests")

        calls, ok, _ = self.run_artifact(always_429)
        self.assertFalse(ok)
        self.assertGreater(len(calls), 1)
        for cmd in calls:
            self.assertEqual(forced_client_values(cmd), [])

    def test_fake_youtube_hostname_never_forces_a_client(self):
        def always_403(cmd, n):
            return subprocess.CompletedProcess(cmd, 1, "", ERR_403)

        calls, _, _ = self.run_artifact(
            always_403, url="https://youtube.com.evil.example/watch?v=x"
        )
        for cmd in calls:
            self.assertEqual(forced_client_values(cmd), [])

    def test_retry_count_never_exceeds_max_attempts(self):
        def always_403(cmd, n):
            return subprocess.CompletedProcess(cmd, 1, "", ERR_403)

        # 2 retries configured => 3 attempts total, fallback included.
        calls, ok, artifact = self.run_artifact(always_403, retries="2")
        self.assertFalse(ok)
        self.assertEqual(len(calls), 3)
        self.assertEqual(artifact["status"], "error")
        # Exactly one switch: the fallback is not re-applied every attempt.
        self.assertEqual(forced_client_values(calls[0]), [])
        self.assertEqual(forced_client_values(calls[1]), ["web_embedded"])
        self.assertEqual(forced_client_values(calls[2]), ["web_embedded"])

    def test_no_retry_budget_means_no_fallback_spawn(self):
        def always_403(cmd, n):
            return subprocess.CompletedProcess(cmd, 1, "", ERR_403)

        calls, ok, _ = self.run_artifact(always_403, retries="0")
        self.assertFalse(ok)
        self.assertEqual(len(calls), 1)
        self.assertEqual(forced_client_values(calls[0]), [])

    def test_cancellation_before_fallback_spawns_no_new_process(self):
        def cancel_then_403(cmd, n):
            app.jobs["artifact01"]["cancel_requested"] = True
            return subprocess.CompletedProcess(cmd, 1, "", ERR_403)

        calls, ok, _ = self.run_artifact(cancel_then_403)
        self.assertFalse(ok)
        self.assertEqual(len(calls), 1)

    def test_expired_deadline_before_fallback_spawns_no_new_process(self):
        job_id = "artifact01"
        calls = []

        def recorder(cmd, parent_job_id, target, timeout):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 1, "", ERR_403)

        artifact = {"id": "a001", "type": "video", "format_id": "137",
                    "label": "1080p", "status": "downloading", "phase": "starting"}
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(app, "DOWNLOAD_DIR", temp_dir), \
                patch.dict(os.environ, {"LINKSIFT_JOB_RETRIES": "2",
                                        "LINKSIFT_RETRY_BASE_DELAY": "0"}), \
                patch.object(app, "run_download_command", side_effect=recorder):
            app.jobs[job_id] = {"status": "downloading", "title": ""}
            # Deadline already in the past when the fallback decision is made.
            with self.assertRaises(subprocess.TimeoutExpired):
                app.execute_single_artifact(
                    job_id, YT_URL, artifact, time.monotonic() - 1, "title"
                )
        self.assertEqual(calls, [])


class LegacyPathFallbackTests(unittest.TestCase):
    """The legacy single-output path shares the same helper."""

    def setUp(self):
        app.jobs.clear()

    def tearDown(self):
        app.jobs.clear()

    def run_legacy(self, side_effect, url=YT_URL, retries="2", fmt="video", format_id="137"):
        job_id = "legacy0001"
        calls = []

        def recorder(cmd, parent_job_id, job, timeout):
            calls.append(list(cmd))
            return side_effect(cmd, len(calls))

        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(app, "DOWNLOAD_DIR", temp_dir), \
                patch.dict(os.environ, {"LINKSIFT_JOB_RETRIES": retries,
                                        "LINKSIFT_RETRY_BASE_DELAY": "0"}), \
                patch.object(app, "ytdlp_runtime_args",
                             return_value=["--extractor-args", PROVIDER_ARG]), \
                patch.object(app, "run_download_command", side_effect=recorder):
            app.jobs[job_id] = {"status": "downloading", "title": ""}
            self.temp_dir = temp_dir
            app.run_download(job_id, url, fmt, format_id)
        return calls, app.jobs[job_id]

    def test_legacy_youtube_403_switches_client_and_keeps_filename_contract(self):
        def responses(cmd, n):
            if n == 1:
                return subprocess.CompletedProcess(cmd, 1, "", ERR_403)
            Path(self.temp_dir, "legacy0001.mp4").write_bytes(b"final")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        calls, job = self.run_legacy(responses)
        self.assertEqual(job["status"], "done")
        self.assertEqual(len(calls), 2)
        self.assertEqual(forced_client_values(calls[0]), [])
        self.assertEqual(forced_client_values(calls[1]), ["web_embedded"])
        # Legacy owns <job_id>.%(ext)s, NOT the artifact-scoped template.
        self.assertTrue(calls[1][calls[1].index("-o") + 1].endswith("legacy0001.%(ext)s"))
        self.assertIn(PROVIDER_ARG, calls[1])
        self.assertEqual(calls[1][calls[1].index("-f") + 1], "137+bestaudio/best")

    def test_legacy_non_youtube_403_never_forces_a_client(self):
        def always_403(cmd, n):
            return subprocess.CompletedProcess(cmd, 1, "", ERR_403)

        calls, job = self.run_legacy(always_403, url="https://example.test/v")
        self.assertEqual(job["status"], "error")
        for cmd in calls:
            self.assertEqual(forced_client_values(cmd), [])

    def test_legacy_retry_count_never_exceeds_max_attempts(self):
        def always_403(cmd, n):
            return subprocess.CompletedProcess(cmd, 1, "", ERR_403)

        calls, job = self.run_legacy(always_403, retries="1")
        self.assertEqual(job["status"], "error")
        self.assertEqual(len(calls), 2)

    def test_legacy_audio_fallback_keeps_audio_flags(self):
        def responses(cmd, n):
            if n == 1:
                return subprocess.CompletedProcess(cmd, 1, "", ERR_403)
            Path(self.temp_dir, "legacy0001.mp3").write_bytes(b"final")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        calls, job = self.run_legacy(responses, fmt="audio", format_id=None)
        self.assertEqual(job["status"], "done")
        self.assertIn("-x", calls[1])
        self.assertEqual(calls[1][calls[1].index("--audio-format") + 1], "mp3")
        self.assertEqual(forced_client_values(calls[1]), ["web_embedded"])


class FallbackLoggingTests(unittest.TestCase):
    """The retry log must not leak the URL or the provider token."""

    def setUp(self):
        app.jobs.clear()

    def tearDown(self):
        app.jobs.clear()

    def test_log_message_omits_url_and_tokens(self):
        job_id = "logsafe001"
        secret_url = "https://www.youtube.com/watch?v=0aQ1HDPcnb0&token=SECRETVALUE"

        def responses(cmd, parent_job_id, target, timeout):
            if not getattr(self, "_seen", False):
                self._seen = True
                return subprocess.CompletedProcess(cmd, 1, "", ERR_403)
            Path(self.temp_dir, "logsafe001.a001.mp4").write_bytes(b"final")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        artifact = {"id": "a001", "type": "video", "format_id": "137",
                    "label": "1080p", "status": "downloading", "phase": "starting"}
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(app, "DOWNLOAD_DIR", temp_dir), \
                patch.dict(os.environ, {"LINKSIFT_JOB_RETRIES": "2",
                                        "LINKSIFT_RETRY_BASE_DELAY": "0"}), \
                patch.object(app, "ytdlp_runtime_args",
                             return_value=["--extractor-args", PROVIDER_ARG]), \
                patch.object(app, "run_download_command", side_effect=responses):
            app.jobs[job_id] = {"status": "downloading", "title": ""}
            self.temp_dir = temp_dir
            with self.assertLogs(app.app.logger, level="WARNING") as captured:
                app.execute_single_artifact(
                    job_id, secret_url, artifact, time.monotonic() + 300, "title"
                )

        fallback_lines = [m for m in captured.output if "embedded client" in m]
        self.assertTrue(fallback_lines, "fallback must log a safe message")
        joined = "\n".join(fallback_lines)
        self.assertIn("HTTP 403", joined)
        self.assertNotIn("SECRETVALUE", joined)
        self.assertNotIn(secret_url, joined)
        self.assertNotIn("0aQ1HDPcnb0", joined)
        self.assertNotIn("base_url", joined)


if __name__ == "__main__":
    unittest.main()
