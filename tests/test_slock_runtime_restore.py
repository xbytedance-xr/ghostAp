"""Tests for SlockEngineManager.restore_from_disk — runtime recovery from markers."""

from __future__ import annotations

import json
import os

import pytest

_acp_available = pytest.importorskip("acp", reason="acp package not installed")

from src.slock_engine.manager import SlockEngineManager  # noqa: E402


def _write_marker(storage_base_path: str, channel_id: str, data: dict) -> str:
    """Helper: write a .slock_channel.json marker under {base}/groups/{channel_id}/."""
    channel_dir = os.path.join(storage_base_path, "groups", channel_id)
    os.makedirs(channel_dir, exist_ok=True)
    marker_path = os.path.join(channel_dir, ".slock_channel.json")
    marker_data = dict(data)
    marker_data.setdefault(
        "root_path",
        os.path.dirname(os.path.dirname(storage_base_path)),
    )
    with open(marker_path, "w", encoding="utf-8") as f:
        json.dump(marker_data, f, ensure_ascii=False)
    return marker_path


class TestRestoreFromDiskHappyPath:
    def test_restore_single_engine(self, tmp_path):
        root = str(tmp_path)
        storage_base = str(tmp_path / "ghostap_config" / "slock")
        _write_marker(storage_base, "oc_chat_1", {
            "channel_id": "oc_chat_1",
            "team_name": "Alpha",
            "name": "Alpha Group",
            "activated_at": "2025-01-01T00:00:00Z",
        })

        manager = SlockEngineManager(storage_base_path=storage_base)
        restored = manager.restore_from_disk(root)

        assert restored == 1
        assert manager.is_managed_chat("oc_chat_1") is True
        engine = manager.get_activated_engine("oc_chat_1")
        assert engine is not None
        assert engine.channel is not None
        assert engine.channel.channel_id == "oc_chat_1"
        assert engine.channel.team_name == "Alpha"
        assert engine.channel.name == "Alpha Group"

    def test_restore_multiple_engines(self, tmp_path):
        root = str(tmp_path)
        storage_base = str(tmp_path / "ghostap_config" / "slock")
        for i, name in enumerate(["Alpha", "Beta", "Gamma"]):
            _write_marker(storage_base, f"oc_chat_{i}", {
                "channel_id": f"oc_chat_{i}",
                "team_name": name,
                "name": f"{name} Group",
            })

        manager = SlockEngineManager(storage_base_path=storage_base)
        restored = manager.restore_from_disk(root)

        assert restored == 3
        for i in range(3):
            assert manager.is_managed_chat(f"oc_chat_{i}") is True
            assert manager.get_activated_engine(f"oc_chat_{i}") is not None

    def test_restore_idempotent(self, tmp_path):
        root = str(tmp_path)
        storage_base = str(tmp_path / "ghostap_config" / "slock")
        _write_marker(storage_base, "oc_chat_1", {
            "channel_id": "oc_chat_1",
            "team_name": "Alpha",
            "name": "Alpha Group",
        })

        manager = SlockEngineManager(storage_base_path=storage_base)
        first = manager.restore_from_disk(root)
        second = manager.restore_from_disk(root)

        assert first == 1
        assert second == 0  # already managed, skipped

    def test_restore_uses_persisted_project_root_instead_of_process_fallback(self, tmp_path):
        fallback_root = tmp_path / "process_cwd"
        project_root = tmp_path / "team_project"
        fallback_root.mkdir()
        project_root.mkdir()
        storage_base = str(tmp_path / "ghostap_config" / "slock")
        _write_marker(storage_base, "oc_chat_1", {
            "channel_id": "oc_chat_1",
            "team_name": "Alpha",
            "name": "Alpha Group",
            "root_path": str(project_root),
        })

        manager = SlockEngineManager(storage_base_path=storage_base)
        assert manager.restore_from_disk(str(fallback_root)) == 1

        engine = manager.get_activated_engine("oc_chat_1")
        assert engine is not None
        assert engine.root_path == str(project_root.resolve())


class TestRestoreFromDiskErrorHandling:
    def test_skips_non_lark_chat_id_test_marker(self, tmp_path):
        root = str(tmp_path)
        storage_base = str(tmp_path / "ghostap_config" / "slock")
        _write_marker(storage_base, "test_chat_007", {
            "channel_id": "test_chat_007",
            "team_name": "TestTeam",
            "name": "test",
        })

        manager = SlockEngineManager(storage_base_path=storage_base)
        restored = manager.restore_from_disk(root)

        assert restored == 0
        assert manager.is_managed_chat("test_chat_007") is False

    def test_skips_marker_whose_channel_id_does_not_match_directory(self, tmp_path):
        root = str(tmp_path)
        storage_base = str(tmp_path / "ghostap_config" / "slock")
        _write_marker(storage_base, "oc_safe", {
            "channel_id": "oc_other",
            "team_name": "Mismatch",
            "name": "Mismatch",
        })

        manager = SlockEngineManager(storage_base_path=storage_base)

        assert manager.restore_from_disk(root) == 0
        assert manager.is_managed_chat("oc_other") is False

    def test_skips_unavailable_persisted_project_instead_of_using_fallback(self, tmp_path):
        fallback_root = tmp_path / "process_cwd"
        fallback_root.mkdir()
        storage_base = str(tmp_path / "ghostap_config" / "slock")
        _write_marker(storage_base, "oc_missing_project", {
            "channel_id": "oc_missing_project",
            "team_name": "Missing",
            "name": "Missing",
            "root_path": str(tmp_path / "does_not_exist"),
        })
        manager = SlockEngineManager(storage_base_path=storage_base)

        assert manager.restore_from_disk(str(fallback_root)) == 0
        assert manager.is_managed_chat("oc_missing_project") is False

    def test_skips_corrupted_marker(self, tmp_path):
        root = str(tmp_path)
        storage_base = str(tmp_path / "ghostap_config" / "slock")
        channel_dir = os.path.join(storage_base, "groups", "oc_bad")
        os.makedirs(channel_dir)
        with open(os.path.join(channel_dir, ".slock_channel.json"), "w") as f:
            f.write("NOT VALID JSON {{{{")

        manager = SlockEngineManager(storage_base_path=storage_base)
        restored = manager.restore_from_disk(root)

        assert restored == 0
        assert manager.is_managed_chat("oc_bad") is False

    def test_skips_missing_channel_id(self, tmp_path):
        root = str(tmp_path)
        storage_base = str(tmp_path / "ghostap_config" / "slock")
        _write_marker(storage_base, "oc_no_id", {
            "team_name": "NoId",
            "name": "Missing channel_id",
        })

        manager = SlockEngineManager(storage_base_path=storage_base)
        restored = manager.restore_from_disk(root)

        assert restored == 0
        assert manager.is_managed_chat("oc_no_id") is False

    def test_no_slock_dir_returns_zero(self, tmp_path):
        storage_base = str(tmp_path / "ghostap_config" / "slock")
        manager = SlockEngineManager(storage_base_path=storage_base)
        restored = manager.restore_from_disk(str(tmp_path))
        assert restored == 0

    def test_empty_slock_dir_returns_zero(self, tmp_path):
        storage_base = str(tmp_path / "ghostap_config" / "slock")
        os.makedirs(os.path.join(storage_base, "groups"))
        manager = SlockEngineManager(storage_base_path=storage_base)
        restored = manager.restore_from_disk(str(tmp_path))
        assert restored == 0

    def test_skips_dir_without_marker(self, tmp_path):
        root = str(tmp_path)
        storage_base = str(tmp_path / "ghostap_config" / "slock")
        os.makedirs(os.path.join(storage_base, "groups", "oc_no_marker"))

        manager = SlockEngineManager(storage_base_path=storage_base)
        restored = manager.restore_from_disk(root)

        assert restored == 0

    def test_mixed_valid_and_corrupted(self, tmp_path):
        root = str(tmp_path)
        storage_base = str(tmp_path / "ghostap_config" / "slock")

        # Valid marker
        _write_marker(storage_base, "oc_good", {
            "channel_id": "oc_good",
            "team_name": "Good",
            "name": "Good Group",
        })

        # Corrupted marker
        channel_dir = os.path.join(storage_base, "groups", "oc_bad")
        os.makedirs(channel_dir)
        with open(os.path.join(channel_dir, ".slock_channel.json"), "w") as f:
            f.write("{broken")

        manager = SlockEngineManager(storage_base_path=storage_base)
        restored = manager.restore_from_disk(root)

        assert restored == 1
        assert manager.is_managed_chat("oc_good") is True
        assert manager.is_managed_chat("oc_bad") is False
