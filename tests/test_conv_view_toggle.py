"""The transcript view is a Compact | Normal | Verbose segmented control.

Static-source guard: all three segments render in one control, persisted under
'ccc-conv-view', Verbose stays the only mode that expands tools, and the
compact CSS beats the conv-bg themes' !important padding.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


class ConvViewToggleTest(unittest.TestCase):
    def test_three_segments_visible(self):
        html = _read("static/index.html")
        for seg in ("compact", "normal", "verbose"):
            self.assertEqual(html.count('data-conv-view="%s"' % seg), 2, seg)

    def test_modes_persist(self):
        js = _read("static/app.js")
        self.assertIn("const CONV_VIEW_MODES = ['compact', 'normal', 'verbose'];", js)
        self.assertIn("document.body.dataset.convView = mode;", js)
        self.assertIn("localStorage.setItem('ccc-conv-view', mode)", js)
        self.assertIn("function convVerboseOn() { return convViewMode() === 'verbose'; }", js)
        self.assertIn("document.body.classList.toggle('conv-compact', mode === 'compact')", js)

    def test_legacy_verbose_key_still_honored(self):
        js = _read("static/app.js")
        self.assertIn("localStorage.getItem('ccc-conv-verbose') === '1' ? 'verbose' : 'normal'", js)

    def test_compact_collapses_and_outranks_stitch_theme(self):
        css = _read("static/app.css")
        rule = re.search(
            r"body\.conv-compact\.conv-compact \.conversations-view \.event\.assistant \.assistant-text \{([^}]*)\}", css)
        self.assertIsNotNone(rule)
        self.assertIn("padding: 3px 10px !important", rule.group(1))
        # Collapsing, not shrinking: messages clamp to two lines and the
        # newest assistant reply is exempt.
        self.assertIn(".event.assistant:has(~ .event.assistant .assistant-text) .assistant-text:not(.cc-open)", css)
        self.assertIn("-webkit-line-clamp: 2;", css)

    def test_tap_expands_in_compact(self):
        js = _read("static/app.js")
        self.assertIn("t.closest('.assistant-text, .event.user_text, .thinking-block, .tool-call')", js)
        self.assertIn("el.classList.toggle('cc-open')", js)


if __name__ == "__main__":
    unittest.main()
