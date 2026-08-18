"""Frontend regression tests for the v0.3 output-selection UI.

Three defects found in real-world testing are locked down here:

  1. Vietnamese titles rendered with detached/spaced diacritics, because
     ``.card-title`` used the decorative ``--font-serif`` stack (Instrument
     Serif) and provider metadata was never normalized to Unicode NFC.
  2. Quality/"Audio only" chips never showed a selected state: ``renderCard``
     emitted a ``selected`` class that no CSS rule matched, while the CSS
     styled ``.chip.quality[aria-pressed="true"]`` -- an attribute the buttons
     never emitted. Two conflicting sources of truth, neither visible.
  3. The primary controls carried no icons.

The behavior tests are not source-string assertions: they extract the real
``renderCard`` body from templates/index.html, execute it in Node against a
minimal DOM stub, and assert on the attributes the real code produced and on
the network calls it did (or did not) make. The source-contract tests run
everywhere, including where Node is unavailable.
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

import app


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


def icon_constants(template):
    """Return the ICON_* constant declarations from the template."""
    lines = re.findall(r"^    const (ICON_[A-Z_]+ = .*)$", template, re.MULTILINE)
    if not lines:
        raise AssertionError("no ICON_* constants found in template")
    return "\n".join("const " + line for line in lines)


# Selectors that previously drew the selection checkmark via a CSS ::before
# pseudo-element with content: "✓". The .selection-check inline SVG replaced
# them, so none of these rules may exist -- standalone or grouped.
CONTROL_BEFORE_SELECTORS = (
    ".format-switch button::before",
    ".filter-btn::before",
    ".chip.quality::before",
)


def _strip_css_comments(text):
    """Remove /* ... */ comments so historical prose mentioning a glyph cannot
    cause a false positive when scanning CSS."""
    return re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)


def _style_blocks(template):
    """Return the concatenated contents of every <style>...</style> block in
    the template. Parsing only real CSS (not the inline JavaScript, whose
    braces would otherwise be mistaken for CSS rule blocks) is what makes the
    rule parser trustworthy."""
    blocks = re.findall(r"<style[^>]*>(.*?)</style>", template, re.DOTALL)
    if not blocks:
        raise AssertionError("no <style> block found in template")
    return "\n".join(blocks)


def _css_rule_blocks(css):
    """Return [(selector_list, body)] for every CSS rule block in `css`,
    descending into @media/@supports wrappers so nested rules are returned too.
    Comments are stripped first."""
    css = _strip_css_comments(css)
    blocks = []
    pos = 0
    n = len(css)
    while pos < n:
        open_brace = css.find("{", pos)
        if open_brace == -1:
            break
        selector = css[pos:open_brace]
        depth = 1
        close = open_brace + 1
        while close < n and depth > 0:
            ch = css[close]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            close += 1
        body = css[open_brace + 1:close - 1]
        if selector.strip().startswith("@"):
            blocks.extend(_css_rule_blocks(body))
        else:
            blocks.append((selector, body))
        pos = close
    return blocks


def _normalize_selector(sel):
    """Collapse internal whitespace so 'a  b' and 'a\\nb' compare equal."""
    return re.sub(r"\s+", " ", sel).strip()


def _control_before_rules(css):
    """Return [(normalized_selector, body)] for every CSS rule whose selector
    list contains one of CONTROL_BEFORE_SELECTORS -- whether the selector
    stands alone or is part of a comma-separated group, across any whitespace.
    Selectors are compared as plain strings after normalization, never passed
    through re.escape, so there is no double-escaping."""
    targets = {_normalize_selector(s) for s in CONTROL_BEFORE_SELECTORS}
    found = []
    for selector_list, body in _css_rule_blocks(css):
        for sel in selector_list.split(","):
            norm = _normalize_selector(sel)
            if norm in targets:
                found.append((norm, body))
                break
    return found


# A DOM stub just large enough to run the real renderCard. Setting innerHTML
# reparses the <button> tags into live stubs immediately, so when renderCard
# calls querySelectorAll('[data-output-index]').forEach(addEventListener) it
# binds handlers to the very objects the tests later .click(). reparseButtons is
# idempotent (gen-tracked) so re-fetching an element after a re-render does not
# replace bound buttons with fresh, handler-less copies.
HARNESS_PRELUDE = r"""
'use strict';
const fetchUrls = [];
let currentFormat = 'video';
let downloadDirectoryHandle = null;
let focusSelectedSelector = null;

function makeElement(tag) {
  const el = {
    tagName: tag,
    id: '',
    className: '',
    dataset: {},
    style: {},
    hidden: false,
    tabIndex: 0,
    children: [],
    classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
    _attrs: {},
    _rawHtml: '',
    _gen: 0,
    _parsedGen: -1,
    _buttons: [],
    _handlers: {},
    _selector: null,
    setAttribute(name, value) { el._attrs[name] = String(value); if (name === 'id') el.id = String(value); },
    getAttribute(name) { const v = el._attrs[name]; return v !== undefined ? v : undefined; },
    hasAttribute(name) { return el._attrs[name] !== undefined; },
    appendChild(child) { el.children.push(child); if (child && child.id) registry[child.id] = child; return child; },
    addEventListener(type, handler) { (el._handlers[type] ||= []).push(handler); },
    focus() { document._active = el; if (el._selector) focusSelectedSelector = el._selector; },
  };
  // innerHTML / textContent both drive _reparse so _buttons is current the
  // instant renderCard finishes writing markup, before it binds handlers.
  Object.defineProperty(el, 'innerHTML', {
    set(v) { el._rawHtml = String(v); el._gen++; _reparse(el); },
    get() { return el._rawHtml; },
  });
  Object.defineProperty(el, 'textContent', {
    set(v) {
      el._rawHtml = String(v == null ? '' : v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      el._gen++; _reparse(el);
    },
    get() { return el._rawHtml; },
  });
  el.querySelectorAll = function (selector) { return el._buttons.filter(b => _matches(b, selector)); };
  el.querySelector = function (selector) { return el._buttons.find(b => _matches(b, selector)) || null; };
  el.contains = function (other) { return el._buttons.includes(other) || el.children.includes(other); };
  return el;
}

// Rebuild the button stubs for an element iff its markup changed since the last
// parse. Fresh objects each time is correct: renderCard re-binds handlers after
// every innerHTML write, so the post-render generation is always the bound one.
function _reparse(el) {
  if (el._parsedGen === el._gen) return;
  el._parsedGen = el._gen;
  el._buttons = [];
  const re = /<button\b([^>]*)>([\s\S]*?)<\/button>/g;
  let m;
  while ((m = re.exec(el._rawHtml)) !== null) {
    const attrs = m[1];
    const inner = m[2];
    const btn = makeElement('button');
    btn._rawHtml = inner;       // bypass the setter so the button itself does not reparse
    btn._attrs = {};
    btn.dataset = {};
    const are = /([a-z][a-z-]*)(?:="([^"]*)")?/g;
    let am;
    while ((am = are.exec(attrs)) !== null) {
      const name = am[1];
      const value = am[2] !== undefined ? am[2] : '';
      btn._attrs[name] = value;
      if (name.startsWith('data-')) {
        const camel = name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
        btn.dataset[camel] = value;
      }
    }
    if (btn.dataset.outputIndex !== undefined) btn._selector = `[data-output-index="${btn.dataset.outputIndex}"]`;
    else if (btn._attrs['data-download'] !== undefined) btn._selector = '[data-download]';
    else if (btn._attrs['data-save'] !== undefined) btn._selector = '[data-save]';
    else if (btn._attrs['data-cancel'] !== undefined) btn._selector = '[data-cancel]';
    else if (btn.dataset.artifactSave !== undefined) btn._selector = `[data-artifact-save="${btn.dataset.artifactSave}"]`;
    btn.click = function () { (btn._handlers.click || []).forEach(h => h()); };
    el._buttons.push(btn);
  }
}

function _matches(btn, selector) {
  if (selector === '.card-thumb img' || selector === '.progress-wrap') return false;
  if (selector === '[data-output-index]') return btn.dataset.outputIndex !== undefined;
  if (selector === '[data-download]') return btn._attrs['data-download'] !== undefined;
  if (selector === '[data-save]') return btn._attrs['data-save'] !== undefined;
  if (selector === '[data-cancel]') return btn._attrs['data-cancel'] !== undefined;
  if (selector === '[data-format-index]') return btn.dataset.formatIndex !== undefined;
  if (selector === '[data-artifact-save]') return btn.dataset.artifactSave !== undefined;
  let m;
  if ((m = /^\[data-output-index="(\d+)"\]$/.exec(selector))) return btn.dataset.outputIndex === m[1];
  if ((m = /^\[data-artifact-save="(\d+)"\]$/.exec(selector))) return btn.dataset.artifactSave === m[1];
  return false;
}

const registry = {};
const cardsRoot = makeElement('div');
registry['cards'] = cardsRoot;
const document = {
  getElementById(id) { return registry[id] || null; },
  createElement(tag) { return makeElement(tag); },
  get activeElement() { return document._active || null; },
  _active: null,
};

// Collaborators renderCard calls that are out of scope for these tests.
function statusMatchesFilter() { return true; }
function artifactSignature() { return 'sig'; }
function artifactDisplayState(art) {
  return { cls: '', text: art.status, showProgress: false, canSave: false };
}
function progressParts() {
  return { phase: 'Downloading', percent: 0, hasPercent: false, size: '', eta: '', reconnecting: false };
}
function updateProgressWrap() {}
function fmtDur(d) { return `${d}s`; }
function friendlyError(e) { return String(e); }
function dlCard() { fetchUrls.push('/api/download'); }
function saveCard() {}
function cancelCard() {}
function saveArtifact() {}
let cardData = [];

function renderAndParse(index) {
  renderCard(index);
  const el = document.getElementById(`card-${index}`);
  reparseButtons(el);
  return el;
}

// Idempotent: the innerHTML setter already reparsed; this just guarantees the
// buttons are current when a test re-fetches an element after a re-render.
function reparseButtons(el) { _reparse(el); }

function pressedStates(el) {
  return el._buttons
    .filter(b => b.dataset.outputIndex !== undefined)
    .map(b => b.getAttribute('aria-pressed'));
}
"""


def build_card(**overrides):
    """A ready card with 1080p + 720p video formats plus an audio option."""
    card = {
        "url": "https://example.test/watch?v=abc",
        "status": "ready",
        "title": "Clip",
        "uploader": "Channel",
        "duration": 90,
        "thumbnail": "",
        "formats": [{"id": "137", "label": "1080p"}, {"id": "136", "label": "720p"}],
        "outputOptions": [
            {"type": "video", "format_id": "137", "label": "1080p"},
            {"type": "video", "format_id": "136", "label": "720p"},
            {"type": "audio", "label": "Audio only"},
        ],
        "selectedOutputs": [{"type": "video", "format_id": "137"}, {"type": "audio"}],
        "selectedFormatId": "137",
    }
    card.update(overrides)
    return card


def run_harness(body, card=None):
    """Run the real renderCard plus an assertion body in Node."""
    template = read_template()
    script = "\n".join([
        HARNESS_PRELUDE,
        icon_constants(template),
        function_source(template, "esc"),
        function_source(template, "normalizeText"),
        function_source(template, "renderCard"),
        f"cardData = [{json.dumps(card if card is not None else build_card())}];",
        body,
    ])
    return subprocess.run(
        ["node", "-"],
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )


class VietnameseTypographyContractTests(unittest.TestCase):
    """Locks bug 1: serif card titles mangled Vietnamese diacritics."""

    def setUp(self):
        self.template = read_template()

    def card_title_rule(self):
        match = re.search(r"^    \.card-title \{([^}]*)\}", self.template, re.MULTILINE)
        self.assertIsNotNone(match, ".card-title rule not found")
        return match.group(1)

    def test_card_title_does_not_use_the_decorative_serif_stack(self):
        rule = self.card_title_rule()
        self.assertIn("font-family:", rule)
        self.assertNotIn("var(--font-serif)", rule)
        self.assertNotIn("Instrument Serif", rule)

    def test_card_title_uses_a_vietnamese_capable_sans_stack(self):
        rule = self.card_title_rule()
        family = re.search(r"font-family:\s*([^;]+);", rule).group(1)
        self.assertIn("--font-sans", family)

        # The referenced variable must itself resolve to a Vietnamese-capable
        # stack, not just be a name that looks right.
        stack = re.search(r"--font-sans-vi:\s*([^;]+);", self.template)
        self.assertIsNotNone(stack, "--font-sans-vi must be defined")
        resolved = stack.group(1)
        self.assertIn("var(--font-sans)", resolved)
        self.assertTrue(
            any(f in resolved for f in ("Segoe UI", "system-ui")),
            f"no Vietnamese-capable fallback in: {resolved}",
        )
        self.assertTrue(resolved.rstrip().endswith("sans-serif"))

    def test_static_english_branding_keeps_the_serif_face(self):
        # The fix must be surgical: headings/branding are unaffected.
        for selector in (r"\.wordmark", r"h1", r"\.manifest-head h2"):
            with self.subTest(selector=selector):
                rule = re.search(rf"^    {selector} \{{([^}}]*)\}}", self.template, re.MULTILINE)
                self.assertIsNotNone(rule)
                self.assertIn("var(--font-serif)", rule.group(1))

    def test_meta_charset_is_first_in_head(self):
        head = self.template[: self.template.index("</head>")]
        metas = re.findall(r"<meta\b[^>]*>", head)
        self.assertTrue(metas)
        self.assertIn("charset=\"UTF-8\"", metas[0])

    def test_inspect_normalizes_title_and_uploader_but_not_technical_fields(self):
        inspect = function_source(self.template, "inspectLinks")
        self.assertIn("title: normalizeText(", inspect)
        self.assertIn("uploader: normalizeText(", inspect)
        # Technical identifiers must pass through untouched.
        self.assertNotIn("normalizeText(url)", inspect)
        self.assertNotIn("selectedFormatId: normalizeText", inspect)
        self.assertNotIn("thumbnail: normalizeText", inspect)


@unittest.skipIf(shutil.which("node") is None, "node is not available")
class VietnameseNormalizationBehaviorTests(unittest.TestCase):
    """Runs the real normalizeText in Node against decomposed input."""

    def run_normalize(self, body):
        template = read_template()
        script = function_source(template, "normalizeText") + "\n" + body
        return subprocess.run(
            ["node", "-"], input=script, capture_output=True, text=True,
            encoding="utf-8", timeout=30,
        )

    def test_decomposed_vietnamese_collapses_to_nfc(self):
        result = self.run_normalize(
            "const input = 'tie\\u0302\\u0301ng';\n"
            "const out = normalizeText(input);\n"
            "console.log(JSON.stringify({"
            "  inputLength: input.length,"
            "  outLength: out.length,"
            "  out,"
            "  matchesPrecomposed: out === 'ti\\u1EBFng',"
            "}));"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        # Positive oracle: the decomposed form is 3 code points longer and the
        # normalized output is byte-identical to the precomposed spelling.
        self.assertEqual(data["inputLength"], 7)
        self.assertEqual(data["outLength"], 5)
        self.assertEqual(data["out"], "tiếng")
        self.assertTrue(data["matchesPrecomposed"])

    def test_already_composed_text_is_unchanged(self):
        result = self.run_normalize(
            "const t = 'Siêu tổng hợp 3 mùa Dexter trong 2 tiếng rưỡi';\n"
            "console.log(JSON.stringify({ same: normalizeText(t) === t, out: normalizeText(t) }));"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertTrue(data["same"])
        self.assertEqual(data["out"], "Siêu tổng hợp 3 mùa Dexter trong 2 tiếng rưỡi")

    def test_non_string_values_pass_through_untouched(self):
        result = self.run_normalize(
            "console.log(JSON.stringify({"
            "  nul: normalizeText(null),"
            "  num: normalizeText(137),"
            "  undef: normalizeText(undefined) === undefined,"
            "  obj: normalizeText({ id: '137' }),"
            "}));"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertIsNone(data["nul"])
        self.assertEqual(data["num"], 137)
        self.assertTrue(data["undef"])
        self.assertEqual(data["obj"], {"id": "137"})


class OutputSelectionContractTests(unittest.TestCase):
    """Locks bug 2 at the source level: one source of truth, not two."""

    def setUp(self):
        self.template = read_template()
        self.render = function_source(self.template, "renderCard")

    def test_output_buttons_emit_aria_pressed(self):
        self.assertIn('aria-pressed="${isSelected ? \'true\' : \'false\'}"', self.render)

    def test_render_does_not_keep_a_second_selected_class_source(self):
        # The dead `selected` class was the whole bug: it changed while the CSS
        # keyed off aria-pressed, so nothing visible ever moved.
        self.assertNotIn("' selected'", self.render)
        self.assertNotIn('" selected"', self.render)

    def test_css_styles_selection_via_aria_pressed(self):
        self.assertIn('.chip.quality[aria-pressed="true"]', self.template)
        # And there is still no bare `.selected` rule that could drift.
        self.assertIsNone(re.search(r"^\s*\.selected\s*\{", self.template, re.MULTILINE))

    def test_output_chips_are_real_buttons(self):
        self.assertIn('<button type="button" class="chip quality"', self.render)

    def test_focus_visible_styling_survives(self):
        self.assertIn(":focus-visible", self.template)


@unittest.skipIf(shutil.which("node") is None, "node is not available")
class OutputSelectionBehaviorTests(unittest.TestCase):
    """Executes the real renderCard in Node and asserts rendered state."""

    def test_default_selection_marks_first_video_and_audio_pressed(self):
        result = run_harness(
            "const el = renderAndParse(0);\n"
            "console.log(JSON.stringify({ pressed: pressedStates(el), count: el._buttons.length }));"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        # 1080p selected, 720p not, Audio only selected -- exactly the default
        # the inspect step sets up, now actually visible.
        self.assertEqual(data["pressed"], ["true", "false", "true"])

    def test_clicking_an_unselected_video_selects_only_that_output(self):
        result = run_harness(
            "let el = renderAndParse(0);\n"
            "el._buttons.find(b => b.dataset.outputIndex === '1').click();\n"
            "el = document.getElementById('card-0'); reparseButtons(el);\n"
            "console.log(JSON.stringify({"
            "  pressed: pressedStates(el),"
            "  selected: cardData[0].selectedOutputs,"
            "  downloads: fetchUrls,"
            "}));"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["pressed"], ["true", "true", "true"])
        self.assertIn({"type": "video", "format_id": "136", "label": "720p"}, data["selected"])
        # Selecting an option must never start a download.
        self.assertEqual(data["downloads"], [])

    def test_clicking_a_selected_video_removes_only_that_output(self):
        result = run_harness(
            "let el = renderAndParse(0);\n"
            "el._buttons.find(b => b.dataset.outputIndex === '1').click();\n"
            "el = document.getElementById('card-0'); reparseButtons(el);\n"
            "el._buttons.find(b => b.dataset.outputIndex === '1').click();\n"
            "el = document.getElementById('card-0'); reparseButtons(el);\n"
            "console.log(JSON.stringify({"
            "  pressed: pressedStates(el),"
            "  selected: cardData[0].selectedOutputs,"
            "  downloads: fetchUrls,"
            "}));"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["pressed"], ["true", "false", "true"])
        types = [(o["type"], o.get("format_id")) for o in data["selected"]]
        self.assertIn(("video", "137"), types)
        self.assertIn(("audio", None), types)
        self.assertNotIn(("video", "136"), types)
        self.assertEqual(data["downloads"], [])

    def test_toggling_audio_keeps_selected_videos(self):
        result = run_harness(
            "let el = renderAndParse(0);\n"
            "el._buttons.find(b => b.dataset.outputIndex === '2').click();\n"
            "el = document.getElementById('card-0'); reparseButtons(el);\n"
            "const afterOff = pressedStates(el);\n"
            "const videosAfterOff = cardData[0].selectedOutputs.filter(o => o.type === 'video').length;\n"
            "el._buttons.find(b => b.dataset.outputIndex === '2').click();\n"
            "el = document.getElementById('card-0'); reparseButtons(el);\n"
            "console.log(JSON.stringify({"
            "  afterOff, videosAfterOff,"
            "  afterOn: pressedStates(el),"
            "  downloads: fetchUrls,"
            "}));"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["afterOff"], ["true", "false", "false"])
        self.assertEqual(data["videosAfterOff"], 1)
        self.assertEqual(data["afterOn"], ["true", "false", "true"])
        self.assertEqual(data["downloads"], [])

    def test_multiple_video_qualities_can_be_selected_together(self):
        result = run_harness(
            "let el = renderAndParse(0);\n"
            "el._buttons.find(b => b.dataset.outputIndex === '1').click();\n"
            "el = document.getElementById('card-0'); reparseButtons(el);\n"
            "const videos = cardData[0].selectedOutputs.filter(o => o.type === 'video');\n"
            "console.log(JSON.stringify({ videoCount: videos.length, pressed: pressedStates(el) }));"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["videoCount"], 2)
        self.assertEqual(data["pressed"], ["true", "true", "true"])

    def test_rerender_after_toggle_restores_focus_to_the_clicked_chip(self):
        result = run_harness(
            "let el = renderAndParse(0);\n"
            "const chip = el._buttons.find(b => b.dataset.outputIndex === '1');\n"
            "document._active = chip;\n"
            "el._buttons.push(chip);\n"
            "chip.click();\n"
            "console.log(JSON.stringify({ focused: focusSelectedSelector }));"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["focused"], '[data-output-index="1"]')

    def test_a_card_with_only_audio_renders_that_option_pressed(self):
        card = build_card(
            formats=[],
            outputOptions=[{"type": "audio", "label": "Audio only"}],
            selectedOutputs=[{"type": "audio"}],
            selectedFormatId=None,
        )
        result = run_harness(
            "const el = renderAndParse(0);\n"
            "console.log(JSON.stringify({ pressed: pressedStates(el) }));",
            card=card,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["pressed"], ["true"])


class IconContractTests(unittest.TestCase):
    """Locks bug 3: the primary controls carried no icons."""

    def setUp(self):
        self.template = read_template()
        self.render = function_source(self.template, "renderCard")

    def icon_body(self, name):
        match = re.search(rf"const {name} = `(.*?)`;", self.template, re.DOTALL)
        self.assertIsNotNone(match, f"{name} not defined")
        return match.group(1)

    def test_every_icon_is_inline_svg_using_currentcolor(self):
        for name in ("ICON_VIDEO", "ICON_AUDIO", "ICON_DOWNLOAD", "ICON_SAVE", "ICON_RETRY", "ICON_CANCEL", "ICON_CHECK"):
            with self.subTest(icon=name):
                body = self.icon_body(name)
                self.assertTrue(body.startswith("<svg "), body[:40])
                self.assertIn("</svg>", body)
                self.assertIn("${ICON_ATTRS}", body)
        attrs = re.search(r"const ICON_ATTRS = '([^']*)'", self.template).group(1)
        self.assertIn('stroke="currentColor"', attrs)
        self.assertIn('aria-hidden="true"', attrs)
        self.assertIn('focusable="false"', attrs)
        self.assertIn('width="14"', attrs)
        self.assertIn('height="14"', attrs)

    def test_icon_check_is_an_inline_svg_check(self):
        # ICON_CHECK is the selection indicator; it must follow the same inline
        # SVG contract as the other icons and must not be a Unicode glyph.
        body = self.icon_body("ICON_CHECK")
        self.assertTrue(body.startswith("<svg "), body[:40])
        self.assertIn("</svg>", body)
        self.assertIn("${ICON_ATTRS}", body)
        self.assertIn("path ", body)
        self.assertNotIn("✓", body)

    def test_no_new_icon_dependency_is_introduced(self):
        # No icon font, sprite sheet, or extra CDN. The favicon link is a
        # pre-existing browser tab icon, not an icon-font dependency, so it
        # stays; only known icon-font/CDN families are rejected.
        head = self.template[: self.template.index("</head>")]
        links = re.findall(r'<link[^>]*href="([^"]+)"', head)
        icon_font_markers = (
            "fontawesome", "materialicons", "bootstrap-icons",
            "ionicons", "feathericons", "font-awesome", "material-icons",
            "boxicons", "remixicon",
        )
        for href in links:
            with self.subTest(href=href):
                lowered = href.lower()
                for marker in icon_font_markers:
                    self.assertNotIn(marker, lowered)
        self.assertNotIn("<use ", self.template)

    def test_quality_and_audio_chips_render_distinct_type_icons(self):
        self.assertIn("opt.type === 'audio' ? ICON_AUDIO : ICON_VIDEO", self.render)
        self.assertNotEqual(self.icon_body("ICON_VIDEO"), self.icon_body("ICON_AUDIO"))

    def test_download_and_recovery_buttons_carry_icons(self):
        self.assertIn("${ICON_DOWNLOAD}", self.render)
        self.assertIn("${ICON_SAVE}", self.render)
        self.assertIn("${ICON_RETRY}", self.render)
        self.assertIn("${ICON_CANCEL}", self.render)

    def test_no_unicode_glyph_is_used_as_a_control_icon(self):
        # Control icons are the toggleable selection indicators on format-switch
        # buttons, queue filter buttons, and output quality chips. They must
        # never rely on a Unicode glyph (U+2713) rendered via CSS content, which
        # depends on the page font shipping a glyph for it. The selection
        # indicator is now an inline SVG in a .selection-check element, so none
        # of the control ::before rules may exist at all -- not as a standalone
        # rule, not as part of a comma-separated group, and not with either
        # quote style for the content value. Message glyphs (url-status ok,
        # status-line done) are not control icons and are out of scope here;
        # they are not in the selector set.
        self.assertNotIn("'✓ Saved'", self.render)
        self.assertNotIn('"✓ Saved"', self.render)

        # Parse the real CSS rules: comments are stripped first (so historical
        # prose mentioning the glyph cannot cause a false positive), @media
        # wrappers are descended into, and every rule whose selector list
        # contains one of the three control ::before selectors is returned --
        # whether it stands alone or is part of a grouped selector, across any
        # whitespace. There must be none: the whole ::before approach is gone.
        matched = _control_before_rules(_style_blocks(self.template))
        self.assertEqual(
            matched, [],
            f"control ::before rules must be removed; found: {matched}",
        )

        # Defensive double-check: even if a control ::before rule existed, its
        # body must not set content to the checkmark glyph in either quote
        # style or carry the bare glyph. This makes the intent explicit and
        # catches a regression that reintroduces only the content line.
        for selector, body in matched:
            for needle in ('content: "✓"', "content: '✓'", "✓"):
                self.assertNotIn(
                    needle, body,
                    f"{selector} body still uses a Unicode checkmark: {body!r}",
                )

        # No <button> control markup may carry a bare U+2713 glyph where an
        # icon belongs (buttons legitimately contain SVG; they must not contain
        # the literal checkmark character).
        for button in re.findall(r"<button\b[^>]*>.*?</button>", self.template, re.DOTALL):
            self.assertNotIn("✓", button, "a <button> carries a bare U+2713 glyph")
        # The ICON_CHECK constant is an SVG, not the glyph.
        self.assertNotIn("✓", self.icon_body("ICON_CHECK"))

        # The legitimate SVG implementation must be present and keyed on
        # aria-pressed, not on a class or a pseudo-element.
        self.assertIn(".selection-check", self.template)
        self.assertIn('[aria-pressed="true"] .selection-check', self.template)

    def test_static_toggle_buttons_carry_an_svg_selection_check(self):
        # Format-switch and queue filter buttons are static HTML; each must
        # carry a .selection-check element with an inline SVG so the selected
        # indicator does not depend on a font glyph, and aria-pressed must still
        # drive whether that indicator is shown. All seven static controls are
        # covered (two format toggles + five queue filters); each assertion is
        # linked to that specific button so a missing icon on one control cannot
        # hide behind a passing total-icon count.
        controls = [
            ('data-format="video"', "true", "MP4 video"),
            ('data-format="audio"', "false", "MP3 audio"),
            ('data-filter="all"', "true", "All"),
            ('data-filter="ready"', "false", "Ready"),
            ('data-filter="downloading"', "false", "Active"),
            ('data-filter="done"', "false", "Complete"),
            ('data-filter="issues"', "false", "Issues"),
        ]
        for attr, pressed, label in controls:
            with self.subTest(control=attr):
                # Pull the exact opening tag + body of this one button, so every
                # assertion below is tied to the named control and not to the
                # whole template.
                pat = rf'(<button[^>]*{re.escape(attr)}[^>]*>)(.*?)</button>'
                m = re.search(pat, self.template, re.DOTALL)
                self.assertIsNotNone(m, f"button with {attr} not found")
                opening, body = m.group(1), m.group(2)
                # aria-pressed reflects the real initial state for this control.
                self.assertIn(
                    f'aria-pressed="{pressed}"', opening,
                    f"{attr}: aria-pressed mismatch in {opening!r}",
                )
                # .selection-check element carrying an inline SVG check icon.
                self.assertIn(
                    'class="selection-check"', body,
                    f"{attr}: .selection-check element missing",
                )
                self.assertIn("<svg", body, f"{attr}: inline SVG missing")
                self.assertIn(
                    'aria-hidden="true"', body,
                    f"{attr}: check SVG is not aria-hidden",
                )
                self.assertIn(
                    'stroke="currentColor"', body,
                    f"{attr}: check SVG does not use currentColor",
                )
                self.assertIn(
                    'focusable="false"', body,
                    f"{attr}: check SVG is not focusable=false",
                )
                # The text label must still be present and readable.
                self.assertIn(label, body, f"{attr}: text label missing")
                # No bare Unicode glyph used as the check icon in the markup.
                self.assertNotIn(
                    "✓", body, f"{attr}: bare U+2713 glyph in button markup",
                )
        # The CSS shows the indicator only when aria-pressed is true.
        self.assertIn(
            '[aria-pressed="true"] .selection-check { opacity: 1; transform: scale(1); }',
            re.sub(r"\s+", " ", self.template),
        )
        # And it is hidden by default (opacity 0).
        self.assertIn(".selection-check {", self.template)
        self.assertIn("opacity: 0", self.template)


@unittest.skipIf(shutil.which("node") is None, "node is not available")
class IconRenderingBehaviorTests(unittest.TestCase):
    """Asserts on the markup renderCard actually produced."""

    def test_chips_pair_an_svg_icon_with_a_readable_text_label(self):
        result = run_harness(
            "const el = renderAndParse(0);\n"
            "const out = el._buttons.filter(b => b.dataset.outputIndex !== undefined)"
            "  .map(b => ({"
            "    hasSvg: /<svg\\b/.test(b.innerHTML),"
            "    ariaHidden: /aria-hidden=\"true\"/.test(b.innerHTML),"
            "    label: (b.innerHTML.match(/<span class=\"chip-label\">([^<]*)<\\/span>/) || [])[1] || '',"
            "  }));\n"
            "console.log(JSON.stringify(out));"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        chips = json.loads(result.stdout)
        self.assertEqual(len(chips), 3)
        for chip in chips:
            with self.subTest(label=chip["label"]):
                self.assertTrue(chip["hasSvg"])
                self.assertTrue(chip["ariaHidden"])
                self.assertTrue(chip["label"].strip())
        self.assertEqual([c["label"] for c in chips], ["1080p", "720p", "Audio only"])

    def test_download_button_renders_an_icon_beside_its_label(self):
        result = run_harness(
            "const el = renderAndParse(0);\n"
            "const btn = el._buttons.find(b => b.hasAttribute('data-download'));\n"
            "console.log(JSON.stringify({"
            "  hasSvg: /<svg\\b/.test(btn.innerHTML),"
            "  ariaHidden: /aria-hidden=\"true\"/.test(btn.innerHTML),"
            "  text: (btn.innerHTML.match(/<span>([^<]*)<\\/span>/) || [])[1] || '',"
            "}));"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertTrue(data["hasSvg"])
        self.assertTrue(data["ariaHidden"])
        self.assertEqual(data["text"], "Download")

    def test_quality_chips_carry_a_selection_check_alongside_the_type_icon(self):
        # Every output chip must contain both the type icon (video/audio) and a
        # selection-check element holding the inline SVG check. The CSS contract
        # (tested in IconContractTests) shows/hides the check via aria-pressed.
        result = run_harness(
            "const el = renderAndParse(0);\n"
            "const out = el._buttons.filter(b => b.dataset.outputIndex !== undefined)"
            "  .map(b => ({"
            "    pressed: b.getAttribute('aria-pressed'),"
            "    hasTypeSvg: /<svg\\b/.test(b.innerHTML),"
            "    hasCheck: /<span class=\"selection-check\">[\\s\\S]*<svg/.test(b.innerHTML),"
            "    checkIsSvg: /<span class=\"selection-check\">[\\s\\S]*<svg[\\s\\S]*<\\/svg>[\\s\\S]*<\\/span>/.test(b.innerHTML),"
            "    typeIconAndCheckCoexist: /<svg\\b[\\s\\S]*?<\\/svg><span class=\"selection-check\">/.test(b.innerHTML),"
            "    label: (b.innerHTML.match(/<span class=\"chip-label\">([^<]*)<\\/span>/) || [])[1] || '',"
            "  }));\n"
            "console.log(JSON.stringify(out));"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        chips = json.loads(result.stdout)
        self.assertEqual(len(chips), 3)
        for chip in chips:
            with self.subTest(label=chip["label"]):
                self.assertTrue(chip["hasTypeSvg"], "type icon missing")
                self.assertTrue(chip["hasCheck"], "selection-check missing")
                self.assertTrue(chip["checkIsSvg"], "check is not an SVG")
                self.assertTrue(chip["typeIconAndCheckCoexist"], "type icon and check not adjacent")
        self.assertEqual([c["label"] for c in chips], ["1080p", "720p", "Audio only"])

    def test_selection_check_visibility_follows_aria_pressed_on_toggle(self):
        # Behavior: a chip that starts unselected (aria-pressed="false") has its
        # check indicator hidden by the CSS contract. After a click it becomes
        # aria-pressed="true" (so the CSS shows the check); after another click
        # it returns to "false". No download is fired during toggling.
        result = run_harness(
            "let el = renderAndParse(0);\n"
            "const before = el._buttons.find(b => b.dataset.outputIndex === '1');\n"
            "const beforePressed = before.getAttribute('aria-pressed');\n"
            "const beforeHasCheck = /<span class=\"selection-check\">[\\s\\S]*<svg/.test(before.innerHTML);\n"
            "before.click();\n"
            "el = document.getElementById('card-0'); reparseButtons(el);\n"
            "const afterOn = el._buttons.find(b => b.dataset.outputIndex === '1');\n"
            "const onPressed = afterOn.getAttribute('aria-pressed');\n"
            "const onHasTypeSvg = /<svg\\b/.test(afterOn.innerHTML);\n"
            "afterOn.click();\n"
            "el = document.getElementById('card-0'); reparseButtons(el);\n"
            "const afterOff = el._buttons.find(b => b.dataset.outputIndex === '1');\n"
            "const offPressed = afterOff.getAttribute('aria-pressed');\n"
            "console.log(JSON.stringify({"
            "  beforePressed, beforeHasCheck,"
            "  onPressed, onHasTypeSvg,"
            "  offPressed,"
            "  downloads: fetchUrls,"
            "}));"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        # Initially unselected: check element present but hidden by CSS.
        self.assertEqual(data["beforePressed"], "false")
        self.assertTrue(data["beforeHasCheck"])
        # After click: selected -> CSS would show the check.
        self.assertEqual(data["onPressed"], "true")
        # The video type icon must survive the re-render alongside the check.
        self.assertTrue(data["onHasTypeSvg"])
        # After a second click: back to unselected.
        self.assertEqual(data["offPressed"], "false")
        # Toggling selection must never start a download.
        self.assertEqual(data["downloads"], [])

    def test_vietnamese_title_round_trips_through_render_unchanged(self):
        card = build_card(title="Siêu tổng hợp 3 mùa Dexter trong 2 tiếng rưỡi")
        result = run_harness(
            "const el = renderAndParse(0);\n"
            "const m = el.innerHTML.match(/<h3 class=\"card-title\">([^<]*)<\\/h3>/);\n"
            "console.log(JSON.stringify({ title: m ? m[1] : null }));",
            card=card,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        title = json.loads(result.stdout)["title"]
        self.assertEqual(title, "Siêu tổng hợp 3 mùa Dexter trong 2 tiếng rưỡi")
        # Rendered output must be in composed form -- no stray combining marks.
        self.assertNotIn("̂", title)
        self.assertNotIn("́", title)


if __name__ == "__main__":
    unittest.main()
