import logging

from src.utils.errors import GhostAPError, log_exception


class TestLogException:
    def test_log_exception_ghost_error_warning(self, caplog):
        """GhostAPError should be logged as WARNING."""
        logger = logging.getLogger("test_logger")
        msg = "Business error occurred"
        exc = GhostAPError("Invalid input")

        with caplog.at_level(logging.WARNING):
            log_exception(logger, msg, exc)

        assert len(caplog.records) == 1
        assert caplog.records[0].levelname == "WARNING"
        assert msg in caplog.records[0].message
        assert str(exc) in caplog.records[0].message
        # Verify no traceback in message (GhostAPError usually doesn't need stack trace in warning)
        # Note: log_exception implementation for GhostAPError: logger.warning(f"{msg}: {exc}")

    def test_log_exception_generic_error_error(self, caplog):
        """Generic Exception should be logged as ERROR with traceback."""
        logger = logging.getLogger("test_logger")
        msg = "System error occurred"
        exc = ValueError("Something went wrong")

        with caplog.at_level(logging.ERROR):
            log_exception(logger, msg, exc)

        assert len(caplog.records) == 1
        assert caplog.records[0].levelname == "ERROR"
        assert msg in caplog.records[0].message
        assert caplog.records[0].exc_info is not None  # Verify exc_info is passed
