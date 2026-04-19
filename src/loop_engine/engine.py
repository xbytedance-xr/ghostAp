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
from dataclasses import dataclass, field
from typing import Callable, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from ..acp import ACPEvent, ACPEventType
from ..agent_session import create_engine_session
from ..engine_base import BaseEngine, BaseEngineManager, EngineRunState
from ..utils.gc_monitor import get_gc_monitor
from ..utils.llm import ChatOpenAICacheKey, get_cached_chat_openai
from ..utils.retry import RetryPolicy
from ..utils.spec_utils import (
    CRITERIA_PATTERNS as _CRITERIA_PATTERNS,
)
from ..utils.spec_utils import (
    PERSPECTIVE_TAG_MAP as _PERSPECTIVE_TAG_MAP,
)
from ..utils.spec_utils import (
    parse_review_output_loose as _parse_review_output_loose,
)
from ..utils.trace import TraceContext
from .models import (
    IterationRecord,
    IterationStatus,
    LoopContextManager,
    LoopProject,
    LoopProjectStatus,
    LoopRequirement,
    PerspectiveReview,
    ReviewPerspective,
    ReviewResult,
)
from .tracker import IterationTracker

logger = logging.getLogger(__name__)


@dataclass
class LoopReviewCircuitState:
    """Loop Engine review 熔断器状态，与 SpecEngine 的 ReviewCircuitState 同构。"""

    review_failure_consecutive: int = 0
    review_circuit_open_until_iter: int = 0
    last_review_failure_diag: Optional[dict] = None
    backoff_level: int = 0
    consecutive_timeouts: int = 0
    consecutive_skips: int = 0
    last_review_elapsed_ms: int = 0
    recent_outcomes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "review_failure_consecutive": self.review_failure_consecutive,
            "review_circuit_open_until_iter": self.review_circuit_open_until_iter,
            "backoff_level": self.backoff_level,
            "consecutive_timeouts": self.consecutive_timeouts,
            "consecutive_skips": self.consecutive_skips,
            "last_review_elapsed_ms": self.last_review_elapsed_ms,
            "recent_outcomes": list(self.recent_outcomes)[-20:],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "LoopReviewCircuitState":
        return cls(
            review_failure_consecutive=int(data.get("review_failure_consecutive") or 0),
            review_circuit_open_until_iter=int(data.get("review_circuit_open_until_iter") or 0),
            backoff_level=int(data.get("backoff_level") or 0),
            consecutive_timeouts=int(data.get("consecutive_timeouts") or 0),
            consecutive_skips=int(data.get("consecutive_skips") or 0),
            last_review_elapsed_ms=int(data.get("last_review_elapsed_ms") or 0),
            recent_outcomes=list(data.get("recent_outcomes") or []),
        )


@dataclass
class LoopEngineCallbacks:
    """Loop Engine event callbacks."""

    on_analyzing_start: Optional[Callable[[str], None]] = None
    on_analyzing_done: Optional[Callable[[LoopProject], None]] = None
    on_iteration_start: Optional[Callable[[int, int], None]] = None  # (current, max)
    on_iteration_event: Optional[Callable[[int, ACPEvent], None]] = None
    on_iteration_done: Optional[Callable[[int, IterationRecord], None]] = None
    on_review_done: Optional[Callable[[int, ReviewResult], None]] = None  # (iteration, review)
    on_project_done: Optional[Callable[[LoopProject], None]] = None
    on_error: Optional[Callable[[str], None]] = None


class LoopEngine(BaseEngine):
    """ACP-driven iterative closed-loop engine."""

    _state_filename = ".loop_engine_state.json"
    _gc_label = "Loop"
    _gc_threshold_default = 85.0

    def __init__(
        self,
        chat_id: str,
        root_path: str,
        agent_type: str = "coco",
        engine_name: str = "Coco",
        model_name: Optional[str] = None,
    ):
        super().__init__(chat_id, root_path, agent_type, engine_name, model_name)
        self._user_guidance: list[str] = []
        self._last_review: Optional[ReviewResult] = None
        self._review_extra_used: int = 0
        self._last_heartbeat: float = 0.0
        self._context_manager: Optional[LoopContextManager] = None
        self._llm_cache: dict[ChatOpenAICacheKey, ChatOpenAI] = {}
        self._review_circuit = LoopReviewCircuitState()

    def save_state(self, filepath: Optional[str] = None) -> str:
        """Override BaseEngine to include review circuit state."""
        if not self._project:
            raise ValueError("没有项目状态可保存")
        if not filepath:
            filepath = os.path.join(self.root_path, self._state_filename)
        state = {
            "chat_id": self.chat_id,
            "root_path": self.root_path,
            "project": self._project.to_dict(),
            "saved_at": time.time(),
            "review_circuit": self._review_circuit.to_dict(),
        }
        tmp_path = filepath + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, filepath)
        return filepath

    @classmethod
    def load_state_with_circuit(cls, filepath: str) -> tuple[Optional[LoopProject], LoopReviewCircuitState]:
        """Load project + review circuit state (backward-compatible)."""
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            proj_data = data.get("project")
            if not isinstance(proj_data, dict):
                return None, LoopReviewCircuitState()
            project = LoopProject.from_dict(proj_data)
            rc = data.get("review_circuit")
            circuit = LoopReviewCircuitState.from_dict(rc) if isinstance(rc, dict) else LoopReviewCircuitState()
            return project, circuit
        except Exception:
            return None, LoopReviewCircuitState()

    def execute(
        self,
        requirement_text: str,
        callbacks: Optional[LoopEngineCallbacks] = None,
        task_id: Optional[str] = None,
        on_rate_limit: Optional[Callable[[int], None]] = None,
    ) -> LoopProject:
        """Iterate until acceptance criteria are satisfied."""
        callbacks = callbacks or LoopEngineCallbacks()
        with self._lock:
            self._run_state = EngineRunState.RUNNING
        self._on_rate_limit = on_rate_limit
        max_iterations = self.settings.loop_max_iterations

        # Create project
        project_name = os.path.basename(self.root_path) or "loop_project"
        with self._lock:
            self._project = LoopProject.create(
                name=project_name,
                root_path=self.root_path,
            )
            self._project.task_id = task_id
            self._project.status = LoopProjectStatus.ANALYZING

        if callbacks.on_analyzing_start:
            callbacks.on_analyzing_start(requirement_text)

        logger.info(
            "[Loop:%s] ACP迭代开始, 需求长度=%d, 路径=%s, agent=%s",
            project_name,
            len(requirement_text),
            self.root_path,
            self._agent_type,
        )

        # Initialize TraceContext
        trace_ctx = TraceContext(trace_id=task_id or f"loop-{int(time.time())}")

        try:
            with trace_ctx:
                # Parse requirement — extract acceptance criteria
                requirement = self._parse_requirement(requirement_text)
                with self._lock:
                    self._project.set_requirement(requirement)  # initializes CriteriaTracker
                    self._project.status = LoopProjectStatus.RUNNING
    
                if callbacks.on_analyzing_done:
                    callbacks.on_analyzing_done(self._project)
    
                # Create session
                from ..utils.path import normalize_ttadk_cwd
    
                self._session = create_engine_session(
                    agent_type=self._agent_type,
                    cwd=normalize_ttadk_cwd(self.root_path) or self.root_path,
                    on_rate_limit=on_rate_limit,
                    model_name=self._model_name,
                )
    
                # Build initial prompt
                initial_prompt = self._build_initial_prompt(requirement)
                timeout = self.settings.loop_execution_timeout

                self._context_manager = LoopContextManager(
                    max_context_tokens=self.settings.loop_max_context_tokens,
                )

                review_enabled = self.settings.loop_review_enabled
                review_extra_max = self.settings.loop_review_extra_iterations
                self._review_extra_used = 0
                self._last_review = None
    
                for iteration in range(1, max_iterations + 1):
                    with self._lock:
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
                    on_event = self._make_on_event(iter_tracker, iteration, callbacks)

                    result = self._session.send_prompt_with_retry(
                        prompt, on_event=on_event, timeout=timeout,
                        retry_policy=RetryPolicy(max_retries=3, retry_delay=2.0),
                    )
    
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
    
                    logger.info(
                        "[Loop:%s] 迭代 %d/%d 完成, 工具=%d, 文件=%d",
                        project_name,
                        iteration,
                        max_iterations,
                        len(iter_tracker.tool_calls),
                        len(iter_tracker.modified_files),
                    )
    
                    # Multi-perspective review phase
                    with self._lock:
                        is_running = self._run_state == EngineRunState.RUNNING
                    
                    if review_enabled and is_running:
                        review_result, review_decision = self._conduct_review(iteration, callbacks)
                        record.review_result = review_result
                        record.review_decision = review_decision
                        self._last_review = review_result
    
                    with self._lock:
                        self._project.iterations.append(record)

                    if self._context_manager:
                        self._context_manager.record_iteration(record)
    
                    if callbacks.on_iteration_done:
                        callbacks.on_iteration_done(iteration, record)
    
                    # Save state precisely after each iteration finishes to support fine-grained recovery
                    try:
                        self.save_state()
                    except Exception as e:
                        logger.warning("[Loop:%s] 细粒度状态保存失败: %s", project_name, str(e) or repr(e))
    
                    # Evaluate acceptance criteria in the same session
                    criteria_result = self._evaluate_criteria(requirement.acceptance_criteria, iteration)
                    all_criteria_satisfied = criteria_result.get("all_satisfied", False)
    
                    # Termination logic: criteria + review
                    if all_criteria_satisfied:
                        if not review_enabled or (self._last_review and self._last_review.all_passed):
                            logger.info("[Loop:%s] 所有验收标准+审查通过, 迭代 %d 轮", project_name, iteration)
                            break
                        # Criteria satisfied but review has suggestions — allow extra iterations
                        self._review_extra_used += 1
                        if self._review_extra_used > review_extra_max:
                            logger.info(
                                "[Loop:%s] 验收标准已满足，审查额外迭代超限(%d), 迭代 %d 轮",
                                project_name,
                                review_extra_max,
                                iteration,
                            )
                            break
                        logger.info(
                            "[Loop:%s] 验收标准已满足但审查有建议, 额外迭代 %d/%d",
                            project_name,
                            self._review_extra_used,
                            review_extra_max,
                        )
    
                    # Convergence detection
                    if self._detect_convergence():
                        logger.info("[Loop:%s] 收敛检测触发, 迭代 %d 轮", project_name, iteration)
                        break
    
                # Determine final status
                with self._lock:
                    if self._run_state == EngineRunState.STOPPING:
                        self._project.status = LoopProjectStatus.PAUSED
                    else:
                        self._project.status = LoopProjectStatus.COMPLETED
                        self._project.completed_at = time.time()
    
                if callbacks.on_project_done:
                    callbacks.on_project_done(self._project)
    
                return self._project

        except TimeoutError as e:
            from ..utils.errors import get_error_detail

            detail = get_error_detail(e)

            error_msg = f"Loop执行超时: {detail}"
            logger.warning("[Loop:%s] %s", project_name, error_msg)
            if self._project:
                self._project.status = LoopProjectStatus.ABORTED
                self._project.completed_at = time.time()
            if callbacks.on_error:
                callbacks.on_error(error_msg)
            return self._project

        except Exception as e:
            from ..utils.errors import get_error_detail

            detail = get_error_detail(e)

            error_msg = f"Loop执行异常: {detail}"
            logger.error("[Loop:%s] %s", project_name, error_msg)
            if self._project:
                self._project.status = LoopProjectStatus.ABORTED
                self._project.completed_at = time.time()
            if callbacks.on_error:
                callbacks.on_error(error_msg)
            return self._project

        finally:
            self._run_state = EngineRunState.IDLE
            get_gc_monitor(memory_threshold_percent=self._gc_threshold_default).check_and_collect(label=self._gc_label)

    def _make_on_event(
        self,
        iter_tracker: IterationTracker,
        iteration: int,
        callbacks: LoopEngineCallbacks,
    ) -> Callable[[ACPEvent], None]:
        """Create the on_event callback shared by execute and resume loops."""

        def on_event(event: ACPEvent, _it=iteration):
            try:
                with self._lock:
                    self._last_heartbeat = time.time()
                iter_tracker.process(event)
                renderer = self._renderer
                if renderer is not None:
                    renderer.process_event(event)
                if callbacks.on_iteration_event:
                    try:
                        callbacks.on_iteration_event(_it, event)
                    except Exception as cb_exc:
                        logger.debug("[Loop] on_iteration_event callback failed: %s", str(cb_exc) or repr(cb_exc))
            except Exception as exc:
                logger.debug("[Loop] on_event handler error: %s", str(exc) or repr(exc))

        return on_event

    def _parse_requirement(self, text: str) -> LoopRequirement:
        """Parse requirement — extract goal and acceptance criteria.

        1. Try to extract criteria from explicit list markers (- / * / [ ]).
        2. If none found, use LLM to summarize and decompose the user's
           colloquial input into structured acceptance criteria.
        3. Fall back to a single generic criterion only if LLM also fails.
        """
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
            # No explicit list markers — use LLM to decompose
            criteria = self._decompose_criteria_with_llm(text)

        if not criteria:
            # LLM failed — last resort fallback
            criteria = [f"完成需求: {text}"]

        return LoopRequirement(
            goal=goal,
            acceptance_criteria=criteria,
            raw_text=text,
        )

    def _get_llm(self, temperature: float) -> ChatOpenAI:
        return get_cached_chat_openai(self.settings, temperature, cache=self._llm_cache, llm_cls=ChatOpenAI)

    def _decompose_criteria_with_llm(self, text: str) -> list[str]:
        """Use LLM to summarize and decompose colloquial user input into acceptance criteria."""
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
            response = self._get_llm(0.1).invoke(
                [
                    SystemMessage(content="你是一个需求分析助手，擅长将口语化的产品需求拆解为结构化的验收标准。"),
                    HumanMessage(content=prompt),
                ]
            )
            return self._extract_criteria_from_llm_response(response.content)
        except Exception as e:
            logger.warning("[Loop] LLM 需求拆解失败: %s, 将使用原始文本", str(e) or repr(e))
            return []

    @staticmethod
    def _extract_criteria_from_llm_response(text: str) -> list[str]:
        """Extract criteria lines from LLM response."""
        criteria = []
        for line in text.strip().split("\n"):
            stripped = line.strip()
            # Accept "- xxx", "* xxx", "N. xxx", "N、xxx" patterns
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

    def _build_initial_prompt(self, requirement: LoopRequirement) -> str:
        criteria_list = "\n".join(f"- [ ] {c}" for c in requirement.acceptance_criteria)
        review_note = ""
        if self.settings.loop_review_enabled:
            review_note = """
## 审查机制
每轮实现完成后，将从架构师、产品经理、用户和测试四个视角对你的工作进行审查。
审查产生的改进建议将作为下一轮迭代的输入，请认真对待每个视角的反馈。
"""
        return f"""你是一个专业的软件工程师。请完成以下产品需求：

## 需求
{requirement.goal}

## 验收标准
{criteria_list}

## 工作目录
{self.root_path}
{review_note}
## 要求
1. 先分析需求，理解验收标准
2. 制定实现计划并逐步执行
3. 每个验收标准都必须通过验证
4. 完成后确认所有标准已满足
"""

    def _build_iteration_prompt(self, iteration: int, requirement: LoopRequirement) -> str:
        tracker = self._project.criteria_tracker if self._project else None
        criteria_lines = []
        for i, c in enumerate(requirement.acceptance_criteria):
            if tracker and tracker.satisfied.get(i, False):
                criteria_lines.append(f"- [x] {c} ✅ (已满足)")
            else:
                criteria_lines.append(f"- [ ] {c}")
        criteria_list = "\n".join(criteria_lines)

        history_section = ""
        if self._context_manager and self._context_manager.iteration_count > 0:
            history_section = f"\n{self._context_manager.build_context_prompt()}\n"

        goal_anchor = f"## 原始目标\n{requirement.goal}\n"

        guidance_section = ""
        if self._user_guidance:
            combined = "\n\n".join(self._user_guidance)
            guidance_section = f"\n## 用户引导\n{combined}\n"
            self._user_guidance.clear()

        review_section = ""
        if self._last_review and not self._last_review.all_passed:
            review_lines = ["## 上轮审查反馈\n以下是上一轮多视角审查中提出的改进建议，请在本轮迭代中优先解决：\n"]
            for pr in self._last_review.failed_perspectives:
                review_lines.append(f"{pr.perspective.emoji} **{pr.perspective.display_name}**:")
                for s in pr.suggestions:
                    review_lines.append(f"  - {s}")
                review_lines.append("")
            review_section = "\n".join(review_lines)

        return f"""继续完成剩余的验收标准。这是第 {iteration} 轮迭代。

{goal_anchor}
## 验收标准进度
{criteria_list}
{history_section}{guidance_section}{review_section}
请聚焦未满足的标准（未打勾的）和审查反馈，继续实现。
完成后报告每个标准的状态。
"""

    def _evaluate_criteria(self, criteria: list[str], iteration: int) -> dict:
        """Evaluate acceptance criteria by asking the agent in the same session.

        Updates the CriteriaTracker with per-criteria PASS/FAIL results.
        """
        if not self._session:
            return {"all_satisfied": False}

        criteria_list = "\n".join(f"CRITERIA_{i + 1}: {c}" for i, c in enumerate(criteria))
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
                pat = (
                    _CRITERIA_PATTERNS[i]
                    if i < len(_CRITERIA_PATTERNS)
                    else re.compile(rf"CRITERIA_{i + 1}\s*:\s*(PASS|FAIL)")
                )
                match = pat.search(full_text)
                if match:
                    per_criteria[i] = match.group(1) == "PASS"

            # Update CriteriaTracker
            if self._project:
                self._project.criteria_tracker.batch_update(per_criteria, iteration)

            pass_count = sum(1 for v in per_criteria.values() if v)
            fail_count = sum(1 for v in per_criteria.values() if not v)
            # Use tracker state for all_satisfied (accumulates across iterations)
            all_satisfied = self._project.criteria_tracker.is_all_satisfied if self._project else False

            return {"all_satisfied": all_satisfied, "pass_count": pass_count, "fail_count": fail_count}

        except Exception as e:
            logger.debug("[Loop] 验收标准评估失败: %s", str(e) or repr(e))
            return {"all_satisfied": False}

    def _build_review_prompt(self) -> str:
        """Build the multi-perspective review prompt."""
        perspective_sections = []
        for p in ReviewPerspective:
            perspective_sections.append(f"- **{p.value.upper()}**: {p.review_focus}")
        perspectives_desc = "\n".join(perspective_sections)

        goal = ""
        if self._project and self._project.requirement:
            goal = self._project.requirement.goal

        return f"""请从以下四个视角审查当前的实现质量，并给出结构化的审查结果。

## 项目目标
{goal}

## 审查视角
{perspectives_desc}

<output_format>
严格按照以下格式输出每个视角的审查结果（每个视角占一个区块）。
不要使用 markdown 表格、JSON、编号列表等任何其他格式。
必须使用 [TAG] 作为区块分隔符。

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

[DESIGNER]
PASS 或 FAIL
- 改进建议1（如果FAIL）
- 改进建议2（如果FAIL）
- (请重点关注: UI视觉、交互体验、移动端适配)
</output_format>

<example>
[ARCHITECT]
PASS

[PRODUCT]
FAIL
- 缺少错误提示文案
- 搜索结果无分页

[USER]
PASS

[TESTER]
FAIL
- 缺少边界条件测试

[DESIGNER]
FAIL
- 按钮间距过小，容易误触
- 错误提示颜色对比度不足
</example>

## 审查标准
- PASS: 该视角认为当前实现质量良好，无需改进
- FAIL: 该视角发现可改进之处，请列出具体建议
- 建议应具体、可操作，而非泛泛而谈
- 如果某视角为 PASS，不需要列出建议
"""

    def _parse_review_output(self, text: str, iteration: int) -> ReviewResult:
        """Parse structured review output into ReviewResult."""
        raw = (text or "").replace("\r\n", "\n")

        # 1) strict/tolerant parsing (shared utils)
        from ..utils.spec_utils import parse_review_output_strict_tolerant

        reviews = parse_review_output_strict_tolerant(raw, iteration)

        # 2.5) loose parsing: keyword-pair / JSON / table formats
        if not reviews:
            reviews = _parse_review_output_loose(raw, iteration)

        # 3) LLM fallback: regex failed, use AI to extract structured data
        if not reviews:
            preview = raw[:500] if raw else "(empty)"
            logger.warning("[Loop] 正则+loose解析全部失败, 尝试LLM兜底解析. 原文预览: %s", preview)
            reviews = self._parse_review_with_llm(raw)

        # 4) Final fallback: nothing parsed at all
        if not reviews:
            logger.warning("[Loop] 审查输出解析失败(含LLM兜底), 将视为有改进建议继续迭代")
            for p in ReviewPerspective:
                reviews.append(
                    PerspectiveReview(
                        perspective=p,
                        passed=False,
                        suggestions=["审查输出解析失败，请检查实现质量"],
                        summary="解析失败",
                    )
                )

        return ReviewResult(reviews=reviews, iteration=iteration)

    def _parse_review_with_llm(self, raw_text: str) -> list[PerspectiveReview]:
        """LLM fallback: extract review verdicts from free-form text using AI."""
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
]

规则：
- perspective 只能是 ARCHITECT/PRODUCT/USER/TESTER
- verdict 只能是 PASS 或 FAIL
- 如果文本中找不到某个视角的审查，verdict 设为 FAIL，suggestions 填 ["未找到该视角的审查结果"]
- suggestions 数组中只放 FAIL 视角的改进建议，PASS 视角为空数组"""

        try:
            response = self._get_llm(0.0).invoke(
                [
                    SystemMessage(content="你是一个文本解析助手。从审查文本中提取结构化的审查结果，只输出JSON。"),
                    HumanMessage(content=prompt),
                ]
            )
            return self._extract_reviews_from_llm_response(response.content)
        except Exception as e:
            logger.warning("[Loop] LLM 兜底审查解析失败: %s", str(e) or repr(e))
            return []

    @staticmethod
    def _extract_reviews_from_llm_response(text: str) -> list[PerspectiveReview]:
        """Parse LLM JSON response into PerspectiveReview list."""
        # Find JSON array in the response (may be wrapped in markdown code block)
        cleaned = text.strip()
        if "```" in cleaned:
            # Extract content between code fences
            parts = cleaned.split("```")
            for part in parts:
                stripped = part.strip()
                if stripped.startswith("json"):
                    stripped = stripped[4:].strip()
                if stripped.startswith("["):
                    cleaned = stripped
                    break

        # Find the JSON array
        start = cleaned.find("[")
        end = cleaned.rfind("]")
        if start == -1 or end == -1 or end <= start:
            return []

        try:
            data = json.loads(cleaned[start : end + 1])
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

            reviews.append(
                PerspectiveReview(
                    perspective=perspective,
                    passed=passed,
                    suggestions=suggestions,
                    summary=f"{'通过' if passed else f'{len(suggestions)}条建议'}",
                )
            )

        return reviews

    def _conduct_review(self, iteration: int, callbacks: LoopEngineCallbacks) -> tuple[ReviewResult, Optional[str]]:
        """Conduct multi-perspective review with circuit breaker.

        Returns:
            (review_result, review_decision) — review_decision is None on
            success, or one of "review_circuit_open_skip" /
            "review_failed_continue" / "review_failed_open_circuit" on
            skip/failure.
        """
        circuit = self._review_circuit
        settings = self.settings

        # --- Circuit breaker: skip review while open ---
        enabled = getattr(settings, "loop_review_failure_circuit_enabled", True)
        max_consecutive = getattr(settings, "loop_review_failure_max_consecutive", 3)
        cooldown = getattr(settings, "loop_review_failure_cooldown_iterations", 3)

        if enabled and iteration <= circuit.review_circuit_open_until_iter:
            circuit.consecutive_skips += 1
            skip_overrun_threshold = max(1, max_consecutive) * 2
            is_skip_overrun = circuit.consecutive_skips >= skip_overrun_threshold
            if is_skip_overrun:
                logger.warning(
                    "[Loop] review_skip_overrun: consecutive_skips=%d >= threshold=%d, iter=%d, "
                    "review circuit may be stuck",
                    circuit.consecutive_skips,
                    skip_overrun_threshold,
                    iteration,
                )
            logger.info(
                "[Loop] Review 熔断中 (iter=%d, open_until=%d), 跳过审查",
                iteration,
                circuit.review_circuit_open_until_iter,
            )
            _base_msg = "审查熔断中，跳过本轮审查"
            if is_skip_overrun:
                _base_msg += f"（⚠ 跳过次数异常偏高：已连续跳过{circuit.consecutive_skips}次，熔断器可能卡住，建议排查）"

            # Lightweight lint fallback
            _lint_msg = ""
            try:
                _lint_enabled = getattr(settings, "review_circuit_lint_fallback_enabled", True)
                if _lint_enabled and self._project and hasattr(self._project, "root_path") and self._project.root_path:
                    from ..utils.lightweight_lint import run_lightweight_lint
                    import glob as _glob
                    _lint_timeout = int(getattr(settings, "review_circuit_lint_timeout", 10) or 10)
                    _py_files = _glob.glob(os.path.join(self._project.root_path, "**/*.py"), recursive=True)[:50]
                    if _py_files:
                        _lint_result = run_lightweight_lint(_py_files, timeout=_lint_timeout)
                        _lint_msg = _lint_result.summary()
            except Exception:
                pass

            _suggestions = [_base_msg]
            if _lint_msg:
                _suggestions.append(_lint_msg)

            fallback = ReviewResult(
                reviews=[
                    PerspectiveReview(
                        perspective=p,
                        passed=False,
                        suggestions=_suggestions,
                        summary="熔断跳过",
                    )
                    for p in ReviewPerspective
                ],
                iteration=iteration,
            )
            if callbacks.on_review_done:
                callbacks.on_review_done(iteration, fallback)
            return fallback, "review_circuit_open_skip"

        if not self._session:
            return ReviewResult(iteration=iteration), None

        review_prompt = self._build_review_prompt()
        review_text: list[str] = []
        thought_text: list[str] = []
        review_decision: Optional[str] = None

        def on_review_event(event: ACPEvent):
            if event.event_type == ACPEventType.TEXT_CHUNK and event.text:
                review_text.append(event.text)
            elif event.event_type == ACPEventType.THOUGHT_CHUNK and event.text:
                thought_text.append(event.text)

        circuit.last_review_failure_diag = None
        review_timeout: int = 0  # sentinel — overwritten inside try; safe fallback for metrics

        import time as _time
        _t0 = _time.monotonic()

        try:
            from ..utils.review_helpers import compute_adaptive_timeout
            base_timeout = int(getattr(settings, "loop_review_timeout", 120) or 120)
            min_timeout = int(getattr(settings, "loop_review_min_timeout", 30) or 30)
            hard_floor = int(getattr(settings, "loop_review_hard_floor", 15) or 15)
            review_timeout = compute_adaptive_timeout(
                circuit.consecutive_timeouts, base_timeout=base_timeout, min_timeout=min_timeout,
                hard_floor=hard_floor,
            )

            def _before_review_retry(attempt: int, error: Exception):
                review_text.clear()
                thought_text.clear()

            self._session.send_prompt_with_retry(
                review_prompt, on_event=on_review_event, timeout=review_timeout,
                retry_policy=RetryPolicy(max_retries=2, retry_delay=2.0),
                before_retry=_before_review_retry,
                total_timeout=float(review_timeout * 2),
            )
            full_text = "".join(review_text)
            combined_text = full_text
            if thought_text:
                combined_text = full_text + "\n" + "".join(thought_text)
            review_result = self._parse_review_output(combined_text, iteration)

            # Success — reset circuit breaker
            circuit.review_failure_consecutive = 0
            circuit.review_circuit_open_until_iter = 0
            circuit.backoff_level = 0
            circuit.consecutive_timeouts = 0
            circuit.consecutive_skips = 0
            # Record success for sliding window tracker
            try:
                circuit.recent_outcomes.append("success")
                if len(circuit.recent_outcomes) > 20:
                    circuit.recent_outcomes[:] = circuit.recent_outcomes[-20:]
            except Exception:
                pass

        except Exception as e:
            from ..utils.review_helpers import handle_review_exception

            result = handle_review_exception(
                e,
                circuit=circuit,
                cycle=iteration,
                settings=settings,
                engine="loop",
                build_diag_kwargs={
                    "project_name": getattr(self._project, "name", ""),
                    "chat_id": self.chat_id,
                    "root_path": self.root_path,
                },
                review_timeout=review_timeout,
                review_elapsed_ms=int((_time.monotonic() - _t0) * 1000),
            )
            review_decision = result.review_decision

            review_result = ReviewResult(
                reviews=[
                    PerspectiveReview(
                        perspective=p,
                        passed=False,
                        suggestions=[result.suggestion_text],
                        summary="异常",
                    )
                    for p in ReviewPerspective
                ],
                iteration=iteration,
            )

        if callbacks.on_review_done:
            callbacks.on_review_done(iteration, review_result)

        return review_result, review_decision

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
        if not self._project or len(self._project.iterations) < self.settings.loop_convergence_window:
            return False

        window = self.settings.loop_convergence_window
        recent = self._project.iterations[-window:]

        # review 异常（timeout 等）产生的 fallback suggestions 是固定模板文本，
        # 连续异常会导致 suggestion 集合完全相同而误判为收敛。
        for r in recent:
            if str(r.review_decision or "").startswith("review_failed"):
                return False

        if all(len(r.output or "") < 50 for r in recent):
            return True

        tracker = self._project.criteria_tracker
        if tracker and tracker.total_count > 0:
            recent_progress = [r.criteria_progress for r in recent]
            if recent_progress:
                satisfied_counts = [
                    sum(1 for v in p.values() if v) for p in recent_progress
                ]
                if len(satisfied_counts) >= window and max(satisfied_counts) - min(satisfied_counts) == 0:
                    all_failed = all(r.status == IterationStatus.FAILED for r in recent)
                    if all_failed:
                        return True

        return False

    def inject_guidance(self, message: str):
        """Inject user guidance — will be included in the next iteration prompt.

        Multiple calls accumulate; all pending guidance is consumed together.
        """
        with self._lock:
            self._user_guidance.append(message)
        logger.info("[Loop] 用户引导已注入(队列=%d): %s...", len(self._user_guidance), message[:100])

    def check_stalled(self, threshold: Optional[float] = None) -> bool:
        """Check if the engine is stalled (no activity for threshold seconds)."""
        if threshold is None:
            threshold = self.settings.loop_watchdog_timeout

        with self._lock:
            if self._run_state != EngineRunState.RUNNING:
                return False
            # If never started heartbeat, use start time or ignore
            if self._last_heartbeat == 0.0:
                return False
            return (time.time() - self._last_heartbeat) > threshold

    def pause(self):
        with self._lock:
            if self._project:
                self._project.status = LoopProjectStatus.PAUSED
            self._run_state = EngineRunState.STOPPING
            if self._session:
                self._session.cancel()

    def resume(self, callbacks: Optional[LoopEngineCallbacks] = None) -> Optional[LoopProject]:
        """Resume a paused loop execution — continue iterating from where we left off."""
        if not self._project or self._project.status not in (LoopProjectStatus.PAUSED, LoopProjectStatus.ABORTED):
            return self._project
        if not self._project.requirement:
            return self._project

        # Restore review circuit state from persistence (survives process restart)
        try:
            state_path = os.path.join(self.root_path, self._state_filename)
            if os.path.isfile(state_path):
                _, circuit = self.load_state_with_circuit(state_path)
                self._review_circuit = circuit
        except Exception as e:
            logger.debug("[Loop] resume circuit restore skipped: %s", str(e) or repr(e))

        callbacks = callbacks or LoopEngineCallbacks()
        self._run_state = EngineRunState.RUNNING
        self._project.status = LoopProjectStatus.RUNNING
        max_iterations = self.settings.loop_max_iterations
        start_iteration = len(self._project.iterations) + 1
        requirement = self._project.requirement
        project_name = self._project.name

        try:
            # Close old session before opening new one (prevent resource leak)
            self._close_session_safely()

            from ..utils.path import normalize_ttadk_cwd

            self._session = create_engine_session(
                agent_type=self._agent_type,
                cwd=normalize_ttadk_cwd(self.root_path) or self.root_path,
                on_rate_limit=getattr(self, "_on_rate_limit", None),
                model_name=self._model_name,
            )

            timeout = self.settings.loop_execution_timeout
            review_enabled = self.settings.loop_review_enabled
            review_extra_max = self.settings.loop_review_extra_iterations

            if not self._context_manager:
                self._context_manager = LoopContextManager(
                    max_context_tokens=self.settings.loop_max_context_tokens,
                )
                for prev_record in self._project.iterations:
                    self._context_manager.record_iteration(prev_record)

            for iteration in range(start_iteration, max_iterations + 1):
                if self._run_state != EngineRunState.RUNNING:
                    break

                iter_start = time.time()

                if callbacks.on_iteration_start:
                    callbacks.on_iteration_start(iteration, max_iterations)

                prompt = self._build_iteration_prompt(iteration, requirement)
                iter_tracker = IterationTracker()
                on_event = self._make_on_event(iter_tracker, iteration, callbacks)
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

                # Multi-perspective review phase
                with self._lock:
                    is_running = self._run_state == EngineRunState.RUNNING
                
                if review_enabled and is_running:
                    review_result, review_decision = self._conduct_review(iteration, callbacks)
                    record.review_result = review_result
                    record.review_decision = review_decision
                    self._last_review = review_result

                with self._lock:
                    self._project.iterations.append(record)

                if self._context_manager:
                    self._context_manager.record_iteration(record)

                if callbacks.on_iteration_done:
                    callbacks.on_iteration_done(iteration, record)

                # Save state precisely after each iteration finishes to support fine-grained recovery
                try:
                    self.save_state()
                except Exception as e:
                    logger.warning("[Loop:%s] 细粒度状态保存失败: %s", project_name, str(e) or repr(e))

                criteria_result = self._evaluate_criteria(requirement.acceptance_criteria, iteration)
                all_criteria_satisfied = criteria_result.get("all_satisfied", False)

                if all_criteria_satisfied:
                    if not review_enabled or (self._last_review and self._last_review.all_passed):
                        logger.info("[Loop:%s] 恢复后所有验收标准+审查通过, 迭代 %d 轮", project_name, iteration)
                        break
                    self._review_extra_used += 1
                    if self._review_extra_used > review_extra_max:
                        logger.info(
                            "[Loop:%s] 恢复后验收标准已满足，审查额外迭代超限(%d), 迭代 %d 轮",
                            project_name,
                            review_extra_max,
                            iteration,
                        )
                        break

                if self._detect_convergence():
                    logger.info("[Loop:%s] 恢复后收敛检测触发, 迭代 %d 轮", project_name, iteration)
                    break

            with self._lock:
                if self._run_state == EngineRunState.STOPPING:
                    self._project.status = LoopProjectStatus.PAUSED
                else:
                    self._project.status = LoopProjectStatus.COMPLETED
                    self._project.completed_at = time.time()

            if callbacks.on_project_done:
                callbacks.on_project_done(self._project)

        except TimeoutError as e:
            from ..utils.errors import get_error_detail

            detail = get_error_detail(e)

            error_msg = f"Loop恢复超时: {detail}"
            logger.warning("[Loop:%s] %s", project_name, error_msg)
            self._project.status = LoopProjectStatus.ABORTED
            self._project.completed_at = time.time()
            if callbacks.on_error:
                callbacks.on_error(error_msg)

        except Exception as e:
            from ..utils.errors import get_error_detail

            detail = get_error_detail(e)

            error_msg = f"Loop恢复异常: {detail}"
            logger.error("[Loop:%s] %s", project_name, error_msg)
            self._project.status = LoopProjectStatus.ABORTED
            self._project.completed_at = time.time()
            if callbacks.on_error:
                callbacks.on_error(error_msg)

        finally:
            self._run_state = EngineRunState.IDLE
            get_gc_monitor(memory_threshold_percent=self._gc_threshold_default).check_and_collect(label=self._gc_label)

        return self._project


class LoopEngineManager(BaseEngineManager["LoopEngine"]):

    def _create_engine(
        self,
        chat_id: str,
        root_path: str,
        agent_type: str,
        engine_name: str,
        model_name: Optional[str],
    ) -> "LoopEngine":
        return LoopEngine(
            chat_id=chat_id,
            root_path=root_path,
            agent_type=agent_type,
            engine_name=engine_name,
            model_name=model_name,
        )
