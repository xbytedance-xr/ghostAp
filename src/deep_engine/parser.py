import json
import re
from typing import Optional
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from ..config import get_settings
from .models import ParsedRequirement


class RequirementParser:
    SYSTEM_PROMPT = """你是一个专业的需求分析师，负责解析用户的软件开发需求。

## 你的任务
分析用户输入的需求描述，提取关键信息并结构化输出。

## 输出格式
请严格按照以下 JSON 格式输出：

```json
{
  "summary": "需求的一句话概述",
  "goals": ["目标1", "目标2", "目标3"],
  "constraints": ["约束条件1", "约束条件2"],
  "tech_stack": ["技术栈1", "技术栈2"],
  "estimated_complexity": "low|medium|high|very_high"
}
```

## 字段说明
- **summary**: 用一句话概括用户的核心需求
- **goals**: 用户想要实现的具体目标列表（3-8个）
- **constraints**: 用户提到的约束条件（如时间、技术限制等），如果没有则为空数组
- **tech_stack**: 从需求中推断出的技术栈，如果没有明确则为空数组
- **estimated_complexity**: 预估复杂度
  - low: 简单任务，1-2个步骤
  - medium: 中等任务，3-5个步骤
  - high: 复杂任务，6-10个步骤
  - very_high: 非常复杂，超过10个步骤

## 示例

输入：帮我写一个 Python 爬虫，爬取豆瓣电影 Top250，保存到 CSV 文件

输出：
```json
{
  "summary": "开发一个 Python 爬虫程序，爬取豆瓣电影 Top250 并保存为 CSV",
  "goals": [
    "创建 Python 爬虫项目结构",
    "实现豆瓣电影 Top250 页面的数据抓取",
    "解析电影名称、评分、简介等信息",
    "将数据保存到 CSV 文件",
    "添加异常处理和重试机制"
  ],
  "constraints": [],
  "tech_stack": ["Python", "requests", "BeautifulSoup", "csv"],
  "estimated_complexity": "medium"
}
```

## 注意事项
1. goals 应该是可执行的具体任务，而不是抽象的描述
2. 每个 goal 应该是独立的、可验证的
3. 按照实现顺序排列 goals
4. 只输出 JSON，不要有其他内容"""

    def __init__(self):
        self.settings = get_settings()
        self._llm: Optional[ChatOpenAI] = None

    def _get_llm(self) -> ChatOpenAI:
        if self._llm is None:
            self._llm = ChatOpenAI(
                base_url=self.settings.ark_base_url,
                api_key=self.settings.ark_api_key,
                model=self.settings.ark_model,
                temperature=0.2,
            )
        return self._llm

    def _parse_json_response(self, content: str) -> Optional[dict]:
        json_match = re.search(r'```json\s*(.*?)\s*```', content, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group(1))
            except json.JSONDecodeError:
                pass

        json_match = re.search(r'\{[^{}]*"summary"[^{}]*\}', content, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group())
            except json.JSONDecodeError:
                pass

        try:
            return json.loads(content.strip())
        except json.JSONDecodeError:
            pass

        return None

    def parse(self, requirement_text: str) -> ParsedRequirement:
        try:
            llm = self._get_llm()
            messages = [
                SystemMessage(content=self.SYSTEM_PROMPT),
                HumanMessage(content=f"请分析以下需求：\n\n{requirement_text}"),
            ]

            response = llm.invoke(messages)
            content = response.content.strip()
            print(f"📋 需求解析结果:\n{content[:500]}...")

            result = self._parse_json_response(content)

            if not result:
                return ParsedRequirement(
                    original_text=requirement_text,
                    summary=requirement_text[:100],
                    goals=[requirement_text],
                    estimated_complexity="medium",
                )

            return ParsedRequirement(
                original_text=requirement_text,
                summary=result.get("summary", requirement_text[:100]),
                goals=result.get("goals", [requirement_text]),
                constraints=result.get("constraints", []),
                tech_stack=result.get("tech_stack", []),
                estimated_complexity=result.get("estimated_complexity", "medium"),
            )

        except Exception as e:
            print(f"需求解析异常: {e}")
            return ParsedRequirement(
                original_text=requirement_text,
                summary=requirement_text[:100],
                goals=[requirement_text],
                estimated_complexity="medium",
            )

    def is_complex_requirement(self, text: str) -> bool:
        complexity_indicators = [
            len(text) > 100,
            text.count("，") + text.count(",") > 3,
            text.count("。") + text.count(".") > 2,
            any(kw in text for kw in ["然后", "接着", "之后", "同时", "并且", "以及"]),
            any(kw in text for kw in ["第一", "第二", "首先", "其次", "最后"]),
            any(kw in text for kw in ["1.", "2.", "1、", "2、", "1)", "2)"]),
        ]
        return sum(complexity_indicators) >= 2
