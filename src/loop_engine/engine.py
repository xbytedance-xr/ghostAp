"""Loop Engine — subprocess-driven iterative closed-loop development.

Uses session.send_prompt_streaming() to iterate until acceptance criteria
are satisfied. Each iteration sends a prompt, collects text output, then
evaluates criteria via a separate prompt.
"""

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional, Callable, Union

from ..session import CocoSessionManager, ClaudeSessionManager
from ..session.base import BaseSession
from ..config import get_settings

from .models import (
    LoopProject,
    LoopProjectStatus,
    LoopRequirement,
    IterationRecord,
    IterationStatus,
)

logger = logging.getLogger(__name__)

AISessionManager = Union[CocoSessionManager, ClaudeSessionManager]


@dataclass
class LoopEngineCallbacks:
    """Loop Engine event callbacks."""
    on_analyzing_start: Optional[Callable[[str], None]] = None
    on_analyzing_done: Optional[Callable[[LoopProject], None]] = None
    on_iteration_start: Optional[Callable[[int, int], None]] = None  # (current, max)
    on_iteration_event: Optional[Callable[[int, str], None]] = None  # (iteration, text_chunk)
    on_iteration_done: Optional[Callable[[int, IterationRecord], None]] = None
    on_project_done: Optional[Callable[[LoopProject], None]] = None
    on_error: Optional[Callable[[str], None]] = None


class LoopEngine:
    """Subprocess-driven iterative closed-loop engine."""

    def __init__(self, chat_id: str, root_path: str,
                 session_manager: Optional[AISessionManager] = None,
                 engine_name: str = "Coco"):
        self.chat_id = chat_id
        self.root_path = os.path.expanduser(root_path)
        self.settings = get_settings()
        self.engine_name = engine_name

        self._session_manager = session_manager or CocoSessionManager()
        self._ai_session: Optional[BaseSession] = None
        self._project: Optional[LoopProject] = None
        self._is_running = False
        self._should_stop = False
        self._user_guidance: Optional[str] = None

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

        logger.info("[Loop:%s] 迭代开始, 需求长度=%d, 路径=%s, engine=%s",
                     project_name, len(requirement_text), self.root_path, self.engine_name)

        try:
            # Parse requirement — extract acceptance criteria
            requirement = self._parse_requirement(requirement_text)
            self._project.set_requirement(requirement)
            self._project.start()

            if callbacks.on_analyzing_done:
                callbacks.on_analyzing_done(self._project)

            # Ensure AI session
            session = self._ensure_ai_session()
            timeout = self.settings.loop_execution_timeout

            # Build initial prompt
            initial_prompt = self._build_initial_prompt(requirement)

            for iteration in range(1, max_iterations + 1):
                if self._should_stop:
                    break

                if callbacks.on_iteration_start:
                    callbacks.on_iteration_start(iteration, max_iterations)

                # Build prompt for this iteration
                if iteration == 1:
                    prompt = initial_prompt
                else:
                    prompt = self._build_iteration_prompt(iteration, requirement)

                # Collect output via streaming
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

                iter_duration = time.time() - iter_start

                # Record iteration
                record = IterationRecord(
                    iteration=iteration,
                    role=None,
                    focus=f"Iteration {iteration}",
                    output=full_output[:2000],
                    status=IterationStatus.SUCCESS,
                    duration=iter_duration,
                )
                record.completed_at = time.time()
                self._project.iterations.append(record)

                logger.info("[Loop:%s] 迭代 %d/%d 完成, 输出长度=%d, 耗时=%.1fs",
                             project_name, iteration, max_iterations,
                             len(full_output), iter_duration)

                if callbacks.on_iteration_done:
                    callbacks.on_iteration_done(iteration, record)

                # Evaluate acceptance criteria
                criteria_result = self._evaluate_criteria(session, requirement.acceptance_criteria)
                if criteria_result.get("all_satisfied", False):
                    logger.info("[Loop:%s] 所有验收标准已满足, 迭代 %d 轮", project_name, iteration)
                    break

                # Convergence detection
                if self._detect_convergence():
                    logger.info("[Loop:%s] 收敛检测触发, 迭代 %d 轮", project_name, iteration)
                    break

            # Determine final status
            if self._should_stop:
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
            self._is_running = False

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
        criteria_list = "\n".join(f"- [ ] {c}" for c in requirement.acceptance_criteria)
        guidance_section = ""
        if self._user_guidance:
            guidance_section = f"\n## 用户引导\n{self._user_guidance}\n"
            self._user_guidance = None  # consume after use
        return f"""继续完成剩余的验收标准。这是第 {iteration} 轮迭代。

## 未满足的验收标准
{criteria_list}
{guidance_section}
请检查哪些标准已经满足，对未满足的标准继续实现。
完成后报告每个标准的状态。
"""

    def _evaluate_criteria(self, session: BaseSession, criteria: list[str]) -> dict:
        """Evaluate acceptance criteria by asking the agent."""
        criteria_list = "\n".join(f"CRITERIA_{i+1}: {c}" for i, c in enumerate(criteria))
        eval_prompt = f"""请评估以下验收标准是否已满足：
{criteria_list}

对每个标准回答 PASS 或 FAIL，格式：
CRITERIA_1: PASS/FAIL
CRITERIA_2: PASS/FAIL
"""
        try:
            eval_output = session.send_prompt(
                prompt=eval_prompt,
                timeout=60,
                cwd=self.root_path,
            )
            full_text = eval_output.upper()

            # Parse results
            pass_count = full_text.count("PASS")
            fail_count = full_text.count("FAIL")
            all_satisfied = pass_count >= len(criteria) and fail_count == 0

            return {"all_satisfied": all_satisfied, "pass_count": pass_count, "fail_count": fail_count}

        except Exception as e:
            logger.debug("[Loop] 验收标准评估失败: %s", e)
            return {"all_satisfied": False}

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
        self._should_stop = True

    def pause(self):
        if self._project:
            self._project.status = LoopProjectStatus.PAUSED
        self._should_stop = True

    def resume(self, callbacks: Optional[LoopEngineCallbacks] = None) -> Optional[LoopProject]:
        return self._project

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

    def get_or_create(self, chat_id: str, root_path: str, engine_name: str = "Coco") -> LoopEngine:
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
            if existing.engine_name.lower() != engine_name.lower() and not existing.is_running:
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
