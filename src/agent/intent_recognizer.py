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
    REACT_SYSTEM_PROMPT = """你是一个智能意图识别助手，使用 ReAct（Reasoning and Acting）模式分析用户输入。

## 可识别的意图类型

1. **enter_coco** - 用户想要进入编程/开发/AI对话模式
   - 关键词：写代码、编程、开发、帮我实现、coco、AI助手、帮我改、帮我优化代码
   - 示例："帮我写一个函数"、"进入开发模式"、"我想编程"、"帮我改下这个bug"
   - data: {}

2. **exit_coco** - 用户想要退出编程模式
   - 关键词：退出、结束、不用了、算了、停止、exit、quit
   - 示例："退出"、"不用了谢谢"、"结束对话"
   - data: {}

3. **change_dir** - 用户想要切换或查询工作目录
   - 关键词：切换目录、去...目录、进入...文件夹、cd、当前目录、在哪个目录、上级目录
   - 示例："切换到workspace目录"、"当前在什么目录"、"去项目文件夹"、"去上级目录"
   - data: {"path": "目标路径"}
   - 特殊路径：上级目录用 ".."，用户目录用 "~"，当前目录查询时 path 为空字符串 ""
   - 示例 data: {"path": ".."} 表示上级目录，{"path": "~/workspace"} 表示用户目录下的workspace

4. **shell** - 用户想执行shell命令，或者用自然语言描述想要执行的操作
   - 特征：以常见命令开头（ls、pwd、cat、git、npm、python等），或描述想要执行的操作
   - 示例："ls -la"、"git status"、"npm install"、"帮我看下上级目录有什么文件"
   - data: {"command": "实际要执行的shell命令"}
   - 重要：必须将用户意图转换为实际的shell命令填入 command 字段
   - 示例：用户说"帮我看下上级目录有什么文件" → data: {"command": "ls .."}

5. **unknown** - 无法确定意图
   - data: {}

## 任务拆解规则

如果用户的请求包含多个步骤或复合意图，请拆解为多个任务：
- 每个任务独立可执行
- 按逻辑顺序排列
- 每个任务包含 intent、description、data

常见的复合意图示例：
- "切换到项目目录然后帮我写代码" → 2个任务：change_dir + enter_coco
- "去workspace目录看看有什么文件" → 2个任务：change_dir + shell(ls)
- "帮我看看当前目录然后切换到src" → 2个任务：shell(ls) + change_dir

## ReAct 推理步骤

请严格按以下格式输出：

### Thought（思考）
分析用户输入的语义特征、关键词、句式结构，思考可能的意图。
判断是单一意图还是复合意图（需要多步骤完成）。

### Action（行动）
基于思考，判断意图类型，并决定是否需要拆解为多个任务。

### Observation（观察）
检验这个判断：
- 是否有其他可能的解释？
- 任务拆解是否合理？
- 置信度如何？

### Reflection（反思）
综合以上分析，确认或修正判断，给出最终结论。

### Result（结果）
```json
{
  "tasks": [
    {"intent": "意图类型", "description": "任务描述", "data": {"相关数据"}}
  ],
  "confidence": 0.0-1.0
}
```

## 重要规则

1. 如果输入明显是shell命令格式（如 `ls -la`、`git status`），直接判断为单个 shell 任务
2. 如果用户表达了编程/开发相关的需求，判断为 enter_coco
3. 如果用户在问目录相关问题或想切换目录，判断为 change_dir
4. 对于复合意图，拆解为多个任务，按执行顺序排列
5. confidence 反映你对判断的确信程度
6. 大多数情况下是单任务，只有明确的复合请求才拆解"""

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

    def _quick_check(self, text: str) -> Optional[IntentResult]:
        text_lower = text.lower().strip()

        if text_lower in ["/coco", "/enter_coco"]:
            return IntentResult.single(
                intent=IntentType.ENTER_COCO,
                confidence=1.0,
                original_text=text,
                reasoning="快速匹配：/coco 命令",
                description="进入 Coco 编程模式"
            )

        if text_lower in ["/end_coco", "/exit_coco", "/exit", "/quit"]:
            return IntentResult.single(
                intent=IntentType.EXIT_COCO,
                confidence=1.0,
                original_text=text,
                reasoning="快速匹配：退出命令",
                description="退出 Coco 模式"
            )

        if text_lower in ["/coco_info"]:
            return IntentResult.single(
                intent=IntentType.COCO_MESSAGE,
                confidence=1.0,
                data={"command": "info"},
                original_text=text,
                reasoning="快速匹配：/coco_info 命令",
                description="查看 Coco 会话信息"
            )

        shell_commands = [
            "ls", "pwd", "cd", "cat", "head", "tail", "grep", "find", "echo",
            "mkdir", "touch", "rm", "cp", "mv", "chmod", "chown",
            "git", "npm", "yarn", "pnpm", "python", "pip", "uv", "node",
            "docker", "kubectl", "curl", "wget", "ssh", "scp",
            "ps", "top", "kill", "df", "du", "free", "whoami", "date", "uname",
            "tar", "zip", "unzip", "gzip", "gunzip",
            "vim", "nano", "less", "more", "wc", "sort", "uniq", "awk", "sed",
            "brew", "apt", "yum", "pacman", "make", "cmake", "cargo", "go",
        ]

        first_word = text_lower.split()[0] if text_lower else ""
        if first_word in shell_commands:
            return IntentResult.single(
                intent=IntentType.SHELL_COMMAND,
                confidence=0.95,
                original_text=text,
                reasoning=f"快速匹配：以 shell 命令 '{first_word}' 开头",
                description=f"执行命令: {text}"
            )

        return None

    def _parse_react_response(self, content: str) -> tuple[dict, str]:
        reasoning_parts = []

        thought_match = re.search(r'###?\s*Thought[^#]*?(?=###|\Z)', content, re.DOTALL | re.IGNORECASE)
        if thought_match:
            reasoning_parts.append(f"思考: {thought_match.group().strip()}")

        action_match = re.search(r'###?\s*Action[^#]*?(?=###|\Z)', content, re.DOTALL | re.IGNORECASE)
        if action_match:
            reasoning_parts.append(f"行动: {action_match.group().strip()}")

        observation_match = re.search(r'###?\s*Observation[^#]*?(?=###|\Z)', content, re.DOTALL | re.IGNORECASE)
        if observation_match:
            reasoning_parts.append(f"观察: {observation_match.group().strip()}")

        reflection_match = re.search(r'###?\s*Reflection[^#]*?(?=###|\Z)', content, re.DOTALL | re.IGNORECASE)
        if reflection_match:
            reasoning_parts.append(f"反思: {reflection_match.group().strip()}")

        reasoning = "\n".join(reasoning_parts)

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

    def recognize(self, text: str, is_in_coco_mode: bool = False) -> IntentResult:
        quick_result = self._quick_check(text)
        if quick_result:
            return quick_result

        if is_in_coco_mode:
            text_lower = text.lower().strip()
            exit_keywords = ["退出", "结束", "exit", "quit", "不用了", "算了", "停止"]
            if any(kw in text_lower for kw in exit_keywords) and len(text) < 20:
                return IntentResult.single(
                    intent=IntentType.EXIT_COCO,
                    confidence=0.8,
                    original_text=text,
                    reasoning="Coco模式下检测到退出关键词",
                    description="退出 Coco 模式"
                )
            return IntentResult.single(
                intent=IntentType.COCO_MESSAGE,
                confidence=0.9,
                original_text=text,
                reasoning="当前处于Coco模式，消息转发给Coco处理",
                description="发送消息给 Coco"
            )

        try:
            llm = self._get_llm()

            context_hint = ""
            if "/" in text or "~" in text or "目录" in text or "文件夹" in text:
                context_hint = "\n注意：用户输入中包含路径相关内容，请特别关注是否是目录切换意图。"

            messages = [
                SystemMessage(content=self.REACT_SYSTEM_PROMPT),
                HumanMessage(content=f"请分析以下用户输入的意图：\n\n\"{text}\"{context_hint}"),
            ]

            response = llm.invoke(messages)
            content = response.content.strip()

            print(f"🧠 ReAct 推理过程:\n{content[:500]}...")

            result, reasoning = self._parse_react_response(content)

            if not result or "tasks" not in result:
                return self._fallback_recognition(text, reasoning)

            tasks = []
            intent_map = {
                "enter_coco": IntentType.ENTER_COCO,
                "exit_coco": IntentType.EXIT_COCO,
                "change_dir": IntentType.CHANGE_DIR,
                "shell": IntentType.SHELL_COMMAND,
                "unknown": IntentType.UNKNOWN,
            }

            for task_data in result.get("tasks", []):
                intent_str = task_data.get("intent", "unknown")
                intent = intent_map.get(intent_str, IntentType.UNKNOWN)

                data = task_data.get("data", {})
                if intent == IntentType.CHANGE_DIR and "path" in data:
                    data["path"] = self._normalize_path(data["path"])

                tasks.append(TaskStep(
                    intent=intent,
                    description=task_data.get("description", ""),
                    data=data
                ))

            if not tasks:
                return self._fallback_recognition(text, reasoning)

            return IntentResult(
                tasks=tasks,
                confidence=result.get("confidence", 0.5),
                original_text=text,
                reasoning=reasoning
            )

        except Exception as e:
            print(f"ReAct 意图识别异常: {e}")
            return self._fallback_recognition(text, f"异常: {e}")

    def _fallback_recognition(self, text: str, reasoning: str) -> IntentResult:
        text_lower = text.lower()

        coco_keywords = ["写代码", "编程", "开发", "实现", "帮我写", "帮我改", "优化代码", "代码"]
        if any(kw in text_lower for kw in coco_keywords):
            return IntentResult.single(
                intent=IntentType.ENTER_COCO,
                confidence=0.6,
                original_text=text,
                reasoning=f"回退识别：包含编程相关关键词\n{reasoning}",
                description="进入 Coco 编程模式"
            )

        dir_keywords = ["目录", "文件夹", "路径", "切换到", "进入"]
        if any(kw in text_lower for kw in dir_keywords):
            return IntentResult.single(
                intent=IntentType.CHANGE_DIR,
                confidence=0.5,
                data={"path": ""},
                original_text=text,
                reasoning=f"回退识别：包含目录相关关键词\n{reasoning}",
                description="切换目录"
            )

        return IntentResult.single(
            intent=IntentType.SHELL_COMMAND,
            confidence=0.4,
            original_text=text,
            reasoning=f"回退识别：默认作为shell命令处理\n{reasoning}",
            description=f"执行命令: {text}"
        )

    def _normalize_path(self, path: str) -> str:
        if not path:
            return ""

        path = path.strip()

        if path.startswith("~"):
            path = os.path.expanduser(path)

        path_mappings = {
            "用户目录": os.path.expanduser("~"),
            "home": os.path.expanduser("~"),
            "主目录": os.path.expanduser("~"),
            "桌面": os.path.expanduser("~/Desktop"),
            "desktop": os.path.expanduser("~/Desktop"),
            "下载": os.path.expanduser("~/Downloads"),
            "downloads": os.path.expanduser("~/Downloads"),
            "文档": os.path.expanduser("~/Documents"),
            "documents": os.path.expanduser("~/Documents"),
            "workspace": os.path.expanduser("~/workspace"),
            "workspaces": os.path.expanduser("~/workspaces"),
            "项目": os.path.expanduser("~/projects"),
            "projects": os.path.expanduser("~/projects"),
            "tmp": "/tmp",
            "临时": "/tmp",
        }

        path_lower = path.lower()
        for key, value in path_mappings.items():
            if key in path_lower:
                remaining = path_lower.replace(key, "").strip()
                remaining = remaining.replace("下的", "/").replace("里的", "/").replace("中的", "/")
                remaining = remaining.replace("目录", "").replace("文件夹", "").strip()
                if remaining:
                    remaining = remaining.strip("/")
                    return os.path.join(value, remaining)
                return value

        return path
