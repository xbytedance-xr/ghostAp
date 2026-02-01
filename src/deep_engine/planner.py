import json
import logging
import re
from typing import Optional
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from ..config import get_settings
from .models import ParsedRequirement, DeepTask

logger = logging.getLogger(__name__)


class TaskPlanner:
    SYSTEM_PROMPT = """你是一个专业的任务规划师，负责将需求拆解为可执行的编程任务。

## 你的任务
根据需求分析结果，生成一系列可以由 AI 编程助手执行的具体任务。

## 输入信息
- summary: 需求概述
- goals: 需要实现的目标列表
- constraints: 约束条件
- tech_stack: 技术栈

## 输出格式
请严格按照以下 JSON 格式输出：

```json
{
  "tasks": [
    {
      "title": "任务标题（简短）",
      "description": "任务详细描述",
      "prompt": "发送给 AI 编程助手的具体指令",
      "dependencies": []
    }
  ]
}
```

## 字段说明
- **title**: 简短的任务标题（10字以内）
- **description**: 任务的详细描述，说明要做什么
- **prompt**: 发送给 AI 编程助手（Coco）的具体指令，要求清晰、可执行
- **dependencies**: 依赖的任务索引数组（从0开始），如果没有依赖则为空数组

## Prompt 编写规则
1. 每个 prompt 应该是独立的、可执行的指令
2. prompt 应该包含足够的上下文信息
3. prompt 应该明确指出要创建/修改的文件
4. prompt 应该说明预期的输出或效果
5. 避免使用"首先"、"然后"等连接词，每个任务应该独立

## 示例

输入：
```
summary: 开发一个 Python 爬虫程序，爬取豆瓣电影 Top250 并保存为 CSV
goals: ["创建项目结构", "实现数据抓取", "解析数据", "保存CSV", "添加异常处理"]
```

输出：
```json
{
  "tasks": [
    {
      "title": "创建项目结构",
      "description": "创建 Python 项目的基本目录结构和配置文件",
      "prompt": "创建一个 Python 爬虫项目，包含以下文件：\\n- main.py: 主程序入口\\n- scraper.py: 爬虫核心逻辑\\n- requirements.txt: 依赖列表（requests, beautifulsoup4, lxml）\\n\\n请先创建这些空文件的基本结构。",
      "dependencies": []
    },
    {
      "title": "实现数据抓取",
      "description": "实现豆瓣电影 Top250 页面的 HTTP 请求和数据抓取",
      "prompt": "在 scraper.py 中实现一个 DoubanScraper 类，包含：\\n1. fetch_page(url) 方法：发送 HTTP 请求获取页面内容\\n2. 添加请求头模拟浏览器\\n3. 添加请求间隔避免被封\\n4. 处理分页（每页25部电影，共10页）",
      "dependencies": [0]
    },
    {
      "title": "解析电影数据",
      "description": "使用 BeautifulSoup 解析页面，提取电影信息",
      "prompt": "在 scraper.py 中添加 parse_movie(html) 方法：\\n1. 使用 BeautifulSoup 解析 HTML\\n2. 提取电影名称、评分、导演、年份、简介\\n3. 返回结构化的电影数据字典",
      "dependencies": [1]
    },
    {
      "title": "保存CSV文件",
      "description": "将爬取的数据保存到 CSV 文件",
      "prompt": "在 scraper.py 中添加 save_to_csv(movies, filename) 方法：\\n1. 使用 csv 模块写入数据\\n2. 包含表头：名称、评分、导演、年份、简介\\n3. 处理中文编码（utf-8-sig）",
      "dependencies": [2]
    },
    {
      "title": "完善主程序",
      "description": "在 main.py 中整合所有功能，添加异常处理",
      "prompt": "完善 main.py：\\n1. 导入 DoubanScraper 类\\n2. 实现主函数，依次爬取所有页面\\n3. 添加 try-except 异常处理\\n4. 添加进度打印\\n5. 最后调用 save_to_csv 保存数据",
      "dependencies": [3]
    }
  ]
}
```

## 注意事项
1. 任务数量应该与需求复杂度匹配（3-10个任务）
2. 每个任务应该是原子性的，可以独立验证
3. 任务之间的依赖关系要合理
4. prompt 要足够详细，让 AI 能够准确执行
5. 只输出 JSON，不要有其他内容"""

    def __init__(self):
        self.settings = get_settings()
        self._llm: Optional[ChatOpenAI] = None

    def _get_llm(self) -> ChatOpenAI:
        if self._llm is None:
            self._llm = ChatOpenAI(
                base_url=self.settings.ark_base_url,
                api_key=self.settings.ark_api_key,
                model=self.settings.ark_model,
                temperature=0.3,
            )
        return self._llm

    def _parse_json_response(self, content: str) -> Optional[dict]:
        json_match = re.search(r'```json\s*(.*?)\s*```', content, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group(1))
            except json.JSONDecodeError:
                pass

        try:
            start = content.find('{')
            end = content.rfind('}') + 1
            if start >= 0 and end > start:
                return json.loads(content[start:end])
        except json.JSONDecodeError:
            pass

        return None

    def _build_task_ids(self, tasks_data: list[dict]) -> list[DeepTask]:
        tasks = []
        task_id_map = {}

        for i, task_data in enumerate(tasks_data):
            task = DeepTask.create(
                title=task_data.get("title", f"任务 {i+1}"),
                description=task_data.get("description", ""),
                prompt=task_data.get("prompt", ""),
                order=i,
            )
            tasks.append(task)
            task_id_map[i] = task.task_id

        for i, task_data in enumerate(tasks_data):
            dep_indices = task_data.get("dependencies", [])
            tasks[i].dependencies = [
                task_id_map[idx] for idx in dep_indices
                if idx in task_id_map and idx < i
            ]

        return tasks

    def plan(self, requirement: ParsedRequirement) -> list[DeepTask]:
        try:
            llm = self._get_llm()

            input_text = f"""需求信息：
- summary: {requirement.summary}
- goals: {json.dumps(requirement.goals, ensure_ascii=False)}
- constraints: {json.dumps(requirement.constraints, ensure_ascii=False)}
- tech_stack: {json.dumps(requirement.tech_stack, ensure_ascii=False)}
- complexity: {requirement.estimated_complexity}

请根据以上信息生成任务列表。"""

            messages = [
                SystemMessage(content=self.SYSTEM_PROMPT),
                HumanMessage(content=input_text),
            ]

            response = llm.invoke(messages)
            content = response.content.strip()
            logger.debug("任务规划结果:\n%s...", content[:800])

            result = self._parse_json_response(content)

            if not result or "tasks" not in result:
                return self._create_fallback_tasks(requirement)

            tasks_data = result["tasks"]
            if not tasks_data:
                return self._create_fallback_tasks(requirement)

            return self._build_task_ids(tasks_data)

        except Exception as e:
            logger.error("任务规划异常: %s", e)
            return self._create_fallback_tasks(requirement)

    def _create_fallback_tasks(self, requirement: ParsedRequirement) -> list[DeepTask]:
        tasks = []
        for i, goal in enumerate(requirement.goals):
            task = DeepTask.create(
                title=goal[:20] if len(goal) > 20 else goal,
                description=goal,
                prompt=f"请完成以下任务：{goal}\n\n背景信息：{requirement.summary}",
                order=i,
                dependencies=[tasks[i-1].task_id] if i > 0 else [],
            )
            tasks.append(task)
        return tasks

    ADAPT_SYSTEM_PROMPT = """你是一个任务指令调整专家。根据执行上下文判断是否需要调整即将执行的任务指令。

## 原则
1. **保守调整**: 只在上下文明确要求变更时才调整，不要过度解读
2. **保留原意**: 调整后的 prompt 必须保留原始任务的核心目标
3. **增量修改**: 优先在原始 prompt 基础上补充/修正，而非重写
4. **用户优先**: 用户注入的上下文具有最高优先级

## 输出格式
请严格按照以下 JSON 格式输出：

```json
{
  "should_adapt": true/false,
  "reason": "调整原因（简短）",
  "adapted_prompt": "调整后的完整 prompt（仅当 should_adapt=true 时需要）"
}
```

只输出 JSON，不要有其他内容。"""

    def adapt_task_prompt(self, task: DeepTask, context_prompt: str) -> tuple[bool, str, str]:
        """根据上下文评估是否需要调整 task prompt。

        Returns:
            (was_adapted, final_prompt, reason)
        """
        try:
            llm = self._get_llm()

            input_text = f"""当前要执行的任务：
- 标题: {task.title}
- 描述: {task.description}
- 原始指令: {task.prompt}

{context_prompt}

请判断是否需要根据上下文调整任务指令。"""

            messages = [
                SystemMessage(content=self.ADAPT_SYSTEM_PROMPT),
                HumanMessage(content=input_text),
            ]

            response = llm.invoke(messages)
            content = response.content.strip()
            logger.debug("任务适配结果:\n%s...", content[:500])

            result = self._parse_json_response(content)
            if not result:
                return False, task.prompt, "LLM 响应解析失败"

            should_adapt = result.get("should_adapt", False)
            reason = result.get("reason", "")
            adapted_prompt = result.get("adapted_prompt", task.prompt)

            if should_adapt and adapted_prompt:
                return True, adapted_prompt, reason
            return False, task.prompt, reason

        except Exception as e:
            logger.error("任务适配异常: %s", e)
            return False, task.prompt, f"适配异常: {e}"

    def replan_task(self, failed_task: DeepTask, error: str, context: str = "") -> DeepTask:
        try:
            llm = self._get_llm()

            prompt = f"""之前的任务执行失败，请重新规划这个任务。

失败的任务：
- 标题: {failed_task.title}
- 描述: {failed_task.description}
- 原始指令: {failed_task.prompt}

失败原因：
{error}

{f"上下文信息：{context}" if context else ""}

请生成一个改进的任务指令，避免之前的问题。只输出新的 prompt 内容，不要其他格式。"""

            messages = [
                SystemMessage(content="你是一个任务优化专家，负责改进失败的任务指令。"),
                HumanMessage(content=prompt),
            ]

            response = llm.invoke(messages)
            new_prompt = response.content.strip()

            return DeepTask.create(
                title=failed_task.title,
                description=f"[重试] {failed_task.description}",
                prompt=new_prompt,
                order=failed_task.order,
                dependencies=failed_task.dependencies,
            )

        except Exception as e:
            logger.error("任务重规划异常: %s", e)
            return failed_task
