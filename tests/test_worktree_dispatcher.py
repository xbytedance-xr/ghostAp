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
