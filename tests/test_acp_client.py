"""Tests for acp.client — GhostAPClient event handling."""

import asyncio
from pathlib import Path
import shutil
from unittest.mock import MagicMock

import pytest

from src.sandbox.executor import SandboxExecutor

from src.acp.models import ACPEvent, ACPEventType, ToolCallInfo
from src.acp.client import GhostAPClient, ACPHistoryStore, _parse_tool_call, _parse_plan
from src.acp.sync_adapter import resolve_agent_spec


def test_acp_manager_retries_start_failure(monkeypatch):
    import time as _time
    from types import SimpleNamespace
    from src.acp import manager as mgr

    calls = {"start": 0}

    class FakeSession:
        def __init__(self, agent_type: str, cwd: str):
            self.session_id = ""
            self.last_active = _time.time()
            self.message_count = 0

        def describe_agent(self):
            return "cmd=fake args=acp serve cwd=."

        def start(self, startup_timeout: float = 60):
            calls["start"] += 1
            if calls["start"] < 3:
                raise TimeoutError("startup timeout")
            self.session_id = "s_ok"
            return self.session_id

        def load_session(self, session_id: str):
            self.session_id = session_id

        def load_local_history(self, *a, **kw):
            return []

        def to_snapshot(self):
            return {"session_id": self.session_id}

        def close(self):
            return None

        def is_server_healthy(self, healthcheck_timeout: float = 2.0) -> bool:
            return True

    monkeypatch.setattr(mgr, "SyncACPSession", FakeSession)
    monkeypatch.setattr(mgr, "get_settings", lambda: SimpleNamespace(acp_startup_retries=3, acp_healthcheck_timeout=0.01))

    m = mgr.ACPSessionManager("coco", session_timeout=999999)
    s = m.start_session("chat1", cwd=".", startup_timeout=0.01)
    assert s.session_id == "s_ok"
    assert calls["start"] == 3


def test_acp_manager_unhealthy_session_is_cleaned(monkeypatch):
    import time as _time
    from types import SimpleNamespace
    from src.acp import manager as mgr

    class DeadSession:
        def __init__(self):
            self.session_id = "s_dead"
            # Idle > 30s to trigger health check path in get_session
            self.last_active = _time.time() - 60
            self.message_count = 0
            self.closed = False

        def is_server_running(self) -> bool:
            return False  # process is dead

        def to_snapshot(self):
            return {"session_id": self.session_id}

        def close(self):
            self.closed = True

    monkeypatch.setattr(mgr, "get_settings", lambda: SimpleNamespace(acp_healthcheck_timeout=0.01, acp_startup_retries=1))

    m = mgr.ACPSessionManager("coco", session_timeout=999999)
    dead = DeadSession()
    key = m._session_key("chat1")
    m._sessions[key] = dead

    assert m.get_session("chat1") is None
    assert dead.closed is True
    assert key not in m._sessions


class MockToolCallStart:
    """Mock ToolCallStart ACP schema object."""
    def __init__(self, tool_call_id="tc1", title="Read file", kind="read",
                 status="in_progress", locations=None):
        self.tool_call_id = tool_call_id
        self.title = title
        self.kind = kind
        self.status = status
        self.locations = locations or []


class MockToolCallProgress:
    """Mock ToolCallProgress ACP schema object."""
    def __init__(self, tool_call_id="tc1", title="Read file", kind="read",
                 status="completed", locations=None):
        self.tool_call_id = tool_call_id
        self.title = title
        self.kind = kind
        self.status = status
        self.locations = locations or []


class MockLocation:
    def __init__(self, path):
        self.path = path


class MockPlanEntry:
    def __init__(self, content, priority="medium", status="pending"):
        self.content = content
        self.priority = priority
        self.status = status


class TestParseToolCall:
    def test_basic(self):
        update = MockToolCallStart(tool_call_id="tc1", title="Read", kind="read", status="in_progress")
        tc = _parse_tool_call(update)
        assert tc.id == "tc1"
        assert tc.title == "Read"
        assert tc.kind == "read"
        assert tc.status == "in_progress"
        assert tc.locations == []

    def test_with_locations(self):
        update = MockToolCallStart(
            locations=[MockLocation("/a.py"), MockLocation("/b.py")],
        )
        tc = _parse_tool_call(update)
        assert tc.locations == ["/a.py", "/b.py"]

    def test_none_title(self):
        update = MockToolCallStart(title=None)
        tc = _parse_tool_call(update)
        assert tc.title == ""

    def test_none_kind(self):
        update = MockToolCallStart(kind=None)
        tc = _parse_tool_call(update)
        assert tc.kind == "other"


class TestParsePlan:
    def test_basic(self):
        class MockAgentPlanUpdate:
            entries = [
                MockPlanEntry("Step 1", status="completed"),
                MockPlanEntry("Step 2", status="in_progress"),
            ]

        plan = _parse_plan(MockAgentPlanUpdate())
        assert len(plan.entries) == 2
        assert plan.entries[0].content == "Step 1"
        assert plan.entries[0].status == "completed"

    def test_skips_empty_entries(self):
        class MockAgentPlanUpdate:
            entries = [
                MockPlanEntry("", status="completed"),
                MockPlanEntry("   ", status="completed"),
                MockPlanEntry(None, status="completed"),
                MockPlanEntry("Real step", status="pending"),
            ]

        plan = _parse_plan(MockAgentPlanUpdate())
        assert [e.content for e in plan.entries] == ["Real step"]

    def test_empty_plan(self):
        class MockAgentPlanUpdate:
            entries = []

        plan = _parse_plan(MockAgentPlanUpdate())
        assert plan.entries == []


class TestGhostAPClient:
    def setup_method(self):
        self.events: list[ACPEvent] = []
        self.client = GhostAPClient(on_event=self.events.append)

    def _run_async(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_request_permission_auto_approve(self):
        # Create mock options with an allow_once option
        mock_option = MagicMock()
        mock_option.kind = "allow_once"
        mock_option.option_id = "opt1"
        result = self._run_async(
            self.client.request_permission(
                options=[mock_option], session_id="s1", tool_call=MagicMock(),
            )
        )
        assert result.outcome.outcome == "selected"
        assert result.outcome.option_id == "opt1"


def test_read_write_text_file(tmp_path: Path):
    root = str(tmp_path)
    events: list[ACPEvent] = []
    client = GhostAPClient(on_event=events.append, root_dir=root)

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(client.write_text_file("hello", "a.txt", session_id="s1"))
        resp = loop.run_until_complete(client.read_text_file("a.txt", session_id="s1"))
        assert resp.content == "hello"
    finally:
        loop.close()


def test_read_text_file_path_escape_denied(tmp_path: Path):
    root = str(tmp_path)
    client = GhostAPClient(on_event=lambda e: None, root_dir=root)
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        resp = loop.run_until_complete(client.read_text_file("../etc/passwd", session_id="s1"))
        assert resp.content == ""
        assert resp.field_meta and "error" in resp.field_meta
    finally:
        loop.close()


def test_terminal_virtual_execution(tmp_path: Path):
    root = str(tmp_path)
    client = GhostAPClient(on_event=lambda e: None, root_dir=root, sandbox=SandboxExecutor())
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        create = loop.run_until_complete(client.create_terminal(command="echo hi", session_id="s1"))
        assert create.terminal_id.startswith("term_")

        out = loop.run_until_complete(client.terminal_output(session_id="s1", terminal_id=create.terminal_id))
        assert "hi" in out.output
        assert out.exit_status and out.exit_status.exit_code == 0
        assert out.truncated in (True, False)
    finally:
        loop.close()


def test_history_store_missing_file_returns_empty(tmp_path: Path):
    store = ACPHistoryStore(base_dir=str(tmp_path))
    assert store.load("no_such_session") == []


def test_history_store_skips_corrupt_lines(tmp_path: Path):
    store = ACPHistoryStore(base_dir=str(tmp_path))
    p = tmp_path / "s1.jsonl"
    p.write_text("{not json}\n" + "{\"kind\": \"execute\", \"data\": {\"command\": \"echo hi\"}}\n", encoding="utf-8")
    items = store.load("s1")
    assert len(items) == 1
    assert items[0]["kind"] == "execute"


def test_client_records_execute_history(tmp_path: Path):
    store = ACPHistoryStore(base_dir=str(tmp_path))
    client = GhostAPClient(on_event=lambda e: None, root_dir=str(tmp_path), sandbox=SandboxExecutor(), history_store=store)
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        resp = loop.run_until_complete(client.create_terminal(command="echo hi", session_id="s1"))
        assert resp.terminal_id
    finally:
        loop.close()

    items = store.load("s1")
    kinds = [x.get("kind") for x in items]
    assert "execute" in kinds


def test_permission_rejects_unsafe_execute():
    events: list[ACPEvent] = []
    client = GhostAPClient(on_event=events.append, auto_approve=True)
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)

        # allow_once option exists but should still be denied by safety policy
        opt = MagicMock()
        opt.kind = "allow_once"
        opt.option_id = "opt1"

        tool_call = MagicMock()
        tool_call.kind = "execute"
        tool_call.raw_input = {"command": "rm -rf /"}

        resp = loop.run_until_complete(
            client.request_permission(options=[opt], session_id="s1", tool_call=tool_call)
        )
        assert resp.outcome.outcome == "cancelled"
    finally:
        loop.close()


def test_resolve_agent_spec_coco_has_command():
    if not shutil.which("coco"):
        pytest.skip("coco binary not available")
    cmd, args = resolve_agent_spec("coco")
    assert cmd == "coco"
    assert args == ["acp", "serve"]


def test_read_text_file_truncates(tmp_path: Path):
    root = str(tmp_path)
    client = GhostAPClient(on_event=lambda e: None, root_dir=root)
    big = "x" * 300_000
    (tmp_path / "big.txt").write_text(big, encoding="utf-8")

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        resp = loop.run_until_complete(client.read_text_file("big.txt", session_id="s1"))
        assert len(resp.content) == 200_000
        assert resp.field_meta and resp.field_meta.get("truncated") is True
    finally:
        loop.close()
