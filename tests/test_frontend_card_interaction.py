"""Regression tests for the queue card's static output summary and for the
hover/focus interaction shape.

Two distinct defects are locked down here:

  1. The non-ready ("static") output summary rendered a text-only chip, so a
     card that was downloading or errored showed ``MP4 video 137`` or
     ``1 video + audio`` with no icon at all -- even though the ready branch
     pairs every chip with an SVG type icon.
  2. A hovered card painted a left-to-right gradient onto a square, zero-inset
     box, so the highlight began with a hard vertical edge flush against the
     thumbnail and read as a clipped patch rather than a rounded row. The
     parent ``.manifest`` sets ``overflow: hidden``, so the shape has to come
     from padding plus a radius -- never from a negative margin.

The icon tests execute the real ``renderCard`` in Node against the DOM stub
already built for test_frontend_output_selection and assert on the markup it
actually produced. The shape tests parse the real ``<style>`` blocks.
"""
import json
import re
import shutil
import subprocess
import unittest

from tests.test_frontend_output_selection import (
    HARNESS_PRELUDE,
    _css_rule_blocks,
    _normalize_selector,
    _style_blocks,
    build_card,
    function_source,
    icon_constants,
    read_template,
)


def rules_for(css, selector):
    """Every rule body whose selector list contains `selector` exactly, in
    source order. Media-query bodies are descended into, so a desktop rule and
    its mobile override both appear (desktop first)."""
    target = _normalize_selector(selector)
    bodies = []
    for selector_list, body in _css_rule_blocks(css):
        for sel in selector_list.split(","):
            if _normalize_selector(sel) == target:
                bodies.append(body)
                break
    return bodies


def horizontal_padding(body):
    """The horizontal component of a `padding:` shorthand, in px, or None."""
    match = re.search(r"padding:\s*([^;]+);", body)
    if not match:
        return None
    parts = match.group(1).split()
    value = parts[1] if len(parts) > 1 else parts[0]
    digits = re.sub(r"[^0-9]", "", value)
    return int(digits) if digits else 0


NODE = shutil.which("node")


@unittest.skipIf(NODE is None, "node is required to execute renderCard")
class StaticSummaryIconTests(unittest.TestCase):
    """The non-ready summary must carry the same type icons the ready chips do."""

    @classmethod
    def setUpClass(cls):
        cls.template = read_template()

    def render_chips(self, card):
        """Render one card with the real renderCard and return the innerHTML of
        its .chips container."""
        body = (
            "const el = renderAndParse(0);\n"
            "const m = /<div class=\"chips\">([\\s\\S]*?)<\\/div>/.exec(el.innerHTML);\n"
            "if (!m) { console.error('no .chips container'); process.exit(3); }\n"
            "process.stdout.write(m[1]);"
        )
        script = "\n".join([
            HARNESS_PRELUDE,
            icon_constants(self.template),
            function_source(self.template, "esc"),
            function_source(self.template, "normalizeText"),
            function_source(self.template, "renderCard"),
            f"cardData = [{json.dumps(card)}];",
            body,
        ])
        result = subprocess.run(
            [NODE, "-"], input=script, capture_output=True, text=True,
            encoding="utf-8", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def assert_inline_svg_contract(self, chips):
        # Inline SVG, decorative (the text label carries the meaning), not
        # focusable, and inheriting the chip's colour.
        self.assertIn("<svg", chips)
        self.assertIn('aria-hidden="true"', chips)
        self.assertIn('focusable="false"', chips)
        self.assertIn('stroke="currentColor"', chips)

    # ICON_VIDEO is a <rect> screen plus a play wedge; ICON_AUDIO is a note with
    # two <circle> beads. Asserting on those primitives proves which branch ran,
    # not merely that some icon appeared.
    def assert_video_icon(self, chips):
        self.assertIn("<rect", chips)

    def assert_audio_icon(self, chips):
        self.assertIn("<circle", chips)

    def test_downloading_video_only_summary_has_a_video_icon(self):
        chips = self.render_chips(build_card(
            status="downloading",
            selectedOutputs=[{"type": "video", "format_id": "137"}],
        ))
        self.assertIn("chip-static", chips)
        self.assertIn("MP4 video 137", chips)
        self.assert_inline_svg_contract(chips)
        self.assert_video_icon(chips)
        self.assertNotIn("<circle", chips)

    def test_downloading_audio_only_summary_has_an_audio_icon(self):
        chips = self.render_chips(build_card(
            status="downloading",
            selectedOutputs=[{"type": "audio"}],
        ))
        self.assertIn("chip-static", chips)
        self.assertIn("MP3 audio", chips)
        self.assert_inline_svg_contract(chips)
        self.assert_audio_icon(chips)
        self.assertNotIn("<rect", chips)

    def test_multi_output_downloading_summary_shows_both_icons(self):
        chips = self.render_chips(build_card(
            status="downloading",
            selectedOutputs=[{"type": "video", "format_id": "137"}, {"type": "audio"}],
        ))
        self.assertIn("1 video + audio", chips)
        self.assert_inline_svg_contract(chips)
        self.assert_video_icon(chips)
        self.assert_audio_icon(chips)
        # Both, not just the first: a mixed summary that showed one icon would
        # misdescribe what is being downloaded.
        self.assertEqual(chips.count("<svg"), 2)

    def test_multi_output_error_summary_shows_both_icons(self):
        chips = self.render_chips(build_card(
            status="error",
            error="boom",
            selectedOutputs=[{"type": "video", "format_id": "137"}, {"type": "audio"}],
        ))
        self.assertIn("1 video + audio", chips)
        self.assert_video_icon(chips)
        self.assert_audio_icon(chips)
        self.assertEqual(chips.count("<svg"), 2)

    def test_multi_video_summary_still_shows_a_video_icon(self):
        chips = self.render_chips(build_card(
            status="downloading",
            selectedOutputs=[
                {"type": "video", "format_id": "137"},
                {"type": "video", "format_id": "136"},
            ],
        ))
        self.assertIn("2 videos", chips)
        self.assert_video_icon(chips)
        # No audio was selected, so no audio icon may be implied.
        self.assertNotIn("<circle", chips)
        self.assertEqual(chips.count("<svg"), 1)

    def test_legacy_card_without_selected_outputs_still_gets_an_icon(self):
        chips = self.render_chips(build_card(
            status="downloading", selectedOutputs=None, downloadFormat="audio",
        ))
        self.assertIn("MP3 audio", chips)
        self.assert_inline_svg_contract(chips)
        self.assert_audio_icon(chips)

    def test_queued_and_done_summaries_also_carry_an_icon(self):
        for status in ("queued", "done", "cancelled", "timed_out"):
            with self.subTest(status=status):
                chips = self.render_chips(build_card(
                    status=status, error="x",
                    selectedOutputs=[{"type": "video", "format_id": "137"}],
                ))
                self.assertIn("chip-static", chips)
                self.assert_inline_svg_contract(chips)
                self.assert_video_icon(chips)

    def test_ready_card_keeps_interactive_chips_and_selection_indicator(self):
        # Non-regression: the static branch must not have displaced the ready
        # chips, their icons, or their aria-pressed selection state.
        chips = self.render_chips(build_card())
        self.assertNotIn("chip-static", chips)
        self.assertIn("chip quality", chips)
        self.assertIn('aria-pressed="true"', chips)
        self.assertIn("selection-check", chips)
        self.assert_inline_svg_contract(chips)

    def test_static_summary_declares_no_selection_state(self):
        # It is a status summary, not a selectable control; claiming a pressed
        # state would misreport it to assistive technology.
        chips = self.render_chips(build_card(
            status="downloading",
            selectedOutputs=[{"type": "video", "format_id": "137"}],
        ))
        self.assertNotIn("selection-check", chips)
        self.assertNotIn("aria-pressed", chips)
        self.assertNotIn("<button", chips)

    def test_no_unicode_glyph_and_no_external_icon_dependency(self):
        cases = (
            ("downloading", [{"type": "video", "format_id": "137"}]),
            ("downloading", [{"type": "audio"}]),
            ("error", [{"type": "video", "format_id": "137"}, {"type": "audio"}]),
        )
        for status, outputs in cases:
            with self.subTest(status=status, outputs=outputs):
                chips = self.render_chips(build_card(
                    status=status, error="x", selectedOutputs=outputs,
                ))
                for glyph in ("✓", "▶", "♪", "♫",
                              "\U0001f3b5", "\U0001f3ac", "\U0001f50a", "⏵"):
                    self.assertNotIn(glyph, chips)
                self.assertNotIn("<img", chips)
                self.assertNotIn("http://", chips)
                self.assertNotIn("https://", chips)


class StaticSummarySourceTests(unittest.TestCase):
    """Source contract for the summary, so the icon requirement is asserted even
    where Node is unavailable."""

    def setUp(self):
        self.template = read_template()
        self.render = function_source(self.template, "renderCard")

    def test_static_chip_interpolates_an_icon_alongside_the_label(self):
        match = re.search(r'chip chip-static">\$\{([^}]+)\}', self.render)
        self.assertIsNotNone(
            match, "the static summary chip must interpolate an icon before its label"
        )
        self.assertIn("Icon", match.group(1))

    def test_every_summary_branch_assigns_an_icon(self):
        # formatLabel is computed in three branches (multi / single / legacy);
        # each one must set the matching icon or a state would render bare.
        self.assertEqual(len(re.findall(r"\bformatLabel\s*=", self.render)),
                         len(re.findall(r"\bsummaryIcons\s*=", self.render)),
                         "every formatLabel branch must also assign summaryIcons")

    def test_summary_icons_use_the_shared_icon_constants(self):
        self.assertIn("ICON_VIDEO", self.render)
        self.assertIn("ICON_AUDIO", self.render)


class CardInteractionShapeTests(unittest.TestCase):
    """The hover/focus highlight must be a complete rounded, inset row."""

    @classmethod
    def setUpClass(cls):
        cls.css = _style_blocks(read_template())

    def desktop_card_body(self):
        bodies = rules_for(self.css, ".card")
        self.assertTrue(bodies, ".card rule must exist")
        return bodies[0]

    def test_card_defines_a_border_radius(self):
        # Without a radius the hover gradient terminates in a square vertical
        # edge -- exactly the reported "cut off at the left" artefact.
        self.assertIn(
            "border-radius", self.desktop_card_body(),
            ".card must define a border-radius so the hover/focus background "
            "has a complete rounded shape",
        )

    def test_card_radius_is_never_flattened_anywhere(self):
        for body in rules_for(self.css, ".card"):
            self.assertNotRegex(body, r"border-radius:\s*0")

    def test_card_has_non_zero_horizontal_padding(self):
        pad = horizontal_padding(self.desktop_card_body())
        self.assertIsNotNone(pad, ".card must set padding")
        self.assertGreater(
            pad, 0,
            "the card needs its own horizontal padding so the thumbnail is not "
            "glued to the hover edge and the radius has room to show",
        )

    def test_hover_and_focus_within_share_the_interaction_background(self):
        hover = rules_for(self.css, ".card:hover")
        focus = rules_for(self.css, ".card:focus-within")
        self.assertTrue(hover, ".card:hover rule must exist")
        self.assertTrue(focus, ".card:focus-within must exist for keyboard users")
        self.assertTrue(any("background" in b for b in hover))
        self.assertTrue(any("background" in b for b in focus))

    def test_focus_within_is_not_trapped_in_the_pointer_only_media_query(self):
        match = re.search(
            r"@media\s*\(hover:\s*hover\)[^{]*\{(.*?)\n    \}", self.css, re.DOTALL
        )
        self.assertIsNotNone(match, "the (hover: hover) query must exist")
        self.assertNotIn(
            ":focus-within", match.group(1),
            ".card:focus-within must live outside the hover-only query or "
            "keyboard and touch users never get the interaction shape",
        )

    def test_no_negative_margin_escapes_the_clipping_parent(self):
        # .manifest sets overflow: hidden, so a negative margin would be clipped
        # and reintroduce the square edge it was meant to cure.
        for selector in (".cards", ".card", ".card:hover", ".card:focus-within",
                         ".card::after"):
            for body in rules_for(self.css, selector):
                with self.subTest(selector=selector):
                    self.assertIsNone(
                        re.search(r"margin[a-z-]*:\s*[^;]*(?:^|[\s(])-\d", body),
                        f"{selector} must not use a negative margin",
                    )

    def test_manifest_still_clips_its_children(self):
        # Documents why the negative-margin ban above matters.
        self.assertTrue(
            any("overflow: hidden" in b for b in rules_for(self.css, ".manifest"))
        )

    def test_interaction_states_do_not_shift_layout(self):
        for selector in (".card:hover", ".card:focus-within"):
            for body in rules_for(self.css, selector):
                with self.subTest(selector=selector):
                    self.assertNotIn("padding", body)
                    self.assertNotIn("border-width", body)
                    self.assertNotRegex(body, r"(?<![-a-z])border:\s")

    def test_separator_between_cards_survives(self):
        separators = rules_for(self.css, ".card::after")
        self.assertTrue(separators, "cards need a separator rule")
        self.assertTrue(
            any("background" in b and "position: absolute" in b for b in separators),
            "the separator must be an inset pseudo-element: a border-bottom on a "
            "rounded box curves at the corners and reads as a broken line",
        )

    def test_separator_is_inset_so_it_does_not_cross_the_rounded_corners(self):
        body = rules_for(self.css, ".card::after")[0]
        left = re.search(r"left:\s*(\d+)px", body)
        right = re.search(r"right:\s*(\d+)px", body)
        self.assertIsNotNone(left, "the separator must be inset from the left")
        self.assertIsNotNone(right, "the separator must be inset from the right")
        self.assertGreater(int(left.group(1)), 0)
        self.assertEqual(left.group(1), right.group(1),
                         "the separator inset must be symmetric")

    def test_last_card_draws_no_extra_separator(self):
        last = rules_for(self.css, ".card:last-child::after")
        self.assertTrue(last, "the last card must suppress its separator")
        self.assertTrue(any("display: none" in b for b in last))

    def test_error_card_keeps_its_danger_styling_under_hover(self):
        error_rules = [
            (sel, body) for sel, body in _css_rule_blocks(self.css)
            if 'data-status="error"' in sel
        ]
        self.assertTrue(error_rules, "the error card rule must still exist")
        self.assertTrue(any("--danger-bg" in body for _, body in error_rules))
        # .card:hover and .card[data-status="error"] have the same specificity,
        # so source order decides. The error rule must come later, otherwise a
        # hovered error card would lose its error signal.
        self.assertGreater(
            self.css.index('.card[data-status="error"]'),
            self.css.index(".card:hover"),
            "error styling must be declared after :hover so a hovered error card "
            "still reads as an error",
        )

    def test_desktop_card_inset_matches_the_manifest_header(self):
        # .cards gives up part of its padding to .card so the card can round off
        # inside the row; the sum must still line rows up with the header.
        cards_pad = horizontal_padding(rules_for(self.css, ".cards")[0])
        card_pad = horizontal_padding(self.desktop_card_body())
        head_pad = horizontal_padding(rules_for(self.css, ".manifest-head")[0])
        self.assertEqual(
            cards_pad + card_pad, head_pad,
            "card content must stay aligned with the manifest header",
        )

    def test_mobile_card_inset_matches_the_mobile_manifest_header(self):
        cards_rules = rules_for(self.css, ".cards")
        head_rules = rules_for(self.css, ".manifest-head")
        self.assertGreaterEqual(len(cards_rules), 2, ".cards needs a mobile override")
        self.assertGreaterEqual(len(head_rules), 2,
                                ".manifest-head needs a mobile override")
        card_rules = rules_for(self.css, ".card")
        # The mobile .card rule may retune the grid without restating padding;
        # in that case the desktop padding still applies.
        card_pad = next(
            (horizontal_padding(b) for b in reversed(card_rules)
             if horizontal_padding(b) is not None),
            None,
        )
        self.assertIsNotNone(card_pad)
        self.assertEqual(
            horizontal_padding(cards_rules[-1]) + card_pad,
            horizontal_padding(head_rules[-1]),
            "the mobile breakpoint must keep rows aligned with the header while "
            "leaving the card its own padding for the rounded shape",
        )

    def test_reduced_motion_still_neutralises_the_card_transition(self):
        self.assertIn("@media (prefers-reduced-motion: reduce)", self.css)
        self.assertIn("transition", self.desktop_card_body())
        reduce_block = re.search(
            r"@media \(prefers-reduced-motion: reduce\)\s*\{(.*?)\n    \}",
            self.css, re.DOTALL,
        )
        self.assertIsNotNone(reduce_block)
        self.assertIn("transition-duration", reduce_block.group(1))


if __name__ == "__main__":
    unittest.main()
