import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.sandbox.executor import ExecutionResult, SandboxExecutor


class TestSandboxExecutor:
    def setup_method(self):
        self.executor = SandboxExecutor()

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
        result = self.executor.execute("echo 'hello' | grep 'hello'")
        assert result.success is True
        assert "hello" in result.stdout

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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
