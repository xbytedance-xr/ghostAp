"""ACP-driven Loop Engine — iterative closed-loop development.

Uses ACP session's multi-turn prompt capability to iterate until
acceptance criteria are satisfied. Each iteration sends a prompt,
tracks tool calls/plan progress, then evaluates criteria.
"""

import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional, Callable

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage

from ..acp import ACPEvent, ACPEventType, ACPEventRenderer
from ..agent_session import SyncSession, close_session_safely, create_engine_session
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
    ReviewPerspective,
    PerspectiveReview,
    ReviewResult,
)
from .tracker import IterationTracker

logger = logging.getLogger(__name__)

# 下沉到 utils：review/criteria 解析能力层（避免引擎间私有符号依赖）
from ..utils.spec_utils import (
    CRITERIA_PATTERNS as _CRITERIA_PATTERNS,
    REVIEW_SECTION_PATTERN as _REVIEW_SECTION_PATTERN,
    PERSPECTIVE_TAG_MAP as _PERSPECTIVE_TAG_MAP,
    normalize_review_verdict as _normalize_review_verdict,
    extract_suggestions_from_body as _extract_suggestions_from_body,
    split_review_sections as _split_review_sections,
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


class LoopEngine:
    """ACP-driven iterative closed-loop engine."""

    def __init__(self, chat_id: str, root_path: str,
                 agent_type: str = "coco", engine_name: str = "Coco"):
        self.chat_id = chat_id
        self.root_path = os.path.expanduser(root_path)
        self.settings = get_settings()
        self.engine_name = engine_name
        self._agent_type = agent_type

        self._session: Optional[SyncSession] = None
        self._project: Optional[LoopProject] = None
        self._renderer = ACPEventRenderer()
        self._run_state = EngineRunState.IDLE
        self._user_guidance: list[str] = []
        self._last_review: Optional[ReviewResult] = None
        self._review_extra_used: int = 0

    @property
    def project(self) -> Optional[LoopProject]:
        return self._project

    @property
    def run_state(self) -> EngineRunState:
        return self._run_state

    @property
    def is_running(self) -> bool:
        return self._run_state != EngineRunState.IDLE

    def _close_session_safely(self) -> None:
        """Close existing ACP session, ignoring errors."""
        close_session_safely(self._session)
        self._session = None

    def execute(
        self,
        requirement_text: str,
        callbacks: Optional[LoopEngineCallbacks] = None,
        task_id: Optional[str] = None,
        on_rate_limit: Optional[Callable[[int], None]] = None,
    ) -> LoopProject:
        """Iterate until acceptance criteria are satisfied."""
        callbacks = callbacks or LoopEngineCallbacks()
        self._run_state = EngineRunState.RUNNING
        self._on_rate_limit = on_rate_limit
        max_iterations = self.settings.loop_max_iterations

        # Create project
        project_name = os.path.basename(self.root_path) or "loop_project"
        self._project = LoopProject.create(
            name=project_name,
            root_path=self.root_path,
        )
        self._project.task_id = task_id
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

            # Create session
            self._session = create_engine_session(
                agent_type=self._agent_type, cwd=self.root_path,
                on_rate_limit=on_rate_limit,
            )

            # Build initial prompt
            initial_prompt = self._build_initial_prompt(requirement)
            timeout = self.settings.loop_execution_timeout

            review_enabled = self.settings.loop_review_enabled
            review_extra_max = self.settings.loop_review_extra_iterations
            self._review_extra_used = 0
            self._last_review = None

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
                on_event = self._make_on_event(iter_tracker, iteration, callbacks)
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

                logger.info("[Loop:%s] 迭代 %d/%d 完成, 工具=%d, 文件=%d",
                             project_name, iteration, max_iterations,
                             len(iter_tracker.tool_calls),
                             len(iter_tracker.modified_files))

                # Multi-perspective review phase
                if review_enabled and self._run_state == EngineRunState.RUNNING:
                    review_result = self._conduct_review(iteration, callbacks)
                    record.review_result = review_result
                    self._last_review = review_result

                self._project.iterations.append(record)

                if callbacks.on_iteration_done:
                    callbacks.on_iteration_done(iteration, record)

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
                        logger.info("[Loop:%s] 验收标准已满足，审查额外迭代超限(%d), 迭代 %d 轮",
                                     project_name, review_extra_max, iteration)
                        break
                    logger.info("[Loop:%s] 验收标准已满足但审查有建议, 额外迭代 %d/%d",
                                 project_name, self._review_extra_used, review_extra_max)

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

    def _make_on_event(
        self,
        iter_tracker: IterationTracker,
        iteration: int,
        callbacks: LoopEngineCallbacks,
    ) -> Callable[[ACPEvent], None]:
        """Create the on_event callback shared by execute and resume loops."""
        def on_event(event: ACPEvent, _it=iteration):
            iter_tracker.process(event)
            self._renderer.process_event(event)
            if callbacks.on_iteration_event:
                callbacks.on_iteration_event(_it, event)
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
            logger.warning("[Loop] LLM 需求拆解失败: %s, 将使用原始文本", e)
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
            combined = "\n\n".join(self._user_guidance)
            guidance_section = f"\n## 用户引导\n{combined}\n"
            self._user_guidance.clear()  # consume after use

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

## 验收标准进度
{criteria_list}
{guidance_section}{review_section}
请聚焦未满足的标准（未打勾的）和审查反馈，继续实现。
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
                pat = _CRITERIA_PATTERNS[i] if i < len(_CRITERIA_PATTERNS) else re.compile(rf"CRITERIA_{i+1}\s*:\s*(PASS|FAIL)")
                match = pat.search(full_text)
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

    def _parse_review_output(self, text: str, iteration: int) -> ReviewResult:
        """Parse structured review output into ReviewResult."""
        raw = (text or "").replace("\r\n", "\n")

        # 1) strict/tolerant parsing (shared utils)
        from ..utils.spec_utils import parse_review_output_strict_tolerant
        reviews = parse_review_output_strict_tolerant(raw, iteration)

        # 3) LLM fallback: regex failed, use AI to extract structured data
        if not reviews:
            preview = raw[:500] if raw else "(empty)"
            logger.warning("[Loop] 正则解析全部失败, 尝试LLM兜底解析. 原文预览: %s", preview)
            reviews = self._parse_review_with_llm(raw)

        # 4) Final fallback: nothing parsed at all
        if not reviews:
            logger.warning("[Loop] 审查输出解析失败(含LLM兜底), 将视为有改进建议继续迭代")
            for p in ReviewPerspective:
                reviews.append(PerspectiveReview(
                    perspective=p, passed=False,
                    suggestions=["审查输出解析失败，请检查实现质量"],
                    summary="解析失败",
                ))

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
            logger.warning("[Loop] LLM 兜底审查解析失败: %s", e)
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
                perspective=perspective,
                passed=passed,
                suggestions=suggestions,
                summary=f"{'通过' if passed else f'{len(suggestions)}条建议'}",
            ))

        return reviews

    def _conduct_review(self, iteration: int, callbacks: LoopEngineCallbacks) -> ReviewResult:
        """Conduct multi-perspective review in the same ACP session."""
        if not self._session:
            return ReviewResult(iteration=iteration)

        review_prompt = self._build_review_prompt()
        review_text: list[str] = []

        def on_review_event(event: ACPEvent):
            if event.event_type == ACPEventType.TEXT_CHUNK and event.text:
                review_text.append(event.text)

        try:
            self._session.send_prompt(review_prompt, on_event=on_review_event, timeout=120)
            full_text = "".join(review_text)
            review_result = self._parse_review_output(full_text, iteration)
        except Exception as e:
            logger.warning("[Loop] 多视角审查异常: %s, 将继续迭代", e)
            review_result = ReviewResult(reviews=[
                PerspectiveReview(
                    perspective=p, passed=False,
                    suggestions=[f"审查执行异常: {e}"],
                    summary="异常",
                ) for p in ReviewPerspective
            ], iteration=iteration)

        if callbacks.on_review_done:
            callbacks.on_review_done(iteration, review_result)

        return review_result

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
        """Inject user guidance — will be included in the next iteration prompt.

        Multiple calls accumulate; all pending guidance is consumed together.
        """
        self._user_guidance.append(message)
        logger.info("[Loop] 用户引导已注入(队列=%d): %s...", len(self._user_guidance), message[:100])

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
            # Close old session before opening new one (prevent resource leak)
            self._close_session_safely()
            self._session = create_engine_session(
                agent_type=self._agent_type, cwd=self.root_path,
                on_rate_limit=getattr(self, "_on_rate_limit", None),
            )

            timeout = self.settings.loop_execution_timeout
            review_enabled = self.settings.loop_review_enabled
            review_extra_max = self.settings.loop_review_extra_iterations

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
                if review_enabled and self._run_state == EngineRunState.RUNNING:
                    review_result = self._conduct_review(iteration, callbacks)
                    record.review_result = review_result
                    self._last_review = review_result

                self._project.iterations.append(record)

                if callbacks.on_iteration_done:
                    callbacks.on_iteration_done(iteration, record)

                criteria_result = self._evaluate_criteria(requirement.acceptance_criteria, iteration)
                all_criteria_satisfied = criteria_result.get("all_satisfied", False)

                if all_criteria_satisfied:
                    if not review_enabled or (self._last_review and self._last_review.all_passed):
                        logger.info("[Loop:%s] 恢复后所有验收标准+审查通过, 迭代 %d 轮", project_name, iteration)
                        break
                    self._review_extra_used += 1
                    if self._review_extra_used > review_extra_max:
                        logger.info("[Loop:%s] 恢复后验收标准已满足，审查额外迭代超限(%d), 迭代 %d 轮",
                                     project_name, review_extra_max, iteration)
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
    """Manages LoopEngine instances per chat.

    Uses a secondary index (_chat_keys) to avoid O(n) full-table scans.
    """

    def __init__(self):
        self._engines: dict[str, LoopEngine] = {}
        self._chat_keys: dict[str, set[str]] = {}  # chat_id → set of keys
        self._lock = threading.Lock()

    def _add_index(self, chat_id: str, key: str) -> None:
        self._chat_keys.setdefault(chat_id, set()).add(key)

    def _iter_chat_engines(self, chat_id: str):
        """Yield engines belonging to a chat (O(k) where k = engines per chat)."""
        for key in self._chat_keys.get(chat_id, ()):
            engine = self._engines.get(key)
            if engine:
                yield engine

    def get_or_create(self, chat_id: str, root_path: str, engine_name: str = "Coco") -> LoopEngine:
        key = f"{chat_id}:{root_path}"
        agent_type = "claude" if engine_name.lower().startswith("claude") else "coco"

        with self._lock:
            if key not in self._engines:
                self._engines[key] = LoopEngine(
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
        for engine in self._iter_chat_engines(chat_id):
            if engine.is_running:
                return engine
        return None

    def get_active_engines(self, chat_id: str) -> list[LoopEngine]:
        return [e for e in self._iter_chat_engines(chat_id) if e.is_running]

    def list_engines(self, chat_id: Optional[str] = None) -> list[LoopEngine]:
        if chat_id is None:
            return list(self._engines.values())
        return list(self._iter_chat_engines(chat_id))

    def cleanup_all(self):
        with self._lock:
            for engine in self._engines.values():
                engine.cleanup()
            self._engines.clear()
            self._chat_keys.clear()
