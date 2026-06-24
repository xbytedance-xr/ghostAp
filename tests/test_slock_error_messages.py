"""Tests for _SAFE_MESSAGES error mapping - TaskQueueFullError and ExecutorQueueFullError."""


import logging

from src.slock_engine.exceptions import (
    ExecutorQueueFullError,
    TaskQueueFullError,
)
from src.utils.errors import _SAFE_MESSAGES, safe_error_message


class TestSafeMessagesQueueErrors:
    """Test that queue-related errors are properly mapped in _SAFE_MESSAGES."""

    def test_task_queue_full_error_in_safe_messages(self):
        """TaskQueueFullError should be in _SAFE_MESSAGES."""
        assert "TaskQueueFullError" in _SAFE_MESSAGES

    def test_executor_queue_full_error_in_safe_messages(self):
        """ExecutorQueueFullError should be in _SAFE_MESSAGES."""
        assert "ExecutorQueueFullError" in _SAFE_MESSAGES

    def test_task_queue_full_error_message(self):
        """TaskQueueFullError should have a user-friendly message."""
        message = _SAFE_MESSAGES["TaskQueueFullError"]
        assert message
        assert len(message) > 0
        # Should not contain technical jargon that users won't understand
        assert "TaskQueueFullError" not in message

    def test_executor_queue_full_error_message(self):
        """ExecutorQueueFullError should have a user-friendly message."""
        message = _SAFE_MESSAGES["ExecutorQueueFullError"]
        assert message
        assert len(message) > 0
        assert "ExecutorQueueFullError" not in message

    def test_safe_error_message_task_queue_full(self):
        """safe_error_message should return user-friendly message for TaskQueueFullError."""
        try:
            raise TaskQueueFullError("Queue is full with 100 tasks")
        except TaskQueueFullError as e:
            message = safe_error_message(e)
            # Should return the mapped message, not the raw exception
            assert message == _SAFE_MESSAGES["TaskQueueFullError"]
            assert "100 tasks" not in message

    def test_safe_error_message_executor_queue_full(self):
        """safe_error_message should return user-friendly message for ExecutorQueueFullError."""
        try:
            raise ExecutorQueueFullError("Executor queue at capacity")
        except ExecutorQueueFullError as e:
            message = safe_error_message(e)
            assert message == _SAFE_MESSAGES["ExecutorQueueFullError"]
            assert "capacity" not in message

    def test_queue_errors_distinct_messages(self):
        """TaskQueueFullError and ExecutorQueueFullError should have appropriate messages."""
        # Both should be user-friendly but may be same or different
        task_msg = _SAFE_MESSAGES["TaskQueueFullError"]
        exec_msg = _SAFE_MESSAGES["ExecutorQueueFullError"]

        # Both should indicate the system is busy
        assert "请" in task_msg or "稍后" in task_msg or "重试" in task_msg
        assert "请" in exec_msg or "稍后" in exec_msg or "重试" in exec_msg


class TestSafeErrorLogging:
    """Test that safe_error_message logs exceptions with full stack trace."""

    def test_mapped_exception_logged_with_exc_info(self, caplog):
        """Mapped exceptions like ValueError should be logged with exc_info (full stack)."""
        caplog.set_level(logging.WARNING)

        exc = ValueError("Invalid input: secret_data=123")
        try:
            raise exc
        except ValueError:
            message = safe_error_message(exc)

        # Verify log was created
        assert len(caplog.records) == 1
        record = caplog.records[0]

        # Should be a warning level log
        assert record.levelno == logging.WARNING

        # Should contain the exception type name in log message
        assert "ValueError" in record.message
        assert "Mapped exception" in record.message

        # Should have exc_info (full stack trace)
        assert record.exc_info is not None
        assert record.exc_info[0] is ValueError

        # User-facing message should be the safe mapped message
        assert message == _SAFE_MESSAGES["ValueError"]

    def test_unmapped_exception_logged_with_exc_info(self, caplog):
        """Unmapped exceptions should be logged with exc_info (full stack)."""
        caplog.set_level(logging.WARNING)

        # Define a custom exception that's not in _SAFE_MESSAGES
        class CustomUnmappedError(Exception):
            pass

        exc = CustomUnmappedError("Something went wrong internally")
        try:
            raise exc
        except CustomUnmappedError:
            message = safe_error_message(exc)

        # Verify log was created
        assert len(caplog.records) == 1
        record = caplog.records[0]

        # Should be a warning level log
        assert record.levelno == logging.WARNING

        # Should contain "Unmapped exception" in log message
        assert "Unmapped exception" in record.message
        assert "CustomUnmappedError" in record.message

        # Should have exc_info (full stack trace)
        assert record.exc_info is not None
        assert record.exc_info[0] is CustomUnmappedError

        # User-facing message should be the default safe message
        from src.utils.errors import _DEFAULT_SAFE_MESSAGE
        assert message == _DEFAULT_SAFE_MESSAGE

    def test_user_message_sanitized_no_exception_type_name(self, caplog):
        """User-facing message should not contain original exception type name."""
        caplog.set_level(logging.WARNING)

        # Test with mapped exception
        try:
            raise ValueError("test error")
        except ValueError as e:
            user_msg = safe_error_message(e)

        # User message should NOT contain the exception type name
        assert "ValueError" not in user_msg
        # User message should be the safe mapped message
        assert user_msg == _SAFE_MESSAGES["ValueError"]

        # Test with unmapped exception
        class MySecretError(Exception):
            pass

        try:
            raise MySecretError("internal failure")
        except MySecretError as e:
            user_msg2 = safe_error_message(e)

        # User message should NOT contain the custom exception type name
        assert "MySecretError" not in user_msg2
        # Should also not contain "Exception" or "Error" in technical context
        from src.utils.errors import _DEFAULT_SAFE_MESSAGE
        assert user_msg2 == _DEFAULT_SAFE_MESSAGE
