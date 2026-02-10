"""RequirementAnalyzer — LLM 驱动的需求解析器。

将模糊的用户需求转化为结构化的 LoopRequirement，
包含可验证的验收标准、约束条件和预估迭代次数。
LLM 解析失败时降级为简单文本解析。
"""

import json
import logging
import re
from typing import Optional

from ..session.base import BaseSession
from .models import LoopRequirement

logger = logging.getLogger(__name__)

ANALYZE_PROMPT = """请分析以下产品需求，提取结构化信息。

## 用户需求
{text}

## 工作目录
{cwd}

请以 JSON 格式输出（不要输出其他内容）：
```json
{{
  "goal": "一句话核心目标",
  "acceptance_criteria": ["可验证的标准1", "可验证的标准2", ...],
  "constraints": ["约束条件1", ...],
  "estimated_iterations": 6
}}
```

要求：
- goal: 用一句话概括核心目标
- acceptance_criteria: 5-8 条可验证的验收标准，覆盖功能、质量、安全维度
- 每条标准必须可通过代码或测试判断是否满足
- constraints: 技术或业务约束（如有）
- estimated_iterations: 预估需要的迭代轮数（3-10）
"""


class RequirementAnalyzer:
    """LLM 驱动的需求解析，降级为文本解析。"""

    def __init__(self, session: Optional[BaseSession] = None, cwd: str = ""):
        self._session = session
        self._cwd = cwd

    def analyze(
        self,
        text: str,
        session: Optional[BaseSession] = None,
        cwd: Optional[str] = None,
    ) -> LoopRequirement:
        """解析用户需求为结构化 LoopRequirement。

        优先使用 LLM 解析，失败则降级为简单文本解析。
        """
        active_session = session or self._session
        active_cwd = cwd or self._cwd

        if active_session:
            try:
                return self._llm_analyze(text, active_session, active_cwd)
            except Exception as e:
                logger.warning(
                    "[RequirementAnalyzer] LLM 解析失败，降级为文本解析: %s", e
                )

        return self._fallback_parse(text)

    def _llm_analyze(
        self, text: str, session: BaseSession, cwd: str
    ) -> LoopRequirement:
        """通过 LLM 解析需求。"""
        prompt = ANALYZE_PROMPT.format(text=text, cwd=cwd)
        output = session.send_prompt(prompt=prompt, timeout=60, cwd=cwd)
        data = self._extract_json(output)

        goal = data.get("goal", "")
        criteria = data.get("acceptance_criteria", [])

        if not goal or not criteria:
            raise ValueError("LLM 输出缺少 goal 或 acceptance_criteria")

        return LoopRequirement(
            goal=goal,
            acceptance_criteria=criteria,
            constraints=data.get("constraints", []),
            estimated_iterations=data.get("estimated_iterations", 6),
            raw_text=text,
        )

    @staticmethod
    def _extract_json(text: str) -> dict:
        """从 LLM 输出中提取 JSON 块。"""
        # 优先匹配 ```json ... ``` 块
        match = re.search(r"```json\s*\n(.*?)\n\s*```", text, re.DOTALL)
        if match:
            return json.loads(match.group(1))
        # 尝试直接解析整段
        return json.loads(text)

    @staticmethod
    def _fallback_parse(text: str) -> LoopRequirement:
        """降级的简单文本解析。"""
        lines = text.strip().split("\n")
        criteria = []

        for line in lines:
            stripped = line.strip()
            if stripped.startswith(("- ", "* ")):
                criteria.append(stripped[2:])
            elif stripped.startswith(("[ ] ", "[x] ")):
                criteria.append(stripped[4:])

        if not criteria:
            criteria = [f"完成需求: {text[:100]}"]

        return LoopRequirement(
            goal=text,
            acceptance_criteria=criteria,
            raw_text=text,
        )
