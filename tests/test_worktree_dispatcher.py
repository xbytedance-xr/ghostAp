from dataclasses import dataclass
from pathlib import Path

from src.worktree_engine.dispatcher import WorktreeDispatcher
from src.worktree_engine.models import WorktreeUnit, WorktreeSelectionItem


def test_dispatcher_plans_and_executes_units_without_cross_contamination(tmp_path):
    unit1_dir = tmp_path / "wt1"
    unit2_dir = tmp_path / "wt2"
    unit1_dir.mkdir()
    unit2_dir.mkdir()

    @dataclass
    class FakePromptResult:
        stop_reason: str
        text: str

    class FakeSession:
        def __init__(self, *, provider, tool_name, working_dir, model_name=None, ttadk_use_pty=False):
            self.provider = provider
            self.tool_name = tool_name
            self.working_dir = working_dir
            self.model_name = model_name

        def start(self, startup_timeout=60):
            return "session"

        def send_prompt(self, text, on_event=None, timeout=None):
            Path(self.working_dir, f"{self.tool_name}.txt").write_text(text, encoding="utf-8")
            return FakePromptResult(stop_reason="end_turn", text=f"done:{self.tool_name}")

        def close(self):
            return None

    dispatcher = WorktreeDispatcher(session_factory=lambda **kwargs: FakeSession(**kwargs))
    units = [
        WorktreeUnit(unit_id="u1", worktree_path=str(unit1_dir)),
        WorktreeUnit(unit_id="u2", worktree_path=str(unit2_dir)),
    ]
    tools = [
        WorktreeSelectionItem(provider="acp", tool_name="coco", display_name="Coco"),
        WorktreeSelectionItem(provider="ttadk", tool_name="codex", display_name="Codex"),
    ]

    planned = dispatcher.plan_user_goal("完成 worktree 模式的实现", units, tools)
    executed = dispatcher.execute_units(planned, max_workers=2)

    assert executed[0].task_title == "分析与方案"
    assert executed[1].task_title == "审查与汇总"
    assert "其它 worktree 单元并行执行" in executed[0].task_prompt
    assert "不要跨 worktree 修改" in executed[0].task_prompt
    assert executed[0].status == "completed"
    assert executed[1].status == "completed"
    assert (unit1_dir / "coco.txt").exists() or (unit1_dir / "codex.txt").exists()
    assert (unit2_dir / "coco.txt").exists() or (unit2_dir / "codex.txt").exists()


def test_execute_goal_single_unit_failure_others_continue(tmp_path):
    """T4: One unit fails, others complete — error isolation (AC9)."""
    dirs = [tmp_path / f"wt{i}" for i in range(3)]
    for d in dirs:
        d.mkdir()

    @dataclass
    class FakePromptResult:
        stop_reason: str
        text: str

    class FailingSession:
        """Session that fails for a specific tool_name."""

        def __init__(self, *, provider, tool_name, working_dir, model_name=None, ttadk_use_pty=False):
            self.tool_name = tool_name
            self.working_dir = working_dir

        def start(self, startup_timeout=60):
            return "ok"

        def send_prompt(self, text, on_event=None, timeout=None):
            if self.tool_name == "codex":
                raise RuntimeError("codex session crashed")
            Path(self.working_dir, f"{self.tool_name}.txt").write_text("ok", encoding="utf-8")
            return FakePromptResult(stop_reason="end_turn", text=f"done:{self.tool_name}")

        def close(self):
            return None

    units = [
        WorktreeUnit(unit_id="u0", worktree_path=str(dirs[0])),
        WorktreeUnit(unit_id="u1", worktree_path=str(dirs[1])),
        WorktreeUnit(unit_id="u2", worktree_path=str(dirs[2])),
    ]
    tools = [
        WorktreeSelectionItem(provider="acp", tool_name="coco", display_name="Coco"),
        WorktreeSelectionItem(provider="acp", tool_name="codex", display_name="Codex"),
        WorktreeSelectionItem(provider="acp", tool_name="gemini", display_name="Gemini"),
    ]

    dispatcher = WorktreeDispatcher(session_factory=lambda **kw: FailingSession(**kw))
    planned = dispatcher.plan_user_goal("implement feature", units, tools)
    executed = dispatcher.execute_units(planned, max_workers=3)

    by_name = {u.tool_name: u for u in executed}

    # codex should have failed
    assert by_name["codex"].status == "failed"
    assert "crashed" in by_name["codex"].error

    # coco and gemini should have completed successfully
    assert by_name["coco"].status == "completed"
    assert by_name["gemini"].status == "completed"


def test_plan_user_goal_smart_role_assignment():
    """T6: claude/gemini get analysis/review roles, codex gets implementation."""
    units = [
        WorktreeUnit(unit_id="u0"),
        WorktreeUnit(unit_id="u1"),
        WorktreeUnit(unit_id="u2"),
    ]
    tools = [
        WorktreeSelectionItem(provider="cli", tool_name="claude", display_name="Claude"),
        WorktreeSelectionItem(provider="acp", tool_name="codex", display_name="Codex"),
        WorktreeSelectionItem(provider="acp", tool_name="gemini", display_name="Gemini"),
    ]

    dispatcher = WorktreeDispatcher(session_factory=lambda **kw: None)
    planned = dispatcher.plan_user_goal("build feature X", units, tools)

    by_name = {u.tool_name: u for u in planned}

    # claude and gemini (reasoning) → analysis and review
    reasoning_roles = {by_name["claude"].task_title, by_name["gemini"].task_title}
    assert "分析与方案" in reasoning_roles
    assert "审查与汇总" in reasoning_roles
    # codex (general) → implementation
    assert "实现与修改" in by_name["codex"].task_title
    assert "不会和其它单元争用同一文件/接口契约" in by_name["codex"].task_prompt


def test_plan_user_goal_no_reasoning_tools_falls_back():
    """When no reasoning tools are present, fallback to positional assignment."""
    units = [
        WorktreeUnit(unit_id="u0"),
        WorktreeUnit(unit_id="u1"),
    ]
    tools = [
        WorktreeSelectionItem(provider="acp", tool_name="coco", display_name="Coco"),
        WorktreeSelectionItem(provider="acp", tool_name="codex", display_name="Codex"),
    ]

    dispatcher = WorktreeDispatcher(session_factory=lambda **kw: None)
    planned = dispatcher.plan_user_goal("build feature Y", units, tools)

    # Sequence: Analysis -> Implementation/Review (if count=2, it's Analysis -> Implementation or Review)
    # Our implementation: [Analysis, Implement/Review]
    assert planned[0].task_title == "分析与方案"
    assert "实现与修改" in planned[1].task_title or planned[1].task_title == "审查与汇总"


# ---------------------------------------------------------------------------
# TimeoutError handling tests
# ---------------------------------------------------------------------------

def _make_timeout_session_class(error_to_raise):
    """Return a FakeSession class whose send_prompt raises *error_to_raise*."""

    @dataclass
    class _FakeResult:
        stop_reason: str
        text: str

    class _Session:
        def __init__(self, *, provider, tool_name, working_dir, model_name=None, ttadk_use_pty=False):
            self.tool_name = tool_name

        def start(self, startup_timeout=60):
            return "ok"

        def send_prompt(self, text, on_event=None, timeout=None):
            raise error_to_raise

        def close(self):
            return None

    return _Session


def test_timeout_error_empty_message_unit_has_friendly_error(tmp_path):
    """TimeoutError('') → unit.error 非空且包含 '超时'."""
    d = tmp_path / "wt"
    d.mkdir()
    unit = WorktreeUnit(unit_id="u0", worktree_path=str(d))
    tool = WorktreeSelectionItem(provider="acp", tool_name="coco", display_name="Coco")
    
    session_cls = _make_timeout_session_class(TimeoutError(""))
    dispatcher = WorktreeDispatcher(session_factory=lambda **kw: session_cls(**kw))
    planned = dispatcher.plan_user_goal("test", [unit], [tool])
    executed = dispatcher.execute_units(planned, timeout=30)

    assert executed[0].status == "failed"
    assert executed[0].error  # non-empty
    assert "超时" in executed[0].error


def test_timeout_error_with_message_preserves_original(tmp_path):
    """TimeoutError('具体原因') → unit.error 保留原始消息."""
    d = tmp_path / "wt"
    d.mkdir()
    unit = WorktreeUnit(unit_id="u0", worktree_path=str(d))
    tool = WorktreeSelectionItem(provider="acp", tool_name="coco", display_name="Coco")

    session_cls = _make_timeout_session_class(TimeoutError("ACP prompt 执行超时 (120s)"))
    dispatcher = WorktreeDispatcher(session_factory=lambda **kw: session_cls(**kw))
    planned = dispatcher.plan_user_goal("test", [unit], [tool])
    executed = dispatcher.execute_units(planned, timeout=120)

    assert executed[0].status == "failed"
    assert "ACP prompt 执行超时 (120s)" in executed[0].error


def test_generic_exception_empty_message_has_fallback(tmp_path):
    """Exception('') → unit.error 为兜底文案而非空串."""
    d = tmp_path / "wt"
    d.mkdir()
    unit = WorktreeUnit(unit_id="u0", worktree_path=str(d))
    tool = WorktreeSelectionItem(provider="acp", tool_name="coco", display_name="Coco")

    session_cls = _make_timeout_session_class(Exception(""))
    dispatcher = WorktreeDispatcher(session_factory=lambda **kw: session_cls(**kw))
    planned = dispatcher.plan_user_goal("test", [unit], [tool])
    executed = dispatcher.execute_units(planned, timeout=30)

    assert executed[0].status == "failed"
    assert executed[0].error  # non-empty — fallback kicks in


def test_fail_unit_log_level_type_safety():
    """验证 _fail_unit 的 log_level 参数是 int 类型（类型安全），且不抛异常."""
    import logging
    from unittest.mock import MagicMock

    unit = WorktreeUnit(unit_id="u0")
    dispatcher = WorktreeDispatcher()
    on_update = MagicMock()

    # 测试 log_level=logging.WARNING (int)
    dispatcher._fail_unit(unit, "test error", log_level=logging.WARNING, on_unit_update=on_update)
    assert unit.status == "failed"
    assert unit.error == "test error"
    assert unit.summary == "test error"
    on_update.assert_called_once_with(unit)

    # 测试默认值 logging.ERROR
    unit2 = WorktreeUnit(unit_id="u1")
    on_update2 = MagicMock()
    dispatcher._fail_unit(unit2, "another error", on_unit_update=on_update2)
    assert unit2.status == "failed"
    on_update2.assert_called_once_with(unit2)


def test_fail_unit_logs_at_specified_level():
    """验证 _fail_unit 按 log_level 指定的级别记录日志（核心效果）。"""
    import logging
    from unittest.mock import MagicMock, patch

    unit = WorktreeUnit(unit_id="u0")
    dispatcher = WorktreeDispatcher()
    on_update = MagicMock()

    # 测试 log_level=logging.WARNING 确实调用 logger.warning
    with patch('src.worktree_engine.dispatcher.logger') as mock_logger:
        dispatcher._fail_unit(unit, "test error", log_level=logging.WARNING, on_unit_update=on_update)
        mock_logger.log.assert_called_once_with(logging.WARNING, "[Worktree] 单元失败: unit=%s, error=%s", "u0", "test error")

    # 测试默认值 logging.ERROR 确实调用 logger.error
    unit2 = WorktreeUnit(unit_id="u1")
    on_update2 = MagicMock()
    with patch('src.worktree_engine.dispatcher.logger') as mock_logger:
        dispatcher._fail_unit(unit2, "another error", on_unit_update=on_update2)
        mock_logger.log.assert_called_once_with(logging.ERROR, "[Worktree] 单元失败: unit=%s, error=%s", "u1", "another error")


# ---------------------------------------------------------------------------
# TTADK startup recovery tests
# ---------------------------------------------------------------------------


def test_ttadk_unit_invalid_model_retries_with_auto(tmp_path):
    """TTADK unit with invalid-model error retries with model_name=None."""
    from dataclasses import dataclass

    d = tmp_path / "wt"
    d.mkdir()

    @dataclass
    class FakePromptResult:
        stop_reason: str
        text: str

    call_log = []

    class RecoverySession:
        def __init__(self, *, provider, tool_name, working_dir, model_name=None, ttadk_use_pty=False):
            self.provider = provider
            self.tool_name = tool_name
            self.working_dir = working_dir
            self.model_name = model_name
            call_log.append(("create", provider, tool_name, model_name))

        def start(self, startup_timeout=60):
            # First call with model_name="bad-model" → raise invalid model error
            if self.model_name == "bad-model":
                call_log.append(("start_fail", self.model_name))
                raise RuntimeError("invalid value for --model: bad-model. model must be one of: gpt-4, gpt-5")
            call_log.append(("start_ok", self.model_name))
            return "ok"

        def send_prompt(self, text, on_event=None, timeout=None):
            return FakePromptResult(stop_reason="end_turn", text="done")

        def close(self):
            return None

    unit = WorktreeUnit(unit_id="u0", worktree_path=str(d))
    tool = WorktreeSelectionItem(provider="ttadk", tool_name="codex", display_name="Codex")

    dispatcher = WorktreeDispatcher(session_factory=lambda **kw: RecoverySession(**kw))
    planned = dispatcher.plan_user_goal("test task", [unit], [tool])
    # Override model_name to trigger invalid-model path
    planned[0].model_name = "bad-model"

    executed = dispatcher.execute_units(planned, max_workers=1)

    assert executed[0].status == "completed"
    # Verify: first create with bad-model, then retry with None (auto)
    creates = [entry for entry in call_log if entry[0] == "create"]
    assert creates[0] == ("create", "ttadk", "codex", "bad-model")  # original
    assert creates[1] == ("create", "ttadk", "codex", None)  # auto retry


def test_ttadk_unit_generic_error_fails_without_coco_fallback(tmp_path):
    """TTADK unit with non-model error must not degrade to ACP/coco."""
    from dataclasses import dataclass

    d = tmp_path / "wt"
    d.mkdir()

    @dataclass
    class FakePromptResult:
        stop_reason: str
        text: str

    call_log = []

    class FallbackSession:
        def __init__(self, *, provider, tool_name, working_dir, model_name=None, ttadk_use_pty=False):
            self.provider = provider
            self.tool_name = tool_name
            self.model_name = model_name
            call_log.append(("create", provider, tool_name))

        def start(self, startup_timeout=60):
            # TTADK codex always fails with generic error; creating ACP/coco would
            # indicate the forbidden final fallback path was taken.
            if self.provider == "ttadk" and self.tool_name == "codex":
                raise RuntimeError("connection refused")
            if self.provider == "acp" and self.tool_name == "coco":
                raise AssertionError("TTADK failure must not create ACP/coco fallback")
            return "ok"

        def send_prompt(self, text, on_event=None, timeout=None):
            return FakePromptResult(stop_reason="end_turn", text="coco-done")

        def close(self):
            return None

    unit = WorktreeUnit(unit_id="u0", worktree_path=str(d))
    tool = WorktreeSelectionItem(provider="ttadk", tool_name="codex", display_name="Codex")

    dispatcher = WorktreeDispatcher(session_factory=lambda **kw: FallbackSession(**kw))
    planned = dispatcher.plan_user_goal("test task", [unit], [tool])
    executed = dispatcher.execute_units(planned, max_workers=1)

    assert executed[0].status == "failed"
    assert "connection refused" in executed[0].summary
    assert "启动失败" in executed[0].summary
    # Verify coco fallback was not created
    coco_creates = [(p, t) for _, p, t in call_log if _ == "create" and t == "coco"]
    assert coco_creates == []


def test_ttadk_invalid_model_auto_retry_failure_does_not_fallback_to_coco(tmp_path):
    """Even after invalid-model auto retry fails, TTADK must not fall through to ACP/coco."""
    d = tmp_path / "wt"
    d.mkdir()
    call_log = []

    class FailingAutoRetrySession:
        def __init__(self, *, provider, tool_name, working_dir, model_name=None, ttadk_use_pty=False):
            self.provider = provider
            self.tool_name = tool_name
            self.model_name = model_name
            call_log.append(("create", provider, tool_name, model_name))

        def start(self, startup_timeout=60):
            if self.provider == "acp" and self.tool_name == "coco":
                raise AssertionError("TTADK invalid-model recovery must not create ACP/coco fallback")
            if self.model_name == "bad-model":
                raise RuntimeError("invalid value for --model: bad-model")
            raise RuntimeError("auto model also unavailable")

        def send_prompt(self, text, on_event=None, timeout=None):
            raise AssertionError("failed start should not prompt")

        def close(self):
            return None

    unit = WorktreeUnit(unit_id="u0", worktree_path=str(d))
    tool = WorktreeSelectionItem(provider="ttadk", tool_name="codex", display_name="Codex")

    dispatcher = WorktreeDispatcher(session_factory=lambda **kw: FailingAutoRetrySession(**kw))
    planned = dispatcher.plan_user_goal("test task", [unit], [tool])
    planned[0].model_name = "bad-model"
    executed = dispatcher.execute_units(planned, max_workers=1)

    assert executed[0].status == "failed"
    assert "invalid value for --model" in executed[0].summary
    assert ("create", "ttadk", "codex", "bad-model") in call_log
    assert ("create", "ttadk", "codex", None) in call_log
    assert not any(entry[1:3] == ("acp", "coco") for entry in call_log)


def test_non_ttadk_unit_no_recovery_on_start_failure(tmp_path):
    """Non-TTADK units do not get recovery; start failure is immediate."""
    d = tmp_path / "wt"
    d.mkdir()

    class FailingStartSession:
        def __init__(self, *, provider, tool_name, working_dir, model_name=None, ttadk_use_pty=False):
            self.provider = provider

        def start(self, startup_timeout=60):
            raise RuntimeError("acp startup failed")

        def send_prompt(self, text, on_event=None, timeout=None):
            raise AssertionError("should not reach send_prompt")

        def close(self):
            return None

    unit = WorktreeUnit(unit_id="u0", worktree_path=str(d))
    tool = WorktreeSelectionItem(provider="acp", tool_name="aiden", display_name="Aiden")

    dispatcher = WorktreeDispatcher(session_factory=lambda **kw: FailingStartSession(**kw))
    planned = dispatcher.plan_user_goal("test task", [unit], [tool])
    executed = dispatcher.execute_units(planned, max_workers=1)

    assert executed[0].status == "failed"
    assert "启动失败" in executed[0].error
    assert "acp startup failed" in executed[0].error
