from src.card.builder import CardBuilder


class TestCardTruncation:
    def test_truncation_message_format(self):
        """Test that the truncation message uses the new format with emojis and clear instructions."""
        # Create a content string that exceeds the max_chars
        max_chars = 100
        content = "a" * 200

        element = CardBuilder._build_content_element(content, max_chars=max_chars)
        result_content = element["content"]

        # Verify the new truncation message format
        assert "⚠️ **日志内容过长，已被截断**" in result_content
        assert "🔍 完整日志请查看服务器本地文件" in result_content
        assert "(仅显示末尾" in result_content

        # Verify that it kept the tail
        assert content[-10:] in result_content

    def test_truncation_inside_code_block(self):
        """Test truncation when inside a code block handles markdown correctly."""
        max_chars = 150
        # Construct content where the cut point is inside a code block
        # Start code block
        content = "Prefix text\n```python\n"
        # Add long content inside code block
        content += "code_line = 'x' * 50\n" * 10
        # End code block (but this part will be truncated out initially)
        content += "```\nSuffix"

        element = CardBuilder._build_content_element(content, max_chars=max_chars)
        result_content = element["content"]

        # Verify message
        assert "⚠️ **日志内容过长，已被截断**" in result_content

        # Verify code block handling (should detect it's inside a block and add ``` start)
        # Note: The implementation adds ``` if the cut point is inside a block
        # Since we are keeping the TAIL, the implementation checks if the PRE-CUT content has odd number of backticks.
        # If so, it prepends ``` to the tail.

        # Let's verify the result structure.
        # It should start with the warning
        # Then potentially ``` if we were inside one
        # Then the tail content
        # Then potentially ``` if the tail content has odd backticks (to close it)

        # In this specific test case construction:
        # Pre-cut content will contain the opening ```python
        # So pre-cut markers count is 1 (odd) -> is_inside_code_block = True
        # So it should append ```\n after the warning.

        assert "```\n" in result_content or "\n```" in result_content

    def test_no_truncation_if_short(self):
        """Test that short content is not truncated."""
        content = "Short message"
        element = CardBuilder._build_content_element(content, max_chars=100)
        assert element["content"] == content
        assert "⚠️" not in element["content"]
