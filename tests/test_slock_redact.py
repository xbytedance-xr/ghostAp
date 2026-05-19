"""Tests for src/utils/redact.py — sensitive information redaction."""

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
