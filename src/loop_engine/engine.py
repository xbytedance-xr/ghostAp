"""ACP-driven Loop Engine — iterative closed-loop development.

Uses ACP session's multi-turn prompt capability to iterate until
acceptance criteria are satisfied. Each iteration sends a prompt,
tracks tool calls/plan progress, then evaluates criteria.
"""

import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional, Callable

from ..acp import ACPEvent, ACPEventType, ACPEventRenderer, SyncACPSession, start_session_with_retry
from ..config import get_settings
from ..deep_engine.models import EngineRunState

from .models import (
    LoopProject,
    LoopProjectStatus,
    LoopRequirement,
    IterationRecord,
    IterationStatus,
    CriteriaTracker,
    TerminationSignal,
)
from .tracker import IterationTracker

logger = logging.getLogger(__name__)


@dataclass
class LoopEngineCallbacks:
    """Loop Engine event callbacks."""
    on_analyzing_start: Optional[Callable[[str], None]] = None
    on_analyzing_done: Optional[Callable[[LoopProject], None]] = None
    on_iteration_start: Optional[Callable[[int, int], None]] = None  # (current, max)
    on_iteration_event: Optional[Callable[[int, ACPEvent], None]] = None
    on_iteration_done: Optional[Callable[[int, IterationRecord], None]] = None
    on_project_done: Optional[Callable[[LoopProject], None]] = None
    on_error: Optional[Callable[[str], None]] = None


class LoopEngine:
    """ACP-driven iterative closed-loop engine."""

    def __init__(self, chat_id: str, root_path: str,
                 agent_type: str = "coco", engine_name: str = "Coco"):
        self.chat_id = chat_id
        self.root_path = os.path.expanduser(root_path)
        self.settings = get_settings()
        self.engine_name = engine_name
        self._agent_type = agent_type

        self._session: Optional[SyncACPSession] = None
        self._project: Optional[LoopProject] = None
        self._renderer = ACPEventRenderer()
        self._run_state = EngineRunState.IDLE
        self._user_guidance: Optional[str] = None

    @property
    def project(self) -> Optional[LoopProject]:
        return self._project

    @property
    def run_state(self) -> EngineRunState:
        return self._run_state

    @property
    def is_running(self) -> bool:
        return self._run_state != EngineRunState.IDLE

    def execute(
        self,
        requirement_text: str,
        callbacks: Optional[LoopEngineCallbacks] = None,
    ) -> LoopProject:
        """Iterate until acceptance criteria are satisfied."""
        callbacks = callbacks or LoopEngineCallbacks()
        self._run_state = EngineRunState.RUNNING
        max_iterations = self.settings.loop_max_iterations

        # Create project
        project_name = os.path.basename(self.root_path) or "loop_project"
        self._project = LoopProject.create(
            name=project_name,
            root_path=self.root_path,
        )
        self._project.status = LoopProjectStatus.ANALYZING

        if callbacks.on_analyzing_start:
            callbacks.on_analyzing_start(requirement_text)

        logger.info("[Loop:%s] ACP迭代开始, 需求长度=%d, 路径=%s, agent=%s",
                     project_name, len(requirement_text), self.root_path, self._agent_type)

        try:
            # Parse requirement — extract acceptance criteria
            requirement = self._parse_requirement(requirement_text)
            self._project.set_requirement(requirement)  # initializes CriteriaTracker
            self._project.status = LoopProjectStatus.RUNNING

            if callbacks.on_analyzing_done:
                callbacks.on_analyzing_done(self._project)

            # Create ACP session (with retry and progressive timeout)
            self._session = start_session_with_retry(
                agent_type=self._agent_type, cwd=self.root_path,
                startup_timeout=self.settings.loop_execution_timeout,
            )

            # Build initial prompt
            initial_prompt = self._build_initial_prompt(requirement)
            timeout = self.settings.loop_execution_timeout

            for iteration in range(1, max_iterations + 1):
                if self._run_state != EngineRunState.RUNNING:
                    break

                iter_start = time.time()

                if callbacks.on_iteration_start:
                    callbacks.on_iteration_start(iteration, max_iterations)

                # Build prompt for this iteration
                if iteration == 1:
                    prompt = initial_prompt
                else:
                    prompt = self._build_iteration_prompt(iteration, requirement)

                # Track events for this iteration
                iter_tracker = IterationTracker()

                def on_event(event: ACPEvent, _it=iteration):
                    iter_tracker.process(event)
                    self._renderer.process_event(event)
                    if callbacks.on_iteration_event:
                        callbacks.on_iteration_event(_it, event)

                result = self._session.send_prompt(prompt, on_event=on_event, timeout=timeout)

                # Record iteration — full output, proper duration, extract focus
                iter_end = time.time()
                focus = self._extract_focus(iter_tracker.text_buffer) or f"迭代 {iteration}"
                record = IterationRecord(
                    iteration=iteration,
                    role=None,
                    focus=focus,
                    output=iter_tracker.text_buffer,
                    status=IterationStatus.SUCCESS if result.stop_reason == "end_turn" else IterationStatus.FAILED,
                    started_at=iter_start,
                    duration=iter_end - iter_start,
                    completed_at=iter_end,
                )
                self._project.iterations.append(record)

                logger.info("[Loop:%s] 迭代 %d/%d 完成, 工具=%d, 文件=%d",
                             project_name, iteration, max_iterations,
                             len(iter_tracker.tool_calls),
                             len(iter_tracker.modified_files))

                if callbacks.on_iteration_done:
                    callbacks.on_iteration_done(iteration, record)

                # Evaluate acceptance criteria in the same session
                criteria_result = self._evaluate_criteria(requirement.acceptance_criteria, iteration)
                if criteria_result.get("all_satisfied", False):
                    logger.info("[Loop:%s] 所有验收标准已满足, 迭代 %d 轮", project_name, iteration)
                    break

                # Convergence detection
                if self._detect_convergence():
                    logger.info("[Loop:%s] 收敛检测触发, 迭代 %d 轮", project_name, iteration)
                    break

            # Determine final status
            if self._run_state == EngineRunState.STOPPING:
                self._project.status = LoopProjectStatus.PAUSED
            else:
                self._project.status = LoopProjectStatus.COMPLETED
                self._project.completed_at = time.time()

            if callbacks.on_project_done:
                callbacks.on_project_done(self._project)

            return self._project

        except Exception as e:
            error_msg = f"Loop执行异常: {str(e)}"
            logger.error("[Loop:%s] %s", project_name, error_msg)
            if self._project:
                self._project.status = LoopProjectStatus.ABORTED
                self._project.completed_at = time.time()
            if callbacks.on_error:
                callbacks.on_error(error_msg)
            return self._project

        finally:
            self._run_state = EngineRunState.IDLE

    def _parse_requirement(self, text: str) -> LoopRequirement:
        """Simple requirement parsing — extract goal and acceptance criteria."""
        lines = text.strip().split("\n")
        criteria = []
        goal = text

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("- ") or stripped.startswith("* "):
                criteria.append(stripped[2:])
            elif stripped.startswith("[ ] ") or stripped.startswith("[x] "):
                criteria.append(stripped[4:])

        if not criteria:
            # If no explicit criteria, use the whole text as goal and generate generic criteria
            criteria = [f"完成需求: {text[:100]}"]

        return LoopRequirement(
            goal=goal,
            acceptance_criteria=criteria,
            raw_text=text,
        )

    def _build_initial_prompt(self, requirement: LoopRequirement) -> str:
        criteria_list = "\n".join(f"- [ ] {c}" for c in requirement.acceptance_criteria)
        return f"""你是一个专业的软件工程师。请完成以下产品需求：

## 需求
{requirement.goal}

## 验收标准
{criteria_list}

## 工作目录
{self.root_path}

## 要求
1. 先分析需求，理解验收标准
2. 制定实现计划并逐步执行
3. 每个验收标准都必须通过验证
4. 完成后确认所有标准已满足
"""

    def _build_iteration_prompt(self, iteration: int, requirement: LoopRequirement) -> str:
        # Show satisfied vs unsatisfied criteria based on tracker state
        tracker = self._project.criteria_tracker if self._project else None
        criteria_lines = []
        for i, c in enumerate(requirement.acceptance_criteria):
            if tracker and tracker.satisfied.get(i, False):
                criteria_lines.append(f"- [x] {c} ✅ (已满足)")
            else:
                criteria_lines.append(f"- [ ] {c}")
        criteria_list = "\n".join(criteria_lines)

        guidance_section = ""
        if self._user_guidance:
            guidance_section = f"\n## 用户引导\n{self._user_guidance}\n"
            self._user_guidance = None  # consume after use
        return f"""继续完成剩余的验收标准。这是第 {iteration} 轮迭代。

## 验收标准进度
{criteria_list}
{guidance_section}
请聚焦未满足的标准（未打勾的），继续实现。
完成后报告每个标准的状态。
"""

    def _evaluate_criteria(self, criteria: list[str], iteration: int) -> dict:
        """Evaluate acceptance criteria by asking the agent in the same session.

        Updates the CriteriaTracker with per-criteria PASS/FAIL results.
        """
        if not self._session:
            return {"all_satisfied": False}

        criteria_list = "\n".join(f"CRITERIA_{i+1}: {c}" for i, c in enumerate(criteria))
        eval_prompt = f"""请评估以下验收标准是否已满足：
{criteria_list}

对每个标准回答 PASS 或 FAIL，严格按照以下格式回复（每行一个）：
CRITERIA_1: PASS
CRITERIA_2: FAIL
...
"""
        try:
            eval_text = []

            def on_eval_event(event: ACPEvent):
                if event.event_type == ACPEventType.TEXT_CHUNK and event.text:
                    eval_text.append(event.text)

            self._session.send_prompt(eval_prompt, on_event=on_eval_event, timeout=60)
            full_text = "".join(eval_text).upper()

            # Parse per-criteria results: look for "CRITERIA_N: PASS" or "CRITERIA_N: FAIL"
            per_criteria: dict[int, bool] = {}
            for i in range(len(criteria)):
                pattern = rf"CRITERIA_{i+1}\s*:\s*(PASS|FAIL)"
                match = re.search(pattern, full_text)
                if match:
                    per_criteria[i] = (match.group(1) == "PASS")

            # Update CriteriaTracker
            if self._project:
                self._project.criteria_tracker.batch_update(per_criteria, iteration)

            pass_count = sum(1 for v in per_criteria.values() if v)
            fail_count = sum(1 for v in per_criteria.values() if not v)
            # Use tracker state for all_satisfied (accumulates across iterations)
            all_satisfied = self._project.criteria_tracker.is_all_satisfied if self._project else False

            return {"all_satisfied": all_satisfied, "pass_count": pass_count, "fail_count": fail_count}

        except Exception as e:
            logger.debug("[Loop] 验收标准评估失败: %s", e)
            return {"all_satisfied": False}

    def _extract_focus(self, text: str) -> str:
        """Extract a brief focus description from agent output.

        Takes the first meaningful line (non-empty, not just punctuation/whitespace)
        and truncates to 80 chars as a concise summary of what the agent worked on.
        """
        if not text:
            return ""
        for line in text.strip().split("\n"):
            line = line.strip()
            # Skip empty lines, markdown markers, code fences
            if not line or line.startswith("```") or line.startswith("---"):
                continue
            # Strip leading markdown markers like #, *, -, >
            cleaned = line.lstrip("#*-> ").strip()
            if len(cleaned) >= 4:
                return cleaned[:80]
        return ""

    def _detect_convergence(self) -> bool:
        """Detect if recent iterations made no progress."""
        if not self._project or len(self._project.iterations) < self.settings.loop_convergence_window:
            return False

        window = self.settings.loop_convergence_window
        recent = self._project.iterations[-window:]

        # If all recent iterations have very short output, consider converged
        if all(len(r.output or "") < 50 for r in recent):
            return True

        return False

    def inject_guidance(self, message: str):
        """Inject user guidance — will be included in the next iteration prompt."""
        self._user_guidance = message
        logger.info("[Loop] 用户引导已注入: %s...", message[:100])

    def stop(self):
        self._run_state = EngineRunState.STOPPING
        if self._session:
            self._session.cancel()

    def pause(self):
        if self._project:
            self._project.status = LoopProjectStatus.PAUSED
        self._run_state = EngineRunState.STOPPING
        if self._session:
            self._session.cancel()

    def resume(self, callbacks: Optional[LoopEngineCallbacks] = None) -> Optional[LoopProject]:
        """Resume a paused loop execution — continue iterating from where we left off."""
        if not self._project or self._project.status != LoopProjectStatus.PAUSED:
            return self._project
        if not self._project.requirement:
            return self._project

        callbacks = callbacks or LoopEngineCallbacks()
        self._run_state = EngineRunState.RUNNING
        self._project.status = LoopProjectStatus.RUNNING
        max_iterations = self.settings.loop_max_iterations
        start_iteration = len(self._project.iterations) + 1
        requirement = self._project.requirement
        project_name = self._project.name

        try:
            self._session = start_session_with_retry(
                agent_type=self._agent_type, cwd=self.root_path,
                startup_timeout=self.settings.loop_execution_timeout,
            )

            timeout = self.settings.loop_execution_timeout

            for iteration in range(start_iteration, max_iterations + 1):
                if self._run_state != EngineRunState.RUNNING:
                    break

                iter_start = time.time()

                if callbacks.on_iteration_start:
                    callbacks.on_iteration_start(iteration, max_iterations)

                prompt = self._build_iteration_prompt(iteration, requirement)
                iter_tracker = IterationTracker()

                def on_event(event: ACPEvent, _it=iteration):
                    iter_tracker.process(event)
                    self._renderer.process_event(event)
                    if callbacks.on_iteration_event:
                        callbacks.on_iteration_event(_it, event)

                result = self._session.send_prompt(prompt, on_event=on_event, timeout=timeout)

                iter_end = time.time()
                focus = self._extract_focus(iter_tracker.text_buffer) or f"迭代 {iteration}"
                record = IterationRecord(
                    iteration=iteration,
                    role=None,
                    focus=focus,
                    output=iter_tracker.text_buffer,
                    status=IterationStatus.SUCCESS if result.stop_reason == "end_turn" else IterationStatus.FAILED,
                    started_at=iter_start,
                    duration=iter_end - iter_start,
                    completed_at=iter_end,
                )
                self._project.iterations.append(record)

                if callbacks.on_iteration_done:
                    callbacks.on_iteration_done(iteration, record)

                criteria_result = self._evaluate_criteria(requirement.acceptance_criteria, iteration)
                if criteria_result.get("all_satisfied", False):
                    logger.info("[Loop:%s] 恢复后所有验收标准已满足, 迭代 %d 轮", project_name, iteration)
                    break

                if self._detect_convergence():
                    logger.info("[Loop:%s] 恢复后收敛检测触发, 迭代 %d 轮", project_name, iteration)
                    break

            if self._run_state == EngineRunState.STOPPING:
                self._project.status = LoopProjectStatus.PAUSED
            else:
                self._project.status = LoopProjectStatus.COMPLETED
                self._project.completed_at = time.time()

            if callbacks.on_project_done:
                callbacks.on_project_done(self._project)

        except Exception as e:
            error_msg = f"Loop恢复异常: {str(e)}"
            logger.error("[Loop:%s] %s", project_name, error_msg)
            self._project.status = LoopProjectStatus.ABORTED
            self._project.completed_at = time.time()
            if callbacks.on_error:
                callbacks.on_error(error_msg)

        finally:
            self._run_state = EngineRunState.IDLE

        return self._project

    def get_rendered_content(self) -> str:
        return self._renderer.get_final_content()

    def save_state(self, filepath: Optional[str] = None) -> str:
        if not self._project:
            raise ValueError("没有项目状态可保存")
        if not filepath:
            filepath = os.path.join(self.root_path, ".loop_engine_state.json")
        state = {
            "chat_id": self.chat_id,
            "root_path": self.root_path,
            "project": self._project.to_dict(),
            "saved_at": time.time(),
        }
        tmp_path = filepath + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, filepath)
        return filepath

    def cleanup(self):
        if self._session:
            try:
                self._session.close()
            except Exception as e:
                logger.debug("关闭ACP session失败: %s", e)
            self._session = None
        self._project = None
        self._run_state = EngineRunState.IDLE


class LoopEngineManager:
    """Manages LoopEngine instances per chat."""

    def __init__(self):
        self._engines: dict[str, LoopEngine] = {}

    def get_or_create(self, chat_id: str, root_path: str, engine_name: str = "Coco") -> LoopEngine:
        key = f"{chat_id}:{root_path}"
        agent_type = "claude" if engine_name.lower().startswith("claude") else "coco"

        if key not in self._engines:
            self._engines[key] = LoopEngine(
                chat_id=chat_id,
                root_path=root_path,
                agent_type=agent_type,
                engine_name=engine_name,
            )
        else:
            existing = self._engines[key]
            if existing.engine_name.lower() != engine_name.lower() and not existing.is_running:
                existing.cleanup()
                self._engines[key] = LoopEngine(
                    chat_id=chat_id,
                    root_path=root_path,
                    agent_type=agent_type,
                    engine_name=engine_name,
                )
        return self._engines[key]

    def get(self, chat_id: str, root_path: str) -> Optional[LoopEngine]:
        key = f"{chat_id}:{root_path}"
        return self._engines.get(key)

    def get_active_engine(self, chat_id: str) -> Optional[LoopEngine]:
        for key, engine in self._engines.items():
            if key.startswith(f"{chat_id}:") and engine.is_running:
                return engine
        return None

    def get_active_engines(self, chat_id: str) -> list[LoopEngine]:
        return [e for k, e in self._engines.items()
                if k.startswith(f"{chat_id}:") and e.is_running]

    def list_engines(self, chat_id: Optional[str] = None) -> list[LoopEngine]:
        if chat_id is None:
            return list(self._engines.values())
        prefix = f"{chat_id}:"
        return [e for k, e in self._engines.items() if k.startswith(prefix)]

    def cleanup_all(self):
        for engine in self._engines.values():
            engine.cleanup()
        self._engines.clear()
