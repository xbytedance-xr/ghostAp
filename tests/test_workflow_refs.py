"""Regression tests for workflow reference (sub-workflow) interactivity.

Coverage:
- Confirm card renders workflow refs with preview/remove/add buttons
- handle_workflow_view_workflow_ref returns a preview card for an existing ref
- handle_workflow_view_workflow_ref rejects invalid session keys, senders, and indexes
- handle_workflow_remove_workflow_ref removes a ref and re-renders the confirm card
- handle_workflow_remove_workflow_ref rejects out-of-range ref_index
- handle_workflow_add_workflow_ref adds a template and re-renders the confirm card
- handle_workflow_add_workflow_ref deduplicates duplicate template names
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from src.workflow_engine.models import PendingConfirmation, WorkflowProject, WorkflowStatus


def _make_handler_with_pending(
    tmp_path: Path,
    meta: dict[str, Any] | None = None,
    status: WorkflowStatus = WorkflowStatus.AWAITING_CONFIRM,
) -> tuple[Any, Any, str, str]:
    """Build a WorkflowHandler wired with a pending project + meta."""
    from src.feishu.handlers.workflow import WorkflowHandler

    project_root = tmp_path / "proj"
    project_root.mkdir(parents=True, exist_ok=True)

    project = MagicMock()
    project.project_id = "bound_project"
    project.root_path = str(project_root)

    ctx = MagicMock()
    ctx.project_manager.get_project_for_chat.return_value = project

    pending = PendingConfirmation(
        script_path=None,
        requirement="test requirement",
        meta=meta or {"workflow_refs": [{"name": "ref-a", "path": ""}]},
        is_fallback=False,
        initiator_user_id="sender-123",
        engine_session_key="session_abc",
        selected_tools=["coco"],
    )

    engine = MagicMock()
    engine.project = WorkflowProject(
        status=status,
        pending=pending,
    )
    ctx.workflow_engine_manager.get.return_value = engine

    handler = WorkflowHandler.__new__(WorkflowHandler)
    handler.ctx = ctx

    # Minimal wiring for helpers used by the confirm card builder.
    handler._validate_tools_against_registry = MagicMock(return_value=(["coco"], []))
    handler._resolve_tool_lists = MagicMock(return_value=(["coco"], ["coco"], [], ["coco"]))
    handler._get_root_path = MagicMock(return_value=str(project_root))

    chat_id = "chat_1"
    return handler, engine, chat_id, "bound_project"


@patch("src.thread.get_current_sender_id", return_value="sender-123")
def test_confirm_card_renders_workflow_ref_buttons(_mock_sender, tmp_path):
    """Confirm card renders the workflow refs list + preview/remove/add buttons."""
    handler, engine, chat_id, project_id = _make_handler_with_pending(
        tmp_path,
        meta={
            "workflow_refs": [
                {"name": "ref-a", "path": "/tmp/a.js"},
                {"name": "ref-b", "path": "/tmp/b.js"},
            ],
        },
    )

    card = handler._build_confirm_card(
        meta=engine.project.pending.meta,
        requirement=engine.project.pending.requirement,
        engine_session_key=engine.project.pending.engine_session_key,
        chat_id=chat_id,
        project_id=project_id,
        is_fallback=False,
        selected_tools=engine.project.pending.selected_tools,
        script_content="",
    )

    assert card is not None
    payload = card.get("card", card) if isinstance(card, dict) else card
    card_json = _card_to_text(payload)

    # Each ref is shown with its name + a button row (预览/移除).
    assert "ref-a" in card_json
    assert "ref-b" in card_json
    assert "workflow_view_workflow_ref" in card_json
    assert "workflow_remove_workflow_ref" in card_json
    # The add button is always present regardless of existing refs.
    assert "workflow_add_workflow_ref" in card_json


@patch("src.thread.get_current_sender_id", return_value="sender-123")
def test_handle_workflow_view_workflow_ref_returns_preview_card(_mock, tmp_path):
    """Preview handler returns an info card with ref metadata."""

    handler, engine, chat_id, project_id = _make_handler_with_pending(
        tmp_path,
        meta={"workflow_refs": [{"name": "ref-a", "path": ""}]},
    )

    captured = {}

    def _update(message_id: str, card: Any):
        captured["last_card"] = card

    handler.update_card = _update

    handler.handle_workflow_view_workflow_ref(
        "msg_id",
        chat_id,
        project_id,
        {
            "action": "workflow_view_workflow_ref",
            "ref_index": 0,
            "chat_id": chat_id,
            "project_id": project_id,
            "engine_session_key": "session_abc",
        },
    )

    rendered = _card_to_text(captured.get("last_card", {}))
    assert "ref-a" in rendered
    # Preview handler must not mutate pending state.
    refs = (engine.project.pending.meta or {}).get("workflow_refs", [])
    assert len(refs) == 1


@patch("src.thread.get_current_sender_id", return_value="sender-123")
def test_handle_workflow_view_workflow_ref_rejects_invalid_index(_mock, tmp_path):
    """ref_index out of range produces invalid_argument rather than crashing."""
    handler, engine, chat_id, project_id = _make_handler_with_pending(
        tmp_path,
        meta={"workflow_refs": [{"name": "ref-a"}]},
    )

    with patch.object(handler, "_reply_workflow_error") as mock_err:
        handler.handle_workflow_view_workflow_ref(
            "msg_id",
            chat_id,
            project_id,
            {
                "action": "workflow_view_workflow_ref",
                "ref_index": 5,  # out of range
                "chat_id": chat_id,
                "project_id": project_id,
                "engine_session_key": "session_abc",
            },
        )
        mock_err.assert_called_once()
        assert mock_err.call_args.args[1] == "invalid_argument"


@patch("src.thread.get_current_sender_id", return_value="sender-123")
def test_handle_workflow_view_workflow_ref_rejects_bad_session(_mock, tmp_path):
    """Session key mismatch triggers session_expired."""
    handler, engine, chat_id, project_id = _make_handler_with_pending(tmp_path)

    with patch.object(handler, "_reply_workflow_error") as mock_err:
        handler.handle_workflow_view_workflow_ref(
            "msg_id",
            chat_id,
            project_id,
            {
                "action": "workflow_view_workflow_ref",
                "ref_index": 0,
                "chat_id": chat_id,
                "project_id": project_id,
                "engine_session_key": "wrong_key",
            },
        )
        mock_err.assert_called_once()
        assert mock_err.call_args.args[1] == "session_expired"


@patch("src.thread.get_current_sender_id", return_value="attacker")
def test_handle_workflow_view_workflow_ref_rejects_other_user(_mock, tmp_path):
    """Different sender is rejected as forbidden."""
    handler, engine, chat_id, project_id = _make_handler_with_pending(tmp_path)

    with patch.object(handler, "_reply_workflow_error") as mock_err:
        handler.handle_workflow_view_workflow_ref(
            "msg_id",
            chat_id,
            project_id,
            {
                "action": "workflow_view_workflow_ref",
                "ref_index": 0,
                "chat_id": chat_id,
                "project_id": project_id,
                "engine_session_key": "session_abc",
            },
        )
        mock_err.assert_called_once()
        assert mock_err.call_args.args[1] == "forbidden"


@patch("src.thread.get_current_sender_id", return_value="sender-123")
def test_handle_workflow_remove_workflow_ref_removes_ref(_mock, tmp_path):
    """Remove handler removes the ref and triggers a re-render."""
    handler, engine, chat_id, project_id = _make_handler_with_pending(
        tmp_path,
        meta={"workflow_refs": [{"name": "ref-a"}, {"name": "ref-b"}]},
    )

    # Capture the re-rendered card.
    captured = {}

    def _update(message_id: str, card: Any):
        captured["last_card"] = card

    handler.update_card = _update

    handler.handle_workflow_remove_workflow_ref(
        "msg_id",
        chat_id,
        project_id,
        {
            "action": "workflow_remove_workflow_ref",
            "ref_index": 0,
            "chat_id": chat_id,
            "project_id": project_id,
            "engine_session_key": "session_abc",
        },
    )

    refs = (engine.project.pending.meta or {}).get("workflow_refs", [])
    assert len(refs) == 1
    assert refs[0].get("name") == "ref-b"
    assert "last_card" in captured


@patch("src.thread.get_current_sender_id", return_value="sender-123")
def test_handle_workflow_remove_workflow_ref_rejects_bad_index(_mock, tmp_path):
    """Remove handler rejects out-of-range indexes."""
    handler, engine, chat_id, project_id = _make_handler_with_pending(
        tmp_path,
        meta={"workflow_refs": [{"name": "ref-a"}]},
    )

    with patch.object(handler, "_reply_workflow_error") as mock_err:
        handler.handle_workflow_remove_workflow_ref(
            "msg_id",
            chat_id,
            project_id,
            {
                "action": "workflow_remove_workflow_ref",
                "ref_index": 99,
                "chat_id": chat_id,
                "project_id": project_id,
                "engine_session_key": "session_abc",
            },
        )
        mock_err.assert_called_once()
        assert mock_err.call_args.args[1] == "invalid_argument"


@patch("src.thread.get_current_sender_id", return_value="sender-123")
def test_handle_workflow_add_workflow_ref_appends_ref(_mock, tmp_path):
    """Add handler appends the chosen template to meta.workflow_refs."""

    handler, engine, chat_id, project_id = _make_handler_with_pending(
        tmp_path,
        meta={"workflow_refs": []},
    )

    captured = {}

    def _update(message_id: str, card: Any):
        captured["last_card"] = card

    handler.update_card = _update

    fake_path = tmp_path / "builtin.js"
    fake_path.write_text(
        "export const meta = { description: 'built-in' };\nexport async function main(){}\n",
        encoding="utf-8",
    )

    def _fake_load(root: str, name: str, *, user_id: str | None = None) -> str:
        return str(fake_path)

    with patch(
        "src.workflow_engine.templates.load_template",
        side_effect=_fake_load,
    ):
        handler.handle_workflow_add_workflow_ref(
            "msg_id",
            chat_id,
            project_id,
            {
                "action": "workflow_add_workflow_ref",
                "template_name": "code-audit",
                "chat_id": chat_id,
                "project_id": project_id,
                "engine_session_key": "session_abc",
            },
        )

    refs = (engine.project.pending.meta or {}).get("workflow_refs", [])
    assert any(ref.get("name") == "code-audit" for ref in refs)
    assert "last_card" in captured


@patch("src.thread.get_current_sender_id", return_value="sender-123")
def test_handle_workflow_add_workflow_ref_dedupes_duplicates(_mock, tmp_path):
    """Adding the same template name twice does not duplicate."""
    from src.workflow_engine.templates import TemplateInfo

    handler, engine, chat_id, project_id = _make_handler_with_pending(
        tmp_path,
        meta={"workflow_refs": [{"name": "code-audit", "path": "", "description": ""}]},
    )

    fake_path = tmp_path / "b.js"
    fake_path.write_text(
        "export const meta = { description: 'built-in' };\n",
        encoding="utf-8",
    )

    def _fake_discover(root: str, *, user_id: str | None = None):
        return [TemplateInfo(name="code-audit", path=str(fake_path), description="built-in", scope="builtin")]

    def _fake_load(root: str, name: str, *, user_id: str | None = None) -> str:
        return str(fake_path)

    with patch(
        "src.workflow_engine.templates.discover_templates",
        side_effect=_fake_discover,
    ), patch(
        "src.workflow_engine.templates.load_template",
        side_effect=_fake_load,
    ):
        handler.handle_workflow_add_workflow_ref(
            "msg_id",
            chat_id,
            project_id,
            {
                "action": "workflow_add_workflow_ref",
                "template_name": "code-audit",
                "chat_id": chat_id,
                "project_id": project_id,
                "engine_session_key": "session_abc",
            },
        )

    refs = (engine.project.pending.meta or {}).get("workflow_refs", [])
    assert len(refs) == 1


@patch("src.thread.get_current_sender_id", return_value="sender-123")
def test_handle_workflow_add_workflow_ref_shows_selector_when_no_template(_mock, tmp_path):
    """When template_name is missing, the handler shows a template selector."""
    from src.workflow_engine.templates import TemplateInfo

    handler, engine, chat_id, project_id = _make_handler_with_pending(tmp_path)

    def _fake_discover(root: str, *, user_id: str | None = None):
        return [TemplateInfo(name="code-audit", path="/fake/a.js", description="d", scope="builtin")]

    captured = {}

    def _update(message_id: str, card: Any):
        captured["last_card"] = card

    handler.update_card = _update

    with patch(
        "src.workflow_engine.templates.discover_templates",
        side_effect=_fake_discover,
    ):
        handler.handle_workflow_add_workflow_ref(
            "msg_id",
            chat_id,
            project_id,
            {
                "action": "workflow_add_workflow_ref",
                "chat_id": chat_id,
                "project_id": project_id,
                "engine_session_key": "session_abc",
            },
        )

    rendered = _card_to_text(captured.get("last_card", {}))
    assert "子 Workflow" in rendered


def test_forwarding_map_has_ref_handlers():
    """router.FORWARDING_MAP must expose the three ref handlers."""
    from src.feishu import router

    for key in (
        "_handle_workflow_view_workflow_ref",
        "_handle_workflow_remove_workflow_ref",
        "_handle_workflow_add_workflow_ref",
    ):
        assert key in router.FORWARDING_MAP, f"{key} missing from FORWARDING_MAP"


def test_action_constants_match_handler_registration_names():
    """dispatch.py WORKFLOW_* constants match forwarder names and are in
    the action registry."""
    from src.card.actions.dispatch import (
        WORKFLOW_ADD_WORKFLOW_REF,
        WORKFLOW_REMOVE_WORKFLOW_REF,
        WORKFLOW_VIEW_WORKFLOW_REF,
    )

    # Action ID strings must be snake_case and consistent across files.
    assert WORKFLOW_VIEW_WORKFLOW_REF == "workflow_view_workflow_ref"
    assert WORKFLOW_REMOVE_WORKFLOW_REF == "workflow_remove_workflow_ref"
    assert WORKFLOW_ADD_WORKFLOW_REF == "workflow_add_workflow_ref"


def _card_to_text(payload: Any) -> str:
    """Flatten a Feishu card dict/list into one searchable string for assertions."""
    if payload is None:
        return ""
    if isinstance(payload, dict):
        pieces = []
        for k, v in payload.items():
            pieces.append(str(k))
            pieces.append(_card_to_text(v))
        return " ".join(pieces)
    if isinstance(payload, (list, tuple)):
        return " ".join(_card_to_text(x) for x in payload)
    return str(payload)


@patch("src.thread.get_current_sender_id", return_value="sender-123")
def test_handle_workflow_view_workflow_ref_name_only_shows_preview(_mock, tmp_path):
    """A ref that only declares a name (no path) must still resolve to a
    preview card containing the script content — the handler must not
    treat ``load_template``'s return value as a path to ``open()``.
    """
    handler, engine, chat_id, project_id = _make_handler_with_pending(
        tmp_path,
        meta={"workflow_refs": [{"name": "named-ref", "path": ""}]},
    )

    # Simulate the template registry: place a minimal JS file under the
    # project's templates dir so ``resolve_template_path`` finds it.
    root = Path(engine.project.pending.meta["_root_path"]) if False else Path(
        handler._get_root_path(chat_id, engine.project)
    )
    # build_template resolution path: ``root/.ghostap/workflows/<name>.js``
    template_dir = root / ".ghostap" / "workflows"
    template_dir.mkdir(parents=True, exist_ok=True)
    template_file = template_dir / "named-ref.js"
    template_file.write_text(
        "// meta: {\"description\": \"named template\", \"tools\": [\"coco\"]}\n"
        "const x = 1;\n",
        encoding="utf-8",
    )

    captured: dict[str, Any] = {}

    def _update(message_id: str, card: Any) -> None:
        captured["last_card"] = card

    handler.update_card = _update

    handler.handle_workflow_view_workflow_ref(
        "msg_id",
        chat_id,
        project_id,
        {
            "action": "workflow_view_workflow_ref",
            "ref_index": 0,
            "chat_id": chat_id,
            "project_id": project_id,
            "engine_session_key": "session_abc",
        },
    )

    rendered = _card_to_text(captured.get("last_card", {}))
    assert "named-ref" in rendered
    # Must surface either script content / description / tools — not only
    # the name and an "unresolved" path marker.
    assert (
        "named template" in rendered
        or "const x" in rendered
        or "coco" in rendered
    )


@patch("src.thread.get_current_sender_id", return_value="sender-123")
def test_handle_workflow_view_workflow_ref_explicit_path(_mock, tmp_path):
    """A ref with an explicit path must read the file content directly
    without going through the template registry.
    """
    handler, engine, chat_id, project_id = _make_handler_with_pending(tmp_path)

    script_file = tmp_path / "explicit.js"
    script_file.write_text(
        "// meta: {\"description\": \"explicit ref\", \"tools\": [\"claude\"]}\n"
        "async function main() { return 1; }\n",
        encoding="utf-8",
    )

    engine.project.pending.meta = {  # type: ignore[union-attr]
        "workflow_refs": [
            {"name": "explicit-ref", "path": str(script_file)},
        ],
    }

    captured: dict[str, Any] = {}

    def _update(message_id: str, card: Any) -> None:
        captured["last_card"] = card

    handler.update_card = _update

    handler.handle_workflow_view_workflow_ref(
        "msg_id",
        chat_id,
        project_id,
        {
            "action": "workflow_view_workflow_ref",
            "ref_index": 0,
            "chat_id": chat_id,
            "project_id": project_id,
            "engine_session_key": "session_abc",
        },
    )

    rendered = _card_to_text(captured.get("last_card", {}))
    assert "explicit-ref" in rendered
    assert "explicit ref" in rendered or "main" in rendered or "claude" in rendered


# ---------------------------------------------------------------------------
# Bridge-level: sub-workflow tool preflight
# ---------------------------------------------------------------------------


def test_runtime_bridge_reads_template_meta_and_rejects_missing_tools(tmp_path):
    """Verify the bridge can (1) read a template's meta.tools via
    extract_meta_from_script, (2) detect tools outside the parent allowlist,
    and (3) surface that as a structured -32004 error payload.
    """
    from src.workflow_engine.script_gen import extract_meta_from_script

    template = tmp_path / "bad-subflow.js"
    template.write_text(
        'export const meta = {"tools": ["coco", "extra_tool"], "description": "needs extra tool"};\n'
        "await agent({tool: 'coco', prompt: 'hello'});\n",
        encoding="utf-8",
    )

    meta = extract_meta_from_script(template.read_text(encoding="utf-8")) or {}
    assert set(meta.get("tools", [])) == {"coco", "extra_tool"}

    # Now drive the bridge's workflow_call path end-to-end: the bridge will
    # reject calls providing `script_path` directly, but we can simulate the
    # same preflight logic by injecting a `name`-only reference that the
    # bridge resolves via its own cwd.
    from src.workflow_engine.bridge import RuntimeBridge

    template_dir = tmp_path / ".ghostap" / "workflows"
    template_dir.mkdir(parents=True, exist_ok=True)
    located = template_dir / "bad-subflow.js"
    located.write_text(
        'export const meta = {"tools": ["coco", "extra_tool"], "description": "needs extra tool"};\n'
        "await agent({tool: 'coco', prompt: 'hello'});\n",
        encoding="utf-8",
    )

    captured: list[dict] = []
    bridge = RuntimeBridge(
        script_path=str(tmp_path / "placeholder.js"),
        cwd=str(tmp_path),
        allowed_tools=["coco"],
        on_agent_call=lambda *a, **kw: None,
        on_phase=lambda *a, **kw: None,
        on_log=lambda *a, **kw: None,
    )
    bridge._send = lambda payload: captured.append(payload)

    bridge._handle_workflow_call({"name": "bad-subflow"}, request_id="req-1")

    assert captured, "bridge should have sent an error response"
    # Preflight error wins over any downstream failure.
    preflight = next(
        (c["error"] for c in captured if isinstance(c, dict) and c.get("error", {}).get("code") == -32004),
        None,
    )
    assert preflight is not None, f"no -32004 tool preflight among {captured}"
    data = preflight.get("data") or {}
    assert data.get("kind") == "missing_tools"
    assert "extra_tool" in data.get("missing_tools", [])
    assert "coco" in data.get("allowed_tools", [])


def test_runtime_bridge_accepts_sub_workflow_when_tools_match(tmp_path):
    """When the template's declared tools are a subset of the parent
    allowlist, the tool preflight in workflow_call does NOT emit a -32004
    error.
    """
    from src.workflow_engine.bridge import RuntimeBridge

    template_dir = tmp_path / ".ghostap" / "workflows"
    template_dir.mkdir(parents=True, exist_ok=True)
    (template_dir / "ok-subflow.js").write_text(
        'export const meta = {"tools": ["coco"], "description": "uses coco only"};\n'
        "await agent({tool: 'coco', prompt: 'hi'});\n",
        encoding="utf-8",
    )

    captured: list[dict] = []
    bridge = RuntimeBridge(
        script_path=str(tmp_path / "placeholder.js"),
        cwd=str(tmp_path),
        allowed_tools=["coco", "claude"],
        on_agent_call=lambda *a, **kw: None,
        on_phase=lambda *a, **kw: None,
        on_log=lambda *a, **kw: None,
    )
    bridge._send = lambda payload: captured.append(payload)

    try:
        bridge._handle_workflow_call({"name": "ok-subflow"}, request_id="req-2")
    except Exception:  # noqa: BLE001 — subprocess/IO failures are out of scope
        pass

    tool_mismatch_messages = [
        c["error"] for c in captured
        if isinstance(c, dict) and c.get("error", {}).get("code") == -32004
    ]
    assert tool_mismatch_messages == [], (
        f"unexpected tool-mismatch rejection: {tool_mismatch_messages}"
    )


# ---------------------------------------------------------------------------
# Handler-level: preview card surfaces missing_tools
# ---------------------------------------------------------------------------


def test_handle_workflow_view_workflow_ref_surfaces_missing_tools(tmp_path):
    """When a sub-workflow ref declares tools not in parent selected_tools,
    the preview card must include a 工具缺失警告 line.
    """
    from src.feishu.handlers.workflow import WorkflowHandler

    project_root = tmp_path / "proj"
    project_root.mkdir(parents=True, exist_ok=True)

    project = MagicMock()
    project.project_id = "bound_project"
    project.root_path = str(project_root)

    # Write a template that declares tools the parent hasn't selected.
    template_dir = project_root / ".ghostap" / "workflows"
    template_dir.mkdir(parents=True, exist_ok=True)
    (template_dir / "greedy.js").write_text(
        'export const meta = {"tools": ["coco", "gemini"], "description": "greedy"};\n'
        "await agent({tool:'coco', prompt:'x'});\n",
        encoding="utf-8",
    )

    pending = PendingConfirmation(
        requirement="test requirement",
        initiator_user_id="sender-123",
        engine_session_key="session_abc",
        selected_tools=["coco"],  # intentionally missing "gemini"
        script_path=None,
        meta={
            "workflow_refs": [{"name": "greedy", "path": ""}],
        },
        is_fallback=False,
    )

    engine = MagicMock()
    engine.project = WorkflowProject(
        status=WorkflowStatus.AWAITING_CONFIRM,
        pending=pending,
    )

    ctx = MagicMock()
    ctx.workflow_engine_manager.get.return_value = engine
    handler = WorkflowHandler.__new__(WorkflowHandler)
    handler.ctx = ctx
    handler._get_root_path = MagicMock(return_value=str(project_root))

    captured: dict[str, Any] = {}

    def _update(message_id: str, card: Any) -> None:
        captured["last_card"] = card

    handler.update_card = _update

    with patch("src.thread.get_current_sender_id", return_value="sender-123"):
        handler.handle_workflow_view_workflow_ref(
            "msg_id",
            "chat_1",
            "bound_project",
            {
                "action": "workflow_view_workflow_ref",
                "ref_index": 0,
                "chat_id": "chat_1",
                "project_id": "bound_project",
                "engine_session_key": "session_abc",
            },
        )

    rendered = _card_to_text(captured.get("last_card", {}))
    assert "greedy" in rendered
    assert "工具缺失警告" in rendered, (
        "预览卡未展示工具缺失警告：\n" + rendered
    )
    assert "gemini" in rendered, "缺失工具名应出现在预览卡正文中"


