import json
import logging
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from ..utils.errors import get_error_detail
from ..utils.llm import ChatOpenAICacheKey, get_cached_chat_openai

from ..config import get_settings

logger = logging.getLogger(__name__)


class IntentType(Enum):
    ENTER_COCO = "enter_coco"
    EXIT_COCO = "exit_coco"
    ENTER_CLAUDE = "enter_claude"
    EXIT_CLAUDE = "exit_claude"
    ENTER_AIDEN = "enter_aiden"
    EXIT_AIDEN = "exit_aiden"
    ENTER_CODEX = "enter_codex"
    EXIT_CODEX = "exit_codex"
    ENTER_GEMINI = "enter_gemini"
    EXIT_GEMINI = "exit_gemini"
    EXIT_MODE = "exit_mode"
    CHANGE_DIR = "change_dir"
    SHELL_COMMAND = "shell"
    COCO_MESSAGE = "coco_message"
    CLAUDE_MESSAGE = "claude_message"
    AIDEN_MESSAGE = "aiden_message"
    CODEX_MESSAGE = "codex_message"
    GEMINI_MESSAGE = "gemini_message"
    TTADK_MESSAGE = "ttadk_message"
    CREATE_PROJECT = "create_project"
    SWITCH_PROJECT = "switch_project"
    LIST_PROJECTS = "list_projects"
    CLOSE_PROJECT = "close_project"
    PROJECT_STATUS = "project_status"
    ENTER_DEEP = "enter_deep"
    DEEP_STATUS = "deep_status"
    STOP_DEEP = "stop_deep"
    DEEP_UPDATE = "deep_update"
    ENTER_LOOP = "enter_loop"
    LOOP_STATUS = "loop_status"
    STOP_LOOP = "stop_loop"
    LOOP_PAUSE = "loop_pause"
    LOOP_RESUME = "loop_resume"
    LOOP_GUIDE = "loop_guide"
    ENTER_SPEC = "enter_spec"
    SPEC_STATUS = "spec_status"
    STOP_SPEC = "stop_spec"
    SPEC_PAUSE = "spec_pause"
    SPEC_RESUME = "spec_resume"
    SPEC_GUIDE = "spec_guide"
    SHOW_HELP = "show_help"
    SHOW_TOOLS = "show_tools"
    TOOLS_STATUS = "tools_status"
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
    def single(
        cls,
        intent: IntentType,
        confidence: float = 0.0,
        data: dict = None,
        original_text: str = "",
        reasoning: str = "",
        description: str = "",
    ) -> "IntentResult":
        return cls(
            tasks=[TaskStep(intent=intent, description=description, data=data or {})],
            confidence=confidence,
            original_text=original_text,
            reasoning=reasoning,
        )


class IntentRecognizer:
    REACT_SYSTEM_PROMPT = """你是一个智能意图识别助手，使用 ReAct 模式分析用户输入。

## 当前上下文
{context_hint}

## ASR 容错指南
用户输入可能来自语音识别（ASR），包含以下噪音或错误，请进行语义修正：
1. **末尾标点**：忽略句号、问号等（如"帮我建个项目。" -> "帮我建个项目"）
2. **语气词**：忽略"一下"、"个"、"那个"等无实义词（如"建个项目" -> "建项目"）
3. **同音错别字**：
   - "即它" / "基特" -> git
   - "抠抠" / "口口" -> coco
   - "克劳德" -> claude
   - "列表" / "列出" -> list

## 可识别的意图类型

### 编程相关
1. **enter_coco** - 用户想要进入编程/开发/AI对话模式（仅在非编程模式下使用）
   - 关键词：写代码、编程、开发、帮我实现、coco、AI助手、帮我改、帮我优化代码、开始编程
   - 示例："帮我写一个函数"、"进入开发模式"、"帮我改下这个bug"、"开始编程"
   - ASR纠错："进入抠抠" -> enter_coco
   - data: {{}}

2. **exit_coco** - 用户想要退出编程模式
   - 关键词：退出、结束、不用了、算了、停止、exit、quit、退出编程、结束编程
   - 示例："退出"、"不用了谢谢"、"结束对话"、"退出编程模式"
   - data: {{}}

3. **coco_message** - 用户在编程模式下发送的编程相关消息（仅在编程模式下使用）
   - 特征：描述编程需求、代码问题、功能实现等
   - 示例："帮我写一个排序函数"、"这段代码有bug"、"优化一下性能"
   - data: {{}}

### 目录操作
4. **change_dir** - 用户想要切换或查询工作目录
   - 关键词：切换目录、去...目录、进入...文件夹、cd、当前目录、上级目录
   - 示例："切换到workspace目录"、"当前在什么目录"、"去上级目录"
   - data: {{"path": "目标路径"}}
   - 路径规则：
     - 上级目录: ".."
     - 用户目录: "~" 或 "~/子目录"
     - 桌面: "~/Desktop"
     - 下载: "~/Downloads"
     - 文档: "~/Documents"
     - 当前目录查询: path 为空字符串 ""

### 项目管理
5. **create_project** - 用户想要创建新项目
   - 关键词：创建项目、新建项目、开始项目、在当前目录创建项目、建个项目
   - 示例："创建项目 myapp"、"在当前目录创建项目"、"新建一个叫test的项目"
   - ASR纠错："建个项目叫测试。" -> create_project(name="测试")
   - data: {{"name": "项目名称", "path": "项目路径(可选，默认当前目录)"}}
   - 如果用户说"在当前目录创建项目"，path 设为空字符串 ""

6. **switch_project** - 用户想要切换到其他项目
   - 关键词：切换项目、换到...项目、去...项目、打开项目
   - 示例："切换到myapp项目"、"换到test项目"
   - data: {{"name": "项目名称"}}

7. **list_projects** - 用户想要查看项目列表
   - 关键词：项目列表、看看项目、所有项目、有哪些项目
   - 示例："看看有哪些项目"、"项目列表"
   - data: {{}}

8. **close_project** - 用户想要关闭项目
   - 关键词：关闭项目、结束项目、删除项目
   - 示例："关闭myapp项目"
   - data: {{"name": "项目名称"}}

9. **project_status** - 用户想要查看项目状态
   - 关键词：项目状态、当前项目、项目信息
   - 示例："当前项目状态"、"看看项目信息"
   - data: {{}}

### Shell 命令
10. **shell** - 用户想执行shell命令或用自然语言描述文件操作
   - 特征：命令格式（ls、git、npm等）或描述想要执行的文件/系统操作
   - 示例："ls -la"、"git status"、"帮我看下有什么文件"
   - ASR纠错："即它状态" -> shell(git status)
   - data: {{"command": "实际要执行的shell命令"}}
   - 重要：必须将用户意图转换为实际的shell命令
   - 示例："帮我看下上级目录有什么文件" → data: {{"command": "ls .."}}

11. **unknown** - 无法确定意图，data: {{}}

## 任务拆解规则

如果用户请求包含多个步骤，拆解为多个任务：
- "切换到项目目录然后帮我写代码" → change_dir + coco_message（如果已在编程模式）或 change_dir + enter_coco
- "去workspace目录看看有什么文件" → change_dir + shell(ls)
- "在当前目录创建项目然后开始编程" → create_project + enter_coco

## 输出格式

请按以下格式输出：

### Thought
分析用户输入的语义特征和可能的意图（包括ASR纠错过程）。

### Action
判断意图类型，决定是否需要拆解为多个任务。

### Result
```json
{{
  "tasks": [
    {{"intent": "意图类型", "description": "任务描述", "data": {{"相关数据"}}}}
  ],
  "confidence": 0.0-1.0
}}
```

## 重要规则

1. shell命令格式（如 `ls -la`、`git status`）直接判断为 shell
2. 在编程模式下，编程相关需求判断为 coco_message；不在编程模式下判断为 enter_coco
3. 目录相关问题判断为 change_dir
4. 项目管理相关判断为 create_project/switch_project/list_projects/close_project/project_status
5. 大多数情况是单任务，只有明确的复合请求才拆解
6. "在当前目录创建项目"时，path 设为空字符串，由系统使用当前工作目录
7. 退出相关的短语（如"退出"、"结束"）在编程模式下优先判断为 exit_coco"""

    INTENT_MAP = {
        "enter_coco": IntentType.ENTER_COCO,
        "exit_coco": IntentType.EXIT_COCO,
        "enter_claude": IntentType.ENTER_CLAUDE,
        "exit_claude": IntentType.EXIT_CLAUDE,
        "enter_aiden": IntentType.ENTER_AIDEN,
        "exit_aiden": IntentType.EXIT_AIDEN,
        "enter_codex": IntentType.ENTER_CODEX,
        "exit_codex": IntentType.EXIT_CODEX,
        "enter_gemini": IntentType.ENTER_GEMINI,
        "exit_gemini": IntentType.EXIT_GEMINI,
        "exit_mode": IntentType.EXIT_MODE,
        "coco_message": IntentType.COCO_MESSAGE,
        "claude_message": IntentType.CLAUDE_MESSAGE,
        "aiden_message": IntentType.AIDEN_MESSAGE,
        "codex_message": IntentType.CODEX_MESSAGE,
        "gemini_message": IntentType.GEMINI_MESSAGE,
        "ttadk_message": IntentType.TTADK_MESSAGE,
        "change_dir": IntentType.CHANGE_DIR,
        "shell": IntentType.SHELL_COMMAND,
        "create_project": IntentType.CREATE_PROJECT,
        "switch_project": IntentType.SWITCH_PROJECT,
        "list_projects": IntentType.LIST_PROJECTS,
        "close_project": IntentType.CLOSE_PROJECT,
        "project_status": IntentType.PROJECT_STATUS,
        "enter_deep": IntentType.ENTER_DEEP,
        "deep_status": IntentType.DEEP_STATUS,
        "stop_deep": IntentType.STOP_DEEP,
        "deep_update": IntentType.DEEP_UPDATE,
        "enter_loop": IntentType.ENTER_LOOP,
        "loop_status": IntentType.LOOP_STATUS,
        "stop_loop": IntentType.STOP_LOOP,
        "loop_pause": IntentType.LOOP_PAUSE,
        "loop_resume": IntentType.LOOP_RESUME,
        "loop_guide": IntentType.LOOP_GUIDE,
        "enter_spec": IntentType.ENTER_SPEC,
        "spec_status": IntentType.SPEC_STATUS,
        "stop_spec": IntentType.STOP_SPEC,
        "spec_pause": IntentType.SPEC_PAUSE,
        "spec_resume": IntentType.SPEC_RESUME,
        "spec_guide": IntentType.SPEC_GUIDE,
        "show_help": IntentType.SHOW_HELP,
        "show_tools": IntentType.SHOW_TOOLS,
        "tools_status": IntentType.TOOLS_STATUS,
        "unknown": IntentType.UNKNOWN,
    }

    EXACT_COMMANDS = {
        "/coco": (IntentType.ENTER_COCO, "进入 Coco 编程模式"),
        "/enter_coco": (IntentType.ENTER_COCO, "进入 Coco 编程模式"),
        "/end_coco": (IntentType.EXIT_COCO, "退出 Coco 编程模式"),
        "/exit_coco": (IntentType.EXIT_COCO, "退出 Coco 编程模式"),
        "/claude": (IntentType.ENTER_CLAUDE, "进入 Claude 编程模式"),
        "/enter_claude": (IntentType.ENTER_CLAUDE, "进入 Claude 编程模式"),
        "/end_claude": (IntentType.EXIT_CLAUDE, "退出 Claude 编程模式"),
        "/exit_claude": (IntentType.EXIT_CLAUDE, "退出 Claude 编程模式"),
        "/aiden": (IntentType.ENTER_AIDEN, "进入 Aiden 编程模式"),
        "/enter_aiden": (IntentType.ENTER_AIDEN, "进入 Aiden 编程模式"),
        "/end_aiden": (IntentType.EXIT_AIDEN, "退出 Aiden 编程模式"),
        "/exit_aiden": (IntentType.EXIT_AIDEN, "退出 Aiden 编程模式"),
        "/codex": (IntentType.ENTER_CODEX, "进入 Codex 编程模式"),
        "/enter_codex": (IntentType.ENTER_CODEX, "进入 Codex 编程模式"),
        "/end_codex": (IntentType.EXIT_CODEX, "退出 Codex 编程模式"),
        "/exit_codex": (IntentType.EXIT_CODEX, "退出 Codex 编程模式"),
        "/gemini": (IntentType.ENTER_GEMINI, "进入 Gemini 编程模式"),
        "/enter_gemini": (IntentType.ENTER_GEMINI, "进入 Gemini 编程模式"),
        "/end_gemini": (IntentType.EXIT_GEMINI, "退出 Gemini 编程模式"),
        "/exit_gemini": (IntentType.EXIT_GEMINI, "退出 Gemini 编程模式"),
        "/ttadk": (IntentType.TTADK_MESSAGE, "打开 TTADK 菜单"),
        "/enter_ttadk": (IntentType.TTADK_MESSAGE, "进入 TTADK 编程模式"),
        "/end_ttadk": (IntentType.EXIT_MODE, "退出 TTADK 编程模式"),
        "/exit_ttadk": (IntentType.EXIT_MODE, "退出 TTADK 编程模式"),
        "/exit": (IntentType.EXIT_MODE, "退出当前模式"),
        "/quit": (IntentType.EXIT_MODE, "退出当前模式"),
        "/projects": (IntentType.LIST_PROJECTS, "查看项目列表"),
        "/switch": (IntentType.SWITCH_PROJECT, "切换项目（打开项目看板）"),
        "/project": (IntentType.PROJECT_STATUS, "查看当前项目"),
        "/status": (IntentType.PROJECT_STATUS, "查看项目状态"),
        "/deep": (IntentType.ENTER_DEEP, "进入 Deep 模式"),
        "/deep_status": (IntentType.DEEP_STATUS, "查看 Deep 任务状态"),
        "/deep_update": (IntentType.DEEP_UPDATE, "更新 Deep 任务上下文"),
        "/stop_deep": (IntentType.STOP_DEEP, "停止 Deep 任务"),
        "/loop": (IntentType.ENTER_LOOP, "进入 Loop 模式"),
        "/loop_status": (IntentType.LOOP_STATUS, "查看 Loop 任务状态"),
        "/stop_loop": (IntentType.STOP_LOOP, "停止 Loop 任务"),
        "/loop_pause": (IntentType.LOOP_PAUSE, "暂停 Loop 任务"),
        "/loop_resume": (IntentType.LOOP_RESUME, "恢复 Loop 任务"),
        "/spec": (IntentType.ENTER_SPEC, "进入 Spec 模式"),
        "/spec_status": (IntentType.SPEC_STATUS, "查看 Spec 任务状态"),
        "/stop_spec": (IntentType.STOP_SPEC, "停止 Spec 任务"),
        "/spec_pause": (IntentType.SPEC_PAUSE, "暂停 Spec 任务"),
        "/spec_resume": (IntentType.SPEC_RESUME, "恢复 Spec 任务"),
        "/help": (IntentType.SHOW_HELP, "显示帮助信息"),
        "/帮助": (IntentType.SHOW_HELP, "显示帮助信息"),
        "/tools": (IntentType.SHOW_TOOLS, "查看所有可用工具"),
        "/tools_status": (IntentType.TOOLS_STATUS, "查看工具状态"),
    }

    SHELL_COMMANDS = {
        "ls",
        "pwd",
        "cd",
        "cat",
        "head",
        "tail",
        "grep",
        "find",
        "echo",
        "mkdir",
        "touch",
        "rm",
        "cp",
        "mv",
        "chmod",
        "chown",
        "ln",
        "git",
        "npm",
        "yarn",
        "pnpm",
        "python",
        "pip",
        "uv",
        "node",
        "docker",
        "kubectl",
        "curl",
        "wget",
        "ssh",
        "scp",
        "rsync",
        "ps",
        "top",
        "kill",
        "df",
        "du",
        "free",
        "whoami",
        "date",
        "uname",
        "tar",
        "zip",
        "unzip",
        "gzip",
        "gunzip",
        "xz",
        "vim",
        "nano",
        "less",
        "more",
        "wc",
        "sort",
        "uniq",
        "awk",
        "sed",
        "brew",
        "apt",
        "yum",
        "pacman",
        "make",
        "cmake",
        "cargo",
        "go",
        "which",
        "whereis",
        "man",
        "env",
        "export",
        "source",
        "alias",
        "ping",
        "netstat",
        "ifconfig",
        "ip",
        "nc",
        "telnet",
        "nslookup",
    }

    COMMON_WORDS = {
        "ok",
        "yes",
        "no",
        "hi",
        "hello",
        "hey",
        "thanks",
        "thank",
        "good",
        "nice",
        "great",
        "cool",
        "fine",
        "sure",
        "please",
        "sorry",
        "wow",
        "test",
        "testing",
        "done",
        "ready",
        "start",
        "stop",
        "wait",
        "check",
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "must",
        "can",
        "need",
        "want",
        "like",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "my",
        "your",
        "our",
        "what",
        "how",
        "why",
        "when",
        "where",
        "who",
        "which",
    }

    def __init__(self):
        self.settings = get_settings()
        self._llm_cache: dict[ChatOpenAICacheKey, ChatOpenAI] = {}

    def _get_llm(self) -> ChatOpenAI:
        return get_cached_chat_openai(self.settings, 0.1, cache=self._llm_cache, llm_cls=ChatOpenAI)

    EXIT_KEYWORDS = {"退出", "结束", "exit", "quit", "不用了", "算了", "停止"}
    PROJECT_SWITCH_KEYWORDS = {"切换项目", "换项目", "换到项目", "去项目", "打开项目"}
    PROJECT_LIST_KEYWORDS = {"项目列表", "所有项目", "有哪些项目", "看看项目"}
    DIR_KEYWORDS = {"切换目录", "去目录", "进入目录", "当前目录", "上级目录", "cd "}

    ENTER_COCO_KEYWORDS = {"进入编程模式", "编程模式", "开始编程", "进入coco", "coco模式"}
    ENTER_CLAUDE_KEYWORDS = {"进入claude模式", "claude模式", "进入claude", "使用claude"}
    ENTER_AIDEN_KEYWORDS = {"进入aiden模式", "aiden模式", "进入aiden", "使用aiden"}
    ENTER_CODEX_KEYWORDS = {"进入codex模式", "codex模式", "进入codex", "使用codex"}
    ENTER_GEMINI_KEYWORDS = {"进入gemini模式", "gemini模式", "进入gemini", "使用gemini"}
    ENTER_TTADK_KEYWORDS = {"进入ttadk模式", "ttadk模式", "进入ttadk", "使用ttadk", "多工具模式"}
    EXIT_MODE_KEYWORDS = {"退出模式", "退出编程模式"}

    DEEP_MODE_KEYWORDS = {"deep模式", "深度模式", "deep agent", "复杂任务", "大任务"}

    HELP_KEYWORDS = {"帮助", "help", "使用说明", "怎么用", "如何使用"}

    COMMAND_TYPO_MAP = {
        "/calude": "/claude",
        "/cluade": "/claude",
        "/cluad": "/claude",
        "/calud": "/claude",
        "/claud": "/claude",
        "/cooc": "/coco",
        "/coc": "/coco",
        "/cocoo": "/coco",
        "/exti": "/exit",
        "/eixt": "/exit",
        "/exut": "/exit",
        "/hlep": "/help",
        "/hepl": "/help",
        "/helo": "/help",
    }

    def _quick_match(self, text: str, current_mode: str = "smart") -> Optional[IntentResult]:
        text_lower = text.lower().strip()

        if text_lower in self.COMMAND_TYPO_MAP:
            corrected = self.COMMAND_TYPO_MAP[text_lower]
            if corrected in self.EXACT_COMMANDS:
                intent, desc = self.EXACT_COMMANDS[corrected]
                return IntentResult.single(
                    intent=intent,
                    confidence=0.95,
                    original_text=text,
                    reasoning=f"纠正拼写错误: {text_lower} -> {corrected}",
                    description=f"{desc}（已纠正拼写）",
                )

        if text_lower in self.EXACT_COMMANDS:
            intent, desc = self.EXACT_COMMANDS[text_lower]
            return IntentResult.single(
                intent=intent,
                confidence=1.0,
                original_text=text,
                reasoning=f"精确匹配命令: {text_lower}",
                description=desc,
            )

        if text_lower == "/coco_info":
            return IntentResult.single(
                intent=IntentType.COCO_MESSAGE,
                confidence=1.0,
                data={"command": "info"},
                original_text=text,
                reasoning="精确匹配: /coco_info",
                description="查看 Coco 会话信息",
            )

        if text_lower.startswith("/new "):
            parts = text.split(None, 2)
            name = parts[1] if len(parts) >= 2 else "project"
            path = parts[2] if len(parts) >= 3 else ""
            return IntentResult.single(
                intent=IntentType.CREATE_PROJECT,
                confidence=1.0,
                data={"name": name, "path": path},
                original_text=text,
                reasoning="精确匹配: /new 命令",
                description=f"创建项目: {name}",
            )

        if text_lower.startswith("/switch "):
            name = text[8:].strip()
            return IntentResult.single(
                intent=IntentType.SWITCH_PROJECT,
                confidence=1.0,
                data={"name": name},
                original_text=text,
                reasoning="精确匹配: /switch 命令",
                description=f"切换到项目: {name}",
            )

        if text_lower.startswith("/close "):
            name = text[7:].strip()
            return IntentResult.single(
                intent=IntentType.CLOSE_PROJECT,
                confidence=1.0,
                data={"name": name},
                original_text=text,
                reasoning="精确匹配: /close 命令",
                description=f"关闭项目: {name}",
            )

        if text_lower.startswith("/deep_update "):
            update_message = text[len("/deep_update ") :].strip()
            return IntentResult.single(
                intent=IntentType.DEEP_UPDATE,
                confidence=1.0,
                data={"message": update_message},
                original_text=text,
                reasoning="精确匹配: /deep_update 命令",
                description="更新 Deep Engine 上下文",
            )

        if text_lower.startswith("/deep_status "):
            # e.g. /deep_status all
            return IntentResult.single(
                intent=IntentType.DEEP_STATUS,
                confidence=1.0,
                data={"arg": text[len("/deep_status ") :].strip()},
                original_text=text,
                reasoning="前缀匹配: /deep_status 命令",
                description="查看 Deep Agent 任务状态",
            )

        if text_lower.startswith("/stop_deep "):
            # e.g. /stop_deep all
            return IntentResult.single(
                intent=IntentType.STOP_DEEP,
                confidence=1.0,
                data={"arg": text[len("/stop_deep ") :].strip()},
                original_text=text,
                reasoning="前缀匹配: /stop_deep 命令",
                description="停止 Deep Agent 任务",
            )

        if text_lower.startswith("/deep "):
            requirement = text[6:].strip()
            return IntentResult.single(
                intent=IntentType.ENTER_DEEP,
                confidence=1.0,
                data={"requirement": requirement},
                original_text=text,
                reasoning="精确匹配: /deep 命令",
                description="启动 Deep Engine",
            )

        if text_lower.startswith("/loop_guide "):
            guide_message = text[len("/loop_guide ") :].strip()
            return IntentResult.single(
                intent=IntentType.LOOP_GUIDE,
                confidence=1.0,
                data={"message": guide_message},
                original_text=text,
                reasoning="精确匹配: /loop_guide 命令",
                description="注入 Loop 引导信息",
            )

        if text_lower.startswith("/loop "):
            requirement = text[6:].strip()
            return IntentResult.single(
                intent=IntentType.ENTER_LOOP,
                confidence=1.0,
                data={"requirement": requirement},
                original_text=text,
                reasoning="精确匹配: /loop 命令",
                description="启动 Loop Engine",
            )

        if text_lower.startswith("/spec_guide "):
            guide_message = text[len("/spec_guide ") :].strip()
            return IntentResult.single(
                intent=IntentType.SPEC_GUIDE,
                confidence=1.0,
                data={"message": guide_message},
                original_text=text,
                reasoning="精确匹配: /spec_guide 命令",
                description="注入 Spec 引导信息",
            )

        if text_lower.startswith("/spec "):
            requirement = text[6:].strip()
            return IntentResult.single(
                intent=IntentType.ENTER_SPEC,
                confidence=1.0,
                data={"requirement": requirement},
                original_text=text,
                reasoning="精确匹配: /spec 命令",
                description="启动 Spec Engine",
            )

        if any(kw in text_lower for kw in self.EXIT_MODE_KEYWORDS):
            return IntentResult.single(
                intent=IntentType.EXIT_MODE,
                confidence=0.95,
                original_text=text,
                reasoning="检测到退出模式关键词",
                description="退出当前模式",
            )

        if any(kw in text_lower for kw in self.ENTER_COCO_KEYWORDS):
            return IntentResult.single(
                intent=IntentType.ENTER_COCO,
                confidence=0.95,
                original_text=text,
                reasoning="检测到进入 Coco 编程模式关键词",
                description="进入 Coco 编程模式",
            )

        if any(kw in text_lower for kw in self.ENTER_CLAUDE_KEYWORDS):
            return IntentResult.single(
                intent=IntentType.ENTER_CLAUDE,
                confidence=0.95,
                original_text=text,
                reasoning="检测到进入 Claude 编程模式关键词",
                description="进入 Claude 编程模式",
            )

        if text_lower == "/claude_info":
            return IntentResult.single(
                intent=IntentType.CLAUDE_MESSAGE,
                confidence=1.0,
                data={"command": "info"},
                original_text=text,
                reasoning="精确匹配: /claude_info",
                description="查看 Claude 会话信息",
            )

        if text_lower == "/aiden_info":
            return IntentResult.single(
                intent=IntentType.AIDEN_MESSAGE,
                confidence=1.0,
                data={"command": "info"},
                original_text=text,
                reasoning="精确匹配: /aiden_info",
                description="查看 Aiden 会话信息",
            )

        if text_lower == "/codex_info":
            return IntentResult.single(
                intent=IntentType.CODEX_MESSAGE,
                confidence=1.0,
                data={"command": "info"},
                original_text=text,
                reasoning="精确匹配: /codex_info",
                description="查看 Codex 会话信息",
            )

        if text_lower == "/gemini_info":
            return IntentResult.single(
                intent=IntentType.GEMINI_MESSAGE,
                confidence=1.0,
                data={"command": "info"},
                original_text=text,
                reasoning="精确匹配: /gemini_info",
                description="查看 Gemini 会话信息",
            )

        if any(kw in text_lower for kw in self.ENTER_AIDEN_KEYWORDS):
            return IntentResult.single(
                intent=IntentType.ENTER_AIDEN,
                confidence=0.95,
                original_text=text,
                reasoning="检测到进入 Aiden 编程模式关键词",
                description="进入 Aiden 编程模式",
            )

        if any(kw in text_lower for kw in self.ENTER_CODEX_KEYWORDS):
            return IntentResult.single(
                intent=IntentType.ENTER_CODEX,
                confidence=0.95,
                original_text=text,
                reasoning="检测到进入 Codex 编程模式关键词",
                description="进入 Codex 编程模式",
            )

        if any(kw in text_lower for kw in self.ENTER_GEMINI_KEYWORDS):
            return IntentResult.single(
                intent=IntentType.ENTER_GEMINI,
                confidence=0.95,
                original_text=text,
                reasoning="检测到进入 Gemini 编程模式关键词",
                description="进入 Gemini 编程模式",
            )

        if any(kw in text_lower for kw in self.ENTER_TTADK_KEYWORDS):
            return IntentResult.single(
                intent=IntentType.TTADK_MESSAGE,
                confidence=0.95,
                original_text=text,
                reasoning="检测到 TTADK 模式关键词",
                description="进入 TTADK 编程模式",
            )

        is_programming = current_mode in ("coco", "claude", "aiden", "codex", "gemini", "ttadk")
        if is_programming and len(text) < 20:
            if any(kw in text_lower for kw in self.EXIT_KEYWORDS):
                return IntentResult.single(
                    intent=IntentType.EXIT_MODE,
                    confidence=0.95,
                    original_text=text,
                    reasoning=f"{current_mode}模式下检测到退出关键词",
                    description="退出当前编程模式",
                )

        if any(kw in text_lower for kw in self.PROJECT_LIST_KEYWORDS):
            return IntentResult.single(
                intent=IntentType.LIST_PROJECTS,
                confidence=0.9,
                original_text=text,
                reasoning="检测到项目列表关键词",
                description="查看项目列表",
            )

        first_word = text_lower.split()[0] if text_lower else ""

        if first_word == "cd":
            parts = text.strip().split(maxsplit=1)
            path = parts[1] if len(parts) > 1 else ""
            return IntentResult.single(
                intent=IntentType.CHANGE_DIR,
                confidence=1.0,
                data={"path": path},
                original_text=text,
                reasoning="cd 命令匹配为目录切换",
                description=f"切换目录: {path}" if path else "查看当前目录",
            )

        if first_word in self.SHELL_COMMANDS:
            return IntentResult.single(
                intent=IntentType.SHELL_COMMAND,
                confidence=0.95,
                original_text=text,
                reasoning=f"Shell命令白名单匹配: {first_word}",
                description=f"执行命令: {text}",
            )

        if first_word in self.COMMON_WORDS:
            return None

        if (
            re.match(r"^[a-z][a-z0-9_.-]*$", first_word)
            and 2 <= len(first_word) <= 15
            and not any(kw in text_lower for kw in ["帮", "请", "能", "可以", "想", "吗", "呢"])
        ):
            return IntentResult.single(
                intent=IntentType.SHELL_COMMAND,
                confidence=0.7,
                original_text=text,
                reasoning=f"可能是命令: {first_word}",
                description=f"执行命令: {text}",
            )

        return None

    def _parse_response(self, content: str) -> tuple[dict, str]:
        thought_match = re.search(r"###?\s*Thought[^#]*?(?=###|\Z)", content, re.DOTALL | re.IGNORECASE)
        reasoning = thought_match.group().strip() if thought_match else ""

        json_match = re.search(r"```json\s*(.*?)\s*```", content, re.DOTALL)
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
                    "tasks": [
                        {
                            "intent": old_format.get("intent", "unknown"),
                            "description": "",
                            "data": old_format.get("data", {}),
                        }
                    ],
                    "confidence": old_format.get("confidence", 0.5),
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

    def _get_context_hint(self, current_mode: str) -> str:
        if current_mode == "coco":
            return "用户当前处于 **Coco 编程模式** 中。编程相关的消息应该判断为 coco_message，而不是 enter_coco。"
        elif current_mode == "claude":
            return "用户当前处于 **Claude 编程模式** 中。编程相关的消息应该判断为 claude_message，而不是 enter_claude。"
        elif current_mode == "aiden":
            return "用户当前处于 **Aiden 编程模式** 中。编程相关的消息应该判断为 aiden_message，而不是 enter_aiden。"
        elif current_mode == "codex":
            return "用户当前处于 **Codex 编程模式** 中。编程相关的消息应该判断为 codex_message，而不是 enter_codex。"
        elif current_mode == "gemini":
            return "用户当前处于 **Gemini 编程模式** 中。编程相关的消息应该判断为 gemini_message，而不是 enter_gemini。"
        elif current_mode == "ttadk":
            return "用户当前处于 **TTADK 编程模式** 中。编程相关消息应判断为 ttadk_message，继续在 TTADK 会话内执行。"
        else:
            return "用户当前处于 **智能模式**（默认模式）。如果用户想要编程，应该判断为 enter_coco、enter_claude、enter_aiden、enter_codex、enter_gemini 或 ttadk_message。"

    def _get_fallback_intent(self, current_mode: str) -> IntentType:
        if current_mode == "coco":
            return IntentType.COCO_MESSAGE
        elif current_mode == "claude":
            return IntentType.CLAUDE_MESSAGE
        elif current_mode == "aiden":
            return IntentType.AIDEN_MESSAGE
        elif current_mode == "codex":
            return IntentType.CODEX_MESSAGE
        elif current_mode == "gemini":
            return IntentType.GEMINI_MESSAGE
        elif current_mode == "ttadk":
            return IntentType.TTADK_MESSAGE
        else:
            return IntentType.SHELL_COMMAND

    def recognize(self, text: str, current_mode: str = "smart") -> IntentResult:
        quick_result = self._quick_match(text, current_mode)
        if quick_result:
            return quick_result

        is_in_coco = current_mode == "coco"
        is_in_claude = current_mode == "claude"
        is_in_aiden = current_mode == "aiden"
        is_in_codex = current_mode == "codex"
        is_in_gemini = current_mode == "gemini"
        is_in_ttadk = current_mode == "ttadk"

        try:
            llm = self._get_llm()
            context_hint = self._get_context_hint(current_mode)
            prompt = self.REACT_SYSTEM_PROMPT.format(context_hint=context_hint)

            messages = [
                SystemMessage(content=prompt),
                HumanMessage(content=f'请分析以下用户输入的意图：\n\n"{text}"'),
            ]

            response = llm.invoke(messages)
            content = response.content.strip()
            logger.debug("ReAct:\n%s...", content[:300])

            result, reasoning = self._parse_response(content)

            if not result or "tasks" not in result:
                fallback = self._get_fallback_intent(current_mode)
                return IntentResult.single(
                    intent=fallback,
                    confidence=0.5,
                    original_text=text,
                    reasoning=f"LLM解析失败，默认{fallback.value}: {reasoning}",
                    description=f"执行: {text}",
                )

            tasks = []
            for task_data in result.get("tasks", []):
                intent_str = task_data.get("intent", "unknown")
                intent = self.INTENT_MAP.get(intent_str, IntentType.UNKNOWN)

                if intent == IntentType.ENTER_COCO and is_in_coco:
                    intent = IntentType.COCO_MESSAGE
                if intent == IntentType.ENTER_CLAUDE and is_in_claude:
                    intent = IntentType.CLAUDE_MESSAGE
                if intent == IntentType.ENTER_AIDEN and is_in_aiden:
                    intent = IntentType.AIDEN_MESSAGE
                if intent == IntentType.ENTER_CODEX and is_in_codex:
                    intent = IntentType.CODEX_MESSAGE
                if intent == IntentType.ENTER_GEMINI and is_in_gemini:
                    intent = IntentType.GEMINI_MESSAGE
                if intent == IntentType.TTADK_MESSAGE and is_in_ttadk:
                    intent = IntentType.TTADK_MESSAGE

                data = task_data.get("data", {})
                if intent == IntentType.CHANGE_DIR and "path" in data:
                    data["path"] = self._normalize_path(data["path"])

                tasks.append(TaskStep(intent=intent, description=task_data.get("description", ""), data=data))

            if not tasks:
                fallback = self._get_fallback_intent(current_mode)
                return IntentResult.single(
                    intent=fallback,
                    confidence=0.5,
                    original_text=text,
                    reasoning="无任务，使用默认意图",
                    description=f"执行: {text}",
                )

            return IntentResult(
                tasks=tasks, confidence=result.get("confidence", 0.5), original_text=text, reasoning=reasoning
            )

        except Exception as e:
            logger.error("意图识别异常: %s", get_error_detail(e))
            fallback = self._get_fallback_intent(current_mode)
            return IntentResult.single(
                intent=fallback,
                confidence=0.3,
                original_text=text,
                reasoning=f"异常回退: {get_error_detail(e)}",
                description=f"执行: {text}",
            )
