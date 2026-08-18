"""Behavior tests for live packaging updates inside multi-output artifact rows.

The defect these lock down is invisible to every existing frontend test,
because those tests either call ``artifactDisplayState`` directly or render a
card exactly once. The bug lives in the *second* render:

``renderCard`` has a fast path. While a job is downloading and the artifact
signature is unchanged, it patches the parent progress bar via
``updateProgressWrap`` and returns without touching the artifact rows. The
signature truncated ``percent`` and ``processed_seconds`` with ``| 0`` and did
not carry ``duration_seconds`` or ``processing_speed`` at all -- yet the
packaging row renders all four. So the parent bar advanced while the Audio row
sat on a stale position, a stale speed and a stale bar width: exactly the
"looks frozen" symptom the packaging work set out to remove, moved one row down.

The harness here is deliberately heavier than the button-only stub in
test_frontend_output_selection: that stub answers ``null`` for
``.progress-wrap``, so the fast path can never be entered and the bug cannot
reproduce. This module parses the markup ``renderCard`` writes into a real
element tree (classes, attributes, dataset, style, text nodes) and runs the
shipped ``renderCard`` / ``updateProgressWrap`` / ``artifactSignature`` /
``artifactDisplayState`` against it, twice, the way two consecutive polls do.

Positive oracle: with the pre-fix template,
``test_speed_only_change_updates_the_artifact_row`` and
``test_sub_integer_percent_moves_the_artifact_bar`` fail on stale row text and
a stale bar width, and ``test_numeric_poll_patches_rows_without_replacing_them``
fails because the whole card is rebuilt. They pass once the numeric fields are
patched in place.
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

import app


NODE = shutil.which("node")

ROOT = Path(app.__file__).parent


def read_template():
    return (ROOT / "templates" / "index.html").read_text(encoding="utf-8")


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


def optional_function_source(template, name):
    """Same, but tolerates absence.

    The in-place patch helper does not exist in the pre-fix template. Returning
    '' instead of raising keeps the RED run meaningful: the tests then fail on
    the stale text and width they are actually about, rather than on a missing
    symbol.
    """
    try:
        return function_source(template, name)
    except AssertionError:
        return ""


def icon_constants(template):
    lines = re.findall(r"^    const (ICON_[A-Z_]+ = .*)$", template, re.MULTILINE)
    if not lines:
        raise AssertionError("no ICON_* constants found in template")
    return "\n".join("const " + line for line in lines)


# ---------------------------------------------------------------------------
# A small but real DOM: element tree, attributes, classes, dataset, style,
# text nodes, innerHTML round-tripping, and querySelector/querySelectorAll with
# class / tag / attribute / descendant selectors. Enough for renderCard's fast
# path to actually run, which the button-only stub cannot do.
# ---------------------------------------------------------------------------
DOM_PRELUDE = r"""
'use strict';

const VOID_TAGS = new Set(['img','br','hr','input','meta','link','path','circle',
  'rect','line','polyline','polygon','source','use','stop','ellipse','area','col']);

function escapeText(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function escapeAttr(s) {
  return String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
}
function unescapeHTML(s) {
  return String(s)
    .replace(/&lt;/g, '<').replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"').replace(/&#39;/g, "'")
    .replace(/&amp;/g, '&');
}
function camel(name) { return name.replace(/-([a-z])/g, (_, c) => c.toUpperCase()); }
function kebab(name) { return name.replace(/[A-Z]/g, m => '-' + m.toLowerCase()); }

function styleToString(style) {
  return Object.keys(style)
    .filter(k => style[k] !== '' && style[k] != null)
    .map(k => `${kebab(k)}: ${style[k]}`)
    .join('; ');
}

function makeElement(tag) {
  const el = {
    nodeType: 1,
    localName: String(tag).toLowerCase(),
    tagName: String(tag).toUpperCase(),
    attributes: Object.create(null),
    childNodes: [],
    parentNode: null,
    style: {},
    hidden: false,
    tabIndex: 0,
    _handlers: Object.create(null),
  };

  el.classList = {
    _list() { return (el.attributes['class'] || '').split(/\s+/).filter(Boolean); },
    contains(name) { return el.classList._list().includes(name); },
    add(...names) {
      const set = new Set(el.classList._list());
      names.forEach(n => set.add(n));
      el.attributes['class'] = [...set].join(' ');
    },
    remove(...names) {
      const set = new Set(el.classList._list());
      names.forEach(n => set.delete(n));
      el.attributes['class'] = [...set].join(' ');
    },
    toggle(name, force) {
      const want = force === undefined ? !el.classList.contains(name) : Boolean(force);
      if (want) el.classList.add(name); else el.classList.remove(name);
      return want;
    },
  };

  el.dataset = new Proxy(Object.create(null), {
    get(_, key) {
      if (typeof key !== 'string') return undefined;
      return el.attributes['data-' + kebab(key)];
    },
    set(_, key, value) { el.attributes['data-' + kebab(key)] = String(value); return true; },
    has(_, key) { return ('data-' + kebab(key)) in el.attributes; },
    deleteProperty(_, key) { delete el.attributes['data-' + kebab(key)]; return true; },
    ownKeys() {
      return Object.keys(el.attributes).filter(a => a.startsWith('data-')).map(a => camel(a.slice(5)));
    },
    getOwnPropertyDescriptor() { return { enumerable: true, configurable: true }; },
  });

  Object.defineProperty(el, 'id', {
    get() { return el.attributes.id || ''; },
    set(v) { el.attributes.id = String(v); },
  });
  Object.defineProperty(el, 'className', {
    get() { return el.attributes['class'] || ''; },
    set(v) { el.attributes['class'] = String(v); },
  });

  el.setAttribute = (name, value) => { el.attributes[name] = String(value); };
  el.getAttribute = name => (name in el.attributes ? el.attributes[name] : null);
  el.hasAttribute = name => name in el.attributes;
  el.removeAttribute = name => { delete el.attributes[name]; };

  el.appendChild = child => {
    child.parentNode = el;
    el.childNodes.push(child);
    if (child.nodeType === 1 && child.id) registry[child.id] = child;
    return child;
  };
  el.contains = other => {
    if (!other) return false;
    let node = other;
    while (node) { if (node === el) return true; node = node.parentNode; }
    return false;
  };
  el.addEventListener = (type, handler) => { (el._handlers[type] ||= []).push(handler); };
  el.click = () => { (el._handlers.click || []).forEach(h => h()); };
  el.focus = () => { document._active = el; };

  Object.defineProperty(el, 'innerHTML', {
    get() { return el.childNodes.map(serializeNode).join(''); },
    set(html) {
      el.childNodes = [];
      parseInto(el, String(html));
    },
  });
  Object.defineProperty(el, 'textContent', {
    get() { return textOf(el); },
    set(value) {
      el.childNodes = [{ nodeType: 3, data: value == null ? '' : String(value), parentNode: el }];
    },
  });

  el.querySelector = selector => queryAll(el, selector)[0] || null;
  el.querySelectorAll = selector => queryAll(el, selector);
  return el;
}

function textOf(node) {
  if (node.nodeType === 3) return node.data;
  return node.childNodes.map(textOf).join('');
}

function serializeNode(node) {
  if (node.nodeType === 3) return escapeText(node.data);
  const attrs = Object.assign(Object.create(null), node.attributes);
  const styleStr = styleToString(node.style);
  if (styleStr) attrs.style = styleStr; else delete attrs.style;
  const rendered = Object.keys(attrs).map(k => ` ${k}="${escapeAttr(attrs[k])}"`).join('');
  const inner = node.childNodes.map(serializeNode).join('');
  if (VOID_TAGS.has(node.localName) && !inner) return `<${node.localName}${rendered}/>`;
  return `<${node.localName}${rendered}>${inner}</${node.localName}>`;
}

const ATTR_RE = /([a-zA-Z_:][-\w:.]*)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+)))?/g;

function applyAttributes(el, raw) {
  let match;
  ATTR_RE.lastIndex = 0;
  while ((match = ATTR_RE.exec(raw)) !== null) {
    const name = match[1];
    const value = match[2] !== undefined ? match[2]
      : match[3] !== undefined ? match[3]
      : match[4] !== undefined ? match[4] : '';
    if (name === 'style') {
      unescapeHTML(value).split(';').forEach(decl => {
        const idx = decl.indexOf(':');
        if (idx === -1) return;
        const prop = camel(decl.slice(0, idx).trim());
        if (prop) el.style[prop] = decl.slice(idx + 1).trim();
      });
    } else {
      el.attributes[name] = unescapeHTML(value);
    }
  }
}

const NODE_RE = /<!--[\s\S]*?-->|<\/([a-zA-Z][\w:-]*)\s*>|<([a-zA-Z][\w:-]*)((?:[^>"']|"[^"]*"|'[^']*')*?)(\/?)>|([^<]+)/g;

function parseInto(root, html) {
  const stack = [root];
  let match;
  NODE_RE.lastIndex = 0;
  while ((match = NODE_RE.exec(html)) !== null) {
    const top = stack[stack.length - 1];
    if (match[1]) {
      const name = match[1].toLowerCase();
      for (let i = stack.length - 1; i > 0; i--) {
        if (stack[i].localName === name) { stack.length = i; break; }
      }
    } else if (match[2]) {
      const el = makeElement(match[2]);
      applyAttributes(el, match[3] || '');
      el.parentNode = top;
      top.childNodes.push(el);
      if (!VOID_TAGS.has(el.localName) && !match[4]) stack.push(el);
    } else if (match[5] !== undefined && match[5].length) {
      top.childNodes.push({ nodeType: 3, data: unescapeHTML(match[5]), parentNode: top });
    }
  }
}

const COMPOUND_RE = /(^[a-zA-Z][\w-]*)|(\.[\w-]+)|(#[\w-]+)|(\[[^\]]+\])/g;

function matchesCompound(el, compound) {
  const parts = compound.match(COMPOUND_RE) || [];
  for (const part of parts) {
    if (part.startsWith('.')) {
      if (!el.classList.contains(part.slice(1))) return false;
    } else if (part.startsWith('#')) {
      if (el.attributes.id !== part.slice(1)) return false;
    } else if (part.startsWith('[')) {
      const inner = part.slice(1, -1);
      const eq = inner.indexOf('=');
      if (eq === -1) {
        if (!(inner.trim() in el.attributes)) return false;
      } else {
        const name = inner.slice(0, eq).trim();
        let value = inner.slice(eq + 1).trim();
        if ((value.startsWith('"') && value.endsWith('"')) ||
            (value.startsWith("'") && value.endsWith("'"))) value = value.slice(1, -1);
        if (el.attributes[name] !== value) return false;
      }
    } else if (el.localName !== part.toLowerCase()) {
      return false;
    }
  }
  return true;
}

function matchesSelector(el, selector) {
  return selector.split(',').some(part => {
    const compounds = part.trim().split(/\s+/).filter(Boolean);
    if (!compounds.length) return false;
    if (!matchesCompound(el, compounds[compounds.length - 1])) return false;
    let node = el.parentNode;
    for (let i = compounds.length - 2; i >= 0; i--) {
      let found = false;
      while (node && node.nodeType === 1) {
        const candidate = node;
        node = node.parentNode;
        if (matchesCompound(candidate, compounds[i])) { found = true; break; }
      }
      if (!found) return false;
    }
    return true;
  });
}

function queryAll(root, selector) {
  const out = [];
  const walk = node => {
    node.childNodes.forEach(child => {
      if (child.nodeType !== 1) return;
      if (matchesSelector(child, selector)) out.push(child);
      walk(child);
    });
  };
  walk(root);
  return out;
}

const registry = Object.create(null);
const document = {
  createElement(tag) { return makeElement(tag); },
  getElementById(id) { return registry[id] || null; },
  get activeElement() { return document._active || null; },
  _active: null,
};
const cardsRoot = makeElement('div');
cardsRoot.id = 'cards';
registry['cards'] = cardsRoot;

// Collaborators outside the scope of these assertions.
let currentFormat = 'video';
let downloadDirectoryHandle = null;
let cardData = [];
const calls = { dlCard: 0, saveCard: 0, cancelCard: 0, saveArtifact: 0 };
function statusMatchesFilter() { return true; }
function dlCard() { calls.dlCard++; }
function saveCard() { calls.saveCard++; }
function cancelCard() { calls.cancelCard++; }
function saveArtifact() { calls.saveArtifact++; }

// What each test reads back: the rendered artifact rows and the parent bar.
function snapshot() {
  const el = document.getElementById('card-0');
  const rows = el.querySelectorAll('.artifact-item').map(row => {
    const status = row.querySelector('.artifact-status');
    const track = row.querySelector('.artifact-progress');
    const fill = row.querySelector('.artifact-progress-fill');
    return {
      artifactId: row.getAttribute('data-artifact-id'),
      label: (row.querySelector('.artifact-label') || {}).textContent || null,
      status: status ? status.textContent : null,
      hasBar: Boolean(track),
      width: fill ? (fill.style.width || null) : null,
      ariaValueNow: track ? track.getAttribute('aria-valuenow') : null,
      indeterminate: Boolean(fill && fill.classList.contains('indeterminate')),
      probe: row._probe || null,
      hasSave: Boolean(row.querySelector('[data-artifact-save]')),
    };
  });
  const wrap = el.querySelector('.progress-wrap');
  const parentSpans = wrap ? wrap.querySelectorAll('.progress-top span') : [];
  const parentFill = wrap ? wrap.querySelector('.progress-fill') : null;
  return {
    rows,
    parentPhase: parentSpans[0] ? parentSpans[0].textContent : null,
    parentPercent: parentSpans[1] ? parentSpans[1].textContent : null,
    parentWidth: parentFill ? (parentFill.style.width || null) : null,
    activeIsCancel: Boolean(document.activeElement &&
      document.activeElement.hasAttribute && document.activeElement.hasAttribute('data-cancel')),
    activeProbe: (document.activeElement && document.activeElement._probe) || null,
    calls,
  };
}

function emit(value) { process.stdout.write('RESULT:' + JSON.stringify(value) + '\n'); }
"""


def build_card(**overrides):
    """A two-output card: a finished video plus an audio output ffmpeg is packaging."""
    card = {
        "url": "https://example.test/watch?v=abc",
        "status": "downloading",
        "title": "Clip",
        "uploader": "Channel",
        "duration": 100,
        "thumbnail": "",
        "jobId": "job1",
        "phase": "processing",
        "percent": 50,
        "processedSeconds": 10,
        "durationSeconds": 100,
        "processingSpeed": 2.0,
        "packagingType": "audio",
        "selectedOutputs": [{"type": "video", "format_id": "137"}, {"type": "audio"}],
        "artifacts": [
            {
                "id": "a000", "type": "video", "label": "1080p", "format_id": "137",
                "status": "done", "phase": "done", "percent": 100,
                "saved": False, "saving": False, "saveError": None,
            },
            {
                "id": "a001", "type": "audio", "label": "Audio only",
                "status": "downloading", "phase": "processing",
                "percent": 50, "processed_seconds": 10, "duration_seconds": 100,
                "processing_speed": 2.0,
                "saved": False, "saving": False, "saveError": None,
            },
        ],
    }
    card.update(overrides)
    return card


def audio_poll(card, **fields):
    """The same card as the next poll would deliver it: only the audio
    artifact's live packaging numbers move, mirrored onto the parent."""
    updated = json.loads(json.dumps(card))
    audio = updated["artifacts"][1]
    audio.update(fields)
    updated["percent"] = audio.get("percent")
    updated["processedSeconds"] = audio.get("processed_seconds")
    updated["durationSeconds"] = audio.get("duration_seconds")
    updated["processingSpeed"] = audio.get("processing_speed")
    return updated


REAL_FUNCTIONS = (
    "esc",
    "normalizeText",
    "fmtDur",
    "fmtBytes",
    "fmtEta",
    "fmtClock",
    "friendlyError",
    "currentArtifactType",
    "packagingProcessedText",
    "packagingPhaseLabel",
    "progressParts",
    "updateProgressWrap",
    "artifactSignature",
    "artifactDisplayState",
    "renderCard",
)


@unittest.skipIf(NODE is None, "node is required to execute renderCard")
class ArtifactRowLivePatchTests(unittest.TestCase):
    """Two consecutive polls, run through the real renderCard against a real
    element tree, with the fast path reachable."""

    @classmethod
    def setUpClass(cls):
        cls.template = read_template()

    def run_js(self, body):
        parts = [DOM_PRELUDE, icon_constants(self.template)]
        parts += [function_source(self.template, name) for name in REAL_FUNCTIONS]
        # Present only after the fix; absent on the pre-fix template.
        patch_helper = optional_function_source(self.template, "patchArtifactRows")
        if patch_helper:
            parts.append(patch_helper)
        parts.append(body)
        result = subprocess.run(
            [NODE, "-"],
            input="\n".join(parts),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        payload = re.search(r"RESULT:(.*)", result.stdout)
        self.assertIsNotNone(payload, result.stdout or result.stderr)
        return json.loads(payload.group(1))

    def poll_twice(self, first, second, between="", after=""):
        """Render `first`, optionally run `between`, render `second`, snapshot."""
        return self.run_js(
            f"cardData = [{json.dumps(first)}];\n"
            "renderCard(0);\n"
            "const firstSnapshot = snapshot();\n"
            f"{between}\n"
            f"cardData = [{json.dumps(second)}];\n"
            "renderCard(0);\n"
            f"{after}\n"
            "emit({ first: firstSnapshot, second: snapshot() });"
        )

    # -- 1. initial render ---------------------------------------------------

    def test_initial_packaging_row_renders_position_speed_and_bar(self):
        data = self.run_js(
            f"cardData = [{json.dumps(build_card())}];\n"
            "renderCard(0);\n"
            "emit(snapshot());"
        )
        audio = data["rows"][1]
        self.assertEqual(audio["label"], "Audio only")
        self.assertEqual(audio["status"], "Processing audio — 0:10 / 1:40 processed · 2.0×")
        self.assertTrue(audio["hasBar"])
        self.assertEqual(audio["width"], "50%")
        self.assertEqual(audio["ariaValueNow"], "50")
        self.assertFalse(audio["indeterminate"])
        # The row must be addressable by artifact id so a later poll can find
        # it without rebuilding the card.
        self.assertEqual(audio["artifactId"], "a001")
        self.assertEqual(data["rows"][0]["artifactId"], "a000")

    # -- 2. a numeric poll patches in place ----------------------------------

    def test_numeric_poll_patches_rows_without_replacing_them(self):
        data = self.poll_twice(
            build_card(),
            audio_poll(build_card(), percent=55.5, processed_seconds=21.5,
                       duration_seconds=100, processing_speed=1.8),
            between=(
                "document.getElementById('card-0')"
                ".querySelectorAll('.artifact-item')[1]._probe = 'kept';"
            ),
        )
        audio = data["second"]["rows"][1]
        self.assertEqual(audio["status"], "Processing audio — 0:21 / 1:40 processed · 1.8×")
        self.assertEqual(audio["width"], "55.5%")
        self.assertEqual(audio["ariaValueNow"], "55.5")
        # The parent bar keeps advancing exactly as before.
        self.assertEqual(data["second"]["parentPercent"], "55.5%")
        self.assertEqual(data["second"]["parentWidth"], "55.5%")
        # The row object survived: the card was patched, not rebuilt. A full
        # rerender would drop this marker along with every bound listener.
        self.assertEqual(audio["probe"], "kept")

    # -- 3. unknown duration becomes determinate -----------------------------

    def test_unknown_duration_becomes_determinate_when_duration_arrives(self):
        unknown = build_card(
            percent=None, processedSeconds=None, durationSeconds=None,
            processingSpeed=None,
        )
        unknown["artifacts"][1].update({
            "percent": None, "processed_seconds": None,
            "duration_seconds": None, "processing_speed": None,
        })
        data = self.poll_twice(
            unknown,
            audio_poll(unknown, percent=5, processed_seconds=5, duration_seconds=100),
        )
        before = data["first"]["rows"][1]
        after = data["second"]["rows"][1]
        # No duration means no honest number, so no bar is drawn at all.
        self.assertFalse(before["hasBar"])
        self.assertEqual(before["status"], "Processing audio…")
        # The very next poll that carries a duration must show a real bar.
        self.assertTrue(after["hasBar"])
        self.assertEqual(after["status"], "Processing audio — 0:05 / 1:40 processed")
        self.assertEqual(after["width"], "5%")
        self.assertEqual(after["ariaValueNow"], "5")
        self.assertFalse(after["indeterminate"])

    # -- 4. speed-only change (the signature omission) -----------------------

    def test_speed_only_change_updates_the_artifact_row(self):
        # ffmpeg's speed drifts continuously while position and percent hold
        # steady between two polls of a long file. processing_speed was not in
        # the artifact signature at all, so this poll took the fast path and the
        # row kept printing the previous multiplier.
        data = self.poll_twice(
            build_card(),
            audio_poll(build_card(), percent=50, processed_seconds=10,
                       duration_seconds=100, processing_speed=9.4),
        )
        audio = data["second"]["rows"][1]
        self.assertIn("9.4×", audio["status"])
        self.assertNotIn("2.0×", audio["status"])
        self.assertEqual(audio["status"], "Processing audio — 0:10 / 1:40 processed · 9.4×")

    # -- 5. sub-integer percent movement -------------------------------------

    def test_sub_integer_percent_moves_the_artifact_bar(self):
        # The signature truncated percent with `| 0`, so every move inside one
        # integer bucket was invisible and the bar only twitched once per whole
        # percent -- on a long extraction, once every several minutes.
        data = self.poll_twice(
            audio_poll(build_card(), percent=50.1, processed_seconds=50.1,
                       duration_seconds=100, processing_speed=2.0),
            audio_poll(build_card(), percent=50.8, processed_seconds=50.8,
                       duration_seconds=100, processing_speed=2.0),
        )
        audio = data["second"]["rows"][1]
        self.assertEqual(audio["width"], "50.8%")
        self.assertEqual(audio["ariaValueNow"], "50.8")
        self.assertEqual(audio["status"], "Processing audio — 0:50 / 1:40 processed · 2.0×")

    # -- 6. focus and listeners ----------------------------------------------

    def test_focus_and_cancel_binding_survive_a_numeric_poll(self):
        data = self.poll_twice(
            build_card(),
            audio_poll(build_card(), percent=55.5, processed_seconds=21.5,
                       duration_seconds=100, processing_speed=1.8),
            between=(
                "const cancel = document.getElementById('card-0').querySelector('[data-cancel]');\n"
                "cancel._probe = 'focused';\n"
                "cancel.focus();"
            ),
            after=(
                "document.getElementById('card-0').querySelector('[data-cancel]').click();"
            ),
        )
        # Focus stayed on the same button object, not on a fresh copy of it.
        self.assertTrue(data["second"]["activeIsCancel"])
        self.assertEqual(data["second"]["activeProbe"], "focused")
        # And exactly one click handler is bound: a patched card must not
        # accumulate a second listener per poll.
        self.assertEqual(data["second"]["calls"]["cancelCard"], 1)

    # -- 7. structural change still full-renders -----------------------------

    def test_structural_transition_still_full_renders(self):
        finished = audio_poll(build_card(), percent=100, processed_seconds=100,
                              duration_seconds=100, processing_speed=2.0)
        finished["artifacts"][1]["status"] = "done"
        finished["artifacts"][1]["phase"] = "done"
        data = self.poll_twice(build_card(), finished)
        audio = data["second"]["rows"][1]
        self.assertEqual(audio["status"], "✓ Complete")
        # A completed artifact offers Save and carries no active progress bar.
        self.assertTrue(audio["hasSave"])
        self.assertFalse(audio["hasBar"])

    def test_status_or_phase_change_is_never_fast_pathed(self):
        retrying = audio_poll(build_card(), percent=50, processed_seconds=10,
                              duration_seconds=100, processing_speed=2.0)
        retrying["artifacts"][1]["phase"] = "retrying"
        data = self.poll_twice(build_card(), retrying)
        self.assertEqual(data["second"]["rows"][1]["status"], "Retrying…")


@unittest.skipIf(NODE is None, "node is required to execute artifactSignature")
class ArtifactSignatureScopeTests(unittest.TestCase):
    """The signature must gate STRUCTURE, and only structure.

    Numeric packaging fields are patched in place, so keeping them in the
    signature forces a full card rebuild on every poll -- which is what drops
    focus and rebinds listeners. Fields that change the row's shape must stay.
    """

    @classmethod
    def setUpClass(cls):
        cls.template = read_template()

    def signatures(self, pairs_js):
        script = "\n".join([
            function_source(self.template, "artifactSignature"),
            "const out = {};",
            pairs_js,
            "process.stdout.write('RESULT:' + JSON.stringify(out) + '\\n');",
        ])
        result = subprocess.run(
            [NODE, "-"], input=script, capture_output=True, text=True,
            encoding="utf-8", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        payload = re.search(r"RESULT:(\{.*\})", result.stdout)
        self.assertIsNotNone(payload, result.stdout)
        return json.loads(payload.group(1))

    BASE = (
        "const base = { id: 'a000', status: 'downloading', phase: 'processing',"
        " percent: 50, processed_seconds: 10, duration_seconds: 100,"
        " processing_speed: 2, saved: false, saving: false, saveError: null };\n"
        "const card = extra => ({ artifacts: [ {...base, id: 'a001'},"
        " {...base, ...extra} ] });\n"
    )

    def test_live_numeric_fields_do_not_force_a_full_render(self):
        data = self.signatures(
            self.BASE +
            "out.baseline = artifactSignature(card({}));\n"
            "out.percent = artifactSignature(card({ percent: 50.8 }));\n"
            "out.processed = artifactSignature(card({ processed_seconds: 10.9 }));\n"
            "out.speed = artifactSignature(card({ processing_speed: 9.4 }));\n"
        )
        self.assertEqual(data["percent"], data["baseline"],
                         "a sub-structural percent move must be patched, not rerendered")
        self.assertEqual(data["processed"], data["baseline"])
        self.assertEqual(data["speed"], data["baseline"])

    def test_structural_fields_still_force_a_full_render(self):
        data = self.signatures(
            self.BASE +
            "out.baseline = artifactSignature(card({}));\n"
            "out.status = artifactSignature(card({ status: 'done' }));\n"
            "out.phase = artifactSignature(card({ phase: 'retrying' }));\n"
            "out.saved = artifactSignature(card({ saved: true }));\n"
            "out.saving = artifactSignature(card({ saving: true }));\n"
            "out.saveError = artifactSignature(card({ saveError: 'disk full' }));\n"
            # Whether the row HAS a bar is structural: an unknown duration draws
            # no bar at all, so its arrival has to rebuild the row.
            "out.noDuration = artifactSignature(card({ duration_seconds: null }));\n"
        )
        for key in ("status", "phase", "saved", "saving", "saveError", "noDuration"):
            with self.subTest(field=key):
                self.assertNotEqual(data[key], data["baseline"])

    def test_single_output_never_uses_the_artifact_fast_path(self):
        data = self.signatures(
            "out.single = artifactSignature({ artifacts: [{ id: 'a000', status: 'done' }] });\n"
            "out.none = artifactSignature({});\n"
        )
        self.assertEqual(data["single"], "")
        self.assertEqual(data["none"], "")


if __name__ == "__main__":
    unittest.main()
