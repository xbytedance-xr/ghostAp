from dataclasses import dataclass
from pathlib import Path

from src.worktree_engine.dispatcher import WorktreeDispatcher
from src.worktree_engine.models import WorktreeUnit


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
        WorktreeUnit(
            unit_id="u1",
            selection_key="acp:coco:doubao",
            provider="acp",
            tool_name="coco",
            display_name="Coco",
            model_name="doubao",
            worktree_path=str(unit1_dir),
        ),
        WorktreeUnit(
            unit_id="u2",
            selection_key="ttadk:codex:gpt-5.2",
            provider="ttadk",
            tool_name="codex",
            display_name="Codex",
            model_name="gpt-5.2",
            worktree_path=str(unit2_dir),
        ),
    ]

    planned = dispatcher.plan_user_goal("完成 worktree 模式的实现", units)
    executed = dispatcher.execute_units(planned, max_workers=2)

    assert executed[0].task_title == "分析与方案"
    assert executed[1].task_title == "审查与汇总"
    assert executed[0].status == "completed"
    assert executed[1].status == "completed"
    assert executed[0].summary == "done:coco"
    assert executed[1].summary == "done:codex"
    assert (unit1_dir / "coco.txt").exists()
    assert (unit2_dir / "codex.txt").exists()
    assert not (unit1_dir / "codex.txt").exists()
    assert not (unit2_dir / "coco.txt").exists()


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
        WorktreeUnit(unit_id="u0", selection_key="acp:coco:d", provider="acp",
                     tool_name="coco", display_name="Coco", worktree_path=str(dirs[0])),
        WorktreeUnit(unit_id="u1", selection_key="acp:codex:d", provider="acp",
                     tool_name="codex", display_name="Codex", worktree_path=str(dirs[1])),
        WorktreeUnit(unit_id="u2", selection_key="acp:gemini:d", provider="acp",
                     tool_name="gemini", display_name="Gemini", worktree_path=str(dirs[2])),
    ]

    dispatcher = WorktreeDispatcher(session_factory=lambda **kw: FailingSession(**kw))
    planned = dispatcher.plan_user_goal("implement feature", units)
    executed = dispatcher.execute_units(planned, max_workers=3)

    by_name = {u.tool_name: u for u in executed}

    # codex should have failed
    assert by_name["codex"].status == "failed"
    assert "crashed" in by_name["codex"].error

    # coco and gemini should have completed successfully
    assert by_name["coco"].status == "completed"
    assert by_name["gemini"].status == "completed"
    assert (dirs[0] / "coco.txt").exists()
    assert (dirs[2] / "gemini.txt").exists()


def test_plan_user_goal_smart_role_assignment():
    """T6: claude/gemini get analysis/review roles, codex gets implementation."""
    units = [
        WorktreeUnit(unit_id="u0", selection_key="cli:claude:d", provider="cli",
                     tool_name="claude", display_name="Claude"),
        WorktreeUnit(unit_id="u1", selection_key="acp:codex:d", provider="acp",
                     tool_name="codex", display_name="Codex"),
        WorktreeUnit(unit_id="u2", selection_key="acp:gemini:d", provider="acp",
                     tool_name="gemini", display_name="Gemini"),
    ]

    dispatcher = WorktreeDispatcher(session_factory=lambda **kw: None)
    planned = dispatcher.plan_user_goal("build feature X", units)

    by_name = {u.tool_name: u for u in planned}

    # claude (reasoning) → analysis
    assert by_name["claude"].task_title == "分析与方案"
    # gemini (reasoning) → review
    assert by_name["gemini"].task_title == "审查与汇总"
    # codex (general) → implementation
    assert "实现与修改" in by_name["codex"].task_title


def test_plan_user_goal_no_reasoning_tools_falls_back():
    """When no reasoning tools are present, fallback to positional assignment."""
    units = [
        WorktreeUnit(unit_id="u0", selection_key="acp:coco:d", provider="acp",
                     tool_name="coco", display_name="Coco"),
        WorktreeUnit(unit_id="u1", selection_key="acp:codex:d", provider="acp",
                     tool_name="codex", display_name="Codex"),
    ]

    dispatcher = WorktreeDispatcher(session_factory=lambda **kw: None)
    planned = dispatcher.plan_user_goal("build feature Y", units)

    # First unit gets analysis, last gets review (positional fallback)
    assert planned[0].task_title == "分析与方案"
    assert planned[1].task_title == "审查与汇总"


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
    unit = WorktreeUnit(
        unit_id="u0", selection_key="acp:coco:d", provider="acp",
        tool_name="coco", display_name="Coco", worktree_path=str(d),
    )
    session_cls = _make_timeout_session_class(TimeoutError(""))
    dispatcher = WorktreeDispatcher(session_factory=lambda **kw: session_cls(**kw))
    planned = dispatcher.plan_user_goal("test", [unit])
    executed = dispatcher.execute_units(planned, timeout=30)

    assert executed[0].status == "failed"
    assert executed[0].error  # non-empty
    assert "超时" in executed[0].error


def test_timeout_error_with_message_preserves_original(tmp_path):
    """TimeoutError('具体原因') → unit.error 保留原始消息."""
    d = tmp_path / "wt"
    d.mkdir()
    unit = WorktreeUnit(
        unit_id="u0", selection_key="acp:coco:d", provider="acp",
        tool_name="coco", display_name="Coco", worktree_path=str(d),
    )
    session_cls = _make_timeout_session_class(TimeoutError("ACP prompt 执行超时 (120s)"))
    dispatcher = WorktreeDispatcher(session_factory=lambda **kw: session_cls(**kw))
    planned = dispatcher.plan_user_goal("test", [unit])
    executed = dispatcher.execute_units(planned, timeout=120)

    assert executed[0].status == "failed"
    assert "ACP prompt 执行超时 (120s)" in executed[0].error


def test_generic_exception_empty_message_has_fallback(tmp_path):
    """Exception('') → unit.error 为兜底文案而非空串."""
    d = tmp_path / "wt"
    d.mkdir()
    unit = WorktreeUnit(
        unit_id="u0", selection_key="acp:coco:d", provider="acp",
        tool_name="coco", display_name="Coco", worktree_path=str(d),
    )
    session_cls = _make_timeout_session_class(Exception(""))
    dispatcher = WorktreeDispatcher(session_factory=lambda **kw: session_cls(**kw))
    planned = dispatcher.plan_user_goal("test", [unit])
    executed = dispatcher.execute_units(planned, timeout=30)

    assert executed[0].status == "failed"
    assert executed[0].error  # non-empty — fallback kicks in
