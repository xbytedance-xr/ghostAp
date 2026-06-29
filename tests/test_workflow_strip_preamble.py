"""Tests for _strip_markdown_fences robustness against AI preamble text."""

import pytest

from src.feishu.handlers.workflow import WorkflowHandler


class TestStripMarkdownFences:
    """Test _strip_markdown_fences handles various AI output formats."""

    def test_clean_code_unchanged(self):
        """Pure JS code passes through unchanged."""
        code = 'export const meta = { name: "test" };\nexport default async function() {}'
        result = WorkflowHandler._strip_markdown_fences(code)
        assert result == code

    def test_markdown_fence_only(self):
        """Standard markdown-fenced code is correctly extracted."""
        raw = '```javascript\nexport const meta = { name: "x" };\nexport default async function() { await agent("y"); }\n```'
        result = WorkflowHandler._strip_markdown_fences(raw)
        assert result.startswith("export const meta")
        assert "```" not in result

    def test_preamble_then_fence(self):
        """Natural language before a markdown fence is stripped."""
        raw = (
            "Here's the workflow script:\n\n"
            "```javascript\n"
            'export const meta = { name: "x" };\n'
            'export default async function() { await agent("y"); }\n'
            "```"
        )
        result = WorkflowHandler._strip_markdown_fences(raw)
        assert result.startswith("export const meta")
        assert "Here's" not in result
        assert "```" not in result

    def test_preamble_no_fence(self):
        """Natural language before code (no fences) is stripped."""
        raw = (
            "Let me analyze the requirement and generate a script.\n\n"
            'export const meta = { name: "test" };\n'
            'export default async function() { await agent("x"); }'
        )
        result = WorkflowHandler._strip_markdown_fences(raw)
        assert result.startswith("export const meta")
        assert "Let me" not in result

    def test_multi_sentence_preamble_no_fence(self):
        """Multiple sentences of preamble (real-world case from logs)."""
        raw = (
            "Let me first understand the spec engine structure to generate an "
            "informed workflow script.Let me read the key files related to goal "
            "completion control in the spec engine.Now I have a thorough understanding. "
            "Here's the workflow script for analyzing how to improve goal completion "
            "controllability.\n\n"
            'export const meta = {\n'
            '  name: "spec-completion-control-analysis",\n'
            '  description: "Analyze spec mode goal completion controllability",\n'
            '};\n\n'
            'export default async function () {\n'
            '  await agent("do something");\n'
            '}'
        )
        result = WorkflowHandler._strip_markdown_fences(raw)
        assert result.startswith("export const meta")
        assert "Let me" not in result

    def test_comment_before_export_preserved(self):
        """JSDoc comments immediately before export are preserved."""
        raw = (
            "Sure, here's the code:\n\n"
            "/**\n"
            " * Generated workflow\n"
            " */\n"
            'export const meta = { name: "t" };\n'
            'export default async function() { await agent("x"); }'
        )
        result = WorkflowHandler._strip_markdown_fences(raw)
        assert result.startswith("/**")
        assert "Sure" not in result

    def test_line_comment_before_export_preserved(self):
        """Line comments immediately before export are preserved."""
        raw = (
            "Here you go:\n\n"
            "// Auto-generated workflow script\n"
            "// Version: 1.0\n"
            'export const meta = { name: "t" };\n'
            'export default async function() { await agent("x"); }'
        )
        result = WorkflowHandler._strip_markdown_fences(raw)
        assert result.startswith("// Auto-generated")
        assert "Here you go" not in result

    def test_fence_with_uppercase_language(self):
        """Fence with uppercase language tag is handled."""
        raw = '```JavaScript\nexport const meta = { name: "x" };\nexport default async function() {}\n```'
        result = WorkflowHandler._strip_markdown_fences(raw)
        assert result.startswith("export const meta")
        assert "```" not in result

    def test_fence_with_js_tag(self):
        """Fence with 'js' tag is handled."""
        raw = '```js\nexport const meta = { name: "x" };\nexport default async function() {}\n```'
        result = WorkflowHandler._strip_markdown_fences(raw)
        assert result.startswith("export const meta")

    def test_fence_no_language_tag(self):
        """Fence without language tag is handled."""
        raw = '```\nexport const meta = { name: "x" };\nexport default async function() {}\n```'
        result = WorkflowHandler._strip_markdown_fences(raw)
        assert result.startswith("export const meta")

    def test_code_starting_with_const(self):
        """Code starting with 'const' (not export) is not treated as preamble."""
        code = 'const helper = "x";\nexport const meta = { name: "t" };\nexport default async function() {}'
        result = WorkflowHandler._strip_markdown_fences(code)
        # const is valid JS start, should not be stripped
        assert "const helper" in result

    def test_empty_string(self):
        """Empty string returns empty."""
        assert WorkflowHandler._strip_markdown_fences("") == ""

    def test_only_preamble_no_code(self):
        """If no export statement found, content is returned as-is (validator will reject)."""
        raw = "I cannot generate the script because the requirement is unclear."
        result = WorkflowHandler._strip_markdown_fences(raw)
        # No export found, so the method returns the text as-is
        # The validator will then reject it for missing structural elements
        assert result == raw
