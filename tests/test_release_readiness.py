import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PINNED_ACTION = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")


class ReleaseReadinessTests(unittest.TestCase):
    def read(self, relative_path):
        return (ROOT / relative_path).read_text(encoding="utf-8")

    def assert_actions_are_pinned(self, workflow_path):
        workflow = self.read(workflow_path)
        action_refs = re.findall(r"^\s*-?\s*uses:\s*([^\s#]+)", workflow, re.MULTILINE)
        self.assertTrue(action_refs, f"No actions found in {workflow_path}")
        for action_ref in action_refs:
            with self.subTest(workflow=workflow_path, action=action_ref):
                self.assertRegex(action_ref, PINNED_ACTION)

    def test_ci_and_release_actions_are_commit_pinned(self):
        self.assert_actions_are_pinned(".github/workflows/ci.yml")
        self.assert_actions_are_pinned(".github/workflows/release.yml")

    def test_release_workflow_publishes_versioned_multi_arch_image(self):
        workflow = self.read(".github/workflows/release.yml")

        self.assertIn('"v[0-9]+.[0-9]+.[0-9]+"', workflow)
        self.assertIn("REGISTRY: ghcr.io", workflow)
        self.assertIn("IMAGE_NAME: ${{ github.repository }}", workflow)
        self.assertIn("platforms: linux/amd64,linux/arm64", workflow)
        self.assertIn("type=semver,pattern={{version}}", workflow)
        self.assertIn("type=semver,pattern={{major}},enable=${{ !startsWith(github.ref, 'refs/tags/v0.') }}", workflow)
        self.assertIn("type=raw,value=latest", workflow)
        self.assertIn("fetch-depth: 0", workflow)
        self.assertIn('git merge-base --is-ancestor "$GITHUB_SHA" origin/main', workflow)
        self.assertIn("sbom: true", workflow)
        self.assertIn("push: true", workflow)
        self.assertIn("packages: write", workflow)
        self.assertIn("contents: write", workflow)
        self.assertIn(
            "docker compose -f docker-compose.yml -f docker-compose.youtube-robust.yml config --quiet",
            workflow,
        )

    def test_ci_validates_robust_compose_overlay(self):
        workflow = self.read(".github/workflows/ci.yml")

        self.assertIn(
            "docker compose -f docker-compose.yml -f docker-compose.youtube-robust.yml config --quiet",
            workflow,
        )

    def test_release_workflow_attests_digest_before_creating_release(self):
        workflow = self.read(".github/workflows/release.yml")

        attest_position = workflow.index("- name: Attest image provenance")
        release_position = workflow.index("- name: Create GitHub release")
        self.assertLess(attest_position, release_position)
        self.assertIn("attestations: write", workflow)
        self.assertIn("id-token: write", workflow)
        self.assertIn("subject-digest: ${{ steps.push.outputs.digest }}", workflow)
        self.assertIn("push-to-registry: true", workflow)
        self.assertIn('gh release create "$GITHUB_REF_NAME"', workflow)
        self.assertIn("--verify-tag", workflow)

    def test_release_compose_uses_public_image_contract(self):
        compose = self.read("compose.ghcr.yml")

        self.assertIn("ghcr.io/loveisbl1nd/linksift:${LINKSIFT_VERSION:-latest}", compose)
        self.assertNotIn("build:", compose)
        self.assertIn('"127.0.0.1:8899:8899"', compose)
        self.assertIn("linksift-downloads:/app/downloads", compose)

    def test_container_has_oci_source_and_license_labels(self):
        dockerfile = self.read("Dockerfile")
        attributes = self.read(".gitattributes")
        entrypoint_bytes = (ROOT / "docker-entrypoint.sh").read_bytes()

        self.assertIn('org.opencontainers.image.source="https://github.com/loveisbl1nd/linksift"', dockerfile)
        self.assertIn('org.opencontainers.image.licenses="MIT"', dockerfile)
        self.assertIn("sed -i 's/\\r$//' /app/docker-entrypoint.sh", dockerfile)
        self.assertIn("*.sh text eol=lf", attributes)
        self.assertNotIn(b"\r\n", entrypoint_bytes)

        dockerignore = self.read(".dockerignore").splitlines()
        self.assertNotIn("LICENSE", dockerignore)
        self.assertNotIn("THIRD_PARTY_NOTICES.md", dockerignore)

    def test_provenance_records_exact_baseline_and_metrics_boundary(self):
        provenance = self.read("PROVENANCE.md")
        notices = self.read("THIRD_PARTY_NOTICES.md")
        license_text = self.read("LICENSE")
        baseline = "1d161d15a4fe93d9b3371377f0a421dc3e965b10"

        self.assertIn("https://github.com/averygan/reclip", provenance)
        self.assertIn(baseline, provenance)
        self.assertIn("does not claim activity or adoption belonging to the upstream repository", provenance)
        self.assertIn(baseline, notices)
        self.assertIn("Copyright (c) 2026\n", notices)
        self.assertIn("Copyright (c) 2026 iaht", license_text)

    def test_roadmap_has_contribution_paths_and_non_goals(self):
        roadmap = self.read("ROADMAP.md")

        for section in (
            "## Project principles",
            "## v0.1 — Reliable distribution",
            "## v0.2 — Queueing, performance, and compatibility",
            "## v0.3 — Multi-output download pipeline",
            "## Non-goals",
            "## How to contribute to the roadmap",
        ):
            with self.subTest(section=section):
                self.assertIn(section, roadmap)

    def test_readme_links_release_provenance_and_roadmap_docs(self):
        readme = self.read("README.md")

        self.assertIn("ghcr.io/loveisbl1nd/linksift:latest", readme)
        self.assertIn("gh attestation verify oci://ghcr.io/loveisbl1nd/linksift:0.2.0", readme)
        for document in ("RELEASING.md", "PROVENANCE.md", "THIRD_PARTY_NOTICES.md", "ROADMAP.md"):
            with self.subTest(document=document):
                self.assertIn(f"]({document})", readme)

    def section(self, text, heading):
        """Return the body of one markdown section, excluding later headings."""
        start = text.index(heading) + len(heading)
        level = len(heading) - len(heading.lstrip("#"))
        next_heading = re.search(rf"^#{{1,{level}}} ", text[start:], re.MULTILINE)
        end = start + next_heading.start() if next_heading else len(text)
        return text[start:end]

    def test_parent_status_docs_keep_starting_a_phase_not_a_status(self):
        """`starting` is a parent phase; the parent status stays `downloading`.

        app.run_pipeline sets status="downloading" together with phase="starting",
        so documenting `starting` as a parent status contradicts the API.
        """
        readme = self.read("README.md")

        statuses = self.section(readme, "### Parent job statuses")
        documented = set(re.findall(r"^\| `([a-z_]+)` \|", statuses, re.MULTILINE))
        self.assertNotIn("starting", documented)
        self.assertEqual(
            documented,
            {"queued", "downloading", "cancelling", "done", "partial", "error", "cancelled", "timed_out"},
        )

        phases = self.section(readme, "### Parent phases while active")
        documented_phases = set(re.findall(r"^\| `([a-z]+)` \|", phases, re.MULTILINE))
        self.assertIn("starting", documented_phases)
        self.assertTrue(
            {"downloading", "retrying", "processing"} <= documented_phases,
            documented_phases,
        )

    def test_reuse_docs_separate_ordinary_fallback_from_fatal_outcomes(self):
        """try_ffmpeg_reuse has three outcomes; only the ordinary one falls back."""
        reuse = self.read("README.md")
        reuse = self.section(reuse, "### Video-to-audio reuse").lower()

        self.assertIn("three outcomes", reuse)
        self.assertIn("ordinary failure", reuse)
        self.assertIn("pipelinecancelled", reuse)
        self.assertIn("timeoutexpired", reuse)
        self.assertIn("timed_out", reuse)
        # The corrected claim: ordinary failures MAY fall back; cancellation and
        # deadline expiry never do.
        self.assertNotIn("fails to publish (timeout or cancellation) is fatal", reuse)
        self.assertIn("not every publication error is fatal", reuse)
        self.assertRegex(reuse, r"only the ordinary[- ]failure branch falls back")

    def test_node_detection_in_tests_is_cross_platform(self):
        """Tests must locate node with shutil.which, not the Unix-only `which`."""
        offenders = []
        uses_shutil_which = []
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            if path.name == Path(__file__).name:  # this file only quotes the patterns
                continue
            source = path.read_text(encoding="utf-8")
            if re.search(r"""\[\s*["'](?:which|where)["']\s*,\s*["']node["']""", source):
                offenders.append(path.name)
            if 'shutil.which("node")' in source:
                uses_shutil_which.append(path.name)
                with self.subTest(path=path.name):
                    self.assertRegex(source, r"(?m)^import shutil$", msg="shutil must be imported")

        self.assertEqual(offenders, [], f"Unix-only node detection in: {offenders}")
        self.assertIn("test_phase_propagation.py", uses_shutil_which)


if __name__ == "__main__":
    unittest.main()
