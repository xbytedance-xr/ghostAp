"""Tests for slock activation failure user feedback.

Covers:
- Activation failure sends user-friendly card notification
- NEEDS_ACTIVATION branch auto-activates or shows friendly message
- build_activation_denied_card uses user-friendly language
"""

from __future__ import annotations


class TestActivationDeniedCardUserFriendly:
    """Test that activation denied card uses user-friendly language."""

    def test_activation_denied_card_no_technical_terms(self):
        """Card should not contain '白名单', '协作模式', '激活' etc."""
        from src.slock_engine.card_templates.queue_feedback import build_activation_denied_card

        card = build_activation_denied_card(
            reason="权限不足",
            hint="自动激活被拒绝",
        )

        # Convert card to string for checking
        import json
        card_str = json.dumps(card, ensure_ascii=False)

        # Should NOT contain technical jargon
        assert "白名单" not in card_str
        assert "协作模式" not in card_str

        # SHOULD contain user-friendly content
        assert "暂时无法处理" in card_str or "无法自动处理" in card_str

    def test_activation_denied_card_has_actionable_hints(self):
        """Card should provide actionable suggestions."""
        from src.slock_engine.card_templates.queue_feedback import build_activation_denied_card

        card = build_activation_denied_card(reason="test")
        import json
        card_str = json.dumps(card, ensure_ascii=False)

        # Should have actionable advice
        assert "联系群管理员" in card_str or "描述你的需求" in card_str


class TestNeedsActivationAutoActivate:
    """Test that NEEDS_ACTIVATION branch auto-activates in passive mode."""

    def test_needs_activation_branch_does_not_require_slock_command(self):
        """The message should NOT ask user to run /slock."""
        # The new behavior is: auto-activate if possible, otherwise show friendly message
        # The key assertion: the old message "请使用 `/slock` 激活后重试" should NOT appear

        # Check that the dispatcher code no longer contains the old message
        import inspect

        from src.feishu import dispatcher
        source = inspect.getsource(dispatcher)

        # The old problematic message should NOT be in the source
        assert "请使用 `/slock` 激活后重试" not in source


class TestNoAgentAvailableCard:
    """Test build_no_agent_available_card has actionable hints."""

    def test_no_agent_card_mentions_slock_default_roles(self):
        """Card should hint at checking SLOCK_DEFAULT_ROLES config."""
        from src.slock_engine.card_templates.queue_feedback import build_no_agent_available_card

        card = build_no_agent_available_card(
            team_name="test_team",
            hint="请检查 SLOCK_DEFAULT_ROLES 配置，或使用 /new-role 手动创建角色。",
        )
        import json
        card_str = json.dumps(card, ensure_ascii=False)

        # Should contain actionable hints
        assert "SLOCK_DEFAULT_ROLES" in card_str or "/new-role" in card_str


class TestReadmeDocs:
    """Test that README contains default role documentation."""

    def test_readme_contains_default_roles_section(self):
        """README should have a section explaining default roles."""
        with open("README.md", "r", encoding="utf-8") as f:
            content = f.read()

        # Should mention the default roles
        assert "planner" in content.lower() or "规划师" in content
        assert "coder" in content.lower() or "编码师" in content
        assert "reviewer" in content.lower() or "审查员" in content
        assert "tester" in content.lower() or "测试员" in content

    def test_readme_contains_slock_default_roles_example(self):
        """README should show how to configure SLOCK_DEFAULT_ROLES."""
        with open("README.md", "r", encoding="utf-8") as f:
            content = f.read()

        assert "SLOCK_DEFAULT_ROLES" in content
