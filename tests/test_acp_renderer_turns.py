from src.acp.models import ACPEvent, ACPEventType, ToolCallInfo
from src.acp.renderer import ACPEventRenderer


def _event(event_type, **kwargs):
    return ACPEvent(event_type=event_type, **kwargs)


def _tool(tool_id, title, status="in_progress"):
    return ToolCallInfo(
        id=tool_id,
        title=title,
        kind="read",
        status=status,
        locations=["src/main.py"],
    )


def test_snapshot_turns_groups_reasoning_then_tool():
    renderer = ACPEventRenderer()

    renderer.process_event(_event(ACPEventType.TEXT_CHUNK, text="先检查入口"))
    renderer.process_event(_event(ACPEventType.TOOL_CALL_START, tool_call=_tool("t1", "Read main")))
    renderer.process_event(
        _event(
            ACPEventType.TOOL_CALL_DONE,
            tool_call=_tool("t1", "Read main", status="completed"),
        )
    )

    turns = renderer.snapshot_turns()

    assert len(turns) == 1
    assert turns[0].reasoning == "先检查入口"
    assert turns[0].tools[0].id == "t1"
    assert turns[0].tools[0].status == "completed"


def test_text_after_tool_starts_a_new_turn():
    renderer = ACPEventRenderer()

    renderer.process_event(_event(ACPEventType.TEXT_CHUNK, text="先读文件"))
    renderer.process_event(_event(ACPEventType.TOOL_CALL_DONE, tool_call=_tool("t1", "Read", status="completed")))
    renderer.process_event(_event(ACPEventType.TEXT_CHUNK, text="再修改"))
    renderer.process_event(_event(ACPEventType.TOOL_CALL_DONE, tool_call=_tool("t2", "Edit", status="completed")))

    turns = renderer.snapshot_turns()

    assert [turn.reasoning for turn in turns] == ["先读文件", "再修改"]
    assert [turn.tools[0].id for turn in turns] == ["t1", "t2"]


def test_reset_clears_turn_snapshots():
    renderer = ACPEventRenderer()
    renderer.process_event(_event(ACPEventType.TEXT_CHUNK, text="old"))
    renderer.process_event(_event(ACPEventType.TOOL_CALL_DONE, tool_call=_tool("t1", "Read", status="completed")))

    renderer.reset()

    assert renderer.snapshot_turns() == ()


def test_snapshot_turns_does_not_change_legacy_markdown_output():
    renderer = ACPEventRenderer()
    renderer.process_event(_event(ACPEventType.TEXT_CHUNK, text="hello"))
    before = renderer.process_event(
        _event(ACPEventType.TOOL_CALL_DONE, tool_call=_tool("t1", "Read", status="completed"))
    )

    _ = renderer.snapshot_turns()
    after = renderer.process_event(_event(ACPEventType.TEXT_CHUNK, text=" world"))

    assert "hello" in before
    assert "hello" in after
    assert "world" in after
