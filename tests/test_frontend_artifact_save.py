"""Node behavior tests for per-artifact saving in the multi-output UI.

These are not source-string assertions: each test extracts the real function
body from templates/index.html, runs it in Node against stubbed browser APIs,
and asserts on the resulting state and the URLs actually requested.

Covered behavior:
  * saveArtifact targets /api/file/<job>/<artifact>, never the parent endpoint
  * the selected-folder flow streams into a directory handle
  * the browser-download flow builds an anchor with the per-artifact URL
  * save state is tracked per artifact, so one save never marks a sibling saved
  * a failed save leaves the artifact unsaved and records the error
  * saveCard fans out to every completed artifact for multi-output jobs
  * saveCard keeps using /api/file/<job> for single-output (legacy) jobs
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

import app


def read_template():
    return (Path(app.__file__).parent / "templates" / "index.html").read_text(encoding="utf-8")


def function_source(template, name):
    """Return the full text of a top-level script function (4-space indent)."""
    match = re.search(
        rf"((?:async )?function {re.escape(name)}\([^)]*\) \{{.*?\n    \}})",
        template,
        re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"function {name} not found in template")
    return match.group(1)


# Stubs shared by every harness. Records each fetch URL and each anchor click so
# a test can assert which endpoint the real code chose.
HARNESS_PRELUDE = """
'use strict';
const fetchUrls = [];
const anchorClicks = [];
const written = [];
let folderGranted = false;
let downloadDirectoryHandle = null;
let useBrowserDownloadsCalls = 0;
let renderCalls = 0;
let manifestCalls = 0;
let failFetch = false;

function renderCard() { renderCalls++; }
function updateManifest() { manifestCalls++; }
function useBrowserDownloads() { useBrowserDownloadsCalls++; downloadDirectoryHandle = null; }
async function selectedFolderHasPermission() { return folderGranted && downloadDirectoryHandle !== null; }
async function uniqueFileHandle(directory, filename) {
  return {
    async createWritable() {
      return { name: filename, _sink: true };
    }
  };
}
async function fetch(url) {
  fetchUrls.push(url);
  if (failFetch) return { ok: false, body: null };
  return {
    ok: true,
    body: {
      async pipeTo(writable) { written.push(writable.name); }
    }
  };
}
async function sendToBrowserDownload(card) {
  anchorClicks.push({ href: `/api/file/${card.jobId}`, download: card.filename || '' });
  card.saved = true;
  card.savedToBrowser = true;
}
const document = {
  body: { appendChild() {}, removeChild() {} },
  createElement() {
    const el = { href: '', download: '', remove() {}, click() { anchorClicks.push({ href: el.href, download: el.download }); } };
    return el;
  }
};
let cardData = [];
"""


def run_harness(*, functions, body):
    """Run the given real template functions plus an assertion body in Node."""
    template = read_template()
    sources = "\n".join(function_source(template, name) for name in functions)
    script = HARNESS_PRELUDE + "\n" + sources + "\n" + body
    return subprocess.run(
        ["node", "-"],
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )


def done_artifact(artifact_id, filename, label):
    return {
        "id": artifact_id,
        "type": "video" if filename.endswith(".mp4") else "audio",
        "status": "done",
        "label": label,
        "filename": filename,
        "percent": 100.0,
    }


class ArtifactSaveHarnessTests(unittest.TestCase):
    def setUp(self):
        if shutil.which("node") is None:
            self.skipTest("node is not available")

    def assertHarnessOk(self, result):
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("HARNESS_OK", result.stdout)
        payload = re.search(r"RESULT:(\{.*\})", result.stdout)
        self.assertIsNotNone(payload, result.stdout)
        return json.loads(payload.group(1))

    def test_folder_flow_uses_per_artifact_endpoint(self):
        """The selected-folder flow streams /api/file/<job>/<artifact> to disk."""
        body = """
(async () => {
  folderGranted = true;
  downloadDirectoryHandle = { name: 'Movies' };
  const artifacts = [
    { id: 'a000', status: 'done', filename: 'clip (MP4 22).mp4', label: 'MP4 22' },
    { id: 'a001', status: 'done', filename: 'clip (MP3 audio).mp3', label: 'MP3 audio' }
  ];
  cardData = [{ jobId: 'job42', artifacts }];
  await saveArtifact(0, 1);
  console.log('RESULT:' + JSON.stringify({
    fetchUrls, written,
    first: { saved: !!artifacts[0].saved, saving: !!artifacts[0].saving },
    second: { saved: !!artifacts[1].saved, saving: !!artifacts[1].saving,
              folder: artifacts[1].savedToFolder || null,
              browser: !!artifacts[1].savedToBrowser }
  }));
  console.log('HARNESS_OK');
})().catch(error => { console.error(error && error.stack || error); process.exit(1); });
"""
        data = self.assertHarnessOk(run_harness(functions=["syncCardSaveState", "saveArtifact"], body=body))
        self.assertEqual(data["fetchUrls"], ["/api/file/job42/a001"])
        self.assertEqual(data["written"], ["clip (MP3 audio).mp3"])
        self.assertTrue(data["second"]["saved"])
        self.assertFalse(data["second"]["saving"])
        self.assertEqual(data["second"]["folder"], "Movies")
        self.assertFalse(data["second"]["browser"])

    def test_saving_one_artifact_does_not_mark_sibling_saved(self):
        """Save state is independent per artifact."""
        body = """
(async () => {
  folderGranted = true;
  downloadDirectoryHandle = { name: 'Movies' };
  const artifacts = [
    { id: 'a000', status: 'done', filename: 'v.mp4', label: 'MP4 22' },
    { id: 'a001', status: 'done', filename: 'a.mp3', label: 'MP3 audio' }
  ];
  cardData = [{ jobId: 'job42', artifacts }];
  await saveArtifact(0, 0);
  console.log('RESULT:' + JSON.stringify({
    fetchUrls,
    a0: { saved: !!artifacts[0].saved },
    a1: { saved: !!artifacts[1].saved, folder: artifacts[1].savedToFolder || null }
  }));
  console.log('HARNESS_OK');
})().catch(error => { console.error(error && error.stack || error); process.exit(1); });
"""
        data = self.assertHarnessOk(run_harness(functions=["syncCardSaveState", "saveArtifact"], body=body))
        self.assertEqual(data["fetchUrls"], ["/api/file/job42/a000"])
        self.assertTrue(data["a0"]["saved"])
        self.assertFalse(data["a1"]["saved"])
        self.assertIsNone(data["a1"]["folder"])

    def test_browser_download_flow_uses_per_artifact_url(self):
        """Without a folder handle the artifact downloads via an anchor."""
        body = """
(async () => {
  folderGranted = false;
  downloadDirectoryHandle = null;
  const artifacts = [{ id: 'a007', status: 'done', filename: 'song.mp3', label: 'MP3 audio' }];
  cardData = [{ jobId: 'jobX', artifacts }];
  await saveArtifact(0, 0);
  console.log('RESULT:' + JSON.stringify({
    fetchUrls, anchorClicks,
    saved: !!artifacts[0].saved,
    browser: !!artifacts[0].savedToBrowser,
    folder: artifacts[0].savedToFolder || null
  }));
  console.log('HARNESS_OK');
})().catch(error => { console.error(error && error.stack || error); process.exit(1); });
"""
        data = self.assertHarnessOk(run_harness(functions=["syncCardSaveState", "saveArtifact"], body=body))
        self.assertEqual(data["fetchUrls"], [])
        self.assertEqual(
            data["anchorClicks"],
            [{"href": "/api/file/jobX/a007", "download": "song.mp3"}],
        )
        self.assertTrue(data["saved"])
        self.assertTrue(data["browser"])
        self.assertIsNone(data["folder"])

    def test_unready_artifact_is_never_fetched(self):
        """A non-done artifact must not hit the network or flip to saved."""
        body = """
(async () => {
  folderGranted = true;
  downloadDirectoryHandle = { name: 'Movies' };
  const artifacts = [{ id: 'a000', status: 'error', filename: null, label: 'MP4 22' }];
  cardData = [{ jobId: 'job42', artifacts }];
  await saveArtifact(0, 0);
  console.log('RESULT:' + JSON.stringify({
    fetchUrls, anchorClicks, saved: !!artifacts[0].saved, saving: !!artifacts[0].saving
  }));
  console.log('HARNESS_OK');
})().catch(error => { console.error(error && error.stack || error); process.exit(1); });
"""
        data = self.assertHarnessOk(run_harness(functions=["syncCardSaveState", "saveArtifact"], body=body))
        self.assertEqual(data["fetchUrls"], [])
        self.assertEqual(data["anchorClicks"], [])
        self.assertFalse(data["saved"])
        self.assertFalse(data["saving"])

    def test_failed_save_records_error_and_leaves_artifact_unsaved(self):
        """A 404 from the artifact endpoint must not report success."""
        body = """
(async () => {
  folderGranted = true;
  downloadDirectoryHandle = { name: 'Movies' };
  failFetch = true;
  const artifacts = [{ id: 'a000', status: 'done', filename: 'v.mp4', label: 'MP4 22' }];
  cardData = [{ jobId: 'job42', artifacts }];
  await saveArtifact(0, 0);
  console.log('RESULT:' + JSON.stringify({
    fetchUrls, written,
    saved: !!artifacts[0].saved,
    saving: !!artifacts[0].saving,
    error: artifacts[0].saveError || null
  }));
  console.log('HARNESS_OK');
})().catch(error => { console.error(error && error.stack || error); process.exit(1); });
"""
        data = self.assertHarnessOk(run_harness(functions=["syncCardSaveState", "saveArtifact"], body=body))
        self.assertEqual(data["fetchUrls"], ["/api/file/job42/a000"])
        self.assertEqual(data["written"], [])
        self.assertFalse(data["saved"])
        self.assertFalse(data["saving"])
        self.assertEqual(data["error"], "File is not ready")

    def test_already_saved_artifact_is_not_resaved_without_force(self):
        """The per-artifact button is one-shot; re-saving needs an explicit force."""
        body = """
(async () => {
  folderGranted = true;
  downloadDirectoryHandle = { name: 'Movies' };
  const artifacts = [{ id: 'a000', status: 'done', filename: 'v.mp4', label: 'MP4 22' }];
  cardData = [{ jobId: 'job42', artifacts }];
  await saveArtifact(0, 0);
  const afterFirst = fetchUrls.length;
  await saveArtifact(0, 0);
  const afterSecond = fetchUrls.length;
  await saveArtifact(0, 0, true);
  console.log('RESULT:' + JSON.stringify({ afterFirst, afterSecond, afterForce: fetchUrls.length, fetchUrls }));
  console.log('HARNESS_OK');
})().catch(error => { console.error(error && error.stack || error); process.exit(1); });
"""
        data = self.assertHarnessOk(run_harness(functions=["syncCardSaveState", "saveArtifact"], body=body))
        self.assertEqual(data["afterFirst"], 1)
        self.assertEqual(data["afterSecond"], 1, "a saved artifact must not re-fetch")
        self.assertEqual(data["afterForce"], 2, "force must re-save")
        self.assertEqual(
            data["fetchUrls"], ["/api/file/job42/a000", "/api/file/job42/a000"]
        )


class SaveCardFanOutTests(unittest.TestCase):
    def setUp(self):
        if shutil.which("node") is None:
            self.skipTest("node is not available")

    def assertHarnessOk(self, result):
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("HARNESS_OK", result.stdout)
        payload = re.search(r"RESULT:(\{.*\})", result.stdout)
        self.assertIsNotNone(payload, result.stdout)
        return json.loads(payload.group(1))

    def test_multi_output_savecard_saves_each_completed_artifact(self):
        """The partial "Save completed outputs" action fans out per artifact."""
        body = """
(async () => {
  folderGranted = true;
  downloadDirectoryHandle = { name: 'Movies' };
  const artifacts = [
    { id: 'a000', status: 'done', filename: 'v1.mp4', label: 'MP4 22' },
    { id: 'a001', status: 'error', filename: null, label: 'MP4 137' },
    { id: 'a002', status: 'done', filename: 'a.mp3', label: 'MP3 audio' }
  ];
  const card = { jobId: 'job99', artifacts, filename: 'ignored.mp4' };
  cardData = [card];
  await saveCard(0, true);
  console.log('RESULT:' + JSON.stringify({
    fetchUrls, written,
    cardSaved: !!card.saved,
    cardSaving: !!card.saving,
    a0: !!artifacts[0].saved, a1: !!artifacts[1].saved, a2: !!artifacts[2].saved
  }));
  console.log('HARNESS_OK');
})().catch(error => { console.error(error && error.stack || error); process.exit(1); });
"""
        data = self.assertHarnessOk(
            run_harness(functions=["syncCardSaveState", "saveArtifact", "saveCard"], body=body)
        )
        self.assertEqual(
            data["fetchUrls"], ["/api/file/job99/a000", "/api/file/job99/a002"]
        )
        self.assertNotIn("/api/file/job99", data["fetchUrls"])
        self.assertEqual(data["written"], ["v1.mp4", "a.mp3"])
        self.assertTrue(data["a0"])
        self.assertFalse(data["a1"], "a failed artifact must not be marked saved")
        self.assertTrue(data["a2"])
        self.assertTrue(data["cardSaved"])
        self.assertFalse(data["cardSaving"])

    def test_single_output_savecard_keeps_legacy_parent_endpoint(self):
        """A job with no artifacts array must still use /api/file/<job>."""
        body = """
(async () => {
  folderGranted = true;
  downloadDirectoryHandle = { name: 'Movies' };
  const card = { jobId: 'legacy1', filename: 'movie.mp4' };
  cardData = [card];
  await saveCard(0);
  console.log('RESULT:' + JSON.stringify({
    fetchUrls, written, saved: !!card.saved, folder: card.savedToFolder || null
  }));
  console.log('HARNESS_OK');
})().catch(error => { console.error(error && error.stack || error); process.exit(1); });
"""
        data = self.assertHarnessOk(
            run_harness(functions=["syncCardSaveState", "saveArtifact", "saveCard"], body=body)
        )
        self.assertEqual(data["fetchUrls"], ["/api/file/legacy1"])
        self.assertEqual(data["written"], ["movie.mp4"])
        self.assertTrue(data["saved"])
        self.assertEqual(data["folder"], "Movies")

    def test_single_artifact_job_saves_through_legacy_parent_endpoint(self):
        """A one-artifact job is single-output: keep /api/file/<job>.

        The server sends an artifacts array even for single-output jobs, so the
        client must discriminate on the count, not the array's presence.
        """
        body = """
(async () => {
  folderGranted = true;
  downloadDirectoryHandle = { name: 'Movies' };
  const artifacts = [{ id: 'a000', status: 'done', filename: 'only.mp4', label: 'MP4 22' }];
  const card = { jobId: 'solo', filename: 'only.mp4', artifacts };
  cardData = [card];
  await saveCard(0);
  console.log('RESULT:' + JSON.stringify({ fetchUrls, written, saved: !!card.saved }));
  console.log('HARNESS_OK');
})().catch(error => { console.error(error && error.stack || error); process.exit(1); });
"""
        data = self.assertHarnessOk(
            run_harness(
                functions=["syncCardSaveState", "saveArtifact", "saveCard"], body=body
            )
        )
        self.assertEqual(data["fetchUrls"], ["/api/file/solo"])
        self.assertTrue(data["saved"])

    def test_savecard_with_no_completed_artifacts_does_nothing(self):
        """An all-failed parent must not fetch anything or claim it saved."""
        body = """
(async () => {
  folderGranted = true;
  downloadDirectoryHandle = { name: 'Movies' };
  const artifacts = [
    { id: 'a000', status: 'error', filename: null, label: 'MP4 22' },
    { id: 'a001', status: 'error', filename: null, label: 'MP3 audio' }
  ];
  const card = { jobId: 'jobbad', artifacts };
  cardData = [card];
  await saveCard(0, true);
  console.log('RESULT:' + JSON.stringify({
    fetchUrls, anchorClicks, saved: !!card.saved, saving: !!card.saving
  }));
  console.log('HARNESS_OK');
})().catch(error => { console.error(error && error.stack || error); process.exit(1); });
"""
        data = self.assertHarnessOk(
            run_harness(functions=["syncCardSaveState", "saveArtifact", "saveCard"], body=body)
        )
        self.assertEqual(data["fetchUrls"], [])
        self.assertEqual(data["anchorClicks"], [])
        self.assertFalse(data["saved"])
        self.assertFalse(data["saving"])

    def test_multi_output_browser_flow_uses_each_artifact_url(self):
        """The browser-download fallback also fans out per artifact."""
        body = """
(async () => {
  folderGranted = false;
  downloadDirectoryHandle = null;
  const artifacts = [
    { id: 'a000', status: 'done', filename: 'v1.mp4', label: 'MP4 22' },
    { id: 'a001', status: 'done', filename: 'a.mp3', label: 'MP3 audio' }
  ];
  const card = { jobId: 'jobbrowser', artifacts };
  cardData = [card];
  await saveCard(0, true);
  console.log('RESULT:' + JSON.stringify({ fetchUrls, anchorClicks, saved: !!card.saved }));
  console.log('HARNESS_OK');
})().catch(error => { console.error(error && error.stack || error); process.exit(1); });
"""
        data = self.assertHarnessOk(
            run_harness(functions=["syncCardSaveState", "saveArtifact", "saveCard"], body=body)
        )
        self.assertEqual(data["fetchUrls"], [])
        self.assertEqual(
            data["anchorClicks"],
            [
                {"href": "/api/file/jobbrowser/a000", "download": "v1.mp4"},
                {"href": "/api/file/jobbrowser/a001", "download": "a.mp3"},
            ],
        )
        self.assertTrue(data["saved"])


class ArtifactSaveRenderTests(unittest.TestCase):
    """The rendered artifact list must expose a Save control per completed output."""

    def setUp(self):
        self.template = read_template()

    def test_done_artifact_renders_save_button(self):
        render = function_source(self.template, "renderCard")
        self.assertIn("artifact-save-btn", render)
        self.assertIn("data-artifact-save", render)

    def test_artifact_list_is_multi_output_only(self):
        """A single-output job keeps the legacy UI (no per-artifact list)."""
        render = function_source(self.template, "renderCard")
        self.assertIn("card.artifacts && card.artifacts.length > 1", render)

    def test_fast_path_render_cannot_skip_a_changed_artifact_list(self):
        """The downloading fast-path must fall through when an artifact changed.

        Otherwise a completed artifact's Save button never appears until the
        whole parent job finishes.
        """
        render = function_source(self.template, "renderCard")
        self.assertIn("artSig", render)
        self.assertIn("element.dataset.artSig === artSig", render)
        self.assertIn("element.dataset.artSig = artSig", render)

    def test_save_button_is_bound_to_saveartifact(self):
        render = function_source(self.template, "renderCard")
        self.assertIn("[data-artifact-save]", render)
        self.assertIn("saveArtifact(index, artifactIdx)", render)

    def test_saved_and_saving_states_are_reflected(self):
        render = function_source(self.template, "renderCard")
        self.assertIn("art.saving ? 'Saving…'", render)
        self.assertIn("art.saved ?", render)
        self.assertIn("saveDisabled", render)

    def test_poll_merge_preserves_per_artifact_save_state(self):
        poll = function_source(self.template, "pollCard")
        self.assertIn("previous[a.id]", poll)
        # The merge mutates the existing artifact object in place so a reference
        # held across an await inside saveArtifact stays live.
        self.assertIn("Object.assign(old, a)", poll)

    def test_single_output_autosave_is_guarded_by_artifact_count(self):
        """Auto-save must remain single-output only.

        The server always sends an artifacts array once planning runs, so the
        old `!data.artifacts` guard would be dead code and auto-save would never
        fire. The discriminator is the artifact count.
        """
        poll = function_source(self.template, "pollCard")
        self.assertIn("data.artifacts.length > 1", poll)
        self.assertIn("if (!isMulti && !card.saved && !card.saving)", poll)
        self.assertIn("saveCard(index)", poll)
        self.assertNotIn("if (!data.artifacts && !card.saved", poll)


POLL_PRELUDE = """
let statusResponses = [];
let statusIndex = 0;
const pollControllers = new Map();
const timeouts = [];
function AbortController() { this.signal = { aborted: false }; this.abort = function () { this.signal.aborted = true; }; }
function setTimeout(fn) { timeouts.push(fn); }
function settleIdleActivity() {}
document.documentElement = { dataset: {} };
async function statusFetch(url) {
  fetchUrls.push(url);
  const payload = statusResponses[Math.min(statusIndex, statusResponses.length - 1)];
  statusIndex++;
  return { ok: true, async json() { return payload; } };
}
"""


class PollCardRuntimeTests(unittest.TestCase):
    """Real pollCard runs: merge behaviour and single-output auto-save.

    These execute the actual pollCard body against scripted /api/status payloads
    rather than asserting on its source text.
    """

    def setUp(self):
        if shutil.which("node") is None:
            self.skipTest("node is not available")

    def assertHarnessOk(self, result):
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("HARNESS_OK", result.stdout)
        payload = re.search(r"RESULT:(\{.*\})", result.stdout)
        self.assertIsNotNone(payload, result.stdout)
        return json.loads(payload.group(1))

    def run_poll(self, body):
        template = read_template()
        sources = "\n".join(
            function_source(template, name)
            for name in ["syncCardSaveState", "saveArtifact", "saveCard", "pollCard"]
        )
        # pollCard fetches /api/status; the save helpers fetch /api/file. Route
        # status requests to the scripted responder, everything else to the
        # regular file stub so a real auto-save is observable.
        script = (
            HARNESS_PRELUDE
            + POLL_PRELUDE
            + "\nconst fileFetch = fetch;\n"
            + "fetch = async (url, opts) => url.startsWith('/api/status/') ? statusFetch(url) : fileFetch(url, opts);\n"
            + sources
            + "\n"
            + body
        )
        # `fetch` is declared with `function` in the prelude, so rebind via var.
        script = script.replace(
            "async function fetch(url) {", "var fetch = async function (url) {"
        ).replace(
            """  return {
    ok: true,
    body: {
      async pipeTo(writable) { written.push(writable.name); }
    }
  };
}""",
            """  return {
    ok: true,
    body: {
      async pipeTo(writable) { written.push(writable.name); }
    }
  };
};""",
        )
        return subprocess.run(
            ["node", "-"],
            input=script,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )

    def test_poll_preserves_a_completed_save_across_a_later_poll(self):
        """A finished per-artifact save must survive the next status poll.

        The server never echoes client save state, so a naive re-assignment
        would silently un-save an artifact the user already wrote to disk.
        """
        body = """
(async () => {
  folderGranted = true;
  downloadDirectoryHandle = { name: 'Movies' };
  statusResponses = [
    { status: 'downloading', percent: 40, artifacts: [
      { id: 'a000', status: 'done', filename: 'v.mp4', label: 'MP4 22', percent: 100 },
      { id: 'a001', status: 'downloading', filename: null, label: 'MP3', percent: 10 }
    ]},
    { status: 'downloading', percent: 70, artifacts: [
      { id: 'a000', status: 'done', filename: 'v.mp4', label: 'MP4 22', percent: 100 },
      { id: 'a001', status: 'downloading', filename: null, label: 'MP3', percent: 60 }
    ]}
  ];
  const card = { jobId: 'job7', artifacts: [] };
  cardData = [card];
  pollCard(0, card);
  await new Promise(r => process.nextTick(r));
  await new Promise(r => process.nextTick(r));
  // First poll landed; save artifact a000 to disk.
  await saveArtifact(0, 0);
  const savedAfterWrite = card.artifacts[0].saved;
  // Now let the second poll run and re-merge the server's artifacts.
  timeouts.pop()();
  await new Promise(r => process.nextTick(r));
  await new Promise(r => process.nextTick(r));
  console.log('RESULT:' + JSON.stringify({
    savedAfterWrite: !!savedAfterWrite,
    savedAfterPoll: !!card.artifacts[0].saved,
    folderAfterPoll: card.artifacts[0].savedToFolder || null,
    siblingSaved: !!card.artifacts[1].saved,
    secondPercent: card.artifacts[1].percent,
    written
  }));
  console.log('HARNESS_OK');
})().catch(error => { console.error(error && error.stack || error); process.exit(1); });
"""
        data = self.assertHarnessOk(self.run_poll(body))
        self.assertTrue(data["savedAfterWrite"])
        self.assertTrue(data["savedAfterPoll"], "poll must not reset a completed save")
        self.assertEqual(data["folderAfterPoll"], "Movies")
        self.assertFalse(data["siblingSaved"])
        # The merge still applies fresh server data to the same objects.
        self.assertEqual(data["secondPercent"], 60)
        self.assertEqual(data["written"], ["v.mp4"])

    def test_save_in_flight_is_not_stranded_by_a_concurrent_poll(self):
        """An artifact saved across a poll must not be left stuck on 'saving'.

        The merge has to mutate the existing artifact objects, otherwise
        saveArtifact's reference is orphaned and its finally block clears
        `saving` on a discarded copy.
        """
        body = """
(async () => {
  folderGranted = true;
  downloadDirectoryHandle = { name: 'Movies' };
  const artifacts = [
    { id: 'a000', status: 'done', filename: 'v.mp4', label: 'MP4 22', percent: 100 },
    { id: 'a001', status: 'done', filename: 'a.mp3', label: 'MP3', percent: 100 }
  ];
  statusResponses = [{ status: 'downloading', percent: 90, artifacts }];
  const card = { jobId: 'job8', artifacts: [] };
  cardData = [card];
  pollCard(0, card);
  await new Promise(r => process.nextTick(r));
  await new Promise(r => process.nextTick(r));
  // Start a save, then run another poll while it is still in flight.
  const pending = saveArtifact(0, 0);
  timeouts.pop()();
  await new Promise(r => process.nextTick(r));
  await pending;
  console.log('RESULT:' + JSON.stringify({
    saving: !!card.artifacts[0].saving,
    saved: !!card.artifacts[0].saved,
    cardSaved: !!card.saved
  }));
  console.log('HARNESS_OK');
})().catch(error => { console.error(error && error.stack || error); process.exit(1); });
"""
        data = self.assertHarnessOk(self.run_poll(body))
        self.assertFalse(data["saving"], "in-flight save must not stay stuck")
        self.assertTrue(data["saved"])
        # a001 is still unsaved, so the parent is not fully saved.
        self.assertFalse(data["cardSaved"])

    def test_single_output_job_auto_saves_through_the_parent_endpoint(self):
        """A finished single-output job auto-saves, as it did before v0.3.

        The server sends a one-element artifacts array for these jobs, so the
        auto-save guard must key off the artifact count.
        """
        body = """
(async () => {
  folderGranted = true;
  downloadDirectoryHandle = { name: 'Movies' };
  statusResponses = [{
    status: 'done', percent: 100, filename: 'movie.mp4',
    artifacts: [{ id: 'a000', status: 'done', filename: 'movie.mp4', label: 'MP4 22', percent: 100 }]
  }];
  const card = { jobId: 'solo9', artifacts: [] };
  cardData = [card];
  pollCard(0, card);
  for (let i = 0; i < 8; i++) await new Promise(r => process.nextTick(r));
  console.log('RESULT:' + JSON.stringify({
    fetchUrls, written, saved: !!card.saved, filename: card.filename || null,
    artifacts: card.artifacts
  }));
  console.log('HARNESS_OK');
})().catch(error => { console.error(error && error.stack || error); process.exit(1); });
"""
        data = self.assertHarnessOk(self.run_poll(body))
        self.assertIn("/api/file/solo9", data["fetchUrls"])
        self.assertNotIn("/api/file/solo9/a000", data["fetchUrls"])
        self.assertEqual(data["written"], ["movie.mp4"])
        self.assertTrue(data["saved"])
        self.assertEqual(data["filename"], "movie.mp4")
        self.assertIsNone(data["artifacts"], "single-output keeps the legacy UI")

    def test_multi_output_job_does_not_auto_save(self):
        """Multi-output waits for the user; it must not write files on its own."""
        body = """
(async () => {
  folderGranted = true;
  downloadDirectoryHandle = { name: 'Movies' };
  statusResponses = [{
    status: 'done', percent: 100,
    artifacts: [
      { id: 'a000', status: 'done', filename: 'v.mp4', label: 'MP4 22', percent: 100 },
      { id: 'a001', status: 'done', filename: 'a.mp3', label: 'MP3', percent: 100 }
    ]
  }];
  const card = { jobId: 'multi9', artifacts: [] };
  cardData = [card];
  pollCard(0, card);
  for (let i = 0; i < 8; i++) await new Promise(r => process.nextTick(r));
  console.log('RESULT:' + JSON.stringify({
    fileFetches: fetchUrls.filter(u => u.startsWith('/api/file/')),
    written, saved: !!card.saved, count: card.artifacts.length
  }));
  console.log('HARNESS_OK');
})().catch(error => { console.error(error && error.stack || error); process.exit(1); });
"""
        data = self.assertHarnessOk(self.run_poll(body))
        self.assertEqual(data["fileFetches"], [])
        self.assertEqual(data["written"], [])
        self.assertFalse(data["saved"])
        self.assertEqual(data["count"], 2)


if __name__ == "__main__":
    unittest.main()
