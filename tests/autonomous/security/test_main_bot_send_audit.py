from __future__ import annotations

from pathlib import Path

import pytest

from src.autonomous.acceptance.main_bot_audit import MainBotSendAuditLog
from src.autonomous.journal.anchor import FileAnchor
from src.autonomous.journal.writer import JournalWriter


def _audit(tmp_path: Path) -> MainBotSendAuditLog:
    writer = JournalWriter.open(
        tmp_path / "audit",
        anchor=FileAnchor(tmp_path / "audit.anchor"),
        hmac_key=b"a" * 32,
        writer_epoch=1,
    )
    return MainBotSendAuditLog(writer)


def test_main_bot_audit_counts_matching_and_unknown_tenant_attempts(tmp_path: Path) -> None:
    audit = _audit(tmp_path)
    audit.record_attempt("tenant-a", "reply", "om_message", attempted_at=100.0)
    audit.record_attempt("", "create", "oc_chat", attempted_at=101.0)
    audit.record_attempt("tenant-b", "patch", "om_other", attempted_at=102.0)

    assert audit.count_attempts("tenant-a", 99.0, 102.0) == 2
    assert audit.count_attempts("tenant-b", 99.0, 103.0) == 2
    assert audit.count_attempts("tenant-a", 102.0, 103.0) == 0
    raw = audit.writer.journal_path.read_text(encoding="utf-8")
    assert "om_message" not in raw
    assert "tenant-a" not in raw
    audit.close()


def test_main_bot_audit_rejects_invalid_query_and_fails_after_write_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit = _audit(tmp_path)
    with pytest.raises(ValueError, match="window"):
        audit.count_attempts("tenant-a", 2.0, 1.0)

    monkeypatch.setattr(audit.writer, "commit", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError, match="disk"):
        audit.record_attempt("tenant-a", "reply", "om_message", attempted_at=100.0)
    with pytest.raises(RuntimeError, match="incomplete"):
        audit.count_attempts("tenant-a", 99.0, 101.0)
    audit.close()


def test_main_bot_audit_uses_external_cross_replica_ledger_as_authority(
    tmp_path: Path,
) -> None:
    class ExternalAudit:
        def __init__(self) -> None:
            self.records: list[dict[str, object]] = []
            self.count = 3

        def record_main_bot_send_attempt(self, **kwargs) -> None:
            self.records.append(kwargs)

        def count_main_bot_send_attempts(self, tenant_key, start, end) -> int:
            assert (tenant_key, start, end) == ("tenant-a", 99.0, 101.0)
            return self.count

    external = ExternalAudit()
    audit = _audit(tmp_path)
    audit.external_audit = external
    audit.record_attempt("tenant-a", "reply", "om_message", attempted_at=100.0)

    assert external.records[0]["tenant_hash"] != "tenant-a"
    assert external.records[0]["target_hash"] != "om_message"
    assert audit.count_attempts("tenant-a", 99.0, 101.0) == 3

    external.count = 0
    with pytest.raises(RuntimeError, match="behind local"):
        audit.count_attempts("tenant-a", 99.0, 101.0)
    audit.close()
