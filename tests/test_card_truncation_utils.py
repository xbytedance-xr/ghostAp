"""Tests for src/card/truncation.py — unified truncation utilities."""


from src.card.truncation import (
    cap_reasoning_tail,
    truncate_bash_output,
    truncate_card_string,
    truncate_terminal_message,
)

# ── truncate_card_string ──────────────────────────────────────


class TestTruncateCardString:
    def test_empty_string(self):
        assert truncate_card_string("") == ""

    def test_within_limits(self):
        short = "hello world"
        assert truncate_card_string(short) == short

    def test_exact_char_limit(self):
        exact = "a" * 220
        assert truncate_card_string(exact) == exact

    def test_over_char_limit(self):
        over = "a" * 300
        result = truncate_card_string(over)
        assert result.endswith("\n...[truncated]")
        # Content before notice should be max_chars long
        assert len(result) == 220 + len("\n...[truncated]")

    def test_exact_line_limit(self):
        lines = "\n".join(f"line{i}" for i in range(6))
        assert truncate_card_string(lines) == lines

    def test_over_line_limit(self):
        lines = "\n".join(f"line{i}" for i in range(10))
        result = truncate_card_string(lines)
        assert result.endswith("\n...[truncated]")
        # Should keep only first 6 lines
        content = result.replace("\n...[truncated]", "")
        assert content.count("\n") == 5  # 6 lines = 5 newlines

    def test_both_limits_exceeded_lines_first(self):
        # Many long lines — line limit should trigger first
        lines = "\n".join("x" * 100 for _ in range(20))
        result = truncate_card_string(lines, max_chars=220, max_lines=6)
        assert result.endswith("\n...[truncated]")

    def test_custom_limits(self):
        result = truncate_card_string("abcdef", max_chars=3, max_lines=100)
        assert result == "abc\n...[truncated]"


# ── truncate_bash_output ──────────────────────────────────────


class TestTruncateBashOutput:
    def test_defaults(self):
        short = "ok"
        assert truncate_bash_output(short) == short

    def test_over_default_chars(self):
        over = "x" * 300
        result = truncate_bash_output(over)
        assert result.endswith("\n...[truncated]")
        assert len(result) == 240 + len("\n...[truncated]")

    def test_over_default_lines(self):
        lines = "\n".join(f"line{i}" for i in range(12))
        result = truncate_bash_output(lines)
        content = result.replace("\n...[truncated]", "")
        assert content.count("\n") == 7  # 8 lines = 7 newlines


# ── cap_reasoning_tail ────────────────────────────────────────


class TestCapReasoningTail:
    def test_empty_string(self):
        assert cap_reasoning_tail("") == ""

    def test_within_limit(self):
        short = "thinking..."
        assert cap_reasoning_tail(short) == short

    def test_exact_limit(self):
        exact = "a" * 500
        assert cap_reasoning_tail(exact) == exact

    def test_over_limit(self):
        over = "a" * 1000
        result = cap_reasoning_tail(over)
        assert result.startswith("...\n")
        # Tail portion should be exactly max_chars
        tail = result[len("...\n"):]
        assert len(tail) == 500

    def test_custom_limit(self):
        result = cap_reasoning_tail("abcdefghij", max_chars=5)
        assert result == "...\nfghij"


# ── truncate_terminal_message ─────────────────────────────────


class TestTruncateTerminalMessage:
    def test_empty_string(self):
        assert truncate_terminal_message("") == ""

    def test_within_limit(self):
        short = "done"
        assert truncate_terminal_message(short) == short

    def test_exact_limit(self):
        exact = "a" * 1600
        assert truncate_terminal_message(exact) == exact

    def test_over_limit(self):
        over = "a" * 2000
        result = truncate_terminal_message(over)
        assert result.endswith("…")
        assert len(result) == 1600
