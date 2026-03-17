import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

SPEC_TASKS_DIR = os.path.expanduser("~/.ghostap/spec_tasks")


@dataclass
class SpecTaskState:
    task_id: str
    created_at: float
    requirement: str
    project_path: str
    chat_id: str
    agent_type: str
    current_cycle: int
    current_phase: str
    last_error: str
    retry_count: int
    # 任务状态（用于恢复列表/详情展示）
    status: str = "失败"
    # 失败原因（面向用户/验收的稳定字段），例如："Phase build 失败: Internal error"
    failure_reason: str = ""
    models_tried: list[str] = field(default_factory=list)
    project_snapshot: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "created_at": self.created_at,
            "status": self.status,
            "failure_reason": self.failure_reason,
            "requirement": self.requirement,
            "project_path": self.project_path,
            "chat_id": self.chat_id,
            "agent_type": self.agent_type,
            "current_cycle": self.current_cycle,
            "current_phase": self.current_phase,
            "last_error": self.last_error,
            "retry_count": self.retry_count,
            "models_tried": self.models_tried,
            "project_snapshot": self.project_snapshot,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SpecTaskState":
        task_id = data["task_id"]
        created_at = data["created_at"]
        requirement = data.get("requirement", "")
        project_path = data.get("project_path", "")
        chat_id = data.get("chat_id", "")
        agent_type = data.get("agent_type", "")
        current_cycle = int(data.get("current_cycle") or 0)
        current_phase = data.get("current_phase", "")
        last_error = data.get("last_error", "")
        retry_count = int(data.get("retry_count") or 0)
        models_tried = data.get("models_tried") or []
        project_snapshot = data.get("project_snapshot")

        status = (data.get("status") or "").strip() or "失败"
        failure_reason = (data.get("failure_reason") or "").strip()
        if not failure_reason:
            phase = (str(current_phase or "").strip() or "")
            err = (str(last_error or "").strip() or "")
            if phase and err:
                # Backward compat: older snapshots only have current_phase/last_error.
                failure_reason = f"Phase {phase} 失败: {err}"

        return cls(
            task_id=task_id,
            created_at=created_at,
            requirement=requirement,
            project_path=project_path,
            chat_id=chat_id,
            agent_type=agent_type,
            current_cycle=current_cycle,
            current_phase=current_phase,
            last_error=last_error,
            retry_count=retry_count,
            status=status,
            failure_reason=failure_reason,
            models_tried=models_tried,
            project_snapshot=project_snapshot,
        )


def generate_task_id() -> str:
    return str(uuid.uuid4())[:8]


def save_task_state(state: SpecTaskState) -> str:
    os.makedirs(SPEC_TASKS_DIR, exist_ok=True)
    filepath = os.path.join(SPEC_TASKS_DIR, f"{state.task_id}.json")
    tmp_path = filepath + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, filepath)
        logger.debug("保存任务状态: %s", filepath)
        return filepath
    finally:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def load_task_state(task_id: str) -> Optional[SpecTaskState]:
    filepath = os.path.join(SPEC_TASKS_DIR, f"{task_id}.json")
    if not os.path.exists(filepath):
        return None
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        return SpecTaskState.from_dict(data)
    except Exception as e:
        logger.warning("加载任务状态失败 %s: %s", task_id, e)
        return None


def delete_task_state(task_id: str) -> bool:
    filepath = os.path.join(SPEC_TASKS_DIR, f"{task_id}.json")
    if not os.path.exists(filepath):
        return False
    try:
        os.unlink(filepath)
        logger.debug("删除任务状态: %s", filepath)
        return True
    except OSError as e:
        logger.warning("删除任务状态失败 %s: %s", task_id, e)
        return False


def list_pending_tasks() -> list[SpecTaskState]:
    if not os.path.isdir(SPEC_TASKS_DIR):
        return []
    tasks = []
    for filename in os.listdir(SPEC_TASKS_DIR):
        if not filename.endswith(".json"):
            continue
        task_id = filename[:-5]
        state = load_task_state(task_id)
        if state:
            tasks.append(state)
    return tasks
