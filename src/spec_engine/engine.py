"""ACP-driven Spec Engine — structured methodology with iterative review.

Follows spec-kit methodology: each cycle progresses through
spec → plan → task → build → review. Review suggestions feed back
as input for the next cycle. Terminates when all criteria are satisfied
and all review perspectives pass.
"""

import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage

from ..acp import ACPEvent, ACPEventType, ACPEventRenderer
from ..agent_session import SyncSession, close_session_safely, create_engine_session
from ..config import get_settings
from ..deep_engine.models import EngineRunState
from ..loop_engine.models import (
    CriteriaTracker,
    ReviewPerspective,
    PerspectiveReview,
    ReviewResult,
)
from ..utils.spec_utils import (
    CRITERIA_PATTERNS as _CRITERIA_PATTERNS,
    PERSPECTIVE_TAG_MAP as _PERSPECTIVE_TAG_MAP,
    extract_json_blob,
    normalize_list,
    parse_review_output_strict_tolerant,
    validate_plan_artifact_dict,
    validate_spec_artifact_dict,
)

from .models import (
    SpecProject,
    SpecProjectStatus,
    SpecPhase,
    SpecCycle,
    SpecTask,
    SpecTaskStatus,
    SpecArtifact,
    PlanArtifact,
    SpecWorkItem,
    SpecWorkItemStatus,
    SpecCycleMetrics,
)
from .tracker import PhaseTracker

logger = logging.getLogger(__name__)

# Pre-compiled regex for task parsing
_TASK_LINE_PATTERN = re.compile(
    r"^\s*(\d+)\s*[.、)]\s*(.+?)(?:\s*\(\s*(?:依赖|depends?)\s*[:：]?\s*(.*?)\s*\))?\s*$",
    re.IGNORECASE,
)


@dataclass
class SpecEngineCallbacks:
    """Spec Engine event callbacks."""

    on_analyzing_start: Optional[Callable[[str], None]] = None
    on_analyzing_done: Optional[Callable[[SpecProject], None]] = None
    on_cycle_start: Optional[Callable[[int, int], None]] = None  # (current, max)
    on_phase_start: Optional[Callable[[int, SpecPhase], None]] = None
    on_phase_event: Optional[Callable[[int, SpecPhase, ACPEvent], None]] = None
    on_phase_done: Optional[Callable[[int, SpecPhase, str], None]] = None
    on_review_done: Optional[Callable[[int, ReviewResult], None]] = None
    on_cycle_done: Optional[Callable[[int, SpecCycle], None]] = None
    on_project_done: Optional[Callable[[SpecProject], None]] = None
    on_error: Optional[Callable[[str], None]] = None


@dataclass
class ContinuationPolicy:
    """Decides whether to continue the next spec-kit optimization cycle."""

    max_cycles: int
    infinite_mode: bool = False
    disable_convergence: bool = False
    disable_early_stop: bool = False

    def should_stop(
        self,
        cycle_num: int,
        all_satisfied: bool,
        review_passed: bool,
        converged: bool,
        metrics: SpecCycleMetrics,
    ) -> Optional[str]:
        # Success always stops
        if all_satisfied and review_passed:
            return "success"

        # Infinite mode: never stop due to convergence/early stop
        if self.infinite_mode:
            return None

        if (not self.disable_convergence) and converged:
            return "converged"

        if self.disable_early_stop:
            return None

        # Secondary guard: low improvement space + empty backlog for several cycles
        if (
            (not all_satisfied)
            and metrics.improvement_space <= 0.2
            and metrics.backlog_pending == 0
            and metrics.new_satisfied == 0
            and cycle_num >= 3
        ):
            return "converged"
        return None


class SpecEngine:
    """ACP-driven structured development engine with iterative review cycles."""

    def __init__(self, chat_id: str, root_path: str,
                 agent_type: str = "coco", engine_name: str = "Coco"):
        self.chat_id = chat_id
        self.root_path = os.path.expanduser(root_path)
        self.settings = get_settings()
        self.engine_name = engine_name
        self._agent_type = agent_type

        self._session: Optional[SyncSession] = None
        self._project: Optional[SpecProject] = None
        self._renderer = ACPEventRenderer()
        self._run_state = EngineRunState.IDLE
        self._user_guidance: list[str] = []
        self._last_review: Optional[ReviewResult] = None
        # success / paused / converged / max_cycles / stopped
        self._termination_reason: Optional[str] = None
        self._resume_meta: Optional[dict] = None

    @property
    def project(self) -> Optional[SpecProject]:
        return self._project

    @property
    def run_state(self) -> EngineRunState:
        return self._run_state

    @property
    def is_running(self) -> bool:
        return self._run_state != EngineRunState.IDLE

    def _close_session_safely(self) -> None:
        close_session_safely(self._session)
        self._session = None

    def _get_bool_setting(self, name: str, default: bool = False) -> bool:
        """Safely read bool settings.

        注意：测试里经常用 MagicMock 作为 settings，缺失字段会返回新的 MagicMock（truthy），
        这里强制要求类型为 bool，否则回退到 default。
        """
        try:
            v = getattr(self.settings, name, default)
            return v if isinstance(v, bool) else default
        except Exception:
            return default

    # ------------------------------------------------------------------
    # Main execution
    # ------------------------------------------------------------------
    def execute(
        self,
        requirement_text: str,
        callbacks: Optional[SpecEngineCallbacks] = None,
        task_id: Optional[str] = None,
        on_rate_limit: Optional[Callable[[int], None]] = None,
    ) -> SpecProject:
        """Run the spec engine: analyze → cycle(spec→plan→task→build→review) → repeat."""
        callbacks = callbacks or SpecEngineCallbacks()
        self._run_state = EngineRunState.RUNNING
        self._on_rate_limit = on_rate_limit
        max_cycles = self._resolve_max_cycles(self.settings.spec_max_cycles)
        self._termination_reason = None

        project_name = os.path.basename(self.root_path) or "spec_project"
        self._project = SpecProject.create(name=project_name, root_path=self.root_path)
        self._project.task_id = task_id
        self._project.status = SpecProjectStatus.ANALYZING
        self._project.requirement = requirement_text

        if callbacks.on_analyzing_start:
            callbacks.on_analyzing_start(requirement_text)

        logger.info("[Spec:%s] 启动, 需求长度=%d, 路径=%s, agent=%s",
                    project_name, len(requirement_text), self.root_path, self._agent_type)

        try:
            # Parse requirement — extract acceptance criteria
            criteria = self._parse_acceptance_criteria(requirement_text)
            self._project.acceptance_criteria = criteria
            self._project.criteria_tracker.init_criteria(criteria)
            self._project.status = SpecProjectStatus.RUNNING
            self._project.started_at = time.time()

            if callbacks.on_analyzing_done:
                callbacks.on_analyzing_done(self._project)

            # Create ACP session
            self._session = create_engine_session(
                agent_type=self._agent_type, cwd=self.root_path,
                on_rate_limit=on_rate_limit,
            )

            self._last_review = None

            self._termination_reason = self._run_cycle_loop(
                start_cycle=1,
                max_cycles=max_cycles,
                callbacks=callbacks,
                timeout=self.settings.spec_execution_timeout,
                first_raw_input=requirement_text,
            )

            # Determine final status
            reason = self._termination_reason or "max_cycles"
            if self._run_state == EngineRunState.STOPPING or reason == "paused":
                self._project.status = SpecProjectStatus.PAUSED
            elif reason == "success":
                self._project.status = SpecProjectStatus.COMPLETED
                self._project.completed_at = time.time()
            else:
                # Converged/max-cycles without satisfying requirements
                if reason == "converged":
                    msg = (
                        f"收敛终止：连续{self.settings.spec_convergence_window}轮无有效改进，"
                        "仍有未满足验收标准或审查未通过"
                    )
                    self._project.abort(msg)
                elif reason == "max_cycles":
                    msg = f"达到最大循环次数({max_cycles})仍未满足验收标准或审查未通过"
                    # Infinite mode: treat max-cycles as a pause point instead of abort
                    if self._get_bool_setting("spec_infinite_mode", False):
                        self._project.status = SpecProjectStatus.PAUSED
                        self._project.error = msg + "（已暂停，可继续 /spec_resume 或提升 SPEC_MAX_CYCLES）"
                        self._project.completed_at = time.time()
                    else:
                        self._project.abort(msg)
                else:
                    msg = f"终止：{reason}"
                    self._project.abort(msg)

            if callbacks.on_project_done:
                callbacks.on_project_done(self._project)

            return self._project

        except Exception as e:
            error_msg = f"Spec执行异常: {str(e)}"
            logger.error("[Spec:%s] %s", project_name, error_msg)
            if self._project:
                self._project.status = SpecProjectStatus.ABORTED
                self._project.completed_at = time.time()
            if callbacks.on_error:
                callbacks.on_error(error_msg)
            return self._project

        finally:
            self._close_session_safely()
            self._run_state = EngineRunState.IDLE

    # ------------------------------------------------------------------
    # Phase execution
    # ------------------------------------------------------------------
    def _run_phase(
        self,
        cycle_num: int,
        phase: SpecPhase,
        prompt: str,
        callbacks: SpecEngineCallbacks,
        timeout: int,
    ) -> str:
        """Execute a single phase: send prompt, collect output, return text."""
        if callbacks.on_phase_start:
            callbacks.on_phase_start(cycle_num, phase)

        tracker = PhaseTracker()

        def on_event(event: ACPEvent):
            tracker.process(event)
            self._renderer.process_event(event)
            if callbacks.on_phase_event:
                callbacks.on_phase_event(cycle_num, phase, event)

        self._session.send_prompt(prompt, on_event=on_event, timeout=timeout)
        output = tracker.text_buffer

        if callbacks.on_phase_done:
            callbacks.on_phase_done(cycle_num, phase, output)

        return output

    # ------------------------------------------------------------------
    # Cycle loop (shared by execute / resume)
    # ------------------------------------------------------------------
    def _run_cycle_loop(
        self,
        start_cycle: int,
        max_cycles: int,
        callbacks: SpecEngineCallbacks,
        timeout: int,
        first_raw_input: Optional[str] = None,
    ) -> str:
        """Execute spec cycles. Modifies ``self._project`` in-place.

        Returns a termination reason:
        - success: all criteria satisfied and (review disabled or all PASS)
        - paused: user stopped/pause requested
        - converged: no measurable progress in window
        - max_cycles: hit max_cycles without success
        - stopped: engine stopped without a cycle (edge)
        """
        requirement = self._project.requirement
        termination: str = "max_cycles"

        policy = ContinuationPolicy(
            max_cycles=max_cycles,
            infinite_mode=self._get_bool_setting("spec_infinite_mode", False),
            disable_convergence=self._get_bool_setting("spec_disable_convergence", False),
            disable_early_stop=self._get_bool_setting("spec_disable_early_stop", False),
        )

        for cycle_num in range(start_cycle, max_cycles + 1):
            if self._run_state != EngineRunState.RUNNING:
                termination = "paused" if self._run_state == EngineRunState.STOPPING else "stopped"
                break

            cycle = SpecCycle(cycle_number=cycle_num)

            if callbacks.on_cycle_start:
                callbacks.on_cycle_start(cycle_num, max_cycles)

            # Pick next work item (spec file) if any; otherwise refine from requirement.
            work_item = self._pick_next_work_item(cycle_num)

            # First cycle of a fresh execute uses raw requirement
            if first_raw_input is not None and cycle_num == start_cycle:
                spec_input = first_raw_input
            elif work_item and work_item.spec_path:
                spec_input = self._build_input_from_spec_file(requirement, work_item)
            else:
                spec_input = self._build_refinement_input(requirement)

            # --- SPEC PHASE ---
            cycle.phase = SpecPhase.SPEC
            if work_item and work_item.spec_path and self._should_load_spec_directly(work_item):
                # spec 文件本身就是 spec-kit 规格产物：直接加载进入下一阶段
                spec_output = self._read_text_file_best_effort(work_item.spec_path)
            else:
                spec_output = self._run_phase(
                    cycle_num, SpecPhase.SPEC,
                    self._build_spec_prompt(spec_input),
                    callbacks, timeout,
                )
            cycle.spec_content = self._truncate_output(spec_output)
            if self._get_bool_setting("spec_persist_phase_artifacts", True):
                cycle.spec_path = self._persist_cycle_artifact(cycle_num, "spec", spec_output, ext="json")
            cycle.spec_artifact, cycle.spec_artifact_errors = self._parse_spec_artifact(spec_output)

            if self._get_bool_setting("spec_persist_every_phase", True):
                self._persist_state_best_effort()

            if work_item:
                work_item.status = SpecWorkItemStatus.DONE
                work_item.used_in_cycle = cycle_num
                self._append_history_event(
                    "work_item_consumed",
                    {
                        "cycle": cycle_num,
                        "item_id": work_item.item_id,
                        "question": work_item.question,
                        "spec_path": work_item.spec_path,
                    },
                )

            # If the spec provides better acceptance criteria, merge into project.
            if cycle_num == 1 and cycle.spec_artifact and cycle.spec_artifact.acceptance_criteria:
                self._merge_acceptance_criteria(cycle.spec_artifact.acceptance_criteria)

            if self._run_state != EngineRunState.RUNNING:
                cycle.fail()
                self._project.cycles.append(cycle)
                self._project.cycle_count_total = max(self._project.cycle_count_total, cycle_num)
                self._persist_state_best_effort()
                termination = "paused"
                break

            # --- PLAN PHASE ---
            cycle.phase = SpecPhase.PLAN
            plan_output = self._run_phase(
                cycle_num, SpecPhase.PLAN,
                self._build_plan_prompt(spec_output),
                callbacks, timeout,
            )
            cycle.plan_content = self._truncate_output(plan_output)
            if self._get_bool_setting("spec_persist_phase_artifacts", True):
                cycle.plan_path = self._persist_cycle_artifact(cycle_num, "plan", plan_output, ext="json")
            cycle.plan_artifact, cycle.plan_artifact_errors = self._parse_plan_artifact(plan_output)

            if self._get_bool_setting("spec_persist_every_phase", True):
                self._persist_state_best_effort()
            if self._run_state != EngineRunState.RUNNING:
                cycle.fail()
                self._project.cycles.append(cycle)
                self._project.cycle_count_total = max(self._project.cycle_count_total, cycle_num)
                self._persist_state_best_effort()
                termination = "paused"
                break

            # --- TASK PHASE ---
            cycle.phase = SpecPhase.TASK
            task_output = self._run_phase(
                cycle_num, SpecPhase.TASK,
                self._build_task_prompt(plan_output),
                callbacks, timeout,
            )
            parsed_tasks = self._parse_tasks(task_output)
            cycle.tasks_total = len(parsed_tasks)
            cycle.tasks = parsed_tasks[: self.settings.spec_cycle_tasks_max]
            if self._get_bool_setting("spec_persist_phase_artifacts", True):
                cycle.tasks_path = self._persist_cycle_artifact(
                    cycle_num, "tasks", json.dumps([t.to_dict() for t in parsed_tasks], ensure_ascii=False, indent=2), ext="json"
                )

            if self._get_bool_setting("spec_persist_every_phase", True):
                self._persist_state_best_effort()
            if self._run_state != EngineRunState.RUNNING:
                cycle.fail()
                self._project.cycles.append(cycle)
                self._project.cycle_count_total = max(self._project.cycle_count_total, cycle_num)
                self._persist_state_best_effort()
                termination = "paused"
                break

            # --- BUILD PHASE ---
            cycle.phase = SpecPhase.BUILD
            build_output = self._run_phase(
                cycle_num, SpecPhase.BUILD,
                self._build_build_prompt(cycle.tasks, plan_output),
                callbacks, timeout,
            )
            cycle.build_output = self._truncate_output(build_output)
            if self._get_bool_setting("spec_persist_phase_artifacts", True):
                cycle.build_path = self._persist_cycle_artifact(cycle_num, "build", build_output, ext="txt")

            if self._get_bool_setting("spec_persist_every_phase", True):
                self._persist_state_best_effort()
            if self._run_state != EngineRunState.RUNNING:
                cycle.fail()
                self._project.cycles.append(cycle)
                self._project.cycle_count_total = max(self._project.cycle_count_total, cycle_num)
                self._persist_state_best_effort()
                termination = "paused"
                break

            # --- REVIEW PHASE (conditional, like loop engine) ---
            review_passed = True
            if self.settings.spec_review_enabled:
                cycle.phase = SpecPhase.REVIEW
                review_result = self._conduct_review(cycle_num, callbacks)
                cycle.review_result = review_result
                if self._get_bool_setting("spec_persist_phase_artifacts", True):
                    cycle.review_path = self._persist_cycle_artifact(cycle_num, "review", self._review_result_to_text(review_result), ext="txt")
                self._last_review = review_result
                review_passed = review_result.all_passed

                if self._get_bool_setting("spec_persist_every_phase", True):
                    self._persist_state_best_effort()

            cycle.complete()

            self._project.cycles.append(cycle)
            self._project.cycle_count_total = max(self._project.cycle_count_total, cycle_num)

            if callbacks.on_cycle_done:
                callbacks.on_cycle_done(cycle_num, cycle)

            logger.info("[Spec:%s] 循环 %d/%d 完成, 审查=%s",
                        self._project.name, cycle_num, max_cycles,
                        f"{cycle.review_result.total_suggestions}条建议" if cycle.review_result else "跳过")

            # --- CRITERIA EVALUATION ---
            criteria_result = self._evaluate_criteria(
                self._project.acceptance_criteria, cycle_num)
            all_satisfied = criteria_result.get("all_satisfied", False)

            # --- POST-CYCLE PROBLEM DISCOVERY + SPEC GENERATION ---
            if self._get_bool_setting("spec_discovery_enabled", True) and self._run_state == EngineRunState.RUNNING:
                discovery = self._discover_optimization_questions(cycle_num)
                cycle.discovery_path = self._persist_cycle_artifact(cycle_num, "discovery", json.dumps(discovery, ensure_ascii=False, indent=2), ext="json")
                new_items = self._generate_specs_from_discovery(cycle_num, discovery)
                # 防止 backlog 无限制膨胀：只保留最近 N 条（长期任务可配合外部清理）
                if new_items:
                    self._project.work_items.extend(new_items)
                    self._project.work_items_total = max(self._project.work_items_total, len(self._project.work_items))
                    for wi in new_items:
                        self._append_history_event(
                            "work_item_generated",
                            {
                                "cycle": cycle_num,
                                "item_id": wi.item_id,
                                "question": wi.question,
                                "spec_path": wi.spec_path,
                            },
                        )

            # --- METRICS SNAPSHOT (monitoring) ---
            metrics = self._compute_cycle_metrics(cycle)
            self._project.metrics_history.append(metrics)
            cycle.metrics_path = self._persist_cycle_artifact(cycle_num, "metrics", json.dumps(metrics.to_dict(), ensure_ascii=False, indent=2), ext="json")

            self._append_history_event(
                "cycle_done",
                {
                    "cycle": cycle_num,
                    "status": cycle.status,
                    "satisfied": metrics.satisfied_count,
                    "total": metrics.total_criteria,
                    "new_satisfied": metrics.new_satisfied,
                    "review_suggestions": metrics.review_suggestions,
                    "goal_attainment": metrics.goal_attainment,
                    "improvement_space": metrics.improvement_space,
                    "backlog_pending": metrics.backlog_pending,
                },
            )

            self._persist_state_best_effort()
            self._cleanup_old_cycle_artifacts(cycle_num)
            self._cleanup_generated_specs()

            # --- TERMINATION CHECK (ContinuationPolicy) ---
            converged = False if policy.disable_convergence else self._detect_convergence()
            if converged:
                logger.info("[Spec:%s] 收敛检测触发, 循环 %d 轮",
                            self._project.name, cycle_num)

            decision = policy.should_stop(
                cycle_num=cycle_num,
                all_satisfied=all_satisfied,
                review_passed=review_passed,
                converged=converged,
                metrics=metrics,
            )
            if decision == "success":
                logger.info("[Spec:%s] 所有标准+审查通过, 循环 %d 轮",
                            self._project.name, cycle_num)
                termination = "success"
                break
            if decision == "converged":
                termination = "converged"
                break

        return termination

    # ------------------------------------------------------------------
    # Long-range: work items, discovery, spec generation
    # ------------------------------------------------------------------
    def _resolve_max_cycles(self, requested: int) -> int:
        try:
            requested = int(requested)
        except Exception:
            requested = 10
        limit = int(getattr(self.settings, "spec_max_cycles_limit", 5000) or 5000)
        if limit <= 0:
            limit = 5000
        if requested <= 0:
            requested = 1
        return min(requested, limit)

    def _pick_next_work_item(self, cycle_num: int) -> Optional[SpecWorkItem]:
        if not self._project:
            return None
        for wi in self._project.work_items:
            if wi.status == SpecWorkItemStatus.PENDING and wi.spec_path:
                wi.status = SpecWorkItemStatus.IN_PROGRESS
                wi.used_in_cycle = cycle_num
                return wi
        return None

    def _should_load_spec_directly(self, work_item: SpecWorkItem) -> bool:
        # 仅当 spec_path 指向已生成的 spec-kit JSON 产物时才直读。
        path = (work_item.spec_path or "").lower()
        return path.endswith(".json")

    def _build_input_from_spec_file(self, original_requirement: str, work_item: SpecWorkItem) -> str:
        spec_text = self._read_text_file_best_effort(work_item.spec_path)
        return (
            f"## 长程任务目标\n{original_requirement}\n\n"
            f"## 本轮优化关注点（由问题发现机制生成）\n{work_item.question}\n\n"
            f"## 已生成的 Spec 产物（供参考，可修正）\n{spec_text}\n"
        )

    def _discover_optimization_questions(self, cycle_num: int) -> list[dict]:
        """在每轮 spec-kit 循环后触发“自我提问”。"""
        if not self._session or not self._project:
            return []

        tracker = self._project.criteria_tracker
        unsatisfied = tracker.unsatisfied_criteria
        pending_suggestions: list[str] = []
        if self._last_review:
            for pr in self._last_review.failed_perspectives:
                for s in pr.suggestions:
                    if s:
                        pending_suggestions.append(str(s))
        unsat_text = "\n".join(f"- {c}" for c in unsatisfied[:12]) if unsatisfied else "(无)"
        sugg_text = "\n".join(f"- {s}" for s in pending_suggestions[:12]) if pending_suggestions else "(无)"

        prompt = f"""你是一个长期任务的自我改进系统（Spec-kit 驱动）。

请在本轮 spec-kit 实现后，自动发现与目标相关的“可优化问题”，并提出下一步要解决的关键问题。

## 长程目标
{self._project.requirement}

## 当前未满足的验收标准（Top）
{unsat_text}

## 上轮审查未通过的建议（Top）
{sugg_text}

## 输出要求（必须严格遵守）
仅输出一个 JSON 数组，放在 ```json fenced code block``` 中，不要输出任何其他文字。

每个元素 schema：
{{
  "id": "Q-...",
  "question": "与目标相关的具体可优化问题（可落到代码/测试/鲁棒性/性能/体验/可维护性）",
  "why": "为什么重要（1-2句）",
  "priority": "P0|P1|P2"
}}

约束：
- 每个 question 必须“可行动”（能转成下一轮 spec-kit 的 Spec 任务单元）
- 优先覆盖未满足验收标准与审查建议
- 数量 1~{int(self.settings.spec_discovery_max_questions or 5)}
"""

        chunks: list[str] = []

        def on_event(event: ACPEvent):
            if event.event_type == ACPEventType.TEXT_CHUNK and event.text:
                chunks.append(event.text)

        try:
            self._session.send_prompt(prompt, on_event=on_event, timeout=120)
            raw = "".join(chunks)
            blob = extract_json_blob(raw)
            data = json.loads(blob) if blob else None
            if isinstance(data, list):
                cleaned: list[dict] = []
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    q = str(item.get("question", "")).strip()
                    if not q:
                        continue
                    cleaned.append({
                        "id": str(item.get("id") or f"Q-{cycle_num}-{len(cleaned)+1}"),
                        "question": q,
                        "why": str(item.get("why", "")).strip(),
                        "priority": str(item.get("priority", "P1")).strip().upper(),
                    })
                if cleaned:
                    return cleaned[: int(self.settings.spec_discovery_max_questions or 5)]
        except Exception as e:
            logger.debug("[Spec] 问题发现机制失败: %s", e)

        # 强制非空：保证每轮都能产出可优化问题
        if self._get_bool_setting("spec_discovery_force_nonempty", True) and self._project:
            fallback_q = None
            if unsatisfied:
                fallback_q = f"如何满足验收标准：{unsatisfied[0]}？"
            elif pending_suggestions:
                fallback_q = f"如何落实改进建议：{pending_suggestions[0]}？"
            else:
                fallback_q = "当前实现还有哪些可测试性/可维护性/鲁棒性方面的改进空间？"
            return [{
                "id": f"Q-{cycle_num}-1",
                "question": fallback_q,
                "why": "兜底：保证长程任务每轮都有明确的下一步优化方向",
                "priority": "P1",
            }]

        return []

    def _generate_specs_from_discovery(self, cycle_num: int, discovery: list[dict]) -> list[SpecWorkItem]:
        if not self._project:
            return []
        if not discovery:
            return []

        max_specs = int(getattr(self.settings, "spec_generated_specs_per_cycle", 3) or 3)
        selected = discovery[:max_specs]

        if not self._session:
            return []

        prompt = f"""你是一个 spec-kit 规格生成器。

请把下面这些“可优化问题”拆解为符合 spec-kit 的 Spec 任务单元，并为每个问题生成一个 Spec JSON 产物。

## 长程目标
{self._project.requirement}

## 可优化问题列表
{json.dumps(selected, ensure_ascii=False, indent=2)}

## 输出要求（必须严格遵守）
仅输出一个 JSON 数组，放在 ```json fenced code block``` 中，不要输出任何其他文字。

数组元素 schema：
{{
  "id": "Q-...", 
  "spec": {{
    "goals": ["..."],
    "functional_spec": ["..."],
    "non_functional_requirements": ["..."],
    "acceptance_criteria": ["可验证条件..."],
    "out_of_scope": ["..."],
    "risks": ["..."],
    "clarification_questions": ["..."],
    "decisions": ["..."],
    "version": "1.0"
  }}
}}
"""

        chunks: list[str] = []

        def on_event(event: ACPEvent):
            if event.event_type == ACPEventType.TEXT_CHUNK and event.text:
                chunks.append(event.text)

        items: list[SpecWorkItem] = []
        try:
            self._session.send_prompt(prompt, on_event=on_event, timeout=180)
            raw = "".join(chunks)
            blob = extract_json_blob(raw)
            data = json.loads(blob) if blob else None
            if not isinstance(data, list):
                data = []

            # Map id -> question
            id_to_question = {str(d.get("id")): str(d.get("question", "")).strip() for d in selected}

            for entry in data:
                if not isinstance(entry, dict):
                    continue
                qid = str(entry.get("id") or "").strip()
                spec = entry.get("spec")
                if not qid or not isinstance(spec, dict):
                    continue
                # validate and persist
                errors = validate_spec_artifact_dict(spec)
                if errors:
                    # still persist for traceability
                    logger.debug("[Spec] 生成的 spec 产物不合规(qid=%s): %s", qid, errors[:3])
                question = id_to_question.get(qid) or qid
                spec_text = json.dumps(spec, ensure_ascii=False, indent=2)
                spec_path = self._persist_generated_spec_file(cycle_num, qid, spec_text)
                items.append(SpecWorkItem(
                    item_id=qid,
                    question=question,
                    created_in_cycle=cycle_num,
                    spec_path=spec_path,
                    status=SpecWorkItemStatus.PENDING,
                ))

        except Exception as e:
            logger.debug("[Spec] spec 生成失败: %s", e)

        # Fallback: 如果生成失败，至少把问题落盘为 minimal spec 文件
        if not items and self._get_bool_setting("spec_discovery_force_nonempty", True):
            for d in selected[:max_specs]:
                qid = str(d.get("id") or f"Q-{cycle_num}-{uuid.uuid4().hex[:4]}")
                question = str(d.get("question") or "").strip() or qid
                minimal = {
                    "goals": [f"解决问题：{question}"],
                    "functional_spec": ["实现必要的改动以满足问题要求"],
                    "non_functional_requirements": ["不引入回归，保持可测试性"],
                    "acceptance_criteria": [f"问题被解决：{question}"],
                    "out_of_scope": [],
                    "risks": [],
                    "clarification_questions": [],
                    "decisions": ["假设：允许基于现有实现做增量改进"],
                    "version": "1.0",
                }
                spec_path = self._persist_generated_spec_file(cycle_num, qid, json.dumps(minimal, ensure_ascii=False, indent=2))
                items.append(SpecWorkItem(
                    item_id=qid,
                    question=question,
                    created_in_cycle=cycle_num,
                    spec_path=spec_path,
                    status=SpecWorkItemStatus.PENDING,
                ))

        return items

    # ------------------------------------------------------------------
    # Persistence helpers (state + artifacts)
    # ------------------------------------------------------------------
    def _persist_state_best_effort(self) -> None:
        if not self._project:
            return
        try:
            self.save_state(self._get_state_path())
        except Exception as e:
            logger.debug("[Spec] 保存状态失败: %s", e)

    def _get_state_path(self) -> str:
        filename = getattr(self.settings, "spec_state_filename", ".spec_engine_state.json")
        return os.path.join(self.root_path, filename)

    def _artifact_root_dir(self) -> str:
        dirname = getattr(self.settings, "spec_artifacts_dirname", ".spec_engine")
        pid = self._project.project_id if self._project else "unknown"
        return os.path.join(self.root_path, dirname, pid)

    def _history_log_path(self) -> str:
        root = self._artifact_root_dir()
        os.makedirs(root, exist_ok=True)
        filename = getattr(self.settings, "spec_history_log_filename", "history.jsonl")
        return os.path.join(root, filename)

    def _append_history_event(self, event_type: str, payload: dict) -> None:
        """Append an event to the per-project history log (JSONL)."""
        try:
            path = self._history_log_path()
            record = {
                "ts": time.time(),
                "type": event_type,
                "project_id": self._project.project_id if self._project else "",
                "cycle": int(payload.get("cycle") or 0),
                "payload": payload,
            }
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            return

    def _persist_cycle_artifact(self, cycle_num: int, name: str, content: str, ext: str = "txt") -> Optional[str]:
        if not content and name not in ("metrics", "discovery"):
            return None
        try:
            root = self._artifact_root_dir()
            cycle_dir = os.path.join(root, f"cycle_{cycle_num:04d}")
            os.makedirs(cycle_dir, exist_ok=True)
            safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", name)
            path = os.path.join(cycle_dir, f"{safe_name}.{ext}")
            tmp = path + ".tmp"

            # Bound disk artifact size as well (keep the system stable for 5k+ cycles)
            persist_max = int(getattr(self.settings, "spec_phase_output_persist_max_chars", 20000) or 20000)
            to_write = content or ""
            if persist_max > 0 and len(to_write) > persist_max:
                to_write = to_write[:persist_max] + "\n...\n(已截断，超长输出未全部落盘)"

            with open(tmp, "w", encoding="utf-8") as f:
                f.write(to_write)
            os.replace(tmp, path)
            return path
        except Exception as e:
            logger.debug("[Spec] 落盘产物失败(%s): %s", name, e)
            return None

    def _cleanup_old_cycle_artifacts(self, current_cycle: int) -> None:
        """Keep only the latest N cycle directories (generated_specs are kept forever)."""
        try:
            retention = int(getattr(self.settings, "spec_cycle_artifact_retention", 50) or 0)
            if retention <= 0:
                return
            root = self._artifact_root_dir()
            cutoff = current_cycle - retention
            if cutoff <= 0:
                return
            old_dir = os.path.join(root, f"cycle_{cutoff:04d}")
            if os.path.isdir(old_dir):
                shutil.rmtree(old_dir, ignore_errors=True)

            # Avoid stale paths in persisted state
            if self._project and self._project.cycles:
                for c in self._project.cycles:
                    if c.cycle_number == cutoff:
                        c.spec_path = None
                        c.plan_path = None
                        c.tasks_path = None
                        c.build_path = None
                        c.review_path = None
                        c.discovery_path = None
                        c.metrics_path = None
                        break
        except Exception:
            return

    def _cleanup_generated_specs(self) -> None:
        """Retention for generated spec files.

        规则：
        - 永远保留 PENDING/IN_PROGRESS 的 work_item spec 文件
        - 对已 DONE 的 work_item，仅保留最近 N 个（按 created_at）
        """
        if not self._project:
            return
        try:
            retention = int(getattr(self.settings, "spec_generated_specs_retention", 1000) or 0)
            if retention <= 0:
                return

            keep_paths: set[str] = set()
            for w in self._project.work_items:
                if w.status in (SpecWorkItemStatus.PENDING, SpecWorkItemStatus.IN_PROGRESS):
                    if w.spec_path:
                        keep_paths.add(w.spec_path)

            done = [w for w in self._project.work_items if w.status == SpecWorkItemStatus.DONE and w.spec_path]
            done.sort(key=lambda x: x.created_at)
            keep_done = done[-retention:]
            for w in keep_done:
                keep_paths.add(w.spec_path)

            # Delete extra DONE spec files
            for w in done[:-retention]:
                p = w.spec_path
                if not p or p in keep_paths:
                    continue
                try:
                    if os.path.exists(p):
                        os.remove(p)
                    w.spec_deleted = True
                except Exception:
                    continue
        except Exception:
            return

    def _persist_generated_spec_file(self, cycle_num: int, qid: str, spec_text: str) -> str:
        root = self._artifact_root_dir()
        spec_dir = os.path.join(root, "generated_specs")
        os.makedirs(spec_dir, exist_ok=True)
        safe_id = re.sub(r"[^a-zA-Z0-9_.-]+", "_", qid)[:80] or f"Q_{cycle_num}"
        path = os.path.join(spec_dir, f"cycle_{cycle_num:04d}_{safe_id}.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(spec_text or "{}")
        os.replace(tmp, path)
        return path

    @staticmethod
    def _read_text_file_best_effort(path: str) -> str:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            return ""

    def _truncate_output(self, text: str) -> str:
        max_chars = int(getattr(self.settings, "spec_cycle_output_max_chars", 4000) or 4000)
        if max_chars <= 0:
            return text or ""
        if not text:
            return ""
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "\n...\n(已截断，完整内容见落盘产物)"

    # ------------------------------------------------------------------
    # Monitoring metrics
    # ------------------------------------------------------------------
    def _compute_cycle_metrics(self, cycle: SpecCycle) -> SpecCycleMetrics:
        if not self._project:
            return SpecCycleMetrics(
                cycle_number=cycle.cycle_number,
                satisfied_count=0,
                total_criteria=0,
                new_satisfied=0,
                review_suggestions=0,
                backlog_pending=0,
                goal_attainment=0.0,
                improvement_space=0.0,
            )

        tracker = self._project.criteria_tracker
        satisfied = tracker.satisfied_count
        total = tracker.total_count

        prev_satisfied = 0
        if self._project.metrics_history:
            prev_satisfied = self._project.metrics_history[-1].satisfied_count
        new_satisfied = max(0, satisfied - prev_satisfied)

        review_suggestions = 0
        if cycle.review_result:
            review_suggestions = int(cycle.review_result.total_suggestions)

        backlog_pending = sum(1 for w in self._project.work_items if w.status == SpecWorkItemStatus.PENDING)

        # 目标达成度：以验收标准为主（0~1），审查可作为次要信号
        criteria_ratio = (satisfied / total) if total else 0.0
        review_ratio = 1.0 if (cycle.review_result and cycle.review_result.all_passed) else 0.0
        goal_attainment = min(1.0, max(0.0, criteria_ratio * 0.8 + review_ratio * 0.2))

        # 优化空间评估：若本轮有增量进展或 backlog 尚有待办，则认为仍有空间
        improvement_space = 0.0
        if new_satisfied > 0:
            improvement_space = 1.0
        elif backlog_pending > 0:
            improvement_space = 0.7
        elif review_suggestions > 0:
            improvement_space = 0.5
        else:
            improvement_space = 0.2

        termination_hint = ""
        if goal_attainment >= 0.999 and improvement_space <= 0.2:
            termination_hint = "可终止：目标达成度高且优化空间小"

        return SpecCycleMetrics(
            cycle_number=cycle.cycle_number,
            satisfied_count=satisfied,
            total_criteria=total,
            new_satisfied=new_satisfied,
            review_suggestions=review_suggestions,
            backlog_pending=backlog_pending,
            goal_attainment=goal_attainment,
            improvement_space=improvement_space,
            termination_hint=termination_hint,
        )

    @staticmethod
    def _review_result_to_text(review: ReviewResult) -> str:
        if not review:
            return ""
        lines: list[str] = []
        for pr in review.reviews:
            verdict = "PASS" if pr.passed else "FAIL"
            lines.append(f"[{pr.perspective.name}] {verdict}")
            for s in pr.suggestions:
                lines.append(f"- {s}")
            lines.append("")
        return "\n".join(lines).strip() + "\n"

    # ------------------------------------------------------------------
    # Prompt builders
    # ------------------------------------------------------------------
    def _build_spec_prompt(self, requirement: str) -> str:
        """Phase 1: Analyze requirement into structured spec (WHAT & WHY, not HOW)."""
        guidance = self._consume_guidance()
        criteria_status = self._format_criteria_status()
        return f"""你是一个专业的软件架构师。请使用 spec-kit 风格产出“规格（Spec）”。

目标：只描述 **做什么/为什么/范围与约束**，不讨论具体怎么实现。

## 需求
{requirement}

## 工作目录
{self.root_path}
{guidance}{criteria_status}
## 输出要求（必须严格遵守）
仅输出一个 JSON 对象，放在 ```json fenced code block``` 中，不要输出任何其他文字。

Schema（字段必须存在；数组元素为字符串）：
{{
  "goals": ["..."],
  "functional_spec": ["..."],
  "non_functional_requirements": ["..."],
  "acceptance_criteria": ["可验证条件..."],
  "out_of_scope": ["明确不做什么..."],
  "risks": ["风险/约束..."],
  "clarification_questions": ["已识别的模糊点（仅记录，不等待用户回答）..."],
  "decisions": ["已确认/可接受的假设（必须显式标注为假设）..."],
  "version": "1.0"
}}

约束：
- acceptance_criteria 必须可被判定 PASS/FAIL
- 遇到信息不足时，不要停下等待用户——基于项目上下文和行业最佳实践自主选择最优方案
- 将模糊点记录在 clarification_questions 中（仅供参考），将你的决策记录在 decisions 中
- 如果用户引导中提供了相关信息，优先使用用户的指示
"""

    def _build_plan_prompt(self, spec: str) -> str:
        """Phase 2: Generate technical plan from spec."""
        return f"""你是一个资深工程师。基于下述 Spec（规格），产出 Plan（规划），强调可执行、可验证。

## Spec 输入
{spec}

## 工作目录
{self.root_path}

## 输出要求（必须严格遵守）
仅输出一个 JSON 对象，放在 ```json fenced code block``` 中，不要输出任何其他文字。

Schema（字段必须存在；数组元素为字符串）：
{{
  "architecture": "总体架构与关键决策（文本）",
  "tech_stack": ["语言/框架/库..."],
  "steps": ["按优先级的一句话步骤..."],
  "file_changes": ["新增/修改文件路径..."],
  "test_plan": ["将新增/更新的测试与验证方式..."],
  "risks": ["风险与应对..."],
  "version": "1.0"
}}
"""

    def _build_task_prompt(self, plan: str) -> str:
        """Phase 3: Break plan into granular tasks."""
        return f"""将以下实现方案分解为可执行的具体任务。

## 实现方案
{plan}

## 输出要求
请输出结构化的任务列表，每个任务包含：
- 任务编号（1, 2, 3, ...）
- 任务描述（一句话）
- 依赖的任务编号（如果有）

格式（严格遵循）：
1. [任务描述] (依赖: 无)
2. [任务描述] (依赖: 1)
3. [任务描述] (依赖: 1, 2)
...

要求：
- 每个任务应可独立测试
- 任务粒度适中，不要过大或过小
- 标注依赖关系以确定执行顺序
"""

    def _build_build_prompt(self, tasks: list[SpecTask], plan: str) -> str:
        """Phase 4: Execute tasks (agent writes actual code)."""
        task_list = "\n".join(
            f"{t.task_id}. {t.description}"
            for t in tasks
        )
        guidance = self._consume_guidance()
        return f"""按以下任务列表逐步执行实现。

## 实现方案
{plan}

## 任务列表
{task_list}

## 工作目录
{self.root_path}
{guidance}
## 要求
1. 严格按照任务顺序执行
2. 每个任务完成后进行自检
3. 确保代码质量：无安全漏洞、有适当的错误处理
4. 完成所有任务后输出总结
"""

    def _build_review_prompt(self) -> str:
        """Build the multi-perspective review prompt (same as loop engine)."""
        perspective_sections = []
        for p in ReviewPerspective:
            perspective_sections.append(f"- **{p.value.upper()}**: {p.review_focus}")
        perspectives_desc = "\n".join(perspective_sections)

        goal = self._project.requirement if self._project else ""

        return f"""请从以下四个视角审查当前的实现质量，并给出结构化的审查结果。

## 项目目标
{goal}

## 审查视角
{perspectives_desc}

## 输出格式要求
严格按照以下格式输出每个视角的审查结果（每个视角占一个区块）：

[ARCHITECT]
PASS 或 FAIL
- 改进建议1（如果FAIL）
- 改进建议2（如果FAIL）

[PRODUCT]
PASS 或 FAIL
- 改进建议1（如果FAIL）

[USER]
PASS 或 FAIL
- 改进建议1（如果FAIL）

[TESTER]
PASS 或 FAIL
- 改进建议1（如果FAIL）

## 审查标准
- PASS: 该视角认为当前实现质量良好，无需改进
- FAIL: 该视角发现可改进之处，请列出具体建议
- 建议应具体、可操作，而非泛泛而谈
- 如果某视角为 PASS，不需要列出建议
"""

    def _build_refinement_input(self, original_requirement: str) -> str:
        """Build refined input from review suggestions for next cycle."""
        lines = [f"## 原始需求\n{original_requirement}\n"]

        if self._last_review:
            failed = self._last_review.failed_perspectives
            if failed:
                lines.append("## 上轮审查改进建议\n以下建议需要在本轮 Spec 循环中解决：\n")
                for pr in failed:
                    lines.append(f"{pr.perspective.emoji} **{pr.perspective.display_name}**:")
                    for s in pr.suggestions:
                        lines.append(f"  - {s}")
                    lines.append("")

        # Include criteria progress
        if self._project:
            tracker = self._project.criteria_tracker
            unsatisfied = tracker.unsatisfied_criteria
            if unsatisfied:
                lines.append("## 未满足的验收标准\n")
                for c in unsatisfied:
                    lines.append(f"- [ ] {c}")
                lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Artifact parsing (spec-kit inspired)
    # ------------------------------------------------------------------
    def _parse_spec_artifact(self, text: str) -> tuple[Optional[SpecArtifact], list[str]]:
        """Parse spec JSON artifact.

        Returns: (artifact|None, validation_errors)
        """
        blob = extract_json_blob(text)
        if not blob:
            return None, ["未找到 ```json``` 规格产物；已降级为纯文本"]
        try:
            data = json.loads(blob)
        except Exception as e:
            return None, [f"规格 JSON 解析失败：{e}"]
        if not isinstance(data, dict):
            return None, ["规格 JSON 不是对象；已降级为纯文本"]

        errors = validate_spec_artifact_dict(data)
        artifact = SpecArtifact.from_dict({
            "goals": normalize_list(data.get("goals")),
            "functional_spec": normalize_list(data.get("functional_spec")),
            "non_functional_requirements": normalize_list(data.get("non_functional_requirements")),
            "acceptance_criteria": normalize_list(data.get("acceptance_criteria")),
            "out_of_scope": normalize_list(data.get("out_of_scope")),
            "risks": normalize_list(data.get("risks")),
            "clarification_questions": normalize_list(data.get("clarification_questions")),
            "decisions": normalize_list(data.get("decisions")),
        })
        return artifact, errors

    def _parse_plan_artifact(self, text: str) -> tuple[Optional[PlanArtifact], list[str]]:
        """Parse plan JSON artifact.

        Returns: (artifact|None, validation_errors)
        """
        blob = extract_json_blob(text)
        if not blob:
            return None, ["未找到 ```json``` 规划产物；已降级为纯文本"]
        try:
            data = json.loads(blob)
        except Exception as e:
            return None, [f"规划 JSON 解析失败：{e}"]
        if not isinstance(data, dict):
            return None, ["规划 JSON 不是对象；已降级为纯文本"]

        errors = validate_plan_artifact_dict(data)
        artifact = PlanArtifact.from_dict({
            "architecture": data.get("architecture", ""),
            "tech_stack": normalize_list(data.get("tech_stack")),
            "steps": normalize_list(data.get("steps")),
            "file_changes": normalize_list(data.get("file_changes")),
            "test_plan": normalize_list(data.get("test_plan")),
            "risks": normalize_list(data.get("risks")),
        })
        return artifact, errors

    def _merge_acceptance_criteria(self, new_criteria: list[str]) -> None:
        """Apply criteria from the spec artifact.

        spec-kit 的验收标准应来自 Spec 产物本身，因此在第一轮我们倾向于
        **用 Spec 的 acceptance_criteria 替换** 之前从用户输入中提取的
        fallback 列表（避免形成“双重标准”导致永远无法满足）。

        仅在以下情况下生效：
        - 还未有任何标准被满足（satisfied_count == 0）
        - 新标准不是明显的占位符（如 "CRITERIA_1" 这种标签）
        """
        if not self._project:
            return

        incoming = [c.strip() for c in (new_criteria or []) if c and c.strip()]
        if not incoming:
            return

        # Filter obvious placeholders
        placeholder_pat = re.compile(r"^CRITERIA_\d+$", re.IGNORECASE)
        non_placeholder = [c for c in incoming if not placeholder_pat.match(c)]
        if not non_placeholder:
            return

        # Deduplicate while preserving order
        seen: set[str] = set()
        deduped: list[str] = []
        for c in non_placeholder:
            if c in seen:
                continue
            seen.add(c)
            deduped.append(c)

        if self._project.criteria_tracker.satisfied_count == 0:
            self._project.acceptance_criteria = deduped
            self._project.criteria_tracker.init_criteria(deduped)

    # ------------------------------------------------------------------
    # Requirement parsing (reuses loop pattern)
    # ------------------------------------------------------------------
    def _parse_acceptance_criteria(self, text: str) -> list[str]:
        """Extract acceptance criteria from user input."""
        lines = text.strip().split("\n")
        criteria = []

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("- ") or stripped.startswith("* "):
                criteria.append(stripped[2:])
            elif stripped.startswith("[ ] ") or stripped.startswith("[x] "):
                criteria.append(stripped[4:])

        if not criteria:
            criteria = self._decompose_criteria_with_llm(text)

        if not criteria:
            criteria = [f"完成需求: {text[:100]}"]

        return criteria

    def _decompose_criteria_with_llm(self, text: str) -> list[str]:
        """Use LLM to decompose colloquial input into acceptance criteria."""
        settings = self.settings
        if not settings.ark_api_key or not settings.ark_model:
            return []

        prompt = f"""请分析以下用户需求，提取并拆解为明确的验收标准。

用户需求（口语化描述）：
{text}

要求：
1. 先理解用户的核心诉求
2. 将需求拆解为 3-8 条具体、可验证的验收标准
3. 每条标准应该是独立可验证的（能明确判断 PASS/FAIL）
4. 标准应覆盖用户提到的所有功能点
5. 用简洁的技术语言描述，不要过于笼统

输出格式（严格按此格式，每行一条，以 "- " 开头）：
- 验收标准1
- 验收标准2
- 验收标准3
..."""

        try:
            llm = ChatOpenAI(
                base_url=settings.ark_base_url,
                api_key=settings.ark_api_key,
                model=settings.ark_model,
                temperature=0.1,
            )
            response = llm.invoke([
                SystemMessage(content="你是一个需求分析助手，擅长将口语化的产品需求拆解为结构化的验收标准。"),
                HumanMessage(content=prompt),
            ])
            return self._extract_criteria_from_llm_response(response.content)
        except Exception as e:
            logger.warning("[Spec] LLM 需求拆解失败: %s, 将使用原始文本", e)
            return []

    @staticmethod
    def _extract_criteria_from_llm_response(text: str) -> list[str]:
        """Extract criteria lines from LLM response."""
        criteria = []
        for line in text.strip().split("\n"):
            stripped = line.strip()
            if stripped.startswith("- ") or stripped.startswith("* "):
                criterion = stripped[2:].strip()
            elif len(stripped) > 2 and stripped[0].isdigit() and stripped[1] in ".、):） ":
                criterion = stripped[2:].strip()
            elif len(stripped) > 3 and stripped[:2].isdigit() and stripped[2] in ".、):） ":
                criterion = stripped[3:].strip()
            else:
                continue
            if criterion:
                criteria.append(criterion)
        return criteria

    # ------------------------------------------------------------------
    # Task parsing
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_tasks(text: str) -> list[SpecTask]:
        """Parse numbered task list from agent output."""
        tasks = []
        for line in text.strip().split("\n"):
            m = _TASK_LINE_PATTERN.match(line)
            if not m:
                continue
            task_id = int(m.group(1))
            description = m.group(2).strip().strip("[]")
            deps_str = (m.group(3) or "").strip()
            dependencies = []
            if deps_str and deps_str.lower() not in ("无", "none", ""):
                for dep in re.split(r"[,，\s]+", deps_str):
                    dep = dep.strip()
                    if dep.isdigit():
                        dependencies.append(int(dep))
            tasks.append(SpecTask(
                task_id=task_id,
                description=description,
                dependencies=dependencies,
            ))
        return tasks

    # ------------------------------------------------------------------
    # Criteria evaluation (reuses loop pattern)
    # ------------------------------------------------------------------
    def _evaluate_criteria(self, criteria: list[str], cycle: int) -> dict:
        """Evaluate acceptance criteria by asking the agent in the same session."""
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
            eval_text: list[str] = []

            def on_eval_event(event: ACPEvent):
                if event.event_type == ACPEventType.TEXT_CHUNK and event.text:
                    eval_text.append(event.text)

            self._session.send_prompt(eval_prompt, on_event=on_eval_event, timeout=60)
            full_text = "".join(eval_text).upper()

            per_criteria: dict[int, bool] = {}
            for i in range(len(criteria)):
                pat = _CRITERIA_PATTERNS[i] if i < len(_CRITERIA_PATTERNS) else re.compile(rf"CRITERIA_{i+1}\s*:\s*(PASS|FAIL)")
                match = pat.search(full_text)
                if match:
                    per_criteria[i] = (match.group(1) == "PASS")

            if self._project:
                self._project.criteria_tracker.batch_update(per_criteria, cycle)

            all_satisfied = self._project.criteria_tracker.is_all_satisfied if self._project else False
            return {"all_satisfied": all_satisfied}

        except Exception as e:
            logger.debug("[Spec] 验收标准评估失败: %s", e)
            return {"all_satisfied": False}

    # ------------------------------------------------------------------
    # Review (reuses loop engine's parsing infrastructure)
    # ------------------------------------------------------------------
    def _conduct_review(self, cycle: int, callbacks: SpecEngineCallbacks) -> ReviewResult:
        """Conduct multi-perspective review in the same ACP session."""
        if not self._session:
            return ReviewResult(iteration=cycle)

        review_prompt = self._build_review_prompt()
        review_text: list[str] = []

        def on_review_event(event: ACPEvent):
            if event.event_type == ACPEventType.TEXT_CHUNK and event.text:
                review_text.append(event.text)

        try:
            self._session.send_prompt(review_prompt, on_event=on_review_event, timeout=120)
            full_text = "".join(review_text)
            review_result = self._parse_review_output(full_text, cycle)
        except Exception as e:
            logger.warning("[Spec] 多视角审查异常: %s, 将继续循环", e)
            review_result = ReviewResult(reviews=[
                PerspectiveReview(
                    perspective=p, passed=False,
                    suggestions=[f"审查执行异常: {e}"],
                    summary="异常",
                ) for p in ReviewPerspective
            ], iteration=cycle)

        if callbacks.on_review_done:
            callbacks.on_review_done(cycle, review_result)

        return review_result

    def _parse_review_output(self, text: str, cycle: int) -> ReviewResult:
        """Parse structured review output into ReviewResult (same logic as loop engine)."""
        raw = (text or "").replace("\r\n", "\n")

        # 1) strict/tolerant parsing (shared utils)
        reviews = parse_review_output_strict_tolerant(raw, cycle)

        # 3) LLM fallback
        if not reviews:
            logger.warning("[Spec] 正则解析全部失败, 尝试LLM兜底解析")
            reviews = self._parse_review_with_llm(raw)

        # 4) Final fallback
        if not reviews:
            logger.warning("[Spec] 审查输出解析失败, 将视为有改进建议继续循环")
            for p in ReviewPerspective:
                reviews.append(PerspectiveReview(
                    perspective=p, passed=False,
                    suggestions=["审查输出解析失败，请检查实现质量"],
                    summary="解析失败",
                ))

        return ReviewResult(reviews=reviews, iteration=cycle)

    def _parse_review_with_llm(self, raw_text: str) -> list[PerspectiveReview]:
        """LLM fallback: extract review verdicts from free-form text."""
        settings = self.settings
        if not settings.ark_api_key or not settings.ark_model:
            return []
        if not raw_text or len(raw_text.strip()) < 10:
            return []

        prompt = f"""请从以下文本中提取四个视角的审查结果。

文本内容：
{raw_text[:3000]}

请严格按以下 JSON 格式输出（不要输出其他内容）：
[
  {{"perspective": "ARCHITECT", "verdict": "PASS或FAIL", "suggestions": ["建议1", "建议2"]}},
  {{"perspective": "PRODUCT", "verdict": "PASS或FAIL", "suggestions": []}},
  {{"perspective": "USER", "verdict": "PASS或FAIL", "suggestions": []}},
  {{"perspective": "TESTER", "verdict": "PASS或FAIL", "suggestions": []}}
]"""

        try:
            llm = ChatOpenAI(
                base_url=settings.ark_base_url,
                api_key=settings.ark_api_key,
                model=settings.ark_model,
                temperature=0.0,
            )
            response = llm.invoke([
                SystemMessage(content="你是一个文本解析助手。从审查文本中提取结构化的审查结果，只输出JSON。"),
                HumanMessage(content=prompt),
            ])
            return self._extract_reviews_from_llm_response(response.content)
        except Exception as e:
            logger.warning("[Spec] LLM 兜底审查解析失败: %s", e)
            return []

    @staticmethod
    def _extract_reviews_from_llm_response(text: str) -> list[PerspectiveReview]:
        """Parse LLM JSON response into PerspectiveReview list."""
        cleaned = text.strip()
        if "```" in cleaned:
            parts = cleaned.split("```")
            for part in parts:
                stripped = part.strip()
                if stripped.startswith("json"):
                    stripped = stripped[4:].strip()
                if stripped.startswith("["):
                    cleaned = stripped
                    break

        start = cleaned.find("[")
        end = cleaned.rfind("]")
        if start == -1 or end == -1 or end <= start:
            return []

        try:
            data = json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError:
            return []

        if not isinstance(data, list):
            return []

        reviews: list[PerspectiveReview] = []
        found: set[str] = set()
        for item in data:
            if not isinstance(item, dict):
                continue
            tag = str(item.get("perspective", "")).upper()
            perspective = _PERSPECTIVE_TAG_MAP.get(tag)
            if not perspective or tag in found:
                continue
            found.add(tag)
            verdict = str(item.get("verdict", "")).upper()
            passed = verdict == "PASS"
            suggestions = item.get("suggestions", [])
            if not isinstance(suggestions, list):
                suggestions = []
            suggestions = [str(s) for s in suggestions if s]
            if passed:
                suggestions = []
            reviews.append(PerspectiveReview(
                perspective=perspective, passed=passed,
                suggestions=suggestions,
                summary=f"{'通过' if passed else f'{len(suggestions)}条建议'}",
            ))
        return reviews

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _format_criteria_status(self) -> str:
        """Format current criteria status for prompt inclusion."""
        if not self._project or not self._project.criteria_tracker.criteria:
            return ""
        tracker = self._project.criteria_tracker
        lines = ["\n## 验收标准进度"]
        for i, c in enumerate(tracker.criteria):
            if tracker.satisfied.get(i, False):
                lines.append(f"- [x] {c} ✅ (已满足)")
            else:
                lines.append(f"- [ ] {c}")
        return "\n".join(lines) + "\n"

    def _consume_guidance(self) -> str:
        """Consume and format pending user guidance."""
        if not self._user_guidance:
            return ""
        combined = "\n\n".join(self._user_guidance)
        self._user_guidance.clear()
        return f"\n## 用户引导\n{combined}\n"

    def _detect_convergence(self) -> bool:
        """Detect if recent cycles made no progress.

        Spec 模式的收敛定义：在最近的窗口期内
        - 验收标准满足数量没有任何增加
        - 且多视角审查建议数量也没有任何变化（一直重复同一批建议/一直无建议）

        注意：收敛仅表示“卡住”，不代表需求达成；最终是否成功由
        “验收标准全部满足 + 审查通过”决定。
        """

        if not self._project:
            return False

        window = int(self.settings.spec_convergence_window or 0)
        if window <= 0:
            return False
        if len(self._project.cycles) < window:
            return False

        # When already all satisfied, never treat as converged.
        if self._project.criteria_tracker.is_all_satisfied:
            return False

        recent = self._project.cycles[-window:]

        # --- Criteria progress ---
        tracker = self._project.criteria_tracker
        counts: list[int] = []
        for c in recent:
            satisfied_now = 0
            for idx in range(len(tracker.criteria)):
                at = tracker.satisfied_at_iteration.get(idx)
                if at is not None and at <= c.cycle_number:
                    satisfied_now += 1
            counts.append(satisfied_now)

        if len(set(counts)) != 1:
            return False  # criteria improved in window

        # --- Review suggestion progress (content-aware, not just counts) ---
        def _norm(s: str) -> str:
            return re.sub(r"\s+", " ", (s or "").strip().lower())

        suggestion_sets: list[frozenset[str]] = []
        if not self.settings.spec_review_enabled:
            suggestion_sets = [frozenset()] * window
        else:
            for c in recent:
                if c.review_result is None:
                    return False
                ss: set[str] = set()
                for pr in c.review_result.failed_perspectives:
                    for s in pr.suggestions:
                        ns = _norm(str(s))
                        if ns:
                            ss.add(ns)
                suggestion_sets.append(frozenset(ss))

        return len(set(suggestion_sets)) == 1

    def inject_guidance(self, message: str):
        """Inject user guidance — will be included in the next phase prompt."""
        self._user_guidance.append(message)
        logger.info("[Spec] 用户引导已注入(队列=%d): %s...", len(self._user_guidance), message[:100])

    def stop(self):
        self._run_state = EngineRunState.STOPPING
        if self._session:
            self._session.cancel()

    def pause(self):
        if self._project:
            self._project.status = SpecProjectStatus.PAUSED
        self._run_state = EngineRunState.STOPPING
        if self._session:
            self._session.cancel()

    def resume(self, callbacks: Optional[SpecEngineCallbacks] = None) -> Optional[SpecProject]:
        """Resume a paused spec execution."""
        if not self._project or self._project.status not in (SpecProjectStatus.PAUSED, SpecProjectStatus.CLARIFYING):
            return self._project

        callbacks = callbacks or SpecEngineCallbacks()
        self._run_state = EngineRunState.RUNNING
        self._project.status = SpecProjectStatus.RUNNING
        max_cycles = self._resolve_max_cycles(self.settings.spec_max_cycles)
        start_cycle = len(self._project.cycles) + 1
        self._termination_reason = None

        try:
            self._close_session_safely()
            self._session = create_engine_session(
                agent_type=self._agent_type, cwd=self.root_path,
                on_rate_limit=getattr(self, "_on_rate_limit", None),
            )

            self._termination_reason = self._run_cycle_loop(
                start_cycle=start_cycle,
                max_cycles=max_cycles,
                callbacks=callbacks,
                timeout=self.settings.spec_execution_timeout,
            )

            reason = self._termination_reason or "max_cycles"
            if self._run_state == EngineRunState.STOPPING or reason == "paused":
                self._project.status = SpecProjectStatus.PAUSED
            elif reason == "success":
                self._project.status = SpecProjectStatus.COMPLETED
                self._project.completed_at = time.time()
            else:
                if reason == "converged":
                    msg = (
                        f"收敛终止：连续{self.settings.spec_convergence_window}轮无有效改进，"
                        "仍有未满足验收标准或审查未通过"
                    )
                elif reason == "max_cycles":
                    msg = f"达到最大循环次数({max_cycles})仍未满足验收标准或审查未通过"
                else:
                    msg = f"终止：{reason}"
                self._project.abort(msg)

            if callbacks.on_project_done:
                callbacks.on_project_done(self._project)

        except Exception as e:
            error_msg = f"Spec恢复异常: {str(e)}"
            logger.error("[Spec:%s] %s", self._project.name, error_msg)
            self._project.status = SpecProjectStatus.ABORTED
            self._project.completed_at = time.time()
            if callbacks.on_error:
                callbacks.on_error(error_msg)

        finally:
            self._close_session_safely()
            self._run_state = EngineRunState.IDLE

        return self._project

    def get_rendered_content(self) -> str:
        return self._renderer.get_final_content()

    def _project_to_compact_dict(self) -> dict:
        """Build a compact project dict for state persistence.

        关键点：避免在 5000+ 循环下每轮把全部 cycles/work_items/metrics 全量写回，
        否则会出现 O(n^2) 的 IO 开销导致不稳定。
        """
        if not self._project:
            return {}

        tail_cycles = int(getattr(self.settings, "spec_state_cycles_tail", 50) or 50)
        tail_items = int(getattr(self.settings, "spec_state_work_items_tail", 200) or 200)
        tail_metrics = int(getattr(self.settings, "spec_state_metrics_tail", 200) or 200)

        cycle_count_total = max(int(getattr(self._project, "cycle_count_total", 0) or 0), len(self._project.cycles))
        work_items_total = max(int(getattr(self._project, "work_items_total", 0) or 0), len(self._project.work_items))

        pd = {
            "project_id": self._project.project_id,
            "name": self._project.name,
            "root_path": self._project.root_path,
            "requirement": self._project.requirement,
            "acceptance_criteria": list(self._project.acceptance_criteria),
            "criteria_tracker": self._project.criteria_tracker.to_dict(),
            "status": self._project.status.value,
            "created_at": self._project.created_at,
            "started_at": self._project.started_at,
            "completed_at": self._project.completed_at,
            "error": self._project.error,
            "cycle_count_total": cycle_count_total,
            "work_items_total": work_items_total,
            "cycles": [c.to_dict() for c in (self._project.cycles[-tail_cycles:] if tail_cycles > 0 else [])],
            "metrics_history": [m.to_dict() for m in (self._project.metrics_history[-tail_metrics:] if tail_metrics > 0 else [])],
        }

        # Work items: keep pending + last N for traceability
        pending = [w for w in self._project.work_items if w.status == SpecWorkItemStatus.PENDING]
        recent = self._project.work_items[-tail_items:] if tail_items > 0 else []
        merged: list[SpecWorkItem] = []
        seen: set[str] = set()
        for w in pending + list(recent):
            if not w or not w.item_id or w.item_id in seen:
                continue
            seen.add(w.item_id)
            merged.append(w)
        pd["work_items"] = [w.to_dict() for w in merged]
        pd["_compact"] = {
            "cycles_tail": tail_cycles,
            "work_items_tail": tail_items,
            "metrics_tail": tail_metrics,
            "cycles_truncated_before": max(0, cycle_count_total - len(pd.get("cycles") or [])),
        }
        # For discoverability
        pd["artifacts_root"] = self._artifact_root_dir()
        pd["history_log_path"] = self._history_log_path()
        return pd

    def save_state(self, filepath: Optional[str] = None) -> str:
        if not self._project:
            raise ValueError("没有项目状态可保存")
        if not filepath:
            filepath = self._get_state_path()
        state = {
            "chat_id": self.chat_id,
            "root_path": self.root_path,
            "project": self._project_to_compact_dict(),
            "saved_at": time.time(),
        }
        tmp_path = filepath + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, filepath)
        return filepath

    @classmethod
    def load_state(cls, filepath: str) -> Optional[SpecProject]:
        """Load a SpecProject from a state file written by save_state()."""
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            proj = data.get("project")
            if not isinstance(proj, dict):
                return None
            return SpecProject.from_dict(proj)
        except Exception:
            return None

    def cleanup(self):
        if self._session:
            try:
                self._session.close()
            except Exception as e:
                logger.debug("关闭ACP session失败: %s", e)
            self._session = None
        self._project = None
        self._run_state = EngineRunState.IDLE


class SpecEngineManager:
    """Manages SpecEngine instances per chat.

    Uses a secondary index (_chat_keys) to avoid O(n) full-table scans.
    """

    def __init__(self):
        self._engines: dict[str, SpecEngine] = {}
        self._chat_keys: dict[str, set[str]] = {}
        self._lock = threading.Lock()

    def _add_index(self, chat_id: str, key: str) -> None:
        self._chat_keys.setdefault(chat_id, set()).add(key)

    def _iter_chat_engines(self, chat_id: str):
        for key in self._chat_keys.get(chat_id, ()):
            engine = self._engines.get(key)
            if engine:
                yield engine

    def get_or_create(self, chat_id: str, root_path: str, engine_name: str = "Coco") -> SpecEngine:
        key = f"{chat_id}:{root_path}"
        agent_type = "claude" if engine_name.lower().startswith("claude") else "coco"

        with self._lock:
            if key not in self._engines:
                self._engines[key] = SpecEngine(
                    chat_id=chat_id,
                    root_path=root_path,
                    agent_type=agent_type,
                    engine_name=engine_name,
                )
                self._add_index(chat_id, key)
            else:
                existing = self._engines[key]
                if existing.engine_name.lower() != engine_name.lower() and not existing.is_running:
                    existing.cleanup()
                    self._engines[key] = SpecEngine(
                        chat_id=chat_id,
                        root_path=root_path,
                        agent_type=agent_type,
                        engine_name=engine_name,
                    )
            return self._engines[key]

    def load_or_create_from_disk(self, chat_id: str, root_path: str, engine_name: str = "Coco") -> SpecEngine:
        """Create engine and hydrate project state from disk if present.

        用于进程重启后的断点续传：handler 在 `/spec_status`/`/spec_resume` 时可调用。
        """
        engine = self.get_or_create(chat_id, root_path, engine_name=engine_name)
        if not engine._get_bool_setting("spec_allow_resume_from_disk", True):
            return engine
        state_path = os.path.join(root_path, getattr(engine.settings, "spec_state_filename", ".spec_engine_state.json"))
        if os.path.exists(state_path) and (engine.project is None):
            try:
                with open(state_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                proj = data.get("project")
                if isinstance(proj, dict):
                    engine._project = SpecProject.from_dict(proj)
                    engine._resume_meta = {
                        "state_path": state_path,
                        "saved_at": data.get("saved_at"),
                        "compact": proj.get("_compact"),
                    }
            except Exception:
                pass
        return engine

    def get(self, chat_id: str, root_path: str) -> Optional[SpecEngine]:
        key = f"{chat_id}:{root_path}"
        return self._engines.get(key)

    def get_active_engine(self, chat_id: str) -> Optional[SpecEngine]:
        for engine in self._iter_chat_engines(chat_id):
            if engine.is_running:
                return engine
        return None

    def get_active_engines(self, chat_id: str) -> list[SpecEngine]:
        return [e for e in self._iter_chat_engines(chat_id) if e.is_running]

    def list_engines(self, chat_id: Optional[str] = None) -> list[SpecEngine]:
        if chat_id is None:
            return list(self._engines.values())
        return list(self._iter_chat_engines(chat_id))

    def cleanup_all(self):
        with self._lock:
            for engine in self._engines.values():
                engine.cleanup()
            self._engines.clear()
            self._chat_keys.clear()
