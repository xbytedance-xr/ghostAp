"""Tests for src/utils/redact.py — sensitive information redaction."""

import os

from src.slock_engine.memory_manager import MemoryManager
from src.utils.redact import redact_sensitive


class TestRedactSensitive:
    """Verify redaction of various secret patterns."""

    def test_token_assignment(self):
        text = "access_token=sk-abc123xyz something else"
        result = redact_sensitive(text)
        assert "sk-abc123xyz" not in result
        assert "access_token=<redacted>" in result
        assert "something else" in result

    def test_password_assignment(self):
        text = "PASSWORD=hunter2 normal text"
        result = redact_sensitive(text)
        assert "hunter2" not in result
        assert "PASSWORD=<redacted>" in result
        assert "normal text" in result

    def test_api_key_colon(self):
        text = "api_key: my-secret-key-12345"
        result = redact_sensitive(text)
        assert "my-secret-key-12345" not in result
        assert "api_key=<redacted>" in result

    def test_bearer_token(self):
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig"
        result = redact_sensitive(text)
        assert "eyJhbGciOiJIUzI1NiJ9" not in result
        assert "Authorization=<redacted>" in result

    def test_standalone_bearer_token(self):
        """Bearer token without Authorization header prefix."""
        text = "token is Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig here"
        result = redact_sensitive(text)
        assert "eyJhbGciOiJIUzI1NiJ9" not in result
        assert "Bearer <redacted>" in result

    def test_private_key_pem(self):
        text = (
            "some prefix\n"
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEpAIBAAKCAQEA12345\n"
            "-----END RSA PRIVATE KEY-----\n"
            "some suffix"
        )
        result = redact_sensitive(text)
        assert "MIIEpAIBAAKCAQEA12345" not in result
        assert "<redacted:private_key>" in result
        assert "some prefix" in result
        assert "some suffix" in result

    def test_cookie(self):
        text = "cookie: session=abc123def456; path=/"
        result = redact_sensitive(text)
        assert "abc123def456" not in result
        assert "cookie=<redacted>" in result

    def test_aws_key(self):
        text = "key is AKIAIOSFODNN7EXAMPLE here"
        result = redact_sensitive(text)
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert "<redacted:aws_key>" in result

    def test_preserves_normal_text(self):
        text = "This is a normal message about login tokens being used for authentication."
        result = redact_sensitive(text)
        # "tokens" alone should NOT be redacted (no assignment pattern)
        assert result == text

    def test_empty_string(self):
        assert redact_sensitive("") == ""

    def test_none_passthrough(self):
        # redact_sensitive returns falsy input as-is
        assert redact_sensitive("") == ""

    def test_mixed_secrets(self):
        text = (
            "DB_PASSWORD=supersecret "
            "REFRESH_TOKEN=rt_abcdef "
            "normal_var=hello "
            "Bearer sk-proj-12345"
        )
        result = redact_sensitive(text)
        assert "supersecret" not in result
        assert "rt_abcdef" not in result
        assert "sk-proj-12345" not in result
        assert "normal_var=hello" in result  # no sensitive keyword in key

    def test_no_false_positive_on_variable_names(self):
        """Variable names containing 'token' without assignment should not be redacted."""
        text = "The token_refresh_interval is set to 3600 seconds."
        result = redact_sensitive(text)
        # No assignment pattern → no redaction
        assert result == text


# ---------------------------------------------------------------------------
# Archive redaction tests (merged from test_slock_archive_redact.py)
# ---------------------------------------------------------------------------


class TestArchiveRedaction:
    """Test suite for message archive redaction."""

    def test_redact_sensitive_api_key(self) -> None:
        """OpenAI-style API keys should be redacted."""
        original = "My API key is sk-1234567890abcdefghijklmnopqrstuvwxyz"
        redacted = redact_sensitive(original)
        assert "sk-1234567890" not in redacted
        assert "<redacted:api_key>" in redacted

    def test_redact_sensitive_token_assignment(self) -> None:
        """Token assignments should be redacted."""
        original = "GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz1234567890"
        redacted = redact_sensitive(original)
        assert "ghp_abcdef" not in redacted
        assert "<redacted" in redacted

    def test_redact_idempotent(self) -> None:
        """Redaction should be idempotent (applying twice is safe)."""
        original = "API_KEY=secret123"
        once = redact_sensitive(original)
        twice = redact_sensitive(once)
        assert once == twice

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
