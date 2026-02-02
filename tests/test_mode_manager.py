"""Tests for ModeManager state machine."""

import threading
from src.mode.manager import ModeManager, InteractionMode, ModeState


class TestModeManagerBasics:
    def test_default_mode_is_smart(self):
        mgr = ModeManager()
        assert mgr.get_mode("chat1") == InteractionMode.SMART

    def test_enter_coco_mode(self):
        mgr = ModeManager()
        old = mgr.enter_coco_mode("chat1")
        assert old == InteractionMode.SMART
        assert mgr.get_mode("chat1") == InteractionMode.COCO

    def test_enter_claude_mode(self):
        mgr = ModeManager()
        old = mgr.enter_claude_mode("chat1")
        assert old == InteractionMode.SMART
        assert mgr.get_mode("chat1") == InteractionMode.CLAUDE

    def test_enter_shell_mode(self):
        mgr = ModeManager()
        old = mgr.enter_shell_mode("chat1")
        assert old == InteractionMode.SMART
        assert mgr.get_mode("chat1") == InteractionMode.SHELL

    def test_exit_to_smart(self):
        mgr = ModeManager()
        mgr.enter_coco_mode("chat1")
        old = mgr.exit_to_smart("chat1")
        assert old == InteractionMode.COCO
        assert mgr.get_mode("chat1") == InteractionMode.SMART

    def test_set_mode_returns_old(self):
        mgr = ModeManager()
        mgr.enter_coco_mode("chat1")
        old = mgr.set_mode("chat1", InteractionMode.CLAUDE)
        assert old == InteractionMode.COCO
        assert mgr.get_mode("chat1") == InteractionMode.CLAUDE


class TestModeManagerPredicates:
    def test_is_coco_mode(self):
        mgr = ModeManager()
        assert mgr.is_coco_mode("chat1") is False
        mgr.enter_coco_mode("chat1")
        assert mgr.is_coco_mode("chat1") is True

    def test_is_claude_mode(self):
        mgr = ModeManager()
        assert mgr.is_claude_mode("chat1") is False
        mgr.enter_claude_mode("chat1")
        assert mgr.is_claude_mode("chat1") is True

    def test_is_smart_mode(self):
        mgr = ModeManager()
        assert mgr.is_smart_mode("chat1") is True
        mgr.enter_coco_mode("chat1")
        assert mgr.is_smart_mode("chat1") is False

    def test_is_shell_mode(self):
        mgr = ModeManager()
        assert mgr.is_shell_mode("chat1") is False
        mgr.enter_shell_mode("chat1")
        assert mgr.is_shell_mode("chat1") is True

    def test_is_programming_mode(self):
        mgr = ModeManager()
        assert mgr.is_programming_mode("chat1") is False
        mgr.enter_coco_mode("chat1")
        assert mgr.is_programming_mode("chat1") is True
        mgr.exit_to_smart("chat1")
        mgr.enter_claude_mode("chat1")
        assert mgr.is_programming_mode("chat1") is True
        mgr.exit_to_smart("chat1")
        mgr.enter_shell_mode("chat1")
        assert mgr.is_programming_mode("chat1") is False


class TestModeManagerDisplayName:
    def test_display_names(self):
        mgr = ModeManager()
        assert "智能" in mgr.get_mode_display_name("chat1")
        mgr.enter_coco_mode("chat1")
        assert "Coco" in mgr.get_mode_display_name("chat1")
        mgr.set_mode("chat1", InteractionMode.CLAUDE)
        assert "Claude" in mgr.get_mode_display_name("chat1")
        mgr.enter_shell_mode("chat1")
        assert "Shell" in mgr.get_mode_display_name("chat1")


class TestModeManagerIsolation:
    def test_different_chats_independent(self):
        mgr = ModeManager()
        mgr.enter_coco_mode("chat1")
        mgr.enter_claude_mode("chat2")
        assert mgr.get_mode("chat1") == InteractionMode.COCO
        assert mgr.get_mode("chat2") == InteractionMode.CLAUDE
        assert mgr.get_mode("chat3") == InteractionMode.SMART

    def test_auto_entered_flag(self):
        mgr = ModeManager()
        mgr.enter_coco_mode("chat1", auto=True)
        with mgr._lock:
            state = mgr._modes["chat1"]
        assert state.auto_entered is True

        mgr.enter_coco_mode("chat1", auto=False)
        with mgr._lock:
            state = mgr._modes["chat1"]
        assert state.auto_entered is False


class TestModeManagerThreadSafety:
    def test_concurrent_mode_switches(self):
        mgr = ModeManager()
        errors = []

        def switch_modes(chat_id, iterations):
            try:
                for _ in range(iterations):
                    mgr.enter_coco_mode(chat_id)
                    mgr.exit_to_smart(chat_id)
                    mgr.enter_claude_mode(chat_id)
                    mgr.exit_to_smart(chat_id)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=switch_modes, args=(f"chat{i}", 50)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread safety errors: {errors}"
        # All chats should end in SMART
        for i in range(10):
            assert mgr.get_mode(f"chat{i}") == InteractionMode.SMART
