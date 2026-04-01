from src.utils.compact import (
    COMPACT_SECTIONS,
    build_compact_prompt,
    extract_summary,
)


class TestCompactSections:
    def test_sections_count(self):
        assert len(COMPACT_SECTIONS) == 7

    def test_sections_are_strings(self):
        assert all(isinstance(s, str) for s in COMPACT_SECTIONS)


class TestBuildCompactPrompt:
    def test_basic_prompt(self):
        history = [
            {"role": "user", "content": "fix the bug"},
            {"role": "assistant", "content": "done"},
        ]
        prompt = build_compact_prompt(history)
        assert "[user]: fix the bug" in prompt
        assert "[assistant]: done" in prompt
        assert "Primary Request" in prompt

    def test_empty_history(self):
        prompt = build_compact_prompt([])
        assert "Conversation:" in prompt

    def test_missing_fields(self):
        prompt = build_compact_prompt([{"other": "value"}])
        assert "[unknown]:" in prompt

    def test_all_sections_present(self):
        prompt = build_compact_prompt([{"role": "user", "content": "hi"}])
        for section in COMPACT_SECTIONS:
            assert section in prompt


class TestExtractSummary:
    def test_extract_with_tags(self):
        response = "<analysis>thinking...</analysis><summary>result here</summary>"
        assert extract_summary(response) == "result here"

    def test_extract_strips_whitespace(self):
        response = "<summary>  trimmed  </summary>"
        assert extract_summary(response) == "trimmed"

    def test_no_summary_tag_strips_analysis(self):
        response = "<analysis>internal</analysis>remaining text"
        assert extract_summary(response) == "remaining text"

    def test_plain_text_passthrough(self):
        response = "just plain text"
        assert extract_summary(response) == "just plain text"

    def test_multiline_summary(self):
        response = "<summary>\n- item1\n- item2\n</summary>"
        result = extract_summary(response)
        assert "- item1" in result
        assert "- item2" in result

    def test_analysis_before_summary(self):
        response = "<analysis>deep thought</analysis>\n<summary>final answer</summary>"
        assert extract_summary(response) == "final answer"
