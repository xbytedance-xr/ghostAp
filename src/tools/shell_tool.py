import subprocess
import re
import logging
from dataclasses import dataclass, field
from typing import Optional, Callable
from enum import Enum
from langchain_core.tools import BaseTool
from pydantic import Field

logger = logging.getLogger(__name__)


class RiskLevel(Enum):
    SAFE = "safe"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class ShellExecutionResult:
    success: bool
    stdout: str
    stderr: str
    return_code: int
    command: str
    risk_level: RiskLevel = RiskLevel.SAFE
    error_message: Optional[str] = None
    blocked: bool = False
    block_reason: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "return_code": self.return_code,
            "command": self.command,
            "risk_level": self.risk_level.value,
            "error_message": self.error_message,
            "blocked": self.blocked,
            "block_reason": self.block_reason,
        }

    def to_message(self) -> str:
        if self.blocked:
            return f"🚫 命令被拦截: {self.block_reason}"
        if self.error_message:
            return f"❌ 执行失败: {self.error_message}"

        parts = []
        if self.stdout:
            parts.append(f"📤 输出:\n```\n{self.stdout}\n```")
        if self.stderr:
            parts.append(f"⚠️ 错误输出:\n```\n{self.stderr}\n```")
        if not parts:
            parts.append("✅ 命令执行成功（无输出）")

        parts.append(f"🔢 返回码: {self.return_code}")
        return "\n".join(parts)


@dataclass
class SecurityPolicy:
    dangerous_patterns: list[str] = field(default_factory=list)
    blacklist_commands: list[str] = field(default_factory=list)
    whitelist_commands: list[str] = field(default_factory=list)
    allowed_paths: list[str] = field(default_factory=list)
    max_output_length: int = 4000
    timeout: int = 30
    enable_whitelist_mode: bool = False

    @classmethod
    def default(cls) -> "SecurityPolicy":
        return cls(
            dangerous_patterns=[
                r"rm\s+(-[rf]+\s+)?/($|\s)",
                r"rm\s+(-[rf]+\s+)?/\*",
                r"mkfs\.",
                r"dd\s+if=",
                r">\s*/dev/sd[a-z]",
                r"shutdown",
                r"reboot",
                r"halt\b",
                r"poweroff",
                r"init\s+[06]",
                r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;",
                r"fork\s*bomb",
                r"chmod\s+(-[rR]+\s+)?777\s+/",
                r"chown\s+.*\s+/($|\s)",
                r"curl\s+.*\|\s*(ba)?sh",
                r"wget\s+.*\|\s*(ba)?sh",
                r"eval\s+.*\$\(",
                r">\s*/etc/",
                r"rm\s+-rf\s+~",
                r"rm\s+-rf\s+\$HOME",
            ],
            blacklist_commands=[
                "rm -rf /",
                "rm -rf /*",
                "mkfs",
                "dd if=",
                "shutdown",
                "reboot",
                "halt",
                "poweroff",
                "init 0",
                "init 6",
                ":(){ :|:& };:",
            ],
            whitelist_commands=[
                "ls", "pwd", "cd", "cat", "head", "tail", "grep", "find", "echo",
                "mkdir", "touch", "cp", "mv", "chmod", "chown",
                "git", "npm", "yarn", "pnpm", "python", "pip", "uv", "node",
                "docker", "kubectl", "curl", "wget", "ssh", "scp",
                "ps", "top", "kill", "df", "du", "free", "whoami", "date", "uname",
                "tar", "zip", "unzip", "gzip", "gunzip",
                "vim", "nano", "less", "more", "wc", "sort", "uniq", "awk", "sed",
                "brew", "apt", "yum", "pacman", "make", "cmake", "cargo", "go",
            ],
        )


class SafeShellTool(BaseTool):
    name: str = "safe_shell"
    description: str = """Execute shell commands safely with security checks.
    Use this tool to run system commands, scripts, and manage processes.
    The tool includes built-in security policies to prevent dangerous operations."""

    security_policy: SecurityPolicy = Field(default_factory=SecurityPolicy.default)
    working_directory: Optional[str] = None
    pre_execute_hook: Optional[Callable[[str], Optional[str]]] = None
    post_execute_hook: Optional[Callable[[ShellExecutionResult], None]] = None

    _compiled_patterns: list = []

    model_config = {"arbitrary_types_allowed": True}

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._compiled_patterns = [
            re.compile(p, re.IGNORECASE)
            for p in self.security_policy.dangerous_patterns
        ]

    def _check_security(self, command: str) -> tuple[bool, Optional[str], RiskLevel]:
        for pattern in self._compiled_patterns:
            if pattern.search(command):
                return False, f"命令包含危险操作模式: {pattern.pattern}", RiskLevel.CRITICAL

        for blacklisted in self.security_policy.blacklist_commands:
            if blacklisted.lower() in command.lower():
                return False, f"命令包含黑名单内容: {blacklisted}", RiskLevel.CRITICAL

        if self.security_policy.enable_whitelist_mode:
            first_word = command.strip().split()[0] if command.strip() else ""
            if first_word not in self.security_policy.whitelist_commands:
                return False, f"命令不在白名单中: {first_word}", RiskLevel.HIGH

        risk_level = self._assess_risk(command)
        return True, None, risk_level

    def _assess_risk(self, command: str) -> RiskLevel:
        high_risk_patterns = [
            r"rm\s+-r", r"sudo\s+", r"chmod\s+", r"chown\s+",
            r">\s+/", r"pip\s+install", r"npm\s+install",
        ]
        medium_risk_patterns = [
            r"curl\s+", r"wget\s+", r"git\s+clone", r"docker\s+",
        ]

        for pattern in high_risk_patterns:
            if re.search(pattern, command, re.IGNORECASE):
                return RiskLevel.HIGH

        for pattern in medium_risk_patterns:
            if re.search(pattern, command, re.IGNORECASE):
                return RiskLevel.MEDIUM

        read_only_commands = ["ls", "pwd", "cat", "head", "tail", "grep", "find",
                             "ps", "top", "df", "du", "free", "whoami", "date", "uname"]
        first_word = command.strip().split()[0] if command.strip() else ""
        if first_word in read_only_commands:
            return RiskLevel.SAFE

        return RiskLevel.LOW

    def _run(self, command: str) -> str:
        result = self.execute(command)
        return result.to_message()

    async def _arun(self, command: str) -> str:
        result = await self.execute_async(command)
        return result.to_message()

    def execute(self, command: str, cwd: Optional[str] = None) -> ShellExecutionResult:
        if self.pre_execute_hook:
            modified_command = self.pre_execute_hook(command)
            if modified_command is None:
                return ShellExecutionResult(
                    success=False,
                    stdout="",
                    stderr="",
                    return_code=-1,
                    command=command,
                    blocked=True,
                    block_reason="命令被预处理钩子拦截",
                )
            command = modified_command

        is_safe, reason, risk_level = self._check_security(command)
        if not is_safe:
            logger.warning(f"命令被安全策略拦截: {command}, 原因: {reason}")
            return ShellExecutionResult(
                success=False,
                stdout="",
                stderr="",
                return_code=-1,
                command=command,
                risk_level=risk_level,
                blocked=True,
                block_reason=reason,
            )

        work_dir = cwd or self.working_directory

        try:
            logger.info(f"执行命令: {command}, 工作目录: {work_dir}")
            process = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self.security_policy.timeout,
                cwd=work_dir,
            )

            stdout = process.stdout
            stderr = process.stderr
            max_len = self.security_policy.max_output_length

            if len(stdout) > max_len:
                stdout = stdout[:max_len] + f"\n... (输出被截断，共 {len(process.stdout)} 字符)"
            if len(stderr) > max_len:
                stderr = stderr[:max_len] + f"\n... (错误输出被截断，共 {len(process.stderr)} 字符)"

            result = ShellExecutionResult(
                success=process.returncode == 0,
                stdout=stdout,
                stderr=stderr,
                return_code=process.returncode,
                command=command,
                risk_level=risk_level,
            )

            if self.post_execute_hook:
                self.post_execute_hook(result)

            logger.info(f"命令执行完成: 返回码={process.returncode}")
            return result

        except subprocess.TimeoutExpired:
            logger.error(f"命令执行超时: {command}")
            return ShellExecutionResult(
                success=False,
                stdout="",
                stderr="",
                return_code=-1,
                command=command,
                risk_level=risk_level,
                error_message=f"命令执行超时（{self.security_policy.timeout}秒）",
            )
        except Exception as e:
            logger.error(f"命令执行异常: {command}, 错误: {e}")
            return ShellExecutionResult(
                success=False,
                stdout="",
                stderr="",
                return_code=-1,
                command=command,
                risk_level=risk_level,
                error_message=f"执行异常: {str(e)}",
            )

    async def execute_async(self, command: str, cwd: Optional[str] = None) -> ShellExecutionResult:
        return self.execute(command, cwd)

    def set_working_directory(self, path: str) -> None:
        self.working_directory = path
        logger.info(f"工作目录已设置为: {path}")

    def add_to_blacklist(self, command: str) -> None:
        self.security_policy.blacklist_commands.append(command)
        logger.info(f"已添加到黑名单: {command}")

    def add_dangerous_pattern(self, pattern: str) -> None:
        self.security_policy.dangerous_patterns.append(pattern)
        self._compiled_patterns.append(re.compile(pattern, re.IGNORECASE))
        logger.info(f"已添加危险模式: {pattern}")
