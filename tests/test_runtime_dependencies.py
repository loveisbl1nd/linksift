import re
import unittest
from pathlib import Path
from unittest.mock import patch

import app


RELEASE_BRAND_FILES = (
    "README.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CHANGELOG.md",
    "PROVENANCE.md",
    "THIRD_PARTY_NOTICES.md",
    "RELEASING.md",
    "ROADMAP.md",
    "Dockerfile",
    "docker-compose.yml",
    "compose.ghcr.yml",
    "docker-entrypoint.sh",
    "linksift.sh",
    "app.py",
    "requirements.txt",
    "templates/index.html",
    "static/favicon.svg",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
    ".github/ISSUE_TEMPLATE/config.yml",
    ".github/pull_request_template.md",
    ".github/workflows/ci.yml",
    ".github/workflows/release.yml",
)

FORBIDDEN_ARTIFACT_MARKERS = ("user-attachments",)


class RuntimeDependencyTests(unittest.TestCase):
    def setUp(self):
        self.app_context = app.app.app_context()
        self.app_context.push()
        self.client = app.app.test_client()

    def tearDown(self):
        self.app_context.pop()

    def test_reports_only_requested_missing_tools(self):
        with patch.object(app.shutil, "which", side_effect=lambda name: None if name == "yt-dlp" else "/usr/bin/ffmpeg"):
            self.assertEqual(app.get_missing_runtime_tools(("yt-dlp",)), ["yt-dlp"])
            self.assertEqual(app.get_missing_runtime_tools(("ffmpeg",)), [])

    def test_unavailable_response_is_safe_and_actionable(self):
        with app.app.test_request_context():
            with patch.object(app, "get_missing_runtime_tools", return_value=["yt-dlp"]):
                response, status = app.runtime_unavailable_response(("yt-dlp",))
        self.assertEqual(status, 503)
        self.assertEqual(response.get_json(), {
            "error": "Server downloader is unavailable. Start LinkSift with Docker Compose or install: yt-dlp.",
            "missing_tools": ["yt-dlp"],
        })
        self.assertNotIn("WinError", response.get_json()["error"])

    def test_health_reports_ok_when_all_tools_present(self):
        with patch.object(app.shutil, "which", return_value="/usr/bin/tool"):
            response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["missing_tools"], [])
        self.assertIn("capabilities", payload)

    def test_health_reports_degraded_with_missing_tools(self):
        with patch.object(app.shutil, "which", side_effect=lambda name: None if name == "yt-dlp" else "/usr/bin/ffmpeg"):
            response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["missing_tools"], ["yt-dlp"])

    def test_info_and_playlist_return_503_when_ytdlp_is_missing(self):
        with patch.object(app, "runtime_unavailable_response") as unavailable:
            unavailable.return_value = (app.jsonify({"error": "Server downloader is unavailable.", "missing_tools": ["yt-dlp"]}), 503)
            for endpoint in ("/api/info", "/api/playlist"):
                response = self.client.post(endpoint, json={"url": "https://example.test/video"})
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.get_json()["missing_tools"], ["yt-dlp"])
        self.assertEqual(unavailable.call_count, 2)
        unavailable.assert_any_call(("yt-dlp",))

    def test_download_returns_503_when_ffmpeg_is_missing(self):
        with patch.object(app, "runtime_unavailable_response") as unavailable:
            unavailable.return_value = (app.jsonify({"error": "Server downloader is unavailable.", "missing_tools": ["ffmpeg"]}), 503)
            response = self.client.post("/api/download", json={"url": "https://example.test/video"})
        self.assertEqual(response.status_code, 503)
        unavailable.assert_called_once_with(("yt-dlp", "ffmpeg"))

    def test_info_converts_subprocess_file_not_found_to_safe_503(self):
        with patch.object(app, "runtime_unavailable_response", return_value=None), patch.object(app.subprocess, "run", side_effect=FileNotFoundError("[WinError 2] ignored")):
            response = self.client.post("/api/info", json={"url": "https://example.test/video"})
        self.assertEqual(response.status_code, 503)
        payload = response.get_json()
        self.assertEqual(payload["missing_tools"], ["yt-dlp"])
        self.assertNotIn("WinError", payload["error"])

    def test_favicon_uses_linksift_identity(self):
        root = Path(app.__file__).parent
        favicon = (root / "static" / "favicon.svg").read_text(encoding="utf-8").lower()
        self.assertIn("#10171b", favicon)
        self.assertIn("#c8f55a", favicon)
        self.assertIn("#f5f0e6", favicon)
        self.assertNotIn("#e85d2a", favicon)
        self.assertIn("viewbox=\"0 0 128 128\"", favicon)

    def test_release_files_do_not_contain_forbidden_artifact_markers(self):
        root = Path(app.__file__).parent
        for relative_path in RELEASE_BRAND_FILES:
            with self.subTest(relative_path=relative_path):
                content = (root / relative_path).read_text(encoding="utf-8").lower()
                for marker in FORBIDDEN_ARTIFACT_MARKERS:
                    self.assertNotIn(marker, content)

    def test_public_docs_and_docker_use_the_docker_first_contract(self):
        root = Path(app.__file__).parent
        readme = (root / "README.md").read_text(encoding="utf-8")
        contributing = (root / "CONTRIBUTING.md").read_text(encoding="utf-8")
        dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
        entrypoint = (root / "docker-entrypoint.sh").read_text(encoding="utf-8")
        compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
        release_compose = (root / "compose.ghcr.yml").read_text(encoding="utf-8")

        first_bash_block = re.search(r"```bash\s*\n(.*?)\n```", readme, re.DOTALL)
        self.assertIsNotNone(first_bash_block)
        self.assertIn("docker run -d --name linksift", first_bash_block.group(1))
        self.assertIn("ghcr.io/loveisbl1nd/linksift:latest", first_bash_block.group(1))
        self.assertIn("Development", readme)
        self.assertIn("./linksift.sh", contributing)
        self.assertIn("Python 3.12", contributing)
        self.assertIn("yt-dlp", contributing)
        self.assertIn("ffmpeg", contributing)
        self.assertIn("ffmpeg", dockerfile)
        self.assertIn("requirements.txt", dockerfile)
        self.assertIn("useradd -m -u 1000 linksift", dockerfile)
        self.assertIn("USER linksift", dockerfile)
        self.assertIn('"-w", "1"', dockerfile)
        self.assertEqual(dockerfile.count('"-w", "1"'), 1)
        self.assertIn("LINKSIFT_NO_UPDATE", entrypoint)
        self.assertIn("linksift:", compose)
        self.assertIn("image: linksift:latest", compose)
        self.assertIn("container_name: linksift", compose)
        self.assertIn('"127.0.0.1:8899:8899"', compose)
        self.assertIn("linksift-downloads", compose)
        self.assertIn("image: ghcr.io/loveisbl1nd/linksift:${LINKSIFT_VERSION:-latest}", release_compose)
        self.assertNotIn("build:", release_compose)
        self.assertIn('"127.0.0.1:8899:8899"', release_compose)
        self.assertIn("linksift-downloads", release_compose)

    def test_multi_output_compose_example_targets_the_real_service(self):
        """The v0.3 override example must configure the existing service.

        Compose service keys are case-sensitive: a `linkSift:` key would define a
        second service instead of overriding `linksift:`.
        """
        root = Path(app.__file__).parent
        readme = (root / "README.md").read_text(encoding="utf-8")
        compose = (root / "docker-compose.yml").read_text(encoding="utf-8")

        override_block = re.search(
            r"```yaml\s*\n(# docker-compose\.yml override.*?)\n```", readme, re.DOTALL
        )
        self.assertIsNotNone(override_block, "expected a yaml compose override example")
        example = override_block.group(1)

        real_services = set(re.findall(r"^  ([A-Za-z0-9_.-]+):", compose, re.MULTILINE))
        example_services = set(re.findall(r"^  ([A-Za-z0-9_.-]+):", example, re.MULTILINE))
        self.assertTrue(example_services)
        self.assertTrue(
            example_services <= real_services,
            f"example services {sorted(example_services)} are not all real services {sorted(real_services)}",
        )
        self.assertIn("linksift", example_services)
        self.assertNotIn("linkSift", example)
