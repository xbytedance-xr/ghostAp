"""Tests for WorkflowProject PendingConfirmation model (AC15)."""

from __future__ import annotations

from src.workflow_engine.models import (
    PendingConfirmation,
    WorkflowProject,
    WorkflowStatus,
)


def test_pending_confirmation_creation():
    """AC15: PendingConfirmation 子模型可正常创建。"""
    pending = PendingConfirmation(
        script_path="/tmp/test.js",
        requirement="test requirement",
        meta={"tools": ["coco", "claude"]},
        is_fallback=False,
        initiator_user_id="user_123",
        engine_session_key="session_456",
        selected_tools=["coco", "claude"],
        budget=2_000_000,
        tools_mismatch=False,
        orchestrator_agent="coco",
        budget_tokens=2_000_000,
    )
    
    assert pending.script_path == "/tmp/test.js"
    assert pending.requirement == "test requirement"
    assert pending.meta == {"tools": ["coco", "claude"]}
    assert pending.initiator_user_id == "user_123"
    assert pending.engine_session_key == "session_456"
    assert pending.selected_tools == ["coco", "claude"]
    assert pending.budget == 2_000_000
    assert pending.orchestrator_agent == "coco"
    assert pending.budget_tokens == 2_000_000


def test_workflow_project_with_pending():
    """AC15: WorkflowProject 可持有 PendingConfirmation 引用。"""
    project = WorkflowProject(
        workflow_id="proj_123",
        status=WorkflowStatus.AWAITING_CONFIRM,
        pending=PendingConfirmation(
            requirement="test",
            initiator_user_id="user_123",
            selected_tools=["coco"],
        ),
    )
    
    assert project.pending is not None
    assert project.pending.requirement == "test"
    assert project.pending.initiator_user_id == "user_123"
    assert project.pending.selected_tools == ["coco"]
    
    # Runtime fields should be None until execution starts
    assert project.initiator_user_id is None
    assert project.selected_tools is None


def test_start_execution_migrates_fields():
    """AC15: start_execution() 迁移字段并置 pending 为 None。"""
    project = WorkflowProject(
        workflow_id="proj_123",
        status=WorkflowStatus.AWAITING_CONFIRM,
        pending=PendingConfirmation(
            requirement="test",
            initiator_user_id="user_123",
            selected_tools=["coco", "claude"],
            script_path="/tmp/test.js",
        ),
    )
    
    # Before start
    assert project.initiator_user_id is None
    assert project.selected_tools is None
    assert project.pending is not None
    
    # Start execution
    project.start_execution()
    
    # After start: fields migrated
    assert project.initiator_user_id == "user_123"
    assert project.selected_tools == ["coco", "claude"]
    assert project.pending is None
    
    # Script path and other pending-only fields are not migrated to runtime
    # (they are only needed during confirmation phase)


def test_start_execution_with_none_pending():
    """AC15: start_execution() 在 pending 为 None 时不报错。"""
    project = WorkflowProject(
        workflow_id="proj_123",
        status=WorkflowStatus.IDLE,
        pending=None,
    )
    
    # Should not raise
    project.start_execution()
    
    assert project.pending is None
    assert project.initiator_user_id is None
    assert project.selected_tools is None


def test_serialization_roundtrip():
    """AC15: 序列化/反序列化正常工作。"""
    project = WorkflowProject(
        workflow_id="proj_123",
        status=WorkflowStatus.AWAITING_CONFIRM,
        pending=PendingConfirmation(
            requirement="test requirement",
            initiator_user_id="user_123",
            selected_tools=["coco", "claude"],
            budget=2_000_000,
            orchestrator_agent="claude",
        ),
    )
    
    # Serialize
    data = project.to_dict()
    assert "pending" in data
    assert data["pending"]["requirement"] == "test requirement"
    assert data["pending"]["orchestrator_agent"] == "claude"
    
    # Deserialize
    restored = WorkflowProject.from_dict(data)
    assert restored.pending is not None
    assert restored.pending.requirement == "test requirement"
    assert restored.pending.orchestrator_agent == "claude"
    assert restored.pending.selected_tools == ["coco", "claude"]


def test_legacy_format_migration():
    """AC15: 从 legacy 扁平 pending_* 格式反序列化时自动迁移。"""
    legacy_data = {
        "workflow_id": "proj_legacy",
        "status": "awaiting_confirm",
        "pending_script_path": "/tmp/legacy.js",
        "pending_requirement": "legacy requirement",
        "pending_meta": {"tools": ["coco"]},
        "pending_is_fallback": False,
        "pending_initiator_user_id": "user_legacy",
        "pending_engine_session_key": "session_legacy",
        "pending_selected_tools": ["coco"],
        "pending_budget": 1_000_000,
        "pending_tools_mismatch": False,
    }
    
    restored = WorkflowProject.from_dict(legacy_data)
    
    # Legacy fields should be migrated to pending sub-model
    assert restored.pending is not None
    assert restored.pending.script_path == "/tmp/legacy.js"
    assert restored.pending.requirement == "legacy requirement"
    assert restored.pending.meta == {"tools": ["coco"]}
    assert restored.pending.initiator_user_id == "user_legacy"
    assert restored.pending.engine_session_key == "session_legacy"
    assert restored.pending.selected_tools == ["coco"]
    assert restored.pending.budget == 1_000_000
    
    # Legacy flat fields should not exist as direct attributes
    assert not hasattr(restored, "pending_script_path")
