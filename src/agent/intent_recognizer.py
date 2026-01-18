import json
import os
import re
from enum import Enum
from typing import Optional
from dataclasses import dataclass, field
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from ..config import get_settings


class IntentType(Enum):
    ENTER_COCO = "enter_coco"
    EXIT_COCO = "exit_coco"
    CHANGE_DIR = "change_dir"
    SHELL_COMMAND = "shell"
    COCO_MESSAGE = "coco_message"
    UNKNOWN = "unknown"


@dataclass
class TaskStep:
    intent: IntentType
    description: str
    data: dict = field(default_factory=dict)


@dataclass
class IntentResult:
    tasks: list[TaskStep] = field(default_factory=list)
    confidence: float = 0.0
    original_text: str = ""
    reasoning: str = ""

    @property
    def is_multi_task(self) -> bool:
        return len(self.tasks) > 1

    @property
    def primary_intent(self) -> IntentType:
        return self.tasks[0].intent if self.tasks else IntentType.UNKNOWN

    @property
    def primary_data(self) -> dict:
        return self.tasks[0].data if self.tasks else {}

    @classmethod
    def single(cls, intent: IntentType, confidence: float = 0.0,
               data: dict = None, original_text: str = "", reasoning: str = "",
               description: str = "") -> "IntentResult":
        return cls(
            tasks=[TaskStep(intent=intent, description=description, data=data or {})],
            confidence=confidence,
            original_text=original_text,
            reasoning=reasoning
        )


class IntentRecognizer:
    REACT_SYSTEM_PROMPT = """你是一个智能意图识别助手，使用 ReAct 模式分析用户输入。

## 可识别的意图类型

1. **enter_coco** - 用户想要进入编程/开发/AI对话模式
   - 关键词：写代码、编程、开发、帮我实现、coco、AI助手、帮我改、帮我优化代码
   - 示例："帮我写一个函数"、"进入开发模式"、"帮我改下这个bug"
   - data: {}

2. **exit_coco** - 用户想要退出编程模式
   - 关键词：退出、结束、不用了、算了、停止、exit、quit
   - 示例："退出"、"不用了谢谢"、"结束对话"
   - data: {}

3. **change_dir** - 用户想要切换或查询工作目录
   - 关键词：切换目录、去...目录、进入...文件夹、cd、当前目录、上级目录
   - 示例："切换到workspace目录"、"当前在什么目录"、"去上级目录"
   - data: {"path": "目标路径"}
   - 路径规则：
     - 上级目录: ".."
     - 用户目录: "~" 或 "~/子目录"
     - 桌面: "~/Desktop"
     - 下载: "~/Downloads"
     - 文档: "~/Documents"
     - 当前目录查询: path 为空字符串 ""

4. **shell** - 用户想执行shell命令或用自然语言描述操作
   - 特征：命令格式（ls、git、npm等）或描述想要执行的操作
   - 示例："ls -la"、"git status"、"帮我看下有什么文件"
   - data: {"command": "实际要执行的shell命令"}
   - 重要：必须将用户意图转换为实际的shell命令
   - 示例："帮我看下上级目录有什么文件" → data: {"command": "ls .."}

5. **unknown** - 无法确定意图，data: {}

## 任务拆解规则

如果用户请求包含多个步骤，拆解为多个任务：
- "切换到项目目录然后帮我写代码" → change_dir + enter_coco
- "去workspace目录看看有什么文件" → change_dir + shell(ls)

## 输出格式

请按以下格式输出：

### Thought
分析用户输入的语义特征和可能的意图。

### Action
判断意图类型，决定是否需要拆解为多个任务。

### Result
```json
{
  "tasks": [
    {"intent": "意图类型", "description": "任务描述", "data": {"相关数据"}}
  ],
  "confidence": 0.0-1.0
}
```

## 重要规则

1. shell命令格式（如 `ls -la`、`git status`）直接判断为 shell
2. 编程/开发相关需求判断为 enter_coco
3. 目录相关问题判断为 change_dir
4. 大多数情况是单任务，只有明确的复合请求才拆解"""

    INTENT_MAP = {
        "enter_coco": IntentType.ENTER_COCO,
        "exit_coco": IntentType.EXIT_COCO,
        "change_dir": IntentType.CHANGE_DIR,
        "shell": IntentType.SHELL_COMMAND,
        "unknown": IntentType.UNKNOWN,
    }

    EXACT_COMMANDS = {
        "/coco": (IntentType.ENTER_COCO, "进入 Coco 编程模式"),
        "/enter_coco": (IntentType.ENTER_COCO, "进入 Coco 编程模式"),
        "/end_coco": (IntentType.EXIT_COCO, "退出 Coco 模式"),
        "/exit_coco": (IntentType.EXIT_COCO, "退出 Coco 模式"),
        "/exit": (IntentType.EXIT_COCO, "退出 Coco 模式"),
        "/quit": (IntentType.EXIT_COCO, "退出 Coco 模式"),
    }

    def __init__(self):
        self.settings = get_settings()
        self._llm: Optional[ChatOpenAI] = None

    def _get_llm(self) -> ChatOpenAI:
        if self._llm is None:
            self._llm = ChatOpenAI(
                base_url=self.settings.ark_base_url,
                api_key=self.settings.ark_api_key,
                model=self.settings.ark_model,
                temperature=0.1,
            )
        return self._llm

    def _quick_match(self, text: str) -> Optional[IntentResult]:
        text_lower = text.lower().strip()

        if text_lower in self.EXACT_COMMANDS:
            intent, desc = self.EXACT_COMMANDS[text_lower]
            return IntentResult.single(
                intent=intent,
                confidence=1.0,
                original_text=text,
                reasoning=f"精确匹配命令: {text_lower}",
                description=desc
            )

        if text_lower == "/coco_info":
            return IntentResult.single(
                intent=IntentType.COCO_MESSAGE,
                confidence=1.0,
                data={"command": "info"},
                original_text=text,
                reasoning="精确匹配: /coco_info",
                description="查看 Coco 会话信息"
            )

        if re.match(r'^[a-z][a-z0-9_.-]*(\s|$)', text_lower):
            first_word = text_lower.split()[0]
            if not any(kw in first_word for kw in ['帮', '请', '能', '可以', '想']):
                return IntentResult.single(
                    intent=IntentType.SHELL_COMMAND,
                    confidence=0.9,
                    original_text=text,
                    reasoning=f"命令格式匹配: {first_word}",
                    description=f"执行命令: {text}"
                )

        return None

    def _parse_response(self, content: str) -> tuple[dict, str]:
        thought_match = re.search(r'###?\s*Thought[^#]*?(?=###|\Z)', content, re.DOTALL | re.IGNORECASE)
        reasoning = thought_match.group().strip() if thought_match else ""

        json_match = re.search(r'```json\s*(.*?)\s*```', content, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group(1)), reasoning
            except json.JSONDecodeError:
                pass

        json_match = re.search(r'\{[^{}]*"tasks"[^{}]*\[.*?\][^{}]*\}', content, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group()), reasoning
            except json.JSONDecodeError:
                pass

        json_match = re.search(r'\{[^{}]*"intent"[^{}]*\}', content, re.DOTALL)
        if json_match:
            try:
                old_format = json.loads(json_match.group())
                return {
                    "tasks": [{
                        "intent": old_format.get("intent", "unknown"),
                        "description": "",
                        "data": old_format.get("data", {})
                    }],
                    "confidence": old_format.get("confidence", 0.5)
                }, reasoning
            except json.JSONDecodeError:
                pass

        return {}, reasoning

    def _normalize_path(self, path: str) -> str:
        if not path:
            return ""
        path = path.strip()
        if path.startswith("~"):
            path = os.path.expanduser(path)
        return path

    def recognize(self, text: str, is_in_coco_mode: bool = False) -> IntentResult:
        quick_result = self._quick_match(text)
        if quick_result:
            return quick_result

        if is_in_coco_mode:
            text_lower = text.lower().strip()
            exit_keywords = ["退出", "结束", "exit", "quit", "不用了", "算了"]
            if any(kw in text_lower for kw in exit_keywords) and len(text) < 15:
                return IntentResult.single(
                    intent=IntentType.EXIT_COCO,
                    confidence=0.85,
                    original_text=text,
                    reasoning="Coco模式下检测到退出关键词",
                    description="退出 Coco 模式"
                )
            return IntentResult.single(
                intent=IntentType.COCO_MESSAGE,
                confidence=0.95,
                original_text=text,
                reasoning="当前处于Coco模式，消息转发给Coco",
                description="发送消息给 Coco"
            )

        try:
            llm = self._get_llm()
            messages = [
                SystemMessage(content=self.REACT_SYSTEM_PROMPT),
                HumanMessage(content=f"请分析以下用户输入的意图：\n\n\"{text}\""),
            ]

            response = llm.invoke(messages)
            content = response.content.strip()
            print(f"🧠 ReAct:\n{content[:300]}...")

            result, reasoning = self._parse_response(content)

            if not result or "tasks" not in result:
                return IntentResult.single(
                    intent=IntentType.SHELL_COMMAND,
                    confidence=0.5,
                    original_text=text,
                    reasoning=f"LLM解析失败，默认shell: {reasoning}",
                    description=f"执行: {text}"
                )

            tasks = []
            for task_data in result.get("tasks", []):
                intent_str = task_data.get("intent", "unknown")
                intent = self.INTENT_MAP.get(intent_str, IntentType.UNKNOWN)

                data = task_data.get("data", {})
                if intent == IntentType.CHANGE_DIR and "path" in data:
                    data["path"] = self._normalize_path(data["path"])

                tasks.append(TaskStep(
                    intent=intent,
                    description=task_data.get("description", ""),
                    data=data
                ))

            if not tasks:
                return IntentResult.single(
                    intent=IntentType.SHELL_COMMAND,
                    confidence=0.5,
                    original_text=text,
                    reasoning="无任务，默认shell",
                    description=f"执行: {text}"
                )

            return IntentResult(
                tasks=tasks,
                confidence=result.get("confidence", 0.5),
                original_text=text,
                reasoning=reasoning
            )

        except Exception as e:
            print(f"意图识别异常: {e}")
            return IntentResult.single(
                intent=IntentType.SHELL_COMMAND,
                confidence=0.3,
                original_text=text,
                reasoning=f"异常回退: {e}",
                description=f"执行: {text}"
            )
