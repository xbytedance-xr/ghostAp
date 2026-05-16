"""Spec-style card processor for Deep engine callbacks."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from ...acp import ACPEvent, ACPEventRenderer, ACPEventType
from ...card.events import CardEvent, CardEventType
from ...card.orchestrator import TaskOrchestrator
from ...card.stream_bridge import ACPStreamBridge
from ...card.task_registry import TaskRegistry, tasks_from_plan_entries
from ...card.ui_text import UI_TEXT
from ...deep_engine import DeepEngineCallbacks
from ...deep_engine.models import DeepProject, DeepProjectStatus
from ..emoji import EmojiReaction

if TYPE_CHECKING:
    from ...card.session.rotator import SessionRotator
    from ...project import ProjectContext
    from .deep_renderer import DeepRenderer


class DeepStreamProcessor:
    """Render Deep engine progress through the shared Spec-style card pipeline."""

    _CYCLE = 1

    def __init__(
        self,
        *,
        rotator: "SessionRotator",
        renderer: "DeepRenderer",
        message_id: str,
        chat_id: str,
        root_path: str | None,
        project: "ProjectContext | None",
    ) -> None:
        self._rotator = rotator
        self._renderer = renderer
        self._message_id = message_id
        self._chat_id = chat_id
        self._root_path = root_path
        self._project = project
        self._acp_renderer = ACPEventRenderer()
        self._stream_bridge = ACPStreamBridge(rotator)
        self._task_registry = TaskRegistry()
        self._start_time = time.time()
        self._tool_count = 0
        self._plan_steps = 0
        self._phase = "analyzing"
        self._current_task_id = ""

    def build_callbacks(self) -> DeepEngineCallbacks:
        return DeepEngineCallbacks(
            on_analyzing_done=self.on_analyzing_done,
            on_event=self.on_event,
            on_project_done=self.on_project_done,
            on_error=self.on_error,
        )

    def on_analyzing_done(self, deep_project: DeepProject) -> None:
        self._rotator.dispatch(CardEvent.started())
        self._rotator.dispatch(CardEvent.cycle_started(self._CYCLE, 1))
        self._rotator.dispatch(CardEvent.phase_started(
            self._CYCLE,
            "analyzing",
            subtitle=UI_TEXT["deep_spec_style_subtitle_analyzing"],
            content=UI_TEXT["deep_exec_start"].format(
                project_name=deep_project.name,
                root_path=deep_project.root_path,
            ),
        ))
        self._phase = "analyzing"

    def on_event(self, event: ACPEvent) -> None:
        """Process ACP events and dispatch Spec-style card events."""
        self._acp_renderer.process_event(event)

        if event.event_type == ACPEventType.PLAN_UPDATE:
            if self._handle_plan_task_list(event):
                self._ensure_build_phase()
            return

        if event.event_type == ACPEventType.TOOL_CALL_START:
            self._tool_count += 1
            self._ensure_build_phase()

        if event.event_type != ACPEventType.TOOL_CALL_DONE:
            self._handle_agent_task_list(event)

        self._stream_bridge.on_event(event)

        if event.event_type == ACPEventType.TOOL_CALL_DONE:
            self._handle_agent_task_list(event)

        if event.event_type == ACPEventType.TOOL_CALL_START:
            self._rotator.dispatch(CardEvent.progress_updated(
                current=self._tool_count,
                total=max(self._plan_steps, self._tool_count),
                label=UI_TEXT["deep_phase_executing"],
            ))

        warning = self._renderer.check_warning_banner(
            time.time() - self._start_time,
            is_executing=self._phase == "build",
        )
        if warning:
            self._rotator.dispatch(CardEvent.warning_updated(warning))

    def on_project_done(self, deep_project: DeepProject) -> None:
        self._stream_bridge.close_open_blocks()
        self._complete_active_phase()
        self._rotator.dispatch(CardEvent.cycle_done(self._CYCLE))

        snap = self._renderer._get_engine(self._chat_id, self._root_path, self._project)
        tool_calls_count = snap.tool_calls_count if snap else self._tool_count
        summary = UI_TEXT["deep_exec_completed"].format(tool_calls_count=tool_calls_count)

        if deep_project.status == DeepProjectStatus.COMPLETED:
            self._rotator.dispatch(CardEvent.completed(summary=summary))
            self._renderer.handler.add_reaction(self._message_id, EmojiReaction.on_multi_task_done())
        else:
            tasks = self._task_payload()
            completed = sum(1 for task in tasks if task.get("status") == "completed")
            self._rotator.dispatch(CardEvent.failed(UI_TEXT["deep_exec_incomplete"].format(
                completed=completed,
                total=len(tasks),
            )))
        self._renderer._current_session = None

    def on_error(self, error: str) -> None:
        self._stream_bridge.close_open_blocks()
        self._rotator.dispatch(CardEvent.failed(error))
        self._renderer._current_session = None

    def _task_payload(self) -> list[dict]:
        return [
            {"task_id": task.task_id, "name": task.name, "status": task.status}
            for task in self._task_registry.get_snapshot()
        ]

    def _pick_current_task_id(self, preferred: str = "") -> str:
        snapshot = self._task_registry.get_snapshot()
        if preferred:
            for item in snapshot:
                if item.task_id == preferred and item.status == "in_progress":
                    return preferred
        for item in reversed(snapshot):
            if item.status == "in_progress":
                return item.task_id
        return ""

    def _dispatch_task_list(self, preferred_current_id: str = "") -> None:
        tasks = self._task_payload()
        if not tasks:
            return
        current = self._pick_current_task_id(preferred_current_id or self._current_task_id)
        self._current_task_id = current
        self._rotator.dispatch(CardEvent(
            type=CardEventType.TASK_LIST_UPDATED,
            payload={"tasks": tasks, "current_task_id": current},
        ))

    def _upsert_task(self, task_id: str, name: str, status: str) -> None:
        task_id = str(task_id or "").strip()
        name = str(name or "").strip()
        if not task_id:
            return
        if status not in {"pending", "in_progress", "completed", "failed"}:
            status = "pending"

        existing = self._task_registry.get(task_id)
        if existing is None:
            self._task_registry.register(task_id=task_id, name=name or "子任务", status=status)
            return

        if (
            name
            and TaskOrchestrator._is_generic_task_label(existing.name)
            and not TaskOrchestrator._is_generic_task_label(name)
        ):
            self._task_registry.update_name(task_id, name)
        self._task_registry.update_status(task_id, status)

    def _handle_plan_task_list(self, event: ACPEvent) -> bool:
        if event.event_type != ACPEventType.PLAN_UPDATE or not event.plan:
            return False
        tasks = tasks_from_plan_entries(event.plan.entries)
        if not tasks:
            return False

        self._plan_steps = len(tasks)
        current = ""
        for task in tasks:
            task_id = str(task.get("task_id") or "")
            status = str(task.get("status") or "pending")
            self._upsert_task(task_id, str(task.get("name") or ""), status)
            if not current and status == "in_progress":
                current = task_id
        self._dispatch_task_list(current)
        return True

    def _handle_agent_task_list(self, event: ACPEvent) -> bool:
        if event.event_type not in {
            ACPEventType.TOOL_CALL_START,
            ACPEventType.TOOL_CALL_UPDATE,
            ACPEventType.TOOL_CALL_DONE,
        }:
            return False
        is_agent_task_event = TaskOrchestrator.is_agent_task_event(event)
        tool_call = event.tool_call
        task_id = str(getattr(tool_call, "id", "") or "").strip()
        if not task_id:
            return False
        if not is_agent_task_event and self._task_registry.get(task_id) is None:
            return False
        if event.event_type == ACPEventType.TOOL_CALL_DONE:
            raw_status = str(getattr(tool_call, "status", "") or "").strip().lower()
            status = "failed" if raw_status == "failed" else "completed"
        else:
            status = "in_progress"
        self._upsert_task(task_id, TaskOrchestrator._extract_agent_task_label(tool_call), status)
        self._dispatch_task_list(task_id)
        return True

    def _ensure_build_phase(self) -> None:
        if self._phase == "build":
            return
        self._rotator.dispatch(CardEvent.phase_done(
            self._CYCLE,
            "analyzing",
            UI_TEXT["deep_spec_style_analyzing_done"],
            subtitle=UI_TEXT["deep_spec_style_subtitle_build"],
        ))
        self._rotator.dispatch(CardEvent.phase_started(
            self._CYCLE,
            "build",
            subtitle=UI_TEXT["deep_spec_style_subtitle_build"],
            content=UI_TEXT["deep_phase_executing"],
        ))
        self._phase = "build"

    def _complete_active_phase(self) -> None:
        if self._phase == "done":
            return
        output_key = (
            "deep_spec_style_build_done"
            if self._phase == "build"
            else "deep_spec_style_analyzing_done"
        )
        self._rotator.dispatch(CardEvent.phase_done(
            self._CYCLE,
            self._phase,
            UI_TEXT[output_key],
        ))
        self._phase = "done"
