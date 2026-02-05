import subprocess
import re
from dataclasses import dataclass
from typing import Optional
from ..config import get_settings
from ..utils.text import truncate_output


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
            # 1) 尽可能把“会打开 pager 的命令”变成非交互式
            command = self._sanitize_command_for_noninteractive(command)

            # 2) 构建环境变量：禁用各种 pager，避免命令阻塞
            import os
            env = os.environ.copy()
            env.update({
                # git / man / systemd 等常见 pager
                "GIT_PAGER": "cat",
                "PAGER": "cat",
                "MANPAGER": "cat",
                "SYSTEMD_PAGER": "cat",
                # less：F(一屏退出) R(支持颜色) X(不清屏)
                "LESS": "FRX",
                # 禁用 git 交互式提示（例如需要输入用户名密码时）
                "GIT_TERMINAL_PROMPT": "0",
                # 终端类型设为 dumb，尽量减少交互/控制序列
                "TERM": "dumb",
            })

            process = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self.settings.sandbox_timeout,
                cwd=cwd,
                env=env,
            )

            stdout = process.stdout
            stderr = process.stderr
            max_len = self.settings.sandbox_max_output_length

            stdout = truncate_output(stdout, max_len)
            stderr = truncate_output(stderr, max_len, label="错误输出被截断")

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

    def _sanitize_command_for_noninteractive(self, command: str) -> str:
        """Best-effort rewrite to avoid interactive pagers.

        目前主要覆盖 git：在检测到 `git ...` 且未显式要求分页时，自动加上 `--no-pager`。

        注意：此函数不做复杂 shell 解析，只做保守的前缀改写。
        """
        cmd = command.strip()
        if not cmd:
            return command

        # 用户显式要求分页/或已禁用 pager 时，不做改写
        lowered = cmd.lower()
        if "--no-pager" in lowered or "--paginate" in lowered or re.search(r"(^|\s)git\s+-p(\s|$)", lowered):
            return command

        # 常见形态：git xxx... / sudo git xxx...
        if re.match(r"^\s*(?:sudo\s+)?git\b", cmd):
            return re.sub(r"^(\s*(?:sudo\s+)?)git\b", r"\1git --no-pager", command, count=1)

        return command
