"""Frontend regression tests for packaging progress and for launch de-duplication.

Two user-visible defects are locked down here, both by executing the real
JavaScript from templates/index.html in Node -- never by reading the source and
hoping.

  1. While ffmpeg repackaged a finished download, the progress row froze. The
     phase read ``Packaging file`` (or nothing at all) and the size column read
     ``Waiting for data`` for the whole packaging step, because that column was
     computed from ``totalBytes``/``downloadedBytes`` -- byte counters that
     describe the download that had *already finished* and are therefore empty
     during packaging. The artifact rows were worse: every packaging artifact
     rendered the constant string ``Processing…`` with no bar, no position and
     no speed, so a twenty-minute MP3 extraction looked identical to a hung one.
     The fix routes the packaging phase through its own branch fed by real
     ffmpeg ``-progress`` output (``processed_seconds`` / ``duration_seconds`` /
     ``processing_speed``). ``'Waiting for data'`` is the positive oracle for
     these tests: it is exactly what the old code returned, so any test that
     asserts its absence fails on the old behavior and passes on the new one.

  2. A download could be launched twice for one user action -- a fast double
     click on Download, a Download All pass that overlapped an in-flight card,
     or a transport-level retry of the POST. Each duplicate burned a worker slot
     and wrote a second file. The fix mints one ``client_request_id`` per
     intentional launch and tags it with a ``launch_source``, so the server can
     recognize a repeat and answer with the job it already created. These tests
     run the real ``dlCard`` against a counting ``fetch`` stub and assert on how
     many POSTs actually left the page and what they carried.

Section C proves an *absence* -- that nothing auto-launches a download when the
page is reloaded or restored from the back/forward cache. Absence is asserted
twice over: once behaviorally (the whole inline script block is evaluated in
Node under a DOM stub, every page-lifecycle handler it registered is fired, and
the POST counter must still read zero) and once structurally (every ``dlCard(``
call site in the template is located by offset and matched against the two
known ones). The behavioral half is the load-bearing one; it was verified to
catch a regression by re-running it with a ``window.addEventListener('load',
() => dlCard(0, 'single-card'))`` line appended, which drives the counter to 1.

Which code is real and which is stubbed, for the ``dlCard`` tests:

  REAL (extracted from the template and executed):
      dlCard, newClientRequestId, isActiveStatus, isStaleLaunch
  STUBBED (collaborators outside the scope of a launch-count assertion):
      fetch (records url/method/parsed body, answers 200 with a job id),
      renderCard, updateManifest, settleIdleActivity, cancelJobOnServer,
      pollCard, alert, and a ``document.documentElement.dataset`` holder.

``dlCard`` itself is never stubbed; if it were, these tests would prove nothing.
"""
import json
import re
import shutil
import subprocess
import unittest

from tests.test_frontend_output_selection import (
    HARNESS_PRELUDE,
    build_card,
    function_source,
    icon_constants,
    read_template,
)


NODE = shutil.which("node")

# Exactly the string the pre-fix size column produced during packaging, because
# the byte counters it read from are empty once the download has finished.
STALE_SIZE_TEXT = "Waiting for data"

# The id format the server validates with CLIENT_REQUEST_ID_PATTERN.
CLIENT_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


def run_node(script):
    """Execute `script` in Node and return the CompletedProcess."""
    return subprocess.run(
        [NODE, "-"],
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )


def script_blocks(template):
    """Contents of every <script>...</script> block in the template."""
    blocks = re.findall(r"<script[^>]*>(.*?)</script>", template, re.DOTALL)
    if not blocks:
        raise AssertionError("no <script> block found in template")
    return blocks


def main_script(template):
    """The application's inline script block: the longest one, which is the
    module holding cardData, dlCard, the poll loop and every event binding."""
    return max(script_blocks(template), key=len)


# ---------------------------------------------------------------------------
# Section A -- packaging progress UI
# ---------------------------------------------------------------------------

# The packaging helpers are pure functions of their arguments, so they need no
# DOM at all; they are pulled straight out of the template and called directly.
PACKAGING_FUNCTIONS = (
    "fmtBytes",
    "fmtEta",
    "fmtClock",
    "currentArtifactType",
    "packagingProcessedText",
    "packagingPhaseLabel",
    "progressParts",
    "artifactDisplayState",
)


def packaging_script(body):
    template = read_template()
    return "\n".join(
        [function_source(template, name) for name in PACKAGING_FUNCTIONS] + [body]
    )


@unittest.skipIf(NODE is None, "node is required to execute the packaging helpers")
class PackagingProgressBehaviorTests(unittest.TestCase):
    """Runs the real fmtClock / packagingProcessedText / packagingPhaseLabel /
    progressParts / artifactDisplayState / currentArtifactType in Node."""

    def evaluate(self, expression_body):
        """Run `expression_body` (which must console.log one JSON value) against
        the real packaging helpers and return the parsed result."""
        result = run_node(packaging_script(expression_body))
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    # -- fmtClock -----------------------------------------------------------

    def test_fmt_clock_reads_a_media_position_not_a_duration(self):
        # A packaging row shows *where ffmpeg is in the file*, so the value has
        # to read as a clock ("42:10"), not as fmtEta's duration phrasing
        # ("42m 10s"). Hours only appear once they exist, and minutes/seconds
        # are zero-padded so the column does not jitter as it counts.
        data = self.evaluate(
            "console.log(JSON.stringify({"
            "  zero: fmtClock(0),"
            "  small: fmtClock(130),"
            "  minutes: fmtClock(2530),"
            "  hours: fmtClock(4991),"
            "  fractional: fmtClock(33.9),"
            "  negative: fmtClock(-1),"
            "  nan: fmtClock(NaN),"
            "  infinite: fmtClock(Infinity),"
            "  nul: fmtClock(null),"
            "  undef: fmtClock(undefined),"
            "  text: fmtClock('nope'),"
            "}));"
        )
        self.assertEqual(data["zero"], "0:00")
        self.assertEqual(data["small"], "2:10")
        self.assertEqual(data["minutes"], "42:10")
        self.assertEqual(data["hours"], "1:23:11")
        # Truncated, not rounded: a position must never read ahead of itself.
        self.assertEqual(data["fractional"], "0:33")
        # Anything that is not a real, finite, non-negative number renders
        # nothing rather than "NaN:NaN".
        self.assertEqual(data["negative"], "")
        self.assertEqual(data["nan"], "")
        self.assertEqual(data["infinite"], "")
        self.assertEqual(data["text"], "")
        # null coerces to 0 through Number(); undefined does not. Both are
        # recorded so a future change to the guard is visible here.
        self.assertEqual(data["nul"], "0:00")
        self.assertEqual(data["undef"], "")

    # -- packagingProcessedText --------------------------------------------

    def test_processed_text_pairs_the_position_with_the_total(self):
        data = self.evaluate(
            "console.log(JSON.stringify({"
            "  full: packagingProcessedText({ processedSeconds: 2530, durationSeconds: 4991 }),"
            "  atStart: packagingProcessedText({ processedSeconds: 0, durationSeconds: 83 }),"
            "}));"
        )
        self.assertEqual(data["full"], "42:10 / 1:23:11 processed")
        # Zero processed is real progress information ("we have started"), not a
        # missing value, so it must render.
        self.assertEqual(data["atStart"], "0:00 / 1:23 processed")

    def test_processed_text_refuses_to_render_without_an_honest_total(self):
        # Half a fraction is not progress a user can read, and inventing the
        # missing half would be worse than showing nothing. ffmpeg genuinely
        # omits Duration for some inputs, so this is a real state.
        data = self.evaluate(
            "console.log(JSON.stringify({"
            "  noDuration: packagingProcessedText({ processedSeconds: 2530 }),"
            "  nullDuration: packagingProcessedText({ processedSeconds: 2530, durationSeconds: null }),"
            "  zeroDuration: packagingProcessedText({ processedSeconds: 10, durationSeconds: 0 }),"
            "  negativeDuration: packagingProcessedText({ processedSeconds: 10, durationSeconds: -5 }),"
            "  negativeProcessed: packagingProcessedText({ processedSeconds: -5, durationSeconds: 83 }),"
            "  nanProcessed: packagingProcessedText({ processedSeconds: NaN, durationSeconds: 83 }),"
            "  noSource: packagingProcessedText(null),"
            "  emptySource: packagingProcessedText({}),"
            "}));"
        )
        for key in (
            "noDuration", "nullDuration", "zeroDuration", "negativeDuration",
            "negativeProcessed", "nanProcessed", "noSource", "emptySource",
        ):
            with self.subTest(case=key):
                self.assertEqual(data[key], "")

    def test_processed_position_is_clamped_to_the_declared_duration(self):
        # ffmpeg's final out_time can overshoot the Duration it announced (it
        # rounds, and it flushes the encoder past the last input frame). The row
        # must read "1:23 / 1:23", never "8:20 / 1:23", which would look like a
        # bug in the app rather than in the arithmetic.
        data = self.evaluate(
            "console.log(JSON.stringify({"
            "  over: packagingProcessedText({ processedSeconds: 500, durationSeconds: 83 }),"
            "  exact: packagingProcessedText({ processedSeconds: 83, durationSeconds: 83 }),"
            "}));"
        )
        self.assertEqual(data["over"], "1:23 / 1:23 processed")
        self.assertEqual(data["exact"], "1:23 / 1:23 processed")

    # -- packagingPhaseLabel ------------------------------------------------

    def test_phase_label_names_the_audio_extraction_step(self):
        data = self.evaluate(
            "console.log(JSON.stringify({"
            "  artifactAudio: packagingPhaseLabel({ type: 'audio' }),"
            "  cardAudio: packagingPhaseLabel({ packagingType: 'audio' }),"
            "  video: packagingPhaseLabel({ type: 'video' }),"
            "  unknown: packagingPhaseLabel({}),"
            "  nothing: packagingPhaseLabel(null),"
            "}));"
        )
        # An artifact carries `type`; a card carries `packagingType` (copied
        # from the payload's current artifact). Both must resolve to the audio
        # wording, because the MP3 extraction is the step users actually wait on.
        self.assertEqual(data["artifactAudio"], "Processing audio")
        self.assertEqual(data["cardAudio"], "Processing audio")
        # Everything else keeps the generic label.
        self.assertEqual(data["video"], "Packaging file")
        self.assertEqual(data["unknown"], "Packaging file")
        self.assertEqual(data["nothing"], "Packaging file")

    # -- progressParts, packaging branch ------------------------------------

    def test_packaging_row_shows_real_position_speed_and_eta(self):
        card = {
            "status": "downloading",
            "phase": "processing",
            "percent": 40,
            "processedSeconds": 33.2,
            "durationSeconds": 83,
            "processingSpeed": 12.3,
            "eta": 4,
        }
        parts = self.evaluate(
            f"console.log(JSON.stringify(progressParts({json.dumps(card)})));"
        )
        self.assertIn(parts["phase"], ("Packaging file", "Processing audio"))
        self.assertIn("processed", parts["size"])
        self.assertIn("0:33 / 1:23", parts["size"])
        self.assertIn("12.3×", parts["size"])
        self.assertIn("left", parts["eta"])
        self.assertEqual(parts["eta"], "4s left")
        self.assertTrue(parts["hasPercent"])
        self.assertEqual(parts["percent"], 40)
        # THE oracle for defect 1: the old code fell through to the byte-count
        # branch, where totalBytes and downloadedBytes are both empty during
        # packaging, and printed exactly this.
        self.assertNotIn(STALE_SIZE_TEXT, parts["size"])

    def test_packaging_row_without_a_duration_is_honestly_indeterminate(self):
        # No duration means no honest percentage, so the bar goes indeterminate
        # and the row says it is working -- it must not fabricate a number and
        # it must not fall back to the download's byte text.
        card = {
            "status": "downloading",
            "phase": "processing",
            "percent": None,
            "processedSeconds": None,
            "durationSeconds": None,
        }
        parts = self.evaluate(
            f"console.log(JSON.stringify(progressParts({json.dumps(card)})));"
        )
        self.assertFalse(parts["hasPercent"])
        self.assertNotIn(STALE_SIZE_TEXT, parts["size"])
        # No invented position, ratio or percentage anywhere in the size column.
        self.assertNotIn("processed", parts["size"])
        self.assertNotIn("/", parts["size"])
        self.assertNotRegex(parts["size"], r"\d")
        # It still reads as an active, named state rather than as a blank cell.
        self.assertTrue(parts["size"].strip())
        self.assertIn("Processing", parts["size"])
        # No ETA can be computed without a total, so none is claimed.
        self.assertEqual(parts["eta"], "")

    def test_packaging_row_names_the_step_from_the_current_artifact_type(self):
        audio = self.evaluate(
            "console.log(JSON.stringify(progressParts("
            "  { status: 'downloading', phase: 'processing', percent: 10,"
            "    processedSeconds: 1, durationSeconds: 2, packagingType: 'audio' }"
            ")));"
        )
        self.assertEqual(audio["phase"], "Processing audio")

        generic = self.evaluate(
            "console.log(JSON.stringify(progressParts("
            "  { status: 'downloading', phase: 'processing', percent: 10,"
            "    processedSeconds: 1, durationSeconds: 2 }"
            ")));"
        )
        self.assertEqual(generic["phase"], "Packaging file")

    def test_yt_dlp_postprocessing_phase_uses_the_same_branch(self):
        # yt-dlp's own merge/postprocess step reports `postprocessing`; it must
        # not drop back into the byte-count branch either.
        parts = self.evaluate(
            "console.log(JSON.stringify(progressParts("
            "  { status: 'downloading', phase: 'postprocessing', percent: 10,"
            "    processedSeconds: 1, durationSeconds: 2 }"
            ")));"
        )
        self.assertEqual(parts["phase"], "Packaging file")
        self.assertIn("processed", parts["size"])
        self.assertNotIn(STALE_SIZE_TEXT, parts["size"])

    def test_speed_is_only_shown_when_it_is_a_real_multiplier(self):
        data = self.evaluate(
            "const base = { status: 'downloading', phase: 'processing', percent: 5,"
            "               processedSeconds: 1, durationSeconds: 2 };\n"
            "console.log(JSON.stringify({"
            "  fast: progressParts({ ...base, processingSpeed: 12.3 }).size,"
            "  zero: progressParts({ ...base, processingSpeed: 0 }).size,"
            "  negative: progressParts({ ...base, processingSpeed: -2 }).size,"
            "  missing: progressParts(base).size,"
            "  nan: progressParts({ ...base, processingSpeed: NaN }).size,"
            "}));"
        )
        self.assertIn("12.3×", data["fast"])
        for key in ("zero", "negative", "missing", "nan"):
            with self.subTest(case=key):
                self.assertNotIn("×", data[key])
                # And the position half survives regardless.
                self.assertIn("0:01 / 0:02 processed", data[key])

    def test_downloading_branch_still_reports_waiting_for_data(self):
        # Non-regression. "Waiting for data" is correct *for a download that has
        # not yet reported bytes*; the fix must only have removed it from the
        # packaging phase, not from the download phase.
        parts = self.evaluate(
            "console.log(JSON.stringify(progressParts("
            "  { status: 'downloading', phase: 'downloading', percent: 12 }"
            ")));"
        )
        self.assertEqual(parts["phase"], "Downloading")
        self.assertEqual(parts["size"], STALE_SIZE_TEXT)
        self.assertEqual(parts["eta"], "Estimating")
        self.assertTrue(parts["hasPercent"])
        self.assertEqual(parts["percent"], 12)

    def test_queued_branch_is_untouched_by_the_packaging_change(self):
        parts = self.evaluate(
            "console.log(JSON.stringify(progressParts("
            "  { status: 'queued', queuePosition: 3, phase: 'processing' }"
            ")));"
        )
        # status wins over phase for a queued card: it has not started yet, so
        # the packaging branch must not claim it.
        self.assertEqual(parts["phase"], "Queued — #3 in line")
        self.assertEqual(parts["size"], "Waiting for a worker")
        self.assertFalse(parts["hasPercent"])

    # -- artifactDisplayState, packaging branch -----------------------------

    def test_artifact_row_reports_position_and_speed_while_packaging(self):
        art = {
            "status": "downloading",
            "phase": "processing",
            "processed_seconds": 33.2,
            "duration_seconds": 83,
            "processing_speed": 12.3,
            "type": "audio",
        }
        state = self.evaluate(
            f"console.log(JSON.stringify(artifactDisplayState({json.dumps(art)})));"
        )
        self.assertIn("processed", state["text"])
        self.assertIn("0:33 / 1:23", state["text"])
        self.assertIn("12.3×", state["text"])
        self.assertIn("Processing audio", state["text"])
        # Positive oracle for the artifact half of defect 1: the old branch
        # returned the constant 'Processing…' with showProgress false, so a real
        # position, a speed, and a visible bar are all new behavior.
        self.assertNotEqual(state["text"], "Processing…")
        self.assertTrue(state["showProgress"])
        self.assertEqual(state["cls"], "downloading")
        self.assertFalse(state["canSave"])

    def test_artifact_row_stays_textual_when_the_duration_is_unknown(self):
        art = {
            "status": "downloading",
            "phase": "processing",
            "processed_seconds": 33.2,
            "processing_speed": 12.3,
            "type": "audio",
        }
        state = self.evaluate(
            f"console.log(JSON.stringify(artifactDisplayState({json.dumps(art)})));"
        )
        # Without a total there is no honest bar, so the row is a label only.
        self.assertTrue(state["text"].endswith("…"))
        self.assertFalse(state["showProgress"])
        self.assertNotIn("processed", state["text"])
        self.assertNotIn("/", state["text"])
        self.assertEqual(state["cls"], "downloading")
        self.assertFalse(state["canSave"])

    def test_artifact_row_labels_a_video_package_generically(self):
        art = {
            "status": "downloading",
            "phase": "processing",
            "processed_seconds": 5,
            "duration_seconds": 10,
            "type": "video",
        }
        state = self.evaluate(
            f"console.log(JSON.stringify(artifactDisplayState({json.dumps(art)})));"
        )
        self.assertIn("Packaging file", state["text"])
        self.assertIn("0:05 / 0:10 processed", state["text"])
        self.assertTrue(state["showProgress"])

    def test_terminal_artifact_states_are_unaffected(self):
        # Non-regression: the packaging branch sits below the terminal checks,
        # so a done/failed artifact must never be described as packaging even if
        # a stale phase field is still attached to it.
        data = self.evaluate(
            "console.log(JSON.stringify({"
            "  done: artifactDisplayState({ status: 'done', phase: 'processing' }),"
            "  error: artifactDisplayState({ status: 'error', phase: 'processing' }),"
            "  cancelled: artifactDisplayState({ status: 'cancelled', phase: 'processing' }),"
            "}));"
        )
        self.assertTrue(data["done"]["canSave"])
        self.assertEqual(data["done"]["cls"], "done")
        self.assertEqual(data["error"]["cls"], "error")
        self.assertEqual(data["cancelled"]["cls"], "cancelled")
        for key in ("done", "error", "cancelled"):
            with self.subTest(case=key):
                self.assertNotIn("processed", data[key]["text"])
                self.assertFalse(data[key]["showProgress"])

    # -- currentArtifactType ------------------------------------------------

    def test_current_artifact_type_resolves_the_running_artifact(self):
        data = self.evaluate(
            "console.log(JSON.stringify({"
            "  match: currentArtifactType({ artifacts: ["
            "    { id: 'a000', type: 'video' }, { id: 'a001', type: 'audio' }"
            "  ], current_artifact_id: 'a001' }),"
            "  first: currentArtifactType({ artifacts: ["
            "    { id: 'a000', type: 'video' }, { id: 'a001', type: 'audio' }"
            "  ], current_artifact_id: 'a000' }),"
            "  unknownId: currentArtifactType({ artifacts: ["
            "    { id: 'a000', type: 'video' }"
            "  ], current_artifact_id: 'nope' }),"
            "  noArtifacts: currentArtifactType({ current_artifact_id: 'a001' }),"
            "  artifactsNotArray: currentArtifactType({ artifacts: 'a001', current_artifact_id: 'a001' }),"
            "  noCurrentId: currentArtifactType({ artifacts: [{ id: 'a000', type: 'video' }] }),"
            "  noData: currentArtifactType(null),"
            "  holes: currentArtifactType({ artifacts: [null, { id: 'a001', type: 'audio' }],"
            "                               current_artifact_id: 'a001' }),"
            "}));"
        )
        self.assertEqual(data["match"], "audio")
        self.assertEqual(data["first"], "video")
        # Every unresolvable shape yields null so packagingPhaseLabel falls back
        # to the generic wording instead of mislabelling the step.
        for key in ("unknownId", "noArtifacts", "artifactsNotArray",
                    "noCurrentId", "noData"):
            with self.subTest(case=key):
                self.assertIsNone(data[key])
        # A null entry in the array must not throw on `.id`.
        self.assertEqual(data["holes"], "audio")


@unittest.skipIf(NODE is None, "node is required to execute renderCard")
class PackagingRowRenderingTests(unittest.TestCase):
    """The packaging text must actually reach the rendered artifact row, not
    just be computed correctly in isolation. Runs the real renderCard against
    the DOM stub from test_frontend_output_selection, with the real
    artifactDisplayState / packagingProcessedText / packagingPhaseLabel /
    fmtClock swapped in over the prelude's placeholder versions."""

    @classmethod
    def setUpClass(cls):
        cls.template = read_template()

    def render(self, card):
        script = "\n".join([
            HARNESS_PRELUDE,
            icon_constants(self.template),
            function_source(self.template, "esc"),
            function_source(self.template, "normalizeText"),
            function_source(self.template, "renderCard"),
            # Real implementations, declared after the prelude's stubs so the
            # function declarations win by hoisting order.
            function_source(self.template, "fmtClock"),
            function_source(self.template, "packagingProcessedText"),
            function_source(self.template, "packagingPhaseLabel"),
            function_source(self.template, "artifactDisplayState"),
            f"cardData = [{json.dumps(card)}];",
            "renderCard(0);\n"
            "process.stdout.write(document.getElementById('card-0').innerHTML);",
        ])
        result = run_node(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_a_packaging_artifact_row_renders_its_real_position(self):
        # The artifact list is a multi-output feature, so the card carries two
        # artifacts: the finished video and the audio ffmpeg is extracting.
        html = self.render(build_card(
            status="downloading",
            artifacts=[
                {"id": "a000", "type": "video", "label": "1080p", "status": "done"},
                {
                    "id": "a001", "type": "audio", "label": "Audio only",
                    "status": "downloading", "phase": "processing",
                    "processed_seconds": 33.2, "duration_seconds": 83,
                    "processing_speed": 12.3,
                },
            ],
        ))
        self.assertIn("artifact-list", html)
        self.assertIn("0:33 / 1:23 processed", html)
        self.assertIn("Processing audio", html)
        self.assertIn("12.3×", html)
        # The frozen constant the old code rendered instead.
        self.assertNotIn(">Processing…<", html)
        # showProgress is true, so the row also grew a real bar.
        self.assertIn("artifact-progress", html)

    def test_a_packaging_artifact_without_a_duration_renders_no_fake_numbers(self):
        html = self.render(build_card(
            status="downloading",
            artifacts=[
                {"id": "a000", "type": "video", "label": "1080p", "status": "done"},
                {
                    "id": "a001", "type": "audio", "label": "Audio only",
                    "status": "downloading", "phase": "processing",
                },
            ],
        ))
        self.assertIn("artifact-list", html)
        self.assertIn("Processing audio…", html)
        self.assertNotIn("processed", html)
        self.assertNotIn("NaN", html)
        # No honest percentage means no bar on that row at all.
        self.assertNotIn("artifact-progress", html)


# ---------------------------------------------------------------------------
# Section B -- launch de-duplication
# ---------------------------------------------------------------------------

# Everything dlCard touches that is not dlCard itself. fetch records the method,
# url and parsed body of every call so the tests can count POSTs to
# /api/download and inspect what each one carried. Nothing here re-implements
# any part of the launch decision: the guard, the id minting, the body shape and
# the launch_source mapping all come from the real extracted functions.
LAUNCH_PRELUDE = r"""
'use strict';
const requests = [];
const alerts = [];
let downloadLaunchCounter = 0;
let cardData = [];
let nextJobId = 0;

function alert(message) { alerts.push(String(message)); }

// dlCard flips document.documentElement.dataset.activity; a plain object is
// enough to observe that without pulling in a DOM.
const document = { documentElement: { dataset: {} } };

// Collaborators that run after the launch decision has already been made.
function renderCard() {}
function updateManifest() {}
function settleIdleActivity() {}
function cancelJobOnServer() {}
function pollCard() {}

function fetch(url, init) {
  const method = (init && init.method) || 'GET';
  let body = null;
  if (init && typeof init.body === 'string') body = JSON.parse(init.body);
  requests.push({ url: String(url), method, body });
  return Promise.resolve({
    ok: true,
    json: () => Promise.resolve({ job_id: `job-${++nextJobId}` }),
  });
}

function downloadPosts() {
  return requests.filter(r => r.url === '/api/download' && r.method === 'POST');
}

function readyCard(overrides) {
  return Object.assign({
    url: 'https://example.test/watch?v=abc',
    status: 'ready',
    title: 'Clip',
    selectedOutputs: [{ type: 'video', format_id: '137' }, { type: 'audio' }],
  }, overrides || {});
}

function report(value) { console.log(JSON.stringify(value)); }
"""

LAUNCH_FUNCTIONS = ("isActiveStatus", "isStaleLaunch", "newClientRequestId", "dlCard")


def launch_script(body, prelude_extra=""):
    template = read_template()
    return "\n".join(
        [LAUNCH_PRELUDE, prelude_extra]
        + [function_source(template, name) for name in LAUNCH_FUNCTIONS]
        + [body]
    )


@unittest.skipIf(NODE is None, "node is required to execute dlCard")
class LaunchDeduplicationBehaviorTests(unittest.TestCase):
    """Runs the real dlCard against a counting fetch stub."""

    def run_launch(self, body, prelude_extra=""):
        result = run_node(launch_script(body, prelude_extra))
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def assert_valid_id(self, value):
        self.assertIsInstance(value, str)
        self.assertRegex(value, CLIENT_REQUEST_ID_RE)

    def test_one_click_sends_exactly_one_tagged_post(self):
        data = self.run_launch(
            "(async () => {\n"
            "  cardData = [readyCard()];\n"
            "  await dlCard(0, 'single-card');\n"
            "  report({ posts: downloadPosts(), all: requests.map(r => r.method + ' ' + r.url) });\n"
            "})();"
        )
        self.assertEqual(len(data["posts"]), 1, data["all"])
        post = data["posts"][0]
        self.assertEqual(post["url"], "/api/download")
        self.assertEqual(post["method"], "POST")
        self.assertEqual(post["body"]["launch_source"], "single-card")
        self.assert_valid_id(post["body"]["client_request_id"])
        # The pre-existing payload fields must still be there: the new keys are
        # additive, not a replacement.
        self.assertEqual(post["body"]["url"], "https://example.test/watch?v=abc")
        self.assertEqual(post["body"]["title"], "Clip")

    def test_a_fast_double_click_still_sends_one_post(self):
        # The second call is made without awaiting the first, which is exactly
        # what a double click produces. dlCard sets status to 'downloading'
        # synchronously, before its first await, so the isActiveStatus guard
        # rejects the second entry.
        data = self.run_launch(
            "(async () => {\n"
            "  cardData = [readyCard()];\n"
            "  const first = dlCard(0, 'single-card');\n"
            "  const second = dlCard(0, 'single-card');\n"
            "  await Promise.all([first, second]);\n"
            "  report({ posts: downloadPosts(), status: cardData[0].status, alerts });\n"
            "})();"
        )
        self.assertEqual(len(data["posts"]), 1)
        self.assertEqual(data["status"], "downloading")
        # The suppressed click is silent -- it is not an error the user caused.
        self.assertEqual(data["alerts"], [])

    def test_the_guard_covers_every_in_flight_status(self):
        data = self.run_launch(
            "(async () => {\n"
            "  const out = {};\n"
            "  for (const status of ['downloading', 'queued', 'cancelling']) {\n"
            "    requests.length = 0;\n"
            "    cardData = [readyCard({ status })];\n"
            "    await dlCard(0, 'single-card');\n"
            "    out[status] = downloadPosts().length;\n"
            "  }\n"
            "  report(out);\n"
            "})();"
        )
        for status in ("downloading", "queued", "cancelling"):
            with self.subTest(status=status):
                self.assertEqual(data[status], 0)

    def test_download_all_launches_each_ready_card_once_with_a_distinct_id(self):
        # The loop is the real handler's loop, transcribed: it walks cardData in
        # order and awaits only the cards whose status is 'ready'.
        data = self.run_launch(
            "(async () => {\n"
            "  cardData = [readyCard(), readyCard({ status: 'done' }), readyCard()];\n"
            "  for (let index = 0; index < cardData.length; index++) {\n"
            "    if (cardData[index].status === 'ready') await dlCard(index, 'download-all');\n"
            "  }\n"
            "  report({ posts: downloadPosts(), statuses: cardData.map(c => c.status) });\n"
            "})();"
        )
        posts = data["posts"]
        self.assertEqual(len(posts), 2)
        for post in posts:
            with self.subTest(id=post["body"]["client_request_id"]):
                self.assertEqual(post["body"]["launch_source"], "download-all")
                self.assert_valid_id(post["body"]["client_request_id"])
        ids = [p["body"]["client_request_id"] for p in posts]
        # Two separate downloads: sharing one id would make the server treat the
        # second card as a duplicate of the first and silently drop it.
        self.assertEqual(len(set(ids)), 2, ids)
        # The already-complete card was skipped, not restarted.
        self.assertEqual(data["statuses"][1], "done")

    def test_each_fresh_user_action_mints_a_new_id(self):
        # Relaunching the same card (e.g. after a failure) is a *different*
        # intentional action, so it must not reuse the previous id -- the server
        # would answer with the old job instead of starting a new one.
        data = self.run_launch(
            "(async () => {\n"
            "  cardData = [readyCard()];\n"
            "  await dlCard(0, 'single-card');\n"
            "  cardData[0].status = 'ready';\n"
            "  await dlCard(0, 'single-card');\n"
            "  report({ ids: downloadPosts().map(p => p.body.client_request_id) });\n"
            "})();"
        )
        self.assertEqual(len(data["ids"]), 2)
        for value in data["ids"]:
            self.assert_valid_id(value)
        self.assertNotEqual(data["ids"][0], data["ids"][1])

    def test_the_same_launch_carries_one_id_end_to_end(self):
        # One POST per launch, so exactly one id per launch: the id is minted
        # before the request body is built and is not regenerated on the way.
        data = self.run_launch(
            "(async () => {\n"
            "  cardData = [readyCard()];\n"
            "  await dlCard(0, 'single-card');\n"
            "  const bodies = downloadPosts().map(p => p.body);\n"
            "  report({ count: bodies.length, ids: bodies.map(b => b.client_request_id) });\n"
            "})();"
        )
        self.assertEqual(data["count"], 1)
        self.assertEqual(len(set(data["ids"])), 1)

    def test_an_unrecognised_launch_source_is_normalised_not_forwarded(self):
        # The server rejects a launch_source outside its allow-list with a 400,
        # so the client must never invent one; anything that is not
        # 'download-all' maps to 'single-card'.
        data = self.run_launch(
            "(async () => {\n"
            "  const out = {};\n"
            "  for (const source of ['download-all', 'single-card', 'weird', undefined]) {\n"
            "    requests.length = 0;\n"
            "    cardData = [readyCard()];\n"
            "    await dlCard(0, source);\n"
            "    out[String(source)] = downloadPosts()[0].body.launch_source;\n"
            "  }\n"
            "  report(out);\n"
            "})();"
        )
        self.assertEqual(data["download-all"], "download-all")
        self.assertEqual(data["single-card"], "single-card")
        self.assertEqual(data["weird"], "single-card")
        self.assertEqual(data["undefined"], "single-card")

    def test_a_card_with_no_selected_output_sends_nothing(self):
        data = self.run_launch(
            "(async () => {\n"
            "  cardData = [readyCard({ selectedOutputs: [] })];\n"
            "  await dlCard(0, 'single-card');\n"
            "  report({ posts: downloadPosts().length, alerts, status: cardData[0].status });\n"
            "})();"
        )
        self.assertEqual(data["posts"], 0)
        self.assertEqual(len(data["alerts"]), 1)
        # The card must not have been left stuck in a downloading state.
        self.assertEqual(data["status"], "ready")

    def test_every_post_body_keeps_the_id_within_the_servers_pattern(self):
        # 200 real launches, so the assertion covers the id source actually used
        # by this runtime rather than one sampled value.
        data = self.run_launch(
            "(async () => {\n"
            "  for (let i = 0; i < 200; i++) {\n"
            "    cardData = [readyCard()];\n"
            "    await dlCard(0, 'single-card');\n"
            "  }\n"
            "  report({ ids: downloadPosts().map(p => p.body.client_request_id) });\n"
            "})();"
        )
        self.assertEqual(len(data["ids"]), 200)
        self.assertEqual(len(set(data["ids"])), 200)
        for value in data["ids"]:
            self.assert_valid_id(value)


@unittest.skipIf(NODE is None, "node is required to execute newClientRequestId")
class ClientRequestIdTests(unittest.TestCase):
    """The id is the whole de-duplication key, so it has to be unique and it has
    to satisfy the server's CLIENT_REQUEST_ID_PATTERN on every code path --
    including the two fallbacks, which older or hardened browsers really do
    take (crypto.randomUUID is secure-context only)."""

    def generate(self, count=1000, prelude=""):
        template = read_template()
        script = "\n".join([
            prelude,
            function_source(template, "newClientRequestId"),
            f"console.log(JSON.stringify(Array.from({{ length: {count} }},"
            "  () => newClientRequestId())));",
        ])
        result = run_node(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def assert_all_valid_and_unique(self, ids, count):
        self.assertEqual(len(ids), count)
        self.assertEqual(len(set(ids)), count, "ids collided")
        for value in ids:
            self.assertIsInstance(value, str)
            self.assertRegex(value, CLIENT_REQUEST_ID_RE)

    def test_a_thousand_ids_are_unique_and_well_formed(self):
        self.assert_all_valid_and_unique(self.generate(1000), 1000)

    def test_the_getrandomvalues_fallback_still_produces_valid_ids(self):
        # crypto.randomUUID is unavailable outside a secure context, which is
        # exactly where a locally served page can end up.
        ids = self.generate(1000, prelude=(
            "const crypto = { getRandomValues(array) {"
            "  for (let i = 0; i < array.length; i++) array[i] = Math.floor(Math.random() * 256);"
            "  return array;"
            "} };"
        ))
        self.assert_all_valid_and_unique(ids, 1000)
        # The hex fallback is a fixed 32 chars, comfortably inside the 8..128
        # the server accepts, and carries no separators to strip.
        for value in ids[:50]:
            self.assertEqual(len(value), 32)
            self.assertRegex(value, r"^[0-9a-f]{32}$")

    def test_the_math_random_fallback_still_produces_valid_ids(self):
        # No crypto object at all: the id is a de-duplication key, not a secret,
        # so a weaker source is acceptable -- but it must still be valid, and it
        # must still be long enough that collisions do not merge two downloads.
        ids = self.generate(1000, prelude="const crypto = undefined;")
        self.assert_all_valid_and_unique(ids, 1000)
        for value in ids[:50]:
            self.assertEqual(len(value), 32)

    def test_the_fallbacks_never_emit_a_character_the_server_rejects(self):
        # Math.random().toString(16) can yield '0.' and 'e-7' style fragments;
        # any leaked '.' or '+' would fail CLIENT_REQUEST_ID_PATTERN server-side
        # and turn every download into a 400.
        for label, prelude in (
            ("no-randomUUID", "const crypto = { getRandomValues(a) {"
                              " for (let i = 0; i < a.length; i++) a[i] = i * 7 % 256; return a; } };"),
            ("no-crypto", "const crypto = undefined;"),
        ):
            with self.subTest(path=label):
                ids = self.generate(200, prelude=prelude)
                for value in ids:
                    self.assertNotIn(".", value)
                    self.assertNotIn("+", value)
                    self.assertNotIn("e-", value)
                    self.assertRegex(value, CLIENT_REQUEST_ID_RE)


# ---------------------------------------------------------------------------
# Section C -- no auto-download on reload
# ---------------------------------------------------------------------------

# Page-lifecycle events that fire on a reload, on a back/forward-cache restore,
# or on a tab regaining visibility. A download started from any of these is a
# download the user never asked for.
LIFECYCLE_EVENTS = (
    "load", "pageshow", "DOMContentLoaded", "visibilitychange",
    "beforeunload", "unload", "pagehide", "focus", "popstate",
)

# A DOM stub broad enough to evaluate the whole inline script block. Every
# element is a permissive no-op, so top-level bindings and initialisation run to
# completion; fetch counts POSTs to /api/download and records every call.
PAGE_PRELUDE = r"""
'use strict';
globalThis.downloadPostCount = 0;
globalThis.fetchLog = [];
const __docHandlers = {};
const __winHandlers = {};

function makeEl() {
  return {
    style: {}, dataset: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    children: [], value: '', textContent: '', innerHTML: '',
    disabled: false, hidden: false, checked: false,
    id: '', className: '', tabIndex: 0, title: '',
    addEventListener() {}, removeEventListener() {},
    appendChild(child) { return child; }, removeChild() {},
    setAttribute() {}, getAttribute() { return null; },
    removeAttribute() {}, hasAttribute() { return false; },
    querySelector() { return makeEl(); }, querySelectorAll() { return []; },
    focus() {}, blur() {}, click() {}, closest() { return null; },
    contains() { return false; }, insertAdjacentHTML() {}, scrollIntoView() {},
  };
}

globalThis.document = {
  documentElement: makeEl(),
  body: makeEl(),
  readyState: 'complete',
  getElementById() { return makeEl(); },
  createElement() { return makeEl(); },
  querySelector() { return makeEl(); },
  querySelectorAll() { return []; },
  addEventListener(type, handler) { (__docHandlers[type] ||= []).push(handler); },
  removeEventListener() {},
};
globalThis.window = {
  addEventListener(type, handler) { (__winHandlers[type] ||= []).push(handler); },
  removeEventListener() {},
  matchMedia() {
    return { matches: false, addEventListener() {}, removeEventListener() {},
             addListener() {}, removeListener() {} };
  },
  location: { href: 'http://localhost/', origin: 'http://localhost' },
};
globalThis.__docHandlers = __docHandlers;
globalThis.__winHandlers = __winHandlers;
globalThis.matchMedia = globalThis.window.matchMedia;
globalThis.localStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
globalThis.alert = function () {};
globalThis.AbortController = class {
  constructor() { this.signal = { aborted: false, addEventListener() {} }; }
  abort() { this.signal.aborted = true; }
};
globalThis.fetch = function (url, init) {
  const method = (init && init.method) || 'GET';
  globalThis.fetchLog.push(method + ' ' + String(url));
  if (String(url) === '/api/download' && method === 'POST') globalThis.downloadPostCount++;
  return Promise.resolve({ ok: true, json: () => Promise.resolve({ job_id: 'job-1' }) });
};
"""

# Seed one ready card, fire every page-lifecycle handler the script registered,
# then -- as a positive control -- perform one real user launch. The card is
# seeded *before* the handlers run so an auto-launching handler has something to
# launch; a run that counts zero POSTs is therefore a run where a POST could
# genuinely have happened.
PAGE_EPILOGUE = """
function seedReadyCard() {
  cardData = [{ url: 'https://example.test/a', status: 'ready', title: 'A',
                selectedOutputs: [{ type: 'audio' }] }];
}
setTimeout(() => {
  seedReadyCard();
  const fired = [];
  for (const type of %(events)s) {
    for (const handler of (globalThis.__winHandlers[type] || [])) {
      fired.push('window:' + type);
      try { handler({ type, persisted: true }); } catch (_) {}
    }
    for (const handler of (globalThis.__docHandlers[type] || [])) {
      fired.push('document:' + type);
      try { handler({ type, persisted: true }); } catch (_) {}
    }
  }
  if (typeof globalThis.window.onload === 'function') {
    fired.push('window.onload');
    try { globalThis.window.onload({}); } catch (_) {}
  }
  if (typeof globalThis.document.onreadystatechange === 'function') {
    fired.push('document.onreadystatechange');
    try { globalThis.document.onreadystatechange({}); } catch (_) {}
  }
  setTimeout(() => {
    const afterLifecycle = globalThis.downloadPostCount;
    const bootLog = globalThis.fetchLog.slice();
    // Positive control: a deliberate launch must still reach the network.
    seedReadyCard();
    dlCard(0, 'single-card');
    setTimeout(() => {
      console.log(JSON.stringify({
        afterLifecycle,
        afterControl: globalThis.downloadPostCount,
        fired,
        bootLog,
        fetchLog: globalThis.fetchLog,
      }));
      process.exit(0);
    }, 20);
  }, 20);
}, 20);
""" % {"events": json.dumps(list(LIFECYCLE_EVENTS))}


@unittest.skipIf(NODE is None, "node is required to evaluate the page script")
class ReloadDoesNotAutoDownloadTests(unittest.TestCase):
    """Evaluates the template's entire inline script block in Node under a DOM
    stub, fires every page-lifecycle event a reload or bfcache restore would
    deliver, and asserts that not one POST to /api/download was made.

    The zero is only meaningful if a POST was possible, so each run ends with a
    deliberate dlCard call as a positive control; that call must move the same
    counter to 1. The suite was itself verified against a regression by
    appending ``window.addEventListener('load', () => dlCard(0, 'single-card'))``
    to the script under test, which makes the lifecycle count read 1."""

    def evaluate_page(self, extra_script=""):
        template = read_template()
        script = PAGE_PRELUDE + main_script(template) + extra_script + PAGE_EPILOGUE
        result = run_node(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_no_download_is_posted_while_the_page_initialises_or_reloads(self):
        data = self.evaluate_page()
        self.assertEqual(
            data["afterLifecycle"], 0,
            f"a page-lifecycle handler started a download; fetches: {data['fetchLog']}",
        )
        # Positive control: the counter is live and a real launch increments it.
        self.assertEqual(data["afterControl"], 1)

    def test_the_regression_this_guards_would_be_caught(self):
        # Mutation check, run against a copy of the script with an auto-launch
        # bolted on. If this ever stops failing the lifecycle assertion above is
        # no longer testing anything.
        data = self.evaluate_page(
            "\nwindow.addEventListener('load', () => { dlCard(0, 'single-card'); });\n"
        )
        self.assertIn("window:load", data["fired"])
        self.assertEqual(
            data["afterLifecycle"], 1,
            "the harness must be able to observe an auto-launch",
        )

    def test_page_initialisation_touches_only_read_only_endpoints(self):
        # Whatever the page does on boot must be safe to repeat on every reload,
        # so every call made before the positive control has to be a GET.
        data = self.evaluate_page()
        self.assertNotIn("POST /api/download", data["bootLog"])
        unsafe = [call for call in data["bootLog"] if not call.startswith("GET ")]
        self.assertEqual(unsafe, [], f"non-GET call during boot: {data['bootLog']}")
        # An empty bootLog would satisfy the assertion above without proving the
        # log observes boot at all, so require the call boot really makes.
        self.assertIn("GET /api/health", data["bootLog"])
        # The control POST that follows proves the log would have recorded one.
        self.assertIn("POST /api/download", data["fetchLog"])
        # ...and that it lands after boot, i.e. the split between bootLog and
        # fetchLog is the real one.
        self.assertNotIn("POST /api/download", data["bootLog"])


class DownloadCallSiteContractTests(unittest.TestCase):
    """Structural half of the absence proof: the two dlCard call sites and the
    single /api/download POST are located by offset in the template, so a third
    call site added anywhere -- including inside a lifecycle handler -- fails
    here even on a machine without Node."""

    @classmethod
    def setUpClass(cls):
        cls.template = read_template()
        cls.script = main_script(cls.template)

    def call_sites(self):
        """(offset, surrounding line) for every dlCard(...) invocation, with the
        function's own declaration excluded."""
        sites = []
        for match in re.finditer(r"(?<![\w.])dlCard\s*\(", self.script):
            start = self.script.rfind("\n", 0, match.start()) + 1
            end = self.script.find("\n", match.start())
            line = self.script[start: end if end != -1 else len(self.script)]
            if re.search(r"(?:async\s+)?function\s+dlCard\s*\(", line):
                continue
            sites.append((match.start(), line.strip()))
        return sites

    def test_dlcard_is_invoked_from_exactly_two_places(self):
        sites = self.call_sites()
        self.assertEqual(
            len(sites), 2,
            "dlCard must have exactly two call sites (the per-card Download "
            f"button and the Download All loop); found: {[s[1] for s in sites]}",
        )

    def test_the_first_call_site_is_the_per_card_download_button(self):
        offset, line = self.call_sites()[0]
        # Bound as a click handler on the card's [data-download] control.
        self.assertIn("[data-download]", line)
        self.assertIn("addEventListener('click'", line)
        self.assertIn("dlCard(index, 'single-card')", line)
        # And that binding lives inside renderCard, not at top level, so it can
        # only run in response to a click on a rendered card.
        render = function_source(self.template, "renderCard")
        self.assertIn(line, render)

    def test_the_second_call_site_is_the_download_all_loop(self):
        offset, line = self.call_sites()[1]
        self.assertIn("dlCard(index, 'download-all')", line)
        self.assertIn("cardData[index].status === 'ready'", line)
        self.assertIn("await", line)
        # It is the body of the Download All button's click handler.
        handler = re.search(
            r"getElementById\('downloadAllBtn'\)\.addEventListener\('click',(.*?)\n    \}\);",
            self.script,
            re.DOTALL,
        )
        self.assertIsNotNone(handler, "the Download All click handler must exist")
        self.assertIn(line, handler.group(1))

    @staticmethod
    def lifecycle_registrations(script):
        """(event, offset, source-window) for every page-lifecycle listener
        registered in `script`. The window is the 4000 characters following the
        registration, which covers any handler body in this template."""
        found = []
        for event in LIFECYCLE_EVENTS:
            for match in re.finditer(
                rf"addEventListener\(\s*['\"]{event}['\"]\s*,", script
            ):
                found.append(
                    (event, match.start(), script[match.start(): match.start() + 4000])
                )
        return found

    def test_the_lifecycle_scan_can_actually_find_a_launching_handler(self):
        # Positive control for the scan below. The template currently registers
        # no page-lifecycle listener at all, so that scan iterates zero times and
        # would pass whatever the template said -- including if LIFECYCLE_EVENTS
        # were emptied or the regex stopped matching. This pins the scanner
        # itself: a planted lifecycle handler that launches must be found.
        planted = (
            self.script
            + "\nwindow.addEventListener('load', () => { dlCard(0, 'single-card'); });\n"
        )
        found = self.lifecycle_registrations(planted)
        self.assertEqual([event for event, _, _ in found], ["load"])
        self.assertIn("dlCard(", found[0][2])
        # And the scan is not simply matching everything: the same scanner run
        # over the unplanted script must not report that registration.
        self.assertEqual(self.lifecycle_registrations(self.script), [])

    def test_no_page_lifecycle_handler_can_reach_a_download(self):
        # With the scanner proven above, every lifecycle listener the template
        # does register must be free of a launch. Today it registers none, which
        # is the strongest form of the guarantee; the moment one is added this
        # assertion goes live.
        for event, at, handler in self.lifecycle_registrations(self.script):
            with self.subTest(event=event, at=at):
                self.assertNotIn("dlCard(", handler)
                self.assertNotIn("'/api/download'", handler)

    def test_no_lifecycle_handler_is_assigned_through_an_on_property(self):
        # addEventListener is not the only way to register one; the property
        # form would slip past the scan above. Each pattern is checked against a
        # planted sample first, so a typo that made a regex unmatchable could
        # not turn this into an assertion that nothing can ever fail.
        for pattern, sample in (
            (r"window\.onload\s*=", "window.onload = () => dlCard(0);"),
            (r"window\.onpageshow\s*=", "window.onpageshow = boot;"),
            (r"document\.onreadystatechange\s*=", "document.onreadystatechange = boot;"),
            (r"document\.onvisibilitychange\s*=", "document.onvisibilitychange = boot;"),
            (r"window\.onfocus\s*=", "window.onfocus = boot;"),
            (r"<body[^>]*\bonload=", '<body class="app" onload="boot()">'),
        ):
            with self.subTest(pattern=pattern):
                self.assertIsNotNone(
                    re.search(pattern, sample),
                    f"{pattern} no longer matches the form it is meant to catch",
                )
                self.assertIsNone(
                    re.search(pattern, self.template),
                    f"{pattern} must not exist: it could auto-launch on reload",
                )

    def test_the_only_download_post_lives_inside_dlcard(self):
        # /api/download is POSTed exactly once in the whole template, and that
        # one call is inside dlCard -- so the guard, the id and the launch_source
        # cannot be bypassed by some other code path.
        dl = function_source(self.template, "dlCard")
        occurrences = [m.start() for m in re.finditer(r"fetch\('/api/download'", self.script)]
        self.assertEqual(
            len(occurrences), 1,
            "there must be exactly one fetch('/api/download') in the page",
        )
        self.assertIn("fetch('/api/download'", dl)
        self.assertIn("method: 'POST'", dl)
        # No other spelling of the endpoint sneaks a launch in.
        for spelling in ('fetch("/api/download"', "fetch(`/api/download`"):
            with self.subTest(spelling=spelling):
                self.assertNotIn(spelling, self.script)

    def test_the_cancel_endpoint_is_not_mistaken_for_a_launch(self):
        # /api/download/<job_id> DELETE is the cancel call; it shares a prefix
        # with the launch endpoint, so the assertion above is scoped to the
        # exact path. This records that the cancel call still exists and is
        # still a DELETE, so the scoping stays honest.
        cancel = function_source(self.template, "cancelJobOnServer")
        self.assertIn("fetch(`/api/download/${jobId}`", cancel)
        self.assertIn("method: 'DELETE'", cancel)

    def test_dlcard_mints_the_id_before_it_awaits_anything(self):
        # The id must be created once per call, synchronously, before the first
        # await -- otherwise two overlapping launches could interleave and share
        # or reorder ids.
        dl = function_source(self.template, "dlCard")
        mint = dl.index("newClientRequestId()")
        first_await = dl.index("await ")
        self.assertLess(mint, first_await)
        self.assertEqual(dl.count("newClientRequestId()"), 1)
        # And the guard runs before even that, so a suppressed click never
        # consumes an id.
        self.assertLess(dl.index("isActiveStatus(card.status)"), mint)


if __name__ == "__main__":
    unittest.main()
