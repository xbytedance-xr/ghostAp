"""Loop Engine — subprocess-driven iterative closed-loop development.

Uses session.send_prompt_streaming() to iterate until acceptance criteria
are satisfied. Each iteration uses role-aware prompts, evaluates criteria
via a separate prompt, and applies multi-dimensional termination checks.
"""

import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from typing import Optional, Callable, Union

from ..session import CocoSessionManager, ClaudeSessionManager
from ..session.base import BaseSession
from ..config import get_settings

from .models import (
    LoopProject,
    LoopProjectStatus,
    LoopRequirement,
    IterationRecord,
    IterationState,
    LoopContextManager,
    TerminationSignal,
    RoleSelection,
)
from .roles import RoleRouter
from .termination import TerminationChecker
from .analyzer import RequirementAnalyzer

logger = logging.getLogger(__name__)

AISessionManager = Union[CocoSessionManager, ClaudeSessionManager]


@dataclass
class LoopEngineCallbacks:
    """Loop Engine event callbacks."""

    on_analyzing_start: Optional[Callable[[str], None]] = None
    on_analyzing_done: Optional[Callable[[LoopProject], None]] = None
    on_iteration_start: Optional[Callable[[int, int, RoleSelection], None]] = (
        None  # (current, max, role)
    )
    on_iteration_event: Optional[Callable[[int, str], None]] = (
        None  # (iteration, text_chunk)
    )
    on_iteration_done: Optional[Callable[[int, IterationRecord], None]] = None
    on_criteria_update: Optional[Callable[[LoopProject], None]] = None
    on_project_done: Optional[Callable[[LoopProject], None]] = None
    on_error: Optional[Callable[[str], None]] = None


class LoopEngine:
    """Subprocess-driven iterative closed-loop engine.

    Integrates:
    - RequirementAnalyzer: LLM-driven requirement parsing (fallback to text)
    - RoleRouter: dynamic role selection per iteration
    - TerminationChecker: multi-signal termination evaluation
    - LoopContextManager: three-tier context compression
    - Criteria writeback: evaluation results update CriteriaTracker
    """

    def __init__(
        self,
        chat_id: str,
        root_path: str,
        session_manager: Optional[AISessionManager] = None,
        engine_name: str = "Coco",
    ):
        self.chat_id = chat_id
        self.root_path = os.path.expanduser(root_path)
        self.settings = get_settings()
        self.engine_name = engine_name

        self._session_manager = session_manager or CocoSessionManager()
        self._ai_session: Optional[BaseSession] = None
        self._project: Optional[LoopProject] = None
        self._is_running = False
        self._should_stop = False

        # Core modules
        self._role_router = RoleRouter()
        self._termination_checker = TerminationChecker(
            max_iterations=self.settings.loop_max_iterations,
            convergence_window=self.settings.loop_convergence_window,
        )
        self._context_manager = LoopContextManager(
            max_context_tokens=getattr(self.settings, "loop_max_context_tokens", 8000),
        )

    @property
    def project(self) -> Optional[LoopProject]:
        return self._project

    @property
    def is_running(self) -> bool:
        return self._is_running

    def _ensure_ai_session(self) -> BaseSession:
        if self._ai_session is None:
            session_id = str(uuid.uuid4())
            self._ai_session = self._session_manager.start_session(
                chat_id=self.chat_id,
                session_id=session_id,
            )
        return self._ai_session

    def execute(
        self,
        requirement_text: str,
        callbacks: Optional[LoopEngineCallbacks] = None,
    ) -> LoopProject:
        """Iterate until acceptance criteria are satisfied."""
        callbacks = callbacks or LoopEngineCallbacks()
        self._is_running = True
        self._should_stop = False
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

        logger.info(
            "[Loop:%s] 迭代开始, 需求长度=%d, 路径=%s, engine=%s",
            project_name,
            len(requirement_text),
            self.root_path,
            self.engine_name,
        )

        try:
            # Ensure AI session
            session = self._ensure_ai_session()

            # Parse requirement (LLM-driven with fallback)
            analyzer = RequirementAnalyzer(session=session, cwd=self.root_path)
            requirement = analyzer.analyze(requirement_text)
            self._project.set_requirement(requirement)
            self._project.start()

            if callbacks.on_analyzing_done:
                callbacks.on_analyzing_done(self._project)

            timeout = self.settings.loop_execution_timeout

            # --- Iteration loop ---
            for iteration in range(1, max_iterations + 1):
                if self._should_stop:
                    break

                # 1. Build IterationState
                state = self._build_iteration_state(iteration, requirement)

                # 2. Select role
                role_selection = self._role_router.select_role(state)

                if callbacks.on_iteration_start:
                    callbacks.on_iteration_start(
                        iteration, max_iterations, role_selection
                    )

                # 3. Build role-aware prompt
                prompt = self._build_role_prompt(state, role_selection)

                # 4. Execute via streaming
                record = IterationRecord(
                    iteration=iteration,
                    role=role_selection.role,
                    focus=role_selection.focus,
                    prompt=prompt,
                )

                text_buffer: list[str] = []

                def on_chunk(chunk: str, _it=iteration):
                    text_buffer.append(chunk)
                    if callbacks.on_iteration_event:
                        callbacks.on_iteration_event(_it, chunk)

                iter_start = time.time()
                try:
                    full_output = session.send_prompt_streaming(
                        prompt=prompt,
                        on_chunk=on_chunk,
                        timeout=timeout,
                        cwd=self.root_path,
                        chunk_interval=0.5,
                        should_stop=lambda: self._should_stop,
                    )
                except Exception as e:
                    full_output = "".join(text_buffer)
                    if not full_output:
                        full_output = f"Error: {e}"
                    record.fail(str(e), full_output[:2000])
                    self._project.iterations.append(record)
                    self._context_manager.record_iteration(record)
                    logger.warning(
                        "[Loop:%s] 迭代 %d 执行失败: %s", project_name, iteration, e
                    )

                    if callbacks.on_iteration_done:
                        callbacks.on_iteration_done(iteration, record)

                    # Check termination after failure
                    term_result = self._termination_checker.evaluate(
                        self._project, self._should_stop
                    )
                    if term_result.signal != TerminationSignal.CONTINUE:
                        self._apply_termination(term_result)
                        break
                    continue

                iter_duration = time.time() - iter_start

                # 5. Evaluate criteria and writeback
                criteria_progress = self._evaluate_criteria(
                    session,
                    requirement.acceptance_criteria,
                    iteration,
                )

                # Complete the record
                record.complete(
                    output=full_output[:2000],
                    summary=f"{role_selection.role.display_name}: {role_selection.focus}",
                    criteria_progress=criteria_progress,
                )
                record.duration = iter_duration

                self._project.iterations.append(record)
                self._context_manager.record_iteration(record)

                logger.info(
                    "[Loop:%s] 迭代 %d/%d 完成 [%s], 输出长度=%d, 耗时=%.1fs, 标准=%d/%d",
                    project_name,
                    iteration,
                    max_iterations,
                    role_selection.role.display_name,
                    len(full_output),
                    iter_duration,
                    self._project.satisfied_count,
                    self._project.total_criteria,
                )

                if callbacks.on_iteration_done:
                    callbacks.on_iteration_done(iteration, record)

                if callbacks.on_criteria_update:
                    callbacks.on_criteria_update(self._project)

                # 6. Termination check
                term_result = self._termination_checker.evaluate(
                    self._project, self._should_stop
                )
                if term_result.signal != TerminationSignal.CONTINUE:
                    logger.info(
                        "[Loop:%s] 终止信号: %s — %s",
                        project_name,
                        term_result.signal.value,
                        term_result.reason,
                    )
                    self._apply_termination(term_result)
                    break

            else:
                # Loop exhausted max_iterations without breaking
                self._project.abort(f"达到最大迭代次数 {max_iterations}")

            # Handle user stop (didn't go through termination checker)
            if self._should_stop and self._project.status == LoopProjectStatus.RUNNING:
                self._project.status = LoopProjectStatus.PAUSED

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
            self._is_running = False

    # ------------------------------------------------------------------
    # State & prompt building
    # ------------------------------------------------------------------

    def _build_iteration_state(
        self, iteration: int, requirement: LoopRequirement
    ) -> IterationState:
        """Build a state snapshot for role selection and prompt construction."""
        return IterationState(
            iteration_number=iteration,
            requirement=requirement,
            criteria_tracker=self._project.criteria_tracker,
            recent_iterations=self._project.iterations[-5:],
            context_summary=self._context_manager.build_context_prompt(),
            user_guidance=self._context_manager.consume_user_guidance(),
            consecutive_failures=self._project.consecutive_failures,
            last_role=self._project.last_role,
        )

    def _build_role_prompt(
        self, state: IterationState, selection: RoleSelection
    ) -> str:
        """Build a role-aware iteration prompt."""
        role_prompt = self._role_router.get_role_prompt(selection.role)

        # Build criteria list with satisfaction status
        criteria_lines = []
        for i, c in enumerate(state.requirement.acceptance_criteria):
            satisfied = state.criteria_tracker.satisfied.get(i, False)
            marker = "[x]" if satisfied else "[ ]"
            criteria_lines.append(f"- {marker} {i + 1}. {c}")

        sections = [
            f"你是一个迭代式开发助手。当前是第 {state.iteration_number} 轮迭代。",
            f"\n## 产品目标\n{state.requirement.goal}",
            "\n## 验收标准\n" + "\n".join(criteria_lines),
        ]

        if state.context_summary:
            sections.append(f"\n## 已完成的工作\n{state.context_summary}")

        sections.append(f"\n## 你的角色: {selection.role.display_name}\n{role_prompt}")
        sections.append(
            f"\n## 本轮任务\n请以 {selection.role.display_name} 的视角，"
            f"针对尚未满足的标准，执行下一步最有价值的工作。"
        )

        if state.user_guidance:
            sections.append(f"\n## 用户引导\n{state.user_guidance}")

        sections.append(f"\n## 工作目录\n{self.root_path}")
        sections.append("\n完成后输出 DEEP_TASK_SUCCESS。")

        return "\n".join(sections)

    # ------------------------------------------------------------------
    # Criteria evaluation with writeback
    # ------------------------------------------------------------------

    def _evaluate_criteria(
        self, session: BaseSession, criteria: list[str], iteration: int
    ) -> dict[int, bool]:
        """Evaluate acceptance criteria and writeback to CriteriaTracker."""
        criteria_list = "\n".join(
            f"CRITERIA_{i + 1}: {c}" for i, c in enumerate(criteria)
        )
        eval_prompt = f"""请评估以下验收标准是否已满足：
{criteria_list}

对每个标准回答 PASS 或 FAIL，格式：
CRITERIA_1: PASS
CRITERIA_2: FAIL
"""
        progress: dict[int, bool] = {}
        try:
            eval_output = session.send_prompt(
                prompt=eval_prompt,
                timeout=60,
                cwd=self.root_path,
            )
            full_text = eval_output.upper()

            # Parse per-criteria results
            for i in range(len(criteria)):
                marker = f"CRITERIA_{i + 1}"
                pattern = rf"{marker}\s*[:：]\s*(PASS|FAIL)"
                match = re.search(pattern, full_text)
                if match:
                    progress[i] = match.group(1) == "PASS"

            # Writeback to CriteriaTracker
            self._project.criteria_tracker.batch_update(progress, iteration)

        except Exception as e:
            logger.debug("[Loop] 验收标准评估失败: %s", e)

        return progress

    # ------------------------------------------------------------------
    # Termination
    # ------------------------------------------------------------------

    def _apply_termination(self, term_result):
        """Apply termination result to project status."""
        if term_result.signal in (
            TerminationSignal.COMPLETE,
            TerminationSignal.CONVERGED,
        ):
            self._project.complete()
        elif term_result.signal == TerminationSignal.USER_STOP:
            self._project.status = LoopProjectStatus.PAUSED
        else:
            self._project.abort(term_result.reason)

    # ------------------------------------------------------------------
    # User interaction
    # ------------------------------------------------------------------

    def inject_guidance(self, message: str):
        """Inject user guidance via ContextManager."""
        self._context_manager.inject_user_guidance(message)
        logger.info("[Loop] 用户引导已注入: %s...", message[:100])

    def stop(self):
        self._should_stop = True

    def pause(self):
        if self._project:
            self._project.status = LoopProjectStatus.PAUSED
        self._should_stop = True

    def resume(
        self, callbacks: Optional[LoopEngineCallbacks] = None
    ) -> Optional[LoopProject]:
        return self._project

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

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
        if self._ai_session:
            self._session_manager.end_session(self.chat_id)
            self._ai_session = None
        self._project = None
        self._is_running = False


class LoopEngineManager:
    """Manages LoopEngine instances per chat."""

    def __init__(self):
        self._engines: dict[str, LoopEngine] = {}
        self._coco_session_manager = CocoSessionManager()
        self._claude_session_manager = ClaudeSessionManager()

    def get_or_create(
        self, chat_id: str, root_path: str, engine_name: str = "Coco"
    ) -> LoopEngine:
        key = f"{chat_id}:{root_path}"

        if engine_name.lower().startswith("claude"):
            session_manager = self._claude_session_manager
        else:
            session_manager = self._coco_session_manager

        if key not in self._engines:
            self._engines[key] = LoopEngine(
                chat_id=chat_id,
                root_path=root_path,
                session_manager=session_manager,
                engine_name=engine_name,
            )
        else:
            existing = self._engines[key]
            if (
                existing.engine_name.lower() != engine_name.lower()
                and not existing.is_running
            ):
                existing.cleanup()
                self._engines[key] = LoopEngine(
                    chat_id=chat_id,
                    root_path=root_path,
                    session_manager=session_manager,
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
        return [
            e
            for k, e in self._engines.items()
            if k.startswith(f"{chat_id}:") and e.is_running
        ]

    def list_engines(self, chat_id: Optional[str] = None) -> list[LoopEngine]:
        if chat_id is None:
            return list(self._engines.values())
        prefix = f"{chat_id}:"
        return [e for k, e in self._engines.items() if k.startswith(prefix)]

    def cleanup_all(self):
        for engine in self._engines.values():
            engine.cleanup()
        self._engines.clear()
