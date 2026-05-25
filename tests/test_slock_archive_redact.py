"""Tests for message archive redaction.

Verifies that sensitive information is redacted before being written
to the message archive, preventing plaintext secrets in local logs.
"""

import os

from src.slock_engine.memory_manager import MemoryManager
from src.utils.redact import redact_sensitive


class TestArchiveRedaction:
    """Test suite for message archive redaction."""

    def test_redact_sensitive_api_key(self) -> None:
        """OpenAI-style API keys should be redacted."""
        original = "My API key is sk-1234567890abcdefghijklmnopqrstuvwxyz"
        redacted = redact_sensitive(original)
        assert "sk-1234567890" not in redacted
        assert "<redacted:api_key>" in redacted

    def test_redact_sensitive_aws_key(self) -> None:
        """AWS access keys should be redacted."""
        original = "AWS Key: AKIAIOSFODNN7EXAMPLE"
        redacted = redact_sensitive(original)
        # AWS key pattern should be redacted (either as aws_key or via assignment pattern)
        assert "AKIAIOSFODNN7EXAMPLE" not in redacted
        assert "<redacted" in redacted

    def test_redact_sensitive_token_assignment(self) -> None:
        """Token assignments should be redacted."""
        original = "GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz1234567890"
        redacted = redact_sensitive(original)
        assert "ghp_abcdef" not in redacted
        assert "<redacted" in redacted

    def test_redact_sensitive_bearer_token(self) -> None:
        """Bearer tokens should be redacted."""
        original = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
        redacted = redact_sensitive(original)
        # JWT token should be redacted
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in redacted
        assert "<redacted" in redacted

    def test_redact_idempotent(self) -> None:
        """Redaction should be idempotent (applying twice is safe)."""
        original = "API_KEY=secret123"
        once = redact_sensitive(original)
        twice = redact_sensitive(once)
        assert once == twice

    def test_non_sensitive_text_unchanged(self) -> None:
        """Non-sensitive text should not be modified."""
        original = "Hello world, this is a normal message without secrets."
        assert redact_sensitive(original) == original

    def test_memory_manager_append_message_archive(self, tmp_path) -> None:
        """MemoryManager should store messages (redaction happens at call site)."""
        mm = MemoryManager(base_path=str(tmp_path))
        channel_id = "test_channel_123"

        # The redaction should happen before calling append_message_archive
        sensitive_msg = "My secret is sk-1234567890abcdefghij"
        redacted_msg = redact_sensitive(sensitive_msg)

        mm.append_message_archive(
            channel_id,
            sender_type="user",
            content=redacted_msg,
            agent_id="agent_1",
            agent_name="TestAgent",
        )

        # Verify the archive file contains redacted content
        archive_path = os.path.join(tmp_path, "archives", channel_id, "messages.jsonl")
        assert os.path.exists(archive_path)

        with open(archive_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        assert len(lines) == 1
        # Original secret should not appear
        assert "sk-1234567890abcdefghij" not in lines[0]
        # Some form of redaction marker should be present
        assert "<redacted" in lines[0]
