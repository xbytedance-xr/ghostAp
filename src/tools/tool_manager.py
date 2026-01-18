import logging
from typing import Optional
from langchain_openai import ChatOpenAI
from langchain_core.tools import BaseTool
from langgraph.prebuilt import create_react_agent

from .shell_tool import SafeShellTool, SecurityPolicy, ShellExecutionResult
from .file_tool import FileEditorTool, FileSecurityPolicy, FileOperationResult
from ..config import get_settings

logger = logging.getLogger(__name__)


class ToolManager:
    AGENT_SYSTEM_PROMPT = """你是一个智能助手，可以帮助用户执行系统操作和文件管理任务。

你有以下工具可用：
1. safe_shell - 安全地执行 Shell 命令
2. file_editor - 读写和管理文件

使用规则：
- 执行命令前，先评估其安全性
- 对于危险操作，先向用户确认
- 文件操作时注意路径安全
- 提供清晰的执行结果反馈

请根据用户的需求，选择合适的工具来完成任务。"""

    def __init__(
        self,
        working_directory: Optional[str] = None,
        shell_policy: Optional[SecurityPolicy] = None,
        file_policy: Optional[FileSecurityPolicy] = None,
    ):
        self.settings = get_settings()
        self.working_directory = working_directory

        self.shell_tool = SafeShellTool(
            security_policy=shell_policy or SecurityPolicy.default(),
            working_directory=working_directory,
        )

        self.file_tool = FileEditorTool(
            security_policy=file_policy or FileSecurityPolicy.default(),
            root_path=working_directory,
        )

        self._llm: Optional[ChatOpenAI] = None
        self._agent = None

        logger.info(f"ToolManager 初始化完成, 工作目录: {working_directory}")

    def _get_llm(self) -> ChatOpenAI:
        if self._llm is None:
            self._llm = ChatOpenAI(
                base_url=self.settings.ark_base_url,
                api_key=self.settings.ark_api_key,
                model=self.settings.ark_model,
                temperature=0,
            )
        return self._llm

    def get_tools(self) -> list[BaseTool]:
        return [self.shell_tool, self.file_tool]

    def get_agent(self):
        if self._agent is None:
            llm = self._get_llm()
            tools = self.get_tools()
            self._agent = create_react_agent(
                llm,
                tools,
                prompt=self.AGENT_SYSTEM_PROMPT,
            )
        return self._agent

    def execute_shell(self, command: str, cwd: Optional[str] = None) -> ShellExecutionResult:
        return self.shell_tool.execute(command, cwd)

    async def execute_shell_async(self, command: str, cwd: Optional[str] = None) -> ShellExecutionResult:
        return await self.shell_tool.execute_async(command, cwd)

    def read_file(self, path: str, start_line: int = 1, max_lines: Optional[int] = None) -> FileOperationResult:
        return self.file_tool.read(path, start_line, max_lines)

    def write_file(self, path: str, content: str) -> FileOperationResult:
        return self.file_tool.write(path, content)

    def list_directory(self, path: str) -> FileOperationResult:
        return self.file_tool.list_directory(path)

    def str_replace(self, path: str, old_str: str, new_str: str) -> FileOperationResult:
        return self.file_tool.str_replace(path, old_str, new_str)

    def set_working_directory(self, path: str) -> None:
        self.working_directory = path
        self.shell_tool.set_working_directory(path)
        self.file_tool.root_path = path
        logger.info(f"工作目录已更新为: {path}")

    def run_agent(self, user_input: str, chat_history: list = None) -> str:
        agent = self.get_agent()
        try:
            messages = [{"role": "user", "content": user_input}]
            if chat_history:
                messages = chat_history + messages
            result = agent.invoke({"messages": messages})
            if result.get("messages"):
                return result["messages"][-1].content
            return ""
        except Exception as e:
            logger.error(f"Agent 执行失败: {e}")
            return f"执行失败: {str(e)}"

    async def run_agent_async(self, user_input: str, chat_history: list = None) -> str:
        agent = self.get_agent()
        try:
            messages = [{"role": "user", "content": user_input}]
            if chat_history:
                messages = chat_history + messages
            result = await agent.ainvoke({"messages": messages})
            if result.get("messages"):
                return result["messages"][-1].content
            return ""
        except Exception as e:
            logger.error(f"Agent 异步执行失败: {e}")
            return f"执行失败: {str(e)}"

    def add_shell_blacklist(self, command: str) -> None:
        self.shell_tool.add_to_blacklist(command)

    def add_dangerous_pattern(self, pattern: str) -> None:
        self.shell_tool.add_dangerous_pattern(pattern)

    def enable_whitelist_mode(self) -> None:
        self.shell_tool.security_policy.enable_whitelist_mode = True
        logger.info("已启用 Shell 白名单模式")

    def disable_whitelist_mode(self) -> None:
        self.shell_tool.security_policy.enable_whitelist_mode = False
        logger.info("已禁用 Shell 白名单模式")

    def enable_file_delete(self) -> None:
        self.file_tool.security_policy.allow_delete = True
        logger.info("已启用文件删除功能")

    def disable_file_delete(self) -> None:
        self.file_tool.security_policy.allow_delete = False
        logger.info("已禁用文件删除功能")

    def get_status(self) -> dict:
        return {
            "working_directory": self.working_directory,
            "shell_policy": {
                "whitelist_mode": self.shell_tool.security_policy.enable_whitelist_mode,
                "timeout": self.shell_tool.security_policy.timeout,
                "max_output_length": self.shell_tool.security_policy.max_output_length,
                "blacklist_count": len(self.shell_tool.security_policy.blacklist_commands),
                "pattern_count": len(self.shell_tool.security_policy.dangerous_patterns),
            },
            "file_policy": {
                "allow_delete": self.file_tool.security_policy.allow_delete,
                "allow_overwrite": self.file_tool.security_policy.allow_overwrite,
                "max_file_size_mb": self.file_tool.security_policy.max_file_size_mb,
                "max_read_lines": self.file_tool.security_policy.max_read_lines,
            },
            "llm_model": self.settings.ark_model,
        }
