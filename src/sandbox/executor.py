import subprocess
import shlex
import re
from dataclasses import dataclass
from typing import Optional
from ..config import get_settings


@dataclass
class ExecutionResult:
    success: bool
    stdout: str
    stderr: str
    return_code: int
    error_message: Optional[str] = None

    def to_message(self) -> str:
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


class SandboxExecutor:
    DANGEROUS_PATTERNS = [
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
    ]

    def __init__(self):
        self.settings = get_settings()
        self._compiled_patterns = [re.compile(p, re.IGNORECASE) for p in self.DANGEROUS_PATTERNS]

    def is_command_safe(self, command: str) -> tuple[bool, Optional[str]]:
        for pattern in self._compiled_patterns:
            if pattern.search(command):
                return False, f"命令包含危险操作模式: {pattern.pattern}"
        
        for blacklisted in self.settings.command_blacklist:
            if blacklisted in command:
                return False, f"命令包含黑名单内容: {blacklisted}"
        
        return True, None

    def execute(self, command: str, cwd: Optional[str] = None) -> ExecutionResult:
        is_safe, reason = self.is_command_safe(command)
        if not is_safe:
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="",
                return_code=-1,
                error_message=f"安全检查未通过: {reason}"
            )

        try:
            process = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self.settings.sandbox_timeout,
                cwd=cwd,
                env=None,
            )

            stdout = process.stdout
            stderr = process.stderr
            max_len = self.settings.sandbox_max_output_length

            if len(stdout) > max_len:
                stdout = stdout[:max_len] + f"\n... (输出被截断，共 {len(process.stdout)} 字符)"
            if len(stderr) > max_len:
                stderr = stderr[:max_len] + f"\n... (错误输出被截断，共 {len(process.stderr)} 字符)"

            return ExecutionResult(
                success=process.returncode == 0,
                stdout=stdout,
                stderr=stderr,
                return_code=process.returncode,
            )

        except subprocess.TimeoutExpired:
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="",
                return_code=-1,
                error_message=f"命令执行超时（{self.settings.sandbox_timeout}秒）"
            )
        except Exception as e:
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="",
                return_code=-1,
                error_message=f"执行异常: {str(e)}"
            )

    async def execute_async(self, command: str, cwd: Optional[str] = None) -> ExecutionResult:
        return self.execute(command, cwd)
