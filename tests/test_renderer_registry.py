import pytest
from src.acp.renderer import ACPEventRenderer
from src.acp.models import ACPEvent, ACPEventType

def test_renderer_registry_dispatch():
    renderer = ACPEventRenderer()
    
    # Test text chunk
    event = ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="Hello world")
    content = renderer.process_event(event)
    assert "Hello world" in content
    
    # Test plan update
    from src.acp.models import PlanInfo, PlanEntryInfo
    plan = PlanInfo(entries=[PlanEntryInfo(content="Task 1", status="completed")])
    event = ACPEvent(event_type=ACPEventType.PLAN_UPDATE, plan=plan)
    content = renderer.process_event(event)
    assert "Task 1" in content
    assert "✅" in content

def test_renderer_reset():
    renderer = ACPEventRenderer()
    event = ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="Hello")
    renderer.process_event(event)
    assert "Hello" in renderer.text_content
    
    renderer.reset()
    assert renderer.text_content == ""
    assert renderer.completed_tool_count == 0


def test_renderer_reset_all_fields():
    """reset() must restore every field to its __init__ default value.

    Strategy: inject events that populate all 9 internal fields, verify they
    are non-default before reset(), then assert each one returns to its
    initial value after reset().
    """
    from src.acp.models import PlanInfo, PlanEntryInfo, ToolCallInfo

    renderer = ACPEventRenderer()

    # 1) TEXT_CHUNK → fills _text_chunks, _text_content
    renderer.process_event(
        ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="some text")
    )

    # 2) TOOL_CALL_START (tool-A, with locations) → fills _active_tools, _modified_files
    renderer.process_event(
        ACPEvent(
            event_type=ACPEventType.TOOL_CALL_START,
            tool_call=ToolCallInfo(
                id="tool-A", title="Read", kind="read",
                status="in_progress", locations=["src/a.py"],
            ),
        )
    )

    # 3) TOOL_CALL_DONE for tool-A (title, no content) → _completed_tool_count +1,
    #    starts a new _last_tool_run, pops tool-A from _active_tools
    renderer.process_event(
        ACPEvent(
            event_type=ACPEventType.TOOL_CALL_DONE,
            tool_call=ToolCallInfo(
                id="tool-A", title="Read", kind="read",
                status="completed", locations=["src/a.py"],
            ),
        )
    )

    # 4) TOOL_CALL_DONE for tool-B (same kind "read", no content) →
    #    hits aggregation branch → _text_dirty = True
    renderer.process_event(
        ACPEvent(
            event_type=ACPEventType.TOOL_CALL_DONE,
            tool_call=ToolCallInfo(
                id="tool-B", title="Read", kind="read",
                status="completed", locations=["src/b.py"],
            ),
        )
    )

    # 5) PLAN_UPDATE → fills _plan
    renderer.process_event(
        ACPEvent(
            event_type=ACPEventType.PLAN_UPDATE,
            plan=PlanInfo(entries=[PlanEntryInfo(content="Step 1", status="completed")]),
        )
    )

    # 6) TOOL_CALL_START (tool-C, with content) → fills _todo_content,
    #    and keeps tool-C in _active_tools so it stays non-empty
    renderer.process_event(
        ACPEvent(
            event_type=ACPEventType.TOOL_CALL_START,
            tool_call=ToolCallInfo(
                id="tool-C", title="TodoWrite", kind="other",
                status="in_progress", content="- [ ] item 1",
            ),
        )
    )

    # ── Pre-reset assertions: every field must be non-default ──
    # Note: _text_dirty is NOT checked here because process_event() internally
    # calls _render() → text_content getter, which always clears the dirty flag.
    # We still verify _text_dirty is reset to False in the post-reset section.
    assert len(renderer._text_chunks) > 0, "_text_chunks should be non-empty"
    assert renderer.text_content != "", "text_content should be non-empty"
    assert len(renderer._active_tools) > 0, "_active_tools should be non-empty"
    assert renderer.completed_tool_count > 0, "completed_tool_count should be > 0"
    assert renderer._plan is not None, "_plan should be set"
    assert len(renderer.modified_files) > 0, "modified_files should be non-empty"
    assert renderer.todo_content != "", "todo_content should be non-empty"
    assert renderer._last_tool_run is not None, "_last_tool_run should be set"

    # ── Reset ──
    renderer.reset()

    # ── Post-reset assertions: every field at __init__ default ──
    assert renderer._text_chunks == [], "_text_chunks not reset"
    assert renderer.text_content == "", "text_content not reset"
    assert renderer._text_dirty is False, "_text_dirty not reset"
    assert renderer._active_tools == {}, "_active_tools not reset"
    assert renderer.completed_tool_count == 0, "completed_tool_count not reset"
    assert renderer._plan is None, "_plan not reset"
    assert renderer.modified_files == set(), "modified_files not reset"
    assert renderer.todo_content == "", "todo_content not reset"
    assert renderer._last_tool_run is None, "_last_tool_run not reset"


def test_reset_does_not_mutate_prior_references():
    """reset() rebinds containers to new instances; old references stay intact."""
    from src.acp.models import ToolCallInfo

    renderer = ACPEventRenderer()

    # Populate state
    renderer.process_event(
        ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="hello")
    )
    renderer.process_event(
        ACPEvent(
            event_type=ACPEventType.TOOL_CALL_START,
            tool_call=ToolCallInfo(
                id="t1", title="Edit", kind="edit",
                status="in_progress", locations=["f.py"],
            ),
        )
    )

    # Capture references BEFORE reset
    old_chunks = renderer._text_chunks
    old_tools = renderer._active_tools
    old_files = renderer._modified_files

    assert len(old_chunks) > 0
    assert len(old_tools) > 0
    assert len(old_files) > 0

    renderer.reset()

    # Old references must still hold original data (not cleared)
    assert len(old_chunks) > 0, "old _text_chunks was mutated by reset()"
    assert len(old_tools) > 0, "old _active_tools was mutated by reset()"
    assert len(old_files) > 0, "old _modified_files was mutated by reset()"

    # Renderer's own containers are new empty instances
    assert renderer._text_chunks == []
    assert renderer._active_tools == {}
    assert renderer._modified_files == set()

    # Identity check: renderer now points to different objects
    assert renderer._text_chunks is not old_chunks
    assert renderer._active_tools is not old_tools
    assert renderer._modified_files is not old_files


def test_get_final_content_does_not_mutate_prior_active_tools_reference():
    """get_final_content() rebinds _active_tools; old reference stays intact."""
    from src.acp.models import ToolCallInfo

    renderer = ACPEventRenderer()

    renderer.process_event(
        ACPEvent(
            event_type=ACPEventType.TOOL_CALL_START,
            tool_call=ToolCallInfo(
                id="t1", title="Search", kind="search",
                status="in_progress", locations=["lib/"],
            ),
        )
    )

    old_tools = renderer._active_tools
    assert len(old_tools) == 1

    renderer.get_final_content()

    # Old reference must still hold original data
    assert len(old_tools) == 1, "old _active_tools was mutated by get_final_content()"
    # Renderer's container is a new empty dict
    assert renderer._active_tools == {}
    assert renderer._active_tools is not old_tools
