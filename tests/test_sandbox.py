import os
import sys
from unittest.mock import MagicMock, Mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import Settings
from src.sandbox.executor import ExecutionResult, SandboxExecutor, SubprocessExecutor


class TestSandboxExecutor:
    def setup_method(self):
        # Disable whitelist for general execution tests (whitelist tested separately)
        settings = Settings(sandbox_use_whitelist=False)
        self.executor = SandboxExecutor(settings=settings)

    def test_safe_command_ls(self):
        result = self.executor.execute("ls -la")
        assert isinstance(result, ExecutionResult)
        assert result.return_code == 0

    def test_safe_command_echo(self):
        result = self.executor.execute("echo 'hello world'")
        assert result.success is True
        assert "hello world" in result.stdout

    def test_safe_command_date(self):
        result = self.executor.execute("date")
        assert result.success is True
        assert result.stdout != ""

    def test_dangerous_command_rm_rf_root(self):
        result = self.executor.execute("rm -rf /")
        assert result.success is False
        assert "安全检查未通过" in result.error_message

    def test_dangerous_command_rm_rf_root_star(self):
        result = self.executor.execute("rm -rf /*")
        assert result.success is False
        assert "安全检查未通过" in result.error_message

    def test_dangerous_command_shutdown(self):
        result = self.executor.execute("shutdown now")
        assert result.success is False
        assert "安全检查未通过" in result.error_message

    def test_dangerous_command_reboot(self):
        result = self.executor.execute("reboot")
        assert result.success is False
        assert "安全检查未通过" in result.error_message

    def test_dangerous_command_mkfs(self):
        result = self.executor.execute("mkfs.ext4 /dev/sda")
        assert result.success is False
        assert "安全检查未通过" in result.error_message

    def test_dangerous_command_dd(self):
        result = self.executor.execute("dd if=/dev/zero of=/dev/sda")
        assert result.success is False
        assert "安全检查未通过" in result.error_message

    def test_is_command_safe_method(self):
        is_safe, reason = self.executor.is_command_safe("ls -la")
        assert is_safe is True
        assert reason is None

        is_safe, reason = self.executor.is_command_safe("rm -rf /")
        assert is_safe is False
        assert reason is not None

    def test_execution_result_to_message(self):
        result = ExecutionResult(success=True, stdout="test output", stderr="", return_code=0)
        message = result.to_message()
        assert "test output" in message
        assert "返回码: 0" in message

    def test_execution_result_error_message(self):
        result = ExecutionResult(success=False, stdout="", stderr="", return_code=-1, error_message="测试错误")
        message = result.to_message()
        assert "执行失败" in message
        assert "测试错误" in message

    def test_command_with_pipe(self):
        # Pipe character is now blocked by DangerousPatternCheckStrategy (shell control char)
        result = self.executor.execute("echo 'hello' | grep 'hello'")
        assert result.success is False
        assert "shell 控制字符" in result.error_message

    def test_command_whoami(self):
        result = self.executor.execute("whoami")
        assert result.success is True
        assert result.stdout.strip() != ""

    def test_git_log_no_pager(self):
        """Test that git log doesn't hang due to pager."""
        # This test verifies that GIT_PAGER is disabled
        result = self.executor.execute("git log -n 3 --oneline")
        # Should complete without hanging (timeout would fail the test)
        assert isinstance(result, ExecutionResult)
        # Either succeeds (in a git repo) or fails gracefully (not a git repo)
        assert result.return_code in [0, 128]

    def test_pager_env_disabled(self):
        """Test that PAGER environment variable is set to cat."""
        result = self.executor.execute("echo $PAGER")
        assert result.success is True
        assert result.stdout.strip() == "cat"

    def test_git_pager_env_disabled(self):
        """Test that GIT_PAGER environment variable is set to cat."""
        result = self.executor.execute('echo "GIT_PAGER=[$GIT_PAGER]"')
        assert result.success is True
        assert "GIT_PAGER=[cat]" in result.stdout

    def test_git_command_auto_no_pager_injection(self):
        """Ensure we inject --no-pager for git commands by default."""
        cmd = "git log -n 1 --oneline"
        rewritten = self.executor._sanitize_command_for_noninteractive(cmd)
        assert rewritten.startswith("git --no-pager ")

        # User explicitly asks for pagination -> don't rewrite
        cmd_paginate = "git --paginate log -n 1 --oneline"
        rewritten2 = self.executor._sanitize_command_for_noninteractive(cmd_paginate)
        assert rewritten2 == cmd_paginate


class TestSandboxExecutorBlacklist:
    def setup_method(self):
        self.executor = SandboxExecutor()

    def test_fork_bomb_pattern(self):
        is_safe, _ = self.executor.is_command_safe(":(){ :|:& };:")
        assert is_safe is False

    def test_init_0_pattern(self):
        is_safe, _ = self.executor.is_command_safe("init 0")
        assert is_safe is False

    def test_init_6_pattern(self):
        is_safe, _ = self.executor.is_command_safe("init 6")
        assert is_safe is False

    def test_chmod_777_root(self):
        is_safe, _ = self.executor.is_command_safe("chmod 777 /")
        assert is_safe is False


class TestSandboxExecutorDependencyInjection:
    """测试 SandboxExecutor 的依赖注入功能"""

    def test_settings_injection(self):
        """测试注入自定义 settings"""
        custom_settings = Settings(sandbox_timeout=10)
        executor = SandboxExecutor(settings=custom_settings)
        assert executor.settings.sandbox_timeout == 10

    def test_subprocess_executor_injection(self):
        """测试注入自定义 subprocess_executor"""
        # 创建模拟的 subprocess_executor
        mock_executor = Mock(spec=SubprocessExecutor)
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "test output"
        mock_result.stderr = ""
        mock_executor.run.return_value = mock_result

        settings = Settings(sandbox_use_whitelist=False)
        executor = SandboxExecutor(settings=settings, subprocess_executor=mock_executor)
        result = executor.execute("echo test")

        # 验证 mock_executor 被调用
        assert mock_executor.run.called
        assert result.success is True
        assert result.stdout == "test output"


class TestSandboxExecutorWhitelist:
    """测试 SandboxExecutor 的白名单机制"""

    def setup_method(self):
        # 创建启用白名单的 settings
        self.settings = Settings(
            sandbox_use_whitelist=True,
            sandbox_command_whitelist="ls,echo,git status"
        )

    def test_whitelist_enabled_allow_listed_command(self):
        """测试白名单模式下允许白名单中的命令"""
        executor = SandboxExecutor(settings=self.settings)

        # 完全匹配的命令
        is_safe, reason = executor.is_command_safe("ls")
        assert is_safe is True, f"命令'ls'应该被允许，但失败原因: {reason}"

        # 带参数的命令
        is_safe, reason = executor.is_command_safe("ls -la")
        assert is_safe is True, f"命令'ls -la'应该被允许，但失败原因: {reason}"

        # 其他白名单命令
        is_safe, reason = executor.is_command_safe("echo hello")
        assert is_safe is True, f"命令'echo hello'应该被允许，但失败原因: {reason}"

    def test_whitelist_enabled_reject_non_listed_command(self):
        """测试白名单模式下拒绝白名单外的命令"""
        executor = SandboxExecutor(settings=self.settings)

        is_safe, reason = executor.is_command_safe("rm -rf /tmp")
        assert is_safe is False
        assert "不在白名单中" in reason

    def test_whitelist_enabled_empty_whitelist(self):
        """测试白名单模式下但白名单为空时拒绝所有命令"""
        settings = Settings(
            sandbox_use_whitelist=True,
            sandbox_command_whitelist=""
        )
        executor = SandboxExecutor(settings=settings)

        is_safe, reason = executor.is_command_safe("ls")
        assert is_safe is False
        assert "白名单为空" in reason

    def test_whitelist_disabled_uses_blacklist(self):
        """测试白名单模式禁用时使用黑名单"""
        settings = Settings(
            sandbox_use_whitelist=False,
            sandbox_command_whitelist="ls,echo"
        )
        executor = SandboxExecutor(settings=settings)

        # 不在白名单但在安全列表中的命令应该被允许
        is_safe, reason = executor.is_command_safe("date")
        assert is_safe is True, f"白名单禁用时，命令'date'应该被允许，但失败原因: {reason}"

        # 危险命令应该被拒绝
        is_safe, reason = executor.is_command_safe("rm -rf /")
        assert is_safe is False

    def test_whitelist_still_checks_dangerous_patterns(self):
        """测试白名单模式下仍会检查危险模式（额外安全层）"""
        settings = Settings(
            sandbox_use_whitelist=True,
            sandbox_command_whitelist="rm"  # 即使 rm 在白名单中
        )
        executor = SandboxExecutor(settings=settings)

        # 危险的 rm 命令仍应被拒绝
        is_safe, reason = executor.is_command_safe("rm -rf /")
        assert is_safe is False
        assert "危险操作模式" in reason

    def test_whitelist_rejects_compound_commands(self):
        """测试白名单模式下拒绝复合命令（如 ls; date）"""
        settings = Settings(
            sandbox_use_whitelist=True,
            sandbox_command_whitelist="ls,echo"
        )
        executor = SandboxExecutor(settings=settings)

        # 单一的白名单命令应该被允许
        is_safe, reason = executor.is_command_safe("ls -la")
        assert is_safe is True

        is_safe, reason = executor.is_command_safe("echo hello")
        assert is_safe is True

        # 复合命令应该被拒绝（使用非危险的命令 date，以便测试白名单而不是危险模式）
        compound_commands = [
            "ls; date",
            "ls && date",
            "ls || date",
            "ls | date",
            "(ls; date)",
            "{ ls; date; }",
            "ls `date`",
            'ls $(date)',
            'ls;whoami',
            'ls&&whoami',
        ]

        for cmd in compound_commands:
            is_safe, reason = executor.is_command_safe(cmd)
            assert is_safe is False, f"复合命令'{cmd}'应该被拒绝，但被允许了"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
