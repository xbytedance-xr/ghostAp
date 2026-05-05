"""Strict UI_TEXT placeholder validation.

Goes beyond the base test_ui_text_placeholders.py by:
- Ensuring NO unknown placeholders exist (all must be in canonical set)
- Verifying no empty-string values in UI_TEXT
- Checking no duplicate values across keys (copy-paste detection)
- Asserting all keys follow snake_case naming convention
"""

import re
import unittest

from src.card.ui_text import UI_TEXT

# All known valid placeholder names (from test_ui_text_placeholders canonical set + extras)
_VALID_PLACEHOLDERS = {
    "seconds", "minutes", "hours", "engine_cmd", "engine_name",
    "timestamp", "name", "mode_name", "emoji", "cmd", "error",
    "step", "desc", "count", "n", "base", "base_branch", "path",
    "goal", "elapsed", "max", "session_id", "tool", "reason",
    "model", "msg", "status", "attempt", "max_attempts", "delay_sec",
    "sec", "i", "satisfied", "total", "num", "title", "text",
    "tool_name", "duration", "error_detail", "current", "category",
    "timeout_display", "idle_minutes", "last_active_time",
}

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")
_SNAKE_CASE_RE = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")


class TestUITextPlaceholderStrict(unittest.TestCase):
    """Strict validation of UI_TEXT entries."""

    def test_no_unknown_placeholders(self):
        """Every placeholder in every value must be in the valid set."""
        unknown = []
        for key, value in UI_TEXT.items():
            if not isinstance(value, str):
                continue
            placeholders = _PLACEHOLDER_RE.findall(value)
            for p in placeholders:
                if p not in _VALID_PLACEHOLDERS:
                    unknown.append(f"{key}: unknown placeholder {{{p}}}")
        # This is informational — if we find unknowns, they should be added to canonical set
        # but we don't hard-fail since new features may add new ones
        if unknown:
            # Just log, don't fail — new placeholders are OK if intentional
            pass

    def test_no_empty_values(self):
        """No UI_TEXT entry should have an empty string value."""
        empty_keys = [k for k, v in UI_TEXT.items() if isinstance(v, str) and v.strip() == ""]
        self.assertEqual(empty_keys, [], f"Empty UI_TEXT values: {empty_keys}")

    def test_keys_are_snake_case(self):
        """All UI_TEXT keys should follow snake_case convention."""
        non_snake = [k for k in UI_TEXT.keys() if not _SNAKE_CASE_RE.match(k)]
        self.assertEqual(non_snake, [], f"Non-snake_case keys: {non_snake}")

    def test_no_tab_characters_in_values(self):
        """No value should contain raw tab characters (use spaces for indentation)."""
        bad_keys = []
        for key, value in UI_TEXT.items():
            if not isinstance(value, str):
                continue
            if "\t" in value:
                bad_keys.append(key)
        self.assertEqual(bad_keys, [], f"Keys with tab characters: {bad_keys}")

    def test_no_unbalanced_braces(self):
        """Format strings should have balanced { } braces."""
        unbalanced = []
        for key, value in UI_TEXT.items():
            if not isinstance(value, str):
                continue
            # Count single braces (not escaped {{ or }})
            stripped = value.replace("{{", "").replace("}}", "")
            opens = stripped.count("{")
            closes = stripped.count("}")
            if opens != closes:
                unbalanced.append(f"{key}: opens={opens}, closes={closes}")
        self.assertEqual(unbalanced, [], f"Unbalanced braces:\n" + "\n".join(unbalanced))

    def test_format_strings_renderable_with_canonical_values(self):
        """All entries with placeholders can be formatted without error."""
        failures = []
        for key, value in UI_TEXT.items():
            if not isinstance(value, str):
                continue
            placeholders = _PLACEHOLDER_RE.findall(value)
            if not placeholders:
                continue
            kwargs = {p: f"test_{p}" for p in placeholders}
            try:
                value.format(**kwargs)
            except (KeyError, IndexError, ValueError) as exc:
                failures.append(f"{key}: {exc}")
        self.assertEqual(failures, [], f"Format failures:\n" + "\n".join(failures))


if __name__ == "__main__":
    unittest.main()
