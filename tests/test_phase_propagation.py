"""Behavior tests for artifact display labels and phase-propagation contracts.

These exercise the REAL frontend functions against scripted browser globals in
Node. They are not source-text assertions.

Frontend coverage:
  * artifactDisplayState maps every supported status/phase to the right label
    and CSS class (the old inline map only knew done/error/downloading and let
    everything else fall through to 'Pending').
  * artifactSignature includes phase, so a phase-only change (downloading ->
    retrying) invalidates the renderCard fast path.
  * completed artifacts retain Save; non-downloadable terminal artifacts do not.

Backend coverage:
  * check_status mirrors the current artifact's phase for every ACTIVE phase
    (starting / downloading / retrying / processing), not just retrying/processing.
  * terminal parent phases are never overwritten by a stale artifact phase.
  * single-output responses keep their legacy shape.
"""
import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

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


NODE_PRELUDE = r"""
'use strict';
const artifactStates = [];
const logCalls = [];
let renderCardCalls = 0;
function renderCard() { renderCardCalls++; }
function updateManifest() {}
const document = {
  documentElement: { dataset: {} },
  body: { dataset: {} },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  createElement() { return { classList: { add(){} }, appendChild(){}, setAttribute(){} }; },
};
// console.log writes directly to stdout (so RESULT: lines are captured by
// subprocess.run) AND mirrors into logCalls for harness inspection. The
// earlier prelude only buffered into an array flushed once at load time,
// which ran before the test body and dropped everything.
const console = {
  log: (...a) => { const s = a.join(' '); logCalls.push(s); process.stdout.write(s + '\n'); },
  error: (...a) => { const s = 'ERR ' + a.join(' '); logCalls.push(s); process.stderr.write(s + '\n'); },
};
"""


class ArtifactDisplayStateTests(unittest.TestCase):
    """Node behavior tests for artifactDisplayState."""

    def setUp(self):
        if shutil.which("node") is None:
            self.skipTest("node is not available")
        self.template = read_template()

    def run_state(self, body):
        src = self.template
        prelude = (
            NODE_PRELUDE
            + "\n"
            + function_source(src, "artifactDisplayState")
            + "\n"
            + body
        )
        result = subprocess.run(
            ["node", "-"], input=prelude, capture_output=True, text=True,
            encoding="utf-8", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        payload = re.search(r"RESULT:(\{.*\})", result.stdout)
        self.assertIsNotNone(payload, result.stdout)
        return json.loads(payload.group(1))

    def test_pending(self):
        out = self.run_state('console.log("RESULT:" + JSON.stringify(artifactDisplayState({status:"pending", phase:"pending"})));')
        self.assertEqual(out["text"], "Pending")
        self.assertEqual(out["cls"], "pending")
        self.assertFalse(out["canSave"])

    def test_starting(self):
        out = self.run_state('console.log("RESULT:" + JSON.stringify(artifactDisplayState({status:"downloading", phase:"starting"})));')
        self.assertEqual(out["text"], "Starting…")
        self.assertEqual(out["cls"], "downloading")
        self.assertFalse(out["canSave"])
        self.assertFalse(out["showProgress"])

    def test_downloading(self):
        out = self.run_state('console.log("RESULT:" + JSON.stringify(artifactDisplayState({status:"downloading", phase:"downloading"})));')
        self.assertEqual(out["text"], "Downloading…")
        self.assertTrue(out["showProgress"])

    def test_retrying_is_not_downloading_or_pending(self):
        out = self.run_state('console.log("RESULT:" + JSON.stringify(artifactDisplayState({status:"downloading", phase:"retrying"})));')
        self.assertEqual(out["text"], "Retrying…",
                         "retrying must NOT read as Downloading or Pending")
        self.assertTrue(out["showProgress"])

    def test_processing_is_not_pending(self):
        out = self.run_state('console.log("RESULT:" + JSON.stringify(artifactDisplayState({status:"processing", phase:"processing"})));')
        self.assertEqual(out["text"], "Processing…",
                         "processing must NOT read as Pending")
        self.assertFalse(out["showProgress"],
                         "processing shows no progress bar")

    def test_done_retains_save_and_no_progress(self):
        out = self.run_state('console.log("RESULT:" + JSON.stringify(artifactDisplayState({status:"done", phase:"done"})));')
        self.assertIn("Complete", out["text"])
        self.assertTrue(out["canSave"], "a done artifact must offer Save")
        self.assertFalse(out["showProgress"])
        self.assertEqual(out["cls"], "done")

    def test_error_has_no_save(self):
        out = self.run_state('console.log("RESULT:" + JSON.stringify(artifactDisplayState({status:"error", phase:"error"})));')
        self.assertIn("Failed", out["text"])
        self.assertFalse(out["canSave"], "a failed artifact must not offer Save")

    def test_cancelled_is_distinct_from_error(self):
        out = self.run_state('console.log("RESULT:" + JSON.stringify(artifactDisplayState({status:"cancelled", phase:"cancelled"})));')
        self.assertEqual(out["text"], "Cancelled")
        self.assertEqual(out["cls"], "cancelled")
        self.assertFalse(out["canSave"])

    def test_timed_out_is_distinct_from_cancelled(self):
        out = self.run_state('console.log("RESULT:" + JSON.stringify(artifactDisplayState({status:"timed_out", phase:"timed_out"})));')
        self.assertEqual(out["text"], "Timed out")
        self.assertEqual(out["cls"], "timed-out",
                         "timed_out and cancelled must use different CSS classes")
        self.assertFalse(out["canSave"])

    def test_status_processing_with_no_phase(self):
        """status processing even without a phase must show Processing, not Pending."""
        out = self.run_state('console.log("RESULT:" + JSON.stringify(artifactDisplayState({status:"processing"})));')
        self.assertEqual(out["text"], "Processing…")

    def test_postprocessing_phase_is_accepted(self):
        out = self.run_state('console.log("RESULT:" + JSON.stringify(artifactDisplayState({status:"downloading", phase:"postprocessing"})));')
        self.assertEqual(out["text"], "Processing…")


class ArtifactSignatureTests(unittest.TestCase):
    """The fast path signature must include phase."""

    def setUp(self):
        if shutil.which("node") is None:
            self.skipTest("node is not available")
        self.template = read_template()

    def run_sig(self, body):
        src = self.template
        prelude = NODE_PRELUDE + "\n" + function_source(src, "artifactSignature") + "\n" + body
        result = subprocess.run(
            ["node", "-"], input=prelude, capture_output=True, text=True,
            encoding="utf-8", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        payload = re.search(r"RESULT:(\{.*\})", result.stdout)
        self.assertIsNotNone(payload, result.stdout)
        return json.loads(payload.group(1))

    def test_phase_only_change_invalidates_the_signature(self):
        """Two artifacts identical except for phase must produce different
        signatures - otherwise the renderCard fast path would skip the rerender
        and the row would sit on 'Downloading...' forever after a retry."""
        body = """
const base = { id: 'a000', status: 'downloading', phase: 'downloading', percent: 42, saved: false, saving: false, saveError: null };
const card1 = { artifacts: [base, {...base, id: 'a001'}] };
const card2 = { artifacts: [base, {...base, id: 'a001', phase: 'retrying'}] };
console.log('RESULT:' + JSON.stringify({s1: artifactSignature(card1), s2: artifactSignature(card2)}));
"""
        out = self.run_sig(body)
        self.assertNotEqual(out["s1"], out["s2"],
                            "a phase-only change must invalidate the signature")

    def test_single_output_returns_empty_signature(self):
        body = "console.log('RESULT:' + JSON.stringify({s: artifactSignature({artifacts:[{id:'a000',status:'done'}]})}));"
        out = self.run_sig(body)
        self.assertEqual(out["s"], "", "single-output must not use the artifact fast path")

    def test_save_error_in_signature(self):
        body = """
const mk = (saveError) => ({ artifacts: [
  {id:'a000',status:'done',phase:'done',percent:100,saved:false,saving:false,saveError:null},
  {id:'a001',status:'done',phase:'done',percent:100,saved:false,saving:false,saveError:saveError}
]});
console.log('RESULT:' + JSON.stringify({s1: artifactSignature(mk(null)), s2: artifactSignature(mk('disk full'))}));
"""
        out = self.run_sig(body)
        self.assertNotEqual(out["s1"], out["s2"], "saveError must be in the signature")


class BackendPhasePropagationTests(unittest.TestCase):
    """check_status must mirror every active artifact phase onto the parent."""

    def setUp(self):
        app.jobs.clear()
        app.processes.clear()
        self.addCleanup(app.jobs.clear)
        self.addCleanup(app.processes.clear)

    def _make_parent_with_artifact(self, artifact_phase, artifact_status="downloading",
                                   parent_status="downloading", parent_phase="starting"):
        job = {
            "id": "jobX", "status": parent_status, "phase": parent_phase,
            "title": "T", "artifacts": [{
                "id": "a000", "type": "video", "status": artifact_status,
                "phase": artifact_phase, "percent": 50.0, "downloaded_bytes": 100,
                "total_bytes": 200, "speed": 10, "eta": 10, "attempt": 1,
                "max_attempts": 3, "filename": None, "label": "MP4 22",
                "format_id": "22",
            }],
            "current_artifact_id": "a000",
            "started_at": 1,
            "error": None,
        }
        app.jobs["jobX"] = job
        return job

    def _status_payload(self):
        client = app.app.test_client()
        resp = client.get("/api/status/jobX")
        return resp.get_json()

    def test_starting_phase_mirrors_onto_parent(self):
        self._make_parent_with_artifact("starting")
        payload = self._status_payload()
        self.assertEqual(payload["phase"], "starting",
                         "the old code left the parent stuck at its spawn-time phase")

    def test_downloading_phase_mirrors_onto_parent(self):
        self._make_parent_with_artifact("downloading")
        payload = self._status_payload()
        self.assertEqual(payload["phase"], "downloading",
                         "downloading was the headline bug: parent stayed 'starting'")

    def test_retrying_phase_mirrors_onto_parent(self):
        self._make_parent_with_artifact("retrying")
        payload = self._status_payload()
        self.assertEqual(payload["phase"], "retrying")

    def test_processing_phase_mirrors_onto_parent(self):
        self._make_parent_with_artifact("processing")
        payload = self._status_payload()
        self.assertEqual(payload["phase"], "processing")

    def test_postprocessing_phase_mirrors_onto_parent(self):
        self._make_parent_with_artifact("postprocessing")
        payload = self._status_payload()
        self.assertEqual(payload["phase"], "postprocessing",
                         "postprocessing is accepted for forward compatibility")

    def test_terminal_parent_phase_is_not_overwritten(self):
        """A terminal parent (partial) with a live-phase artifact must keep its
        terminal phase - a stale artifact phase must not resurrect it."""
        self._make_parent_with_artifact(
            "downloading", parent_status="partial", parent_phase="partial")
        payload = self._status_payload()
        self.assertEqual(payload["phase"], "partial",
                         "terminal parent phases must never be overwritten")

    def test_aggregate_values_remain_backend_authoritative(self):
        self._make_parent_with_artifact("downloading")
        payload = self._status_payload()
        self.assertEqual(payload["percent"], 50.0)
        self.assertEqual(payload["downloaded_bytes"], 100)
        self.assertEqual(payload["total_bytes"], 200)
        self.assertEqual(payload["speed"], 10)
        self.assertEqual(payload["eta"], 10)
        self.assertEqual(payload["attempt"], 1)
        self.assertEqual(payload["max_attempts"], 3)
        self.assertEqual(payload["current_artifact_id"], "a000")

    def test_single_output_response_keeps_legacy_shape(self):
        """A single-artifact parent must surface the legacy flat fields and
        report exactly one artifact. The server always emits the artifacts
        array once planning runs; the frontend uses length>1 as the
        multi-output discriminator, so a one-element array keeps the legacy
        single-output UI."""
        self._make_parent_with_artifact("downloading")
        client = app.app.test_client()
        resp = client.get("/api/status/jobX")
        payload = resp.get_json()
        # Legacy flat fields stay present and authoritative.
        for field in ("status", "phase", "percent", "downloaded_bytes",
                      "total_bytes", "speed", "eta", "attempt", "max_attempts",
                      "filename", "current_artifact_id"):
            self.assertIn(field, payload, f"legacy field {field} must remain")
        self.assertEqual(payload["phase"], "downloading")
        self.assertEqual(payload["status"], "downloading")
        # Exactly one artifact; the frontend treats length<=1 as single-output.
        self.assertEqual(len(payload.get("artifacts", [])), 1)


if __name__ == "__main__":
    unittest.main()
