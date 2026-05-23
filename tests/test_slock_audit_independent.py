"""Tests that audit log writes to AUDIT_LOG.md independently from SHARED_MEMORY.md."""

from __future__ import annotations

import os
import tempfile
import time

import pytest


@pytest.fixture
def mm_tempdir():
    """Create a MemoryManager with a fresh temp directory and clean shutdown."""
    from src.slock_engine.memory_manager import MemoryManager

    tmpdir = tempfile.mkdtemp(prefix="slock_audit_test_")
    mgr = MemoryManager(base_path=tmpdir)
    yield mgr, tmpdir
    mgr.shutdown()


class TestAuditLogIndependent:
    """Verify audit log is decoupled from SHARED_MEMORY.md."""

    def test_audit_log_written_to_audit_log_md(self, mm_tempdir):
        """append_audit_log() should create global/AUDIT_LOG.md with expected content."""
        mgr, tmpdir = mm_tempdir

        mgr.append_audit_log(
            operator_id="op-1",
            action="test_action",
            target="agent-x",
            detail="sample detail",
        )

        # Allow the async writer to flush
        mgr.shutdown()
        time.sleep(0.1)

        audit_path = os.path.join(tmpdir, "global", "AUDIT_LOG.md")
        assert os.path.exists(audit_path), "AUDIT_LOG.md should exist after append_audit_log()"

        with open(audit_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Check table header
        assert "| Timestamp | Operator | Action | Target | Detail |" in content
        assert "|---|---|---|---|---|" in content

        # Check data row
        assert "| op-1 |" in content
        assert "| test_action |" in content
        assert "| agent-x |" in content
        assert "| sample detail |" in content

    def test_shared_memory_not_polluted_by_audit(self, mm_tempdir):
        """SHARED_MEMORY.md in global/ should NOT contain audit table rows."""
        mgr, tmpdir = mm_tempdir

        mgr.append_audit_log(
            operator_id="op-2",
            action="write_test",
            target="agent-y",
            detail="should not appear in shared memory",
        )

        # Flush
        mgr.shutdown()
        time.sleep(0.1)

        shared_memory_path = os.path.join(tmpdir, "global", "SHARED_MEMORY.md")

        if os.path.exists(shared_memory_path):
            with open(shared_memory_path, "r", encoding="utf-8") as f:
                content = f.read()
            # Audit rows must NOT be present
            assert "| op-2 |" not in content
            assert "| write_test |" not in content
            assert "| Timestamp | Operator | Action | Target | Detail |" not in content
        # If the file doesn't exist at all, the test passes (no pollution)

    def test_audit_log_contains_multiple_rows(self, mm_tempdir):
        """Multiple audit entries should all appear in AUDIT_LOG.md."""
        mgr, tmpdir = mm_tempdir

        for i in range(5):
            mgr.append_audit_log(
                operator_id=f"op-{i}",
                action=f"action-{i}",
                target=f"target-{i}",
                detail=f"detail-{i}",
            )

        mgr.shutdown()
        time.sleep(0.1)

        audit_path = os.path.join(tmpdir, "global", "AUDIT_LOG.md")
        assert os.path.exists(audit_path)

        with open(audit_path, "r", encoding="utf-8") as f:
            content = f.read()

        for i in range(5):
            assert f"| op-{i} |" in content
            assert f"| action-{i} |" in content
            assert f"| target-{i} |" in content
