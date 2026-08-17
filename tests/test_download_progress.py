import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class DownloadProgressTests(unittest.TestCase):
    def test_progress_line_updates_job_metrics(self):
        job = {"status": "downloading"}
        progress = {
            "status": "downloading",
            "downloaded_bytes": 52428800,
            "total_bytes": 209715200,
            "speed": 10485760.0,
            "eta": 15,
        }
        line = app.PROGRESS_PREFIX + json.dumps(progress)

        parsed = app.update_job_progress(job, line)

        self.assertTrue(parsed)
        self.assertEqual(job["phase"], "downloading")
        self.assertEqual(job["downloaded_bytes"], 52428800)
        self.assertEqual(job["total_bytes"], 209715200)
        self.assertEqual(job["speed"], 10485760.0)
        self.assertEqual(job["eta"], 15)
        self.assertEqual(job["percent"], 25.0)

    def test_progress_line_ignores_non_object_json(self):
        job = {"status": "downloading"}

        parsed = app.update_job_progress(job, app.PROGRESS_PREFIX + "[]")

        self.assertFalse(parsed)
        self.assertEqual(job, {"status": "downloading"})

    def test_finished_stream_progress_remains_downloading_until_command_completes(self):
        job = {
            "status": "downloading",
            "phase": "downloading",
            "speed": 10485760,
            "eta": 1,
            "percent": 99.9,
        }
        line = app.PROGRESS_PREFIX + json.dumps({
            "status": "finished",
            "downloaded_bytes": 209715200,
            "total_bytes": 209715200,
        })

        self.assertTrue(app.update_job_progress(job, line))
        self.assertEqual(job["phase"], "downloading")
        self.assertEqual(job["percent"], 100.0)
        self.assertIsNone(job["speed"])
        self.assertIsNone(job["eta"])

    def test_status_endpoint_exposes_progress_metrics(self):
        job_id = "progress-status"
        app.jobs[job_id] = {
            "status": "downloading",
            "phase": "downloading",
            "downloaded_bytes": 52428800,
            "total_bytes": 209715200,
            "speed": 10485760.0,
            "eta": 15,
            "percent": 25.0,
        }
        try:
            response = app.app.test_client().get(f"/api/status/{job_id}")
            payload = response.get_json()
            self.assertEqual(response.status_code, 200)
            self.assertEqual(payload["phase"], "downloading")
            self.assertEqual(payload["downloaded_bytes"], 52428800)
            self.assertEqual(payload["total_bytes"], 209715200)
            self.assertEqual(payload["speed"], 10485760.0)
            self.assertEqual(payload["eta"], 15)
            self.assertEqual(payload["percent"], 25.0)
        finally:
            app.jobs.pop(job_id, None)

    def test_streaming_command_updates_progress_and_honors_timeout(self):
        job_id = "streaming-test"
        job = {"status": "downloading"}
        app.jobs[job_id] = job
        try:
            line = app.PROGRESS_PREFIX + json.dumps({
                "status": "downloading",
                "downloaded_bytes": 1024,
                "total_bytes": 4096,
                "speed": 512,
                "eta": 6,
            }) + "\n"

            class FakeProcess:
                def __init__(self):
                    self.stdout = iter([line, "warning line\n"])
                    self.returncode = 0
                    self.wait_timeout = None

                def wait(self, timeout):
                    self.wait_timeout = timeout
                    return self.returncode

                def kill(self):
                    self.returncode = -9

            process = FakeProcess()
            with patch.object(app.subprocess, "Popen", return_value=process) as popen:
                result = app.run_download_command(["yt-dlp", "url"], job_id, job, 3600)

            self.assertEqual(result.returncode, 0)
            self.assertIn("warning line", result.stderr)
            self.assertEqual(job["percent"], 25.0)
            self.assertEqual(process.wait_timeout, 3600)
            self.assertEqual(popen.call_args.kwargs["stderr"], app.subprocess.STDOUT)
        finally:
            app.jobs.pop(job_id, None)

    def test_new_job_starts_queued_with_empty_progress(self):
        class AcceptingScheduler:
            def submit(self, job_id, task):
                return True

        with patch.object(app.threading, "Thread") as thread, patch.object(
            app, "runtime_unavailable_response", return_value=None
        ), patch.object(app, "get_scheduler", return_value=AcceptingScheduler()):
            response = app.app.test_client().post("/api/download", json={
                "url": "https://example.com/video",
                "format": "video",
                "title": "Example",
            })

        self.assertEqual(response.status_code, 200)
        job_id = response.get_json()["job_id"]
        try:
            job = app.jobs[job_id]
            self.assertEqual(job["status"], "queued")
            self.assertEqual(job["phase"], "queued")
            self.assertEqual(job["downloaded_bytes"], 0)
            self.assertIsNone(job["total_bytes"])
            self.assertIsNone(job["speed"])
            self.assertIsNone(job["eta"])
            self.assertIsNone(job["percent"])
            self.assertIsNone(job["started_at"])
            thread.assert_not_called()
        finally:
            app.jobs.pop(job_id, None)

    def test_successful_download_finishes_progress_with_file_size(self):
        job_id = "progress-done"
        app.jobs[job_id] = {"status": "downloading", "phase": "processing", "title": "Example"}
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                output = Path(temp_dir) / f"{job_id}.mp4"

                def finish_download(command, parent_job_id, progress_target, timeout):
                    output.write_bytes(b"complete-file")
                    return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

                with patch.object(app, "DOWNLOAD_DIR", temp_dir), patch.object(
                    app,
                    "run_download_command",
                    side_effect=finish_download,
                ):
                    app.run_download(job_id, "https://example.com/video", "video", None)

                job = app.jobs[job_id]
                self.assertEqual(job["status"], "done")
                self.assertEqual(job["phase"], "done")
                self.assertEqual(job["percent"], 100.0)
                self.assertEqual(job["downloaded_bytes"], len(b"complete-file"))
                self.assertEqual(job["total_bytes"], len(b"complete-file"))
                self.assertIsNone(job["speed"])
                self.assertIsNone(job["eta"])
        finally:
            app.jobs.pop(job_id, None)

    def test_frontend_contains_progress_contract(self):
        template = (Path(app.__file__).parent / "templates" / "index.html").read_text(encoding="utf-8")
        for token in (
            "function fmtBytes(bytes)",
            "function fmtEta(seconds)",
            'class="progress-track"',
            'class="progress-fill',
            "data.downloaded_bytes",
            "data.total_bytes",
            "data.speed",
            "data.eta",
            "data.percent",
        ):
            with self.subTest(token=token):
                self.assertIn(token, template)

    def test_frontend_contains_folder_picker_contract(self):
        template = (Path(app.__file__).parent / "templates" / "index.html").read_text(encoding="utf-8")
        for token in (
            'id="folderBtn"',
            'id="folderName"',
            'id="browserDownloadsBtn"',
            "let downloadDirectoryHandle",
            "const supportsDirectoryPicker",
            "window.showDirectoryPicker",
            "queryPermission({ mode: 'readwrite' })",
            "function useBrowserDownloads()",
            "function selectedFolderHasPermission()",
            "function sendToBrowserDownload(card)",
            ".getFileHandle(",
            ".createWritable()",
            "response.body.pipeTo(writable)",
            "Browser default — Downloads or Ask where to save",
            "folderBtn.disabled = true",
        ):
            with self.subTest(token=token):
                self.assertIn(token, template)

    def test_frontend_has_linksift_brand_and_theme_contract(self):
        template = (Path(app.__file__).parent / "templates" / "index.html").read_text(encoding="utf-8")
        for token in (
            "LinkSift",
            "linksift.theme",
            'data-theme-choice="system"',
            'data-theme-choice="light"',
            'data-theme-choice="dark"',
            "html[data-theme=\"light\"]",
            "prefers-reduced-motion",
            "prefers-contrast: more",
            "const pollControllers = new Map()",
            "function cancelActivePolls()",
            "setTimeout(poll",
            "function uniqueFileHandle",
            "if (image) image.src = card.thumbnail",
            "data-format-index",
            "Download queue",
            "queue-dock",
            "/api/health",
            'id="readyState"',
            "width: min(1240px",
            "@media (hover: hover) and (pointer: fine)",
            "--grid-line",
            "context-rail",
            "data-rail-panel",
            "How it works",
            "supported-sites-list",
            "View more supported sites",
            "data-activity",
            "scan-edge",
            "@media (max-width: 960px)",
            "minmax(0, 1fr)",
            "overflow-wrap: anywhere",
            "env(safe-area-inset-bottom",
            "queue-placeholder",
            "family=Inter",
        ):
            with self.subTest(token=token):
                self.assertIn(token, template)
        self.assertNotIn('footer">Powered by yt-dlp · 1,000+ supported sites', template)
        self.assertNotIn('src="${card.thumbnail}"', template)
        self.assertNotIn("onclick=\"pickFormat", template)

    def test_failed_download_cleans_partial_and_stops_metrics(self):
        job_id = "progress-error"
        app.jobs[job_id] = {
            "status": "downloading",
            "phase": "downloading",
            "title": "Example",
            "speed": 1024,
            "eta": 30,
        }
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                partial = Path(temp_dir) / f"{job_id}.f11.mp4.part"
                partial.write_bytes(b"partial")
                failed = subprocess.CompletedProcess(
                    ["yt-dlp"],
                    1,
                    stdout="",
                    stderr="download failed",
                )
                with patch.object(app, "DOWNLOAD_DIR", temp_dir), patch.object(
                    app,
                    "run_download_command",
                    return_value=failed,
                ):
                    app.run_download(job_id, "https://example.com/video", "video", None)

                job = app.jobs[job_id]
                self.assertEqual(job["status"], "error")
                self.assertEqual(job["phase"], "error")
                self.assertEqual(job["error"], "download failed")
                self.assertIsNone(job["speed"])
                self.assertIsNone(job["eta"])
                self.assertFalse(partial.exists())
        finally:
            app.jobs.pop(job_id, None)


if __name__ == "__main__":
    unittest.main()
