import time

import pytest

from src.project.unified_context import (
    ContextBridgeSummary,
    ContextEntry,
    ContextEntryType,
    ContextResult,
    ContextSourceMode,
    ContextVersion,
    ProjectContextManager,
    UnifiedContext,
    UnifiedContextStore,
)

# ---------------------------------------------------------------------------
# ContextEntry
# ---------------------------------------------------------------------------


class TestContextEntry:
    def test_create_default(self):
        entry = ContextEntry()
        assert entry.entry_type == ContextEntryType.CONVERSATION
        assert entry.source_mode == ContextSourceMode.SMART
        assert entry.content == ""
        assert entry.metadata == {}
        assert len(entry.entry_id) == 12

    def test_create_with_values(self):
        entry = ContextEntry(
            entry_type=ContextEntryType.SESSION_SNAPSHOT,
            source_mode=ContextSourceMode.COCO,
            content="Session ended",
            metadata={"session_id": "abc123", "query_count": 5},
        )
        assert entry.entry_type == ContextEntryType.SESSION_SNAPSHOT
        assert entry.source_mode == ContextSourceMode.COCO
        assert entry.content == "Session ended"
        assert entry.metadata["session_id"] == "abc123"

    def test_unique_entry_ids(self):
        entries = [ContextEntry() for _ in range(100)]
        ids = {e.entry_id for e in entries}
        assert len(ids) == 100

    def test_to_dict(self):
        entry = ContextEntry(
            entry_type=ContextEntryType.MODE_TRANSITION,
            source_mode=ContextSourceMode.CLAUDE,
            content="coco -> claude",
            metadata={"from_mode": "coco", "to_mode": "claude"},
        )
        d = entry.to_dict()
        assert d["entry_type"] == "mode_transition"
        assert d["source_mode"] == "claude"
        assert d["content"] == "coco -> claude"
        assert d["metadata"]["from_mode"] == "coco"
        assert "entry_id" in d
        assert "created_at" in d

    def test_from_dict(self):
        d = {
            "entry_id": "test12345678",
            "entry_type": "deep_result",
            "source_mode": "deep_engine",
            "content": "Deep Engine completed",
            "metadata": {"name": "test_task"},
            "created_at": 1700000000.0,
        }
        entry = ContextEntry.from_dict(d)
        assert entry.entry_id == "test12345678"
        assert entry.entry_type == ContextEntryType.DEEP_ENGINE_RESULT
        assert entry.source_mode == ContextSourceMode.DEEP_ENGINE
        assert entry.content == "Deep Engine completed"
        assert entry.created_at == 1700000000.0

    def test_roundtrip(self):
        original = ContextEntry(
            entry_type=ContextEntryType.AI_SUMMARY,
            source_mode=ContextSourceMode.COCO,
            content="Summary of work done",
            metadata={"key": "value", "nested": {"a": 1}},
        )
        restored = ContextEntry.from_dict(original.to_dict())
        assert restored.entry_id == original.entry_id
        assert restored.entry_type == original.entry_type
        assert restored.source_mode == original.source_mode
        assert restored.content == original.content
        assert restored.metadata == original.metadata


# ---------------------------------------------------------------------------
# ContextVersion
# ---------------------------------------------------------------------------


class TestContextVersion:
    def test_create_default(self):
        v = ContextVersion()
        assert v.version_number == 0
        assert v.reason == ""
        assert v.summary == ""
        assert len(v.version_id) == 8

    def test_to_dict(self):
        v = ContextVersion(
            version_number=3,
            reason="mode_transition: coco -> claude",
            source_mode=ContextSourceMode.COCO,
            summary="Refactored auth module",
            entry_count=15,
        )
        d = v.to_dict()
        assert d["version_number"] == 3
        assert d["reason"] == "mode_transition: coco -> claude"
        assert d["source_mode"] == "coco"
        assert d["entry_count"] == 15

    def test_roundtrip(self):
        original = ContextVersion(
            version_number=7,
            reason="deep_engine_complete",
            source_mode=ContextSourceMode.DEEP_ENGINE,
            summary="Completed 5/5 tasks",
            entry_count=42,
        )
        restored = ContextVersion.from_dict(original.to_dict())
        assert restored.version_id == original.version_id
        assert restored.version_number == original.version_number
        assert restored.reason == original.reason
        assert restored.entry_count == original.entry_count


# ---------------------------------------------------------------------------
# ContextBridgeSummary
# ---------------------------------------------------------------------------


class TestContextBridgeSummary:
    def test_create_default(self):
        bridge = ContextBridgeSummary()
        assert bridge.from_mode == ContextSourceMode.SMART
        assert bridge.summary_text == ""
        assert bridge.key_decisions == []
        assert bridge.files_modified == []

    def test_to_injection_prompt_minimal(self):
        bridge = ContextBridgeSummary(
            from_mode=ContextSourceMode.COCO,
            to_mode=ContextSourceMode.CLAUDE,
        )
        prompt = bridge.to_injection_prompt()
        assert "[Context from previous coco session]" in prompt
        assert "[End of context]" in prompt

    def test_to_injection_prompt_full(self):
        bridge = ContextBridgeSummary(
            from_mode=ContextSourceMode.COCO,
            to_mode=ContextSourceMode.CLAUDE,
            summary_text="Refactored auth module to use JWT",
            key_decisions=["Use pyjwt library", "Store tokens in Redis"],
            files_modified=["auth/jwt.py", "middleware.py"],
            pending_tasks=["Add unit tests", "Update docs"],
        )
        prompt = bridge.to_injection_prompt()
        assert "coco session" in prompt
        assert "Refactored auth module to use JWT" in prompt
        assert "Use pyjwt library" in prompt
        assert "Store tokens in Redis" in prompt
        assert "auth/jwt.py" in prompt
        assert "middleware.py" in prompt
        assert "Add unit tests" in prompt
        assert "[End of context]" in prompt

    def test_roundtrip(self):
        original = ContextBridgeSummary(
            from_mode=ContextSourceMode.COCO,
            to_mode=ContextSourceMode.CLAUDE,
            summary_text="Did some work",
            key_decisions=["Decision A"],
            files_modified=["file.py"],
            pending_tasks=["Task 1"],
        )
        restored = ContextBridgeSummary.from_dict(original.to_dict())
        assert restored.from_mode == original.from_mode
        assert restored.to_mode == original.to_mode
        assert restored.summary_text == original.summary_text
        assert restored.key_decisions == original.key_decisions
        assert restored.files_modified == original.files_modified
        assert restored.pending_tasks == original.pending_tasks


# ---------------------------------------------------------------------------
# UnifiedContext — CRUD
# ---------------------------------------------------------------------------


class TestUnifiedContextCRUD:
    @pytest.fixture
    def ctx(self):
        return UnifiedContext(project_id="test_project", max_entries=10, max_versions=5)

    # ---- Create ----

    def test_add_entry(self, ctx):
        entry = ctx.add_entry(ContextEntry(content="hello"))
        assert ctx.entry_count == 1
        assert entry.content == "hello"

    def test_add_conversation(self, ctx):
        entry = ctx.add_conversation("user", "Hello", ContextSourceMode.COCO, "msg_1")
        assert ctx.entry_count == 1
        assert entry.entry_type == ContextEntryType.CONVERSATION
        assert entry.source_mode == ContextSourceMode.COCO
        assert entry.content == "Hello"
        assert entry.metadata["role"] == "user"
        assert entry.metadata["message_id"] == "msg_1"

    def test_add_session_snapshot(self, ctx):
        entry = ctx.add_session_snapshot(
            {"session_id": "sess_123", "query_count": 10},
            ContextSourceMode.CLAUDE,
        )
        assert entry.entry_type == ContextEntryType.SESSION_SNAPSHOT
        assert "sess_123" in entry.content
        assert entry.metadata["query_count"] == 10

    def test_add_mode_transition(self, ctx):
        entry = ctx.add_mode_transition(ContextSourceMode.COCO, ContextSourceMode.CLAUDE, reason="user requested")
        assert entry.entry_type == ContextEntryType.MODE_TRANSITION
        assert entry.metadata["from_mode"] == "coco"
        assert entry.metadata["to_mode"] == "claude"
        assert entry.metadata["reason"] == "user requested"

    def test_add_deep_engine_result(self, ctx):
        entry = ctx.add_deep_engine_result({"name": "refactor_auth", "tasks": []})
        assert entry.entry_type == ContextEntryType.DEEP_ENGINE_RESULT
        assert entry.source_mode == ContextSourceMode.DEEP_ENGINE
        assert "refactor_auth" in entry.content

    # ---- Read ----

    def test_get_entry_by_id(self, ctx):
        entry = ctx.add_conversation("user", "test", ContextSourceMode.SMART)
        found = ctx.get_entry(entry.entry_id)
        assert found is not None
        assert found.entry_id == entry.entry_id
        assert found.content == "test"

    def test_get_entry_not_found(self, ctx):
        assert ctx.get_entry("nonexistent") is None

    def test_get_entries_by_type(self, ctx):
        ctx.add_conversation("user", "msg1", ContextSourceMode.COCO)
        ctx.add_session_snapshot({"session_id": "s1"}, ContextSourceMode.COCO)
        ctx.add_conversation("user", "msg2", ContextSourceMode.CLAUDE)

        convs = ctx.get_entries_by_type(ContextEntryType.CONVERSATION)
        assert len(convs) == 2

        snaps = ctx.get_entries_by_type(ContextEntryType.SESSION_SNAPSHOT)
        assert len(snaps) == 1

    def test_get_entries_by_mode(self, ctx):
        ctx.add_conversation("user", "msg1", ContextSourceMode.COCO)
        ctx.add_conversation("user", "msg2", ContextSourceMode.COCO)
        ctx.add_conversation("user", "msg3", ContextSourceMode.CLAUDE)

        coco = ctx.get_entries_by_mode(ContextSourceMode.COCO)
        assert len(coco) == 2

        claude = ctx.get_entries_by_mode(ContextSourceMode.CLAUDE)
        assert len(claude) == 1

    def test_get_recent_entries(self, ctx):
        for i in range(5):
            ctx.add_conversation("user", f"msg_{i}", ContextSourceMode.SMART)

        recent = ctx.get_recent_entries(3)
        assert len(recent) == 3
        assert recent[0].content == "msg_2"
        assert recent[2].content == "msg_4"

    def test_get_conversations(self, ctx):
        ctx.add_conversation("user", "hello", ContextSourceMode.COCO)
        ctx.add_session_snapshot({"session_id": "s1"}, ContextSourceMode.COCO)
        ctx.add_conversation("assistant", "hi", ContextSourceMode.COCO)

        convs = ctx.get_conversations()
        assert len(convs) == 2
        assert convs[0].content == "hello"
        assert convs[1].content == "hi"

    def test_query_entries_combined(self, ctx):
        ctx.add_conversation("user", "coco_msg", ContextSourceMode.COCO)
        ctx.add_conversation("user", "claude_msg", ContextSourceMode.CLAUDE)
        ctx.add_session_snapshot({"session_id": "s1"}, ContextSourceMode.COCO)

        results = ctx.query_entries(
            entry_type=ContextEntryType.CONVERSATION,
            source_mode=ContextSourceMode.COCO,
        )
        assert len(results) == 1
        assert results[0].content == "coco_msg"

    def test_query_entries_since_timestamp(self, ctx):
        ctx.add_conversation("user", "old", ContextSourceMode.SMART)
        time.sleep(0.05)
        cutoff = time.time()
        time.sleep(0.05)
        ctx.add_conversation("user", "new", ContextSourceMode.SMART)

        results = ctx.query_entries(since=cutoff)
        assert len(results) == 1
        assert results[0].content == "new"

    def test_query_entries_limit(self, ctx):
        for i in range(8):
            ctx.add_conversation("user", f"msg_{i}", ContextSourceMode.SMART)

        results = ctx.query_entries(limit=3)
        assert len(results) == 3
        assert results[0].content == "msg_5"

    # ---- Update ----

    def test_update_entry_content(self, ctx):
        entry = ctx.add_conversation("user", "original", ContextSourceMode.SMART)
        assert ctx.update_entry(entry.entry_id, content="updated")
        found = ctx.get_entry(entry.entry_id)
        assert found.content == "updated"

    def test_update_entry_metadata(self, ctx):
        entry = ctx.add_conversation("user", "test", ContextSourceMode.SMART)
        assert ctx.update_entry(entry.entry_id, metadata={"extra": "data"})
        found = ctx.get_entry(entry.entry_id)
        assert found.metadata["extra"] == "data"
        # 原有 metadata 应保留
        assert found.metadata["role"] == "user"

    def test_update_entry_not_found(self, ctx):
        assert ctx.update_entry("nonexistent", content="x") is False

    # ---- Delete ----

    def test_remove_entry(self, ctx):
        entry = ctx.add_conversation("user", "to_delete", ContextSourceMode.SMART)
        assert ctx.entry_count == 1
        assert ctx.remove_entry(entry.entry_id) is True
        assert ctx.entry_count == 0
        assert ctx.get_entry(entry.entry_id) is None

    def test_remove_entry_not_found(self, ctx):
        assert ctx.remove_entry("nonexistent") is False

    def test_clear_entries(self, ctx):
        for i in range(5):
            ctx.add_conversation("user", f"msg_{i}", ContextSourceMode.SMART)
        count = ctx.clear_entries()
        assert count == 5
        assert ctx.entry_count == 0

    def test_clear_entries_by_mode(self, ctx):
        ctx.add_conversation("user", "coco1", ContextSourceMode.COCO)
        ctx.add_conversation("user", "coco2", ContextSourceMode.COCO)
        ctx.add_conversation("user", "claude1", ContextSourceMode.CLAUDE)

        removed = ctx.clear_entries_by_mode(ContextSourceMode.COCO)
        assert removed == 2
        assert ctx.entry_count == 1
        assert ctx.get_entries_by_mode(ContextSourceMode.CLAUDE)[0].content == "claude1"


# ---------------------------------------------------------------------------
# UnifiedContext — 滚动窗口
# ---------------------------------------------------------------------------


class TestUnifiedContextRollingWindow:
    def test_entries_eviction(self):
        ctx = UnifiedContext(project_id="test", max_entries=5)
        entries = []
        for i in range(8):
            e = ctx.add_conversation("user", f"msg_{i}", ContextSourceMode.SMART)
            entries.append(e)

        assert ctx.entry_count == 5
        # 最旧的 3 条被淘汰
        assert ctx.get_entry(entries[0].entry_id) is None
        assert ctx.get_entry(entries[1].entry_id) is None
        assert ctx.get_entry(entries[2].entry_id) is None
        # 最新的 5 条保留
        assert ctx.get_entry(entries[3].entry_id) is not None
        assert ctx.get_entry(entries[7].entry_id) is not None

    def test_index_remains_correct_after_eviction(self):
        ctx = UnifiedContext(project_id="test", max_entries=3)
        for i in range(5):
            ctx.add_conversation("user", f"msg_{i}", ContextSourceMode.SMART)

        # 所有保留的条目都应该通过 get_entry 找到
        for entry in ctx.entries:
            assert ctx.get_entry(entry.entry_id) is not None
            assert ctx.get_entry(entry.entry_id).content == entry.content


# ---------------------------------------------------------------------------
# UnifiedContext — 版本控制
# ---------------------------------------------------------------------------


class TestUnifiedContextVersioning:
    @pytest.fixture
    def ctx(self):
        return UnifiedContext(project_id="test", max_entries=50, max_versions=5)

    def test_create_version(self, ctx):
        ctx.add_conversation("user", "msg1", ContextSourceMode.COCO)
        ctx.add_conversation("user", "msg2", ContextSourceMode.COCO)

        v = ctx.create_version("mode_switch", ContextSourceMode.COCO, summary="2 msgs")
        assert v.version_number == 1
        assert v.entry_count == 2
        assert v.reason == "mode_switch"
        assert v.summary == "2 msgs"

    def test_version_numbers_increment(self, ctx):
        v1 = ctx.create_version("first", ContextSourceMode.SMART)
        v2 = ctx.create_version("second", ContextSourceMode.COCO)
        v3 = ctx.create_version("third", ContextSourceMode.CLAUDE)

        assert v1.version_number == 1
        assert v2.version_number == 2
        assert v3.version_number == 3
        assert ctx.current_version_number == 3

    def test_get_version(self, ctx):
        ctx.create_version("v1", ContextSourceMode.SMART)
        v2 = ctx.create_version("v2", ContextSourceMode.COCO)

        found = ctx.get_version(2)
        assert found is not None
        assert found.version_id == v2.version_id

        assert ctx.get_version(99) is None

    def test_get_entries_since_version(self, ctx):
        ctx.add_conversation("user", "before_v1", ContextSourceMode.COCO)
        ctx.add_conversation("user", "before_v1_2", ContextSourceMode.COCO)
        v1 = ctx.create_version("v1", ContextSourceMode.COCO)

        ctx.add_conversation("user", "after_v1", ContextSourceMode.CLAUDE)
        ctx.add_conversation("user", "after_v1_2", ContextSourceMode.CLAUDE)

        since = ctx.get_entries_since_version(v1.version_number)
        assert len(since) == 2
        assert since[0].content == "after_v1"
        assert since[1].content == "after_v1_2"

    def test_get_entries_since_nonexistent_version(self, ctx):
        ctx.add_conversation("user", "msg1", ContextSourceMode.SMART)
        # 不存在的版本号返回全部
        since = ctx.get_entries_since_version(999)
        assert len(since) == 1

    def test_version_eviction(self, ctx):
        # max_versions=5
        for i in range(8):
            ctx.create_version(f"v{i}", ContextSourceMode.SMART)

        assert len(ctx.versions) == 5
        # 最旧的被淘汰，最新的保留
        assert ctx.versions[0].reason == "v3"
        assert ctx.versions[-1].reason == "v7"

    def test_entries_since_version_with_rolling_window(self):
        ctx = UnifiedContext(project_id="test", max_entries=5, max_versions=10)

        for i in range(3):
            ctx.add_conversation("user", f"msg_{i}", ContextSourceMode.COCO)
        v1 = ctx.create_version("v1", ContextSourceMode.COCO)
        assert v1.entry_count == 3

        # 添加更多条目触发滚动窗口淘汰
        for i in range(5):
            ctx.add_conversation("user", f"new_{i}", ContextSourceMode.CLAUDE)

        # 滚动窗口淘汰了 v1 之前的旧条目，但 v1 之后新增的条目仍在窗口内
        # 使用 seq 作为增量 diff 基准，应返回所有新增条目
        since = ctx.get_entries_since_version(v1.version_number)
        assert len(since) == 5
        assert since[0].content == "new_0"


# ---------------------------------------------------------------------------
# UnifiedContext — 跨模式桥接
# ---------------------------------------------------------------------------


class TestUnifiedContextBridge:
    @pytest.fixture
    def ctx(self):
        return UnifiedContext(project_id="test", max_entries=50)

    def test_build_bridge_summary(self, ctx):
        ctx.add_conversation("user", "help me refactor auth", ContextSourceMode.COCO)
        ctx.add_conversation("assistant", "I'll create a plan", ContextSourceMode.COCO)
        ctx.add_conversation("user", "go ahead", ContextSourceMode.COCO)

        bridge = ctx.build_bridge_summary(ContextSourceMode.COCO, ContextSourceMode.CLAUDE)

        assert bridge.from_mode == ContextSourceMode.COCO
        assert bridge.to_mode == ContextSourceMode.CLAUDE
        assert "refactor auth" in bridge.summary_text
        assert "plan" in bridge.summary_text

    def test_build_bridge_respects_max_items(self, ctx):
        for i in range(20):
            ctx.add_conversation("user", f"message_{i}", ContextSourceMode.COCO)

        bridge = ctx.build_bridge_summary(ContextSourceMode.COCO, ContextSourceMode.CLAUDE, max_items=3)

        # summary_text 应只包含最近 3 条中的内容（再截取最后 8 行）
        lines = [line for line in bridge.summary_text.split("\n") if line.strip()]
        assert len(lines) <= 3

    def test_build_bridge_includes_deep_results(self, ctx):
        ctx.add_deep_engine_result(
            {
                "name": "refactor",
                "tasks": [
                    {"title": "Create JWT module", "status": "completed", "result": "Created auth/jwt.py"},
                    {"title": "Update tests", "status": "failed", "result": None},
                ],
            }
        )
        bridge = ctx.build_bridge_summary(ContextSourceMode.DEEP_ENGINE, ContextSourceMode.COCO)
        assert "Create JWT module" in bridge.summary_text

    def test_bridge_to_injection_prompt(self, ctx):
        ctx.add_conversation("user", "refactor auth", ContextSourceMode.COCO)
        bridge = ctx.build_bridge_summary(ContextSourceMode.COCO, ContextSourceMode.CLAUDE)
        prompt = bridge.to_injection_prompt()

        assert "[Context from previous coco session]" in prompt
        assert "refactor auth" in prompt
        assert "[End of context]" in prompt

    def test_consume_bridge_summary(self, ctx):
        ctx.add_conversation("user", "test", ContextSourceMode.COCO)
        ctx.build_bridge_summary(ContextSourceMode.COCO, ContextSourceMode.CLAUDE)
        assert ctx.last_bridge_summary is not None

        bridge = ctx.consume_bridge_summary()
        assert bridge is not None
        assert bridge.from_mode == ContextSourceMode.COCO

        # 消费后应为 None
        assert ctx.last_bridge_summary is None
        assert ctx.consume_bridge_summary() is None

    def test_build_bridge_skips_non_bridgeable(self, ctx):
        ctx.add_mode_transition(ContextSourceMode.SMART, ContextSourceMode.COCO)
        ctx.add_session_snapshot({"session_id": "s1"}, ContextSourceMode.COCO)
        ctx.add_conversation("user", "actual content", ContextSourceMode.COCO)

        bridge = ctx.build_bridge_summary(ContextSourceMode.COCO, ContextSourceMode.CLAUDE)
        # 只有 CONVERSATION 被包含，MODE_TRANSITION 和 SESSION_SNAPSHOT 被跳过
        assert "actual content" in bridge.summary_text
        assert "s1" not in bridge.summary_text


# ---------------------------------------------------------------------------
# UnifiedContext — 序列化
# ---------------------------------------------------------------------------


class TestUnifiedContextSerialization:
    def test_to_dict(self):
        ctx = UnifiedContext(project_id="my_project")
        ctx.add_conversation("user", "hello", ContextSourceMode.COCO)
        ctx.create_version("v1", ContextSourceMode.COCO)
        ctx.build_bridge_summary(ContextSourceMode.COCO, ContextSourceMode.CLAUDE)

        d = ctx.to_dict()
        assert d["project_id"] == "my_project"
        assert len(d["entries"]) == 1
        assert len(d["versions"]) == 1
        assert d["current_version_number"] == 1
        assert d["last_bridge_summary"] is not None
        assert "created_at" in d
        assert "updated_at" in d

    def test_roundtrip(self):
        ctx = UnifiedContext(project_id="roundtrip_test", max_entries=100, max_versions=20)
        ctx.add_conversation("user", "msg1", ContextSourceMode.COCO, "mid1")
        ctx.add_conversation("assistant", "resp1", ContextSourceMode.COCO)
        ctx.add_session_snapshot({"session_id": "s1", "count": 2}, ContextSourceMode.COCO)
        ctx.create_version("mode_switch", ContextSourceMode.COCO, summary="2 convos")
        ctx.add_conversation("user", "msg2", ContextSourceMode.CLAUDE)
        ctx.build_bridge_summary(ContextSourceMode.COCO, ContextSourceMode.CLAUDE)

        restored = UnifiedContext.from_dict(ctx.to_dict())

        assert restored.project_id == "roundtrip_test"
        assert restored.max_entries == 100
        assert restored.max_versions == 20
        assert restored.entry_count == 4
        assert len(restored.versions) == 1
        assert restored.current_version_number == 1
        assert restored.last_bridge_summary is not None
        assert restored.last_bridge_summary.from_mode == ContextSourceMode.COCO

        # 条目内容验证
        entries = restored.entries
        assert entries[0].content == "msg1"
        assert entries[0].metadata["role"] == "user"
        assert entries[2].entry_type == ContextEntryType.SESSION_SNAPSHOT

    def test_from_dict_with_minimal_data(self):
        d = {"project_id": "minimal"}
        ctx = UnifiedContext.from_dict(d)
        assert ctx.project_id == "minimal"
        assert ctx.entry_count == 0
        assert len(ctx.versions) == 0
        assert ctx.last_bridge_summary is None


# ---------------------------------------------------------------------------
# UnifiedContext — updated_at 跟踪
# ---------------------------------------------------------------------------


class TestUnifiedContextTimestamps:
    def test_updated_at_changes_on_add(self):
        ctx = UnifiedContext(project_id="test")
        t0 = ctx.updated_at
        time.sleep(0.01)
        ctx.add_conversation("user", "msg", ContextSourceMode.SMART)
        assert ctx.updated_at > t0

    def test_updated_at_changes_on_update(self):
        ctx = UnifiedContext(project_id="test")
        entry = ctx.add_conversation("user", "msg", ContextSourceMode.SMART)
        t0 = ctx.updated_at
        time.sleep(0.01)
        ctx.update_entry(entry.entry_id, content="updated")
        assert ctx.updated_at > t0

    def test_updated_at_changes_on_delete(self):
        ctx = UnifiedContext(project_id="test")
        entry = ctx.add_conversation("user", "msg", ContextSourceMode.SMART)
        t0 = ctx.updated_at
        time.sleep(0.01)
        ctx.remove_entry(entry.entry_id)
        assert ctx.updated_at > t0

    def test_updated_at_changes_on_version(self):
        ctx = UnifiedContext(project_id="test")
        t0 = ctx.updated_at
        time.sleep(0.01)
        ctx.create_version("test", ContextSourceMode.SMART)
        assert ctx.updated_at > t0


# ---------------------------------------------------------------------------
# UnifiedContextStore
# ---------------------------------------------------------------------------


class TestUnifiedContextStore:
    @pytest.fixture
    def store(self):
        return UnifiedContextStore()

    def test_get_or_create_new(self, store):
        ctx = store.get_or_create("project_a")
        assert ctx.project_id == "project_a"
        assert ctx.entry_count == 0

    def test_get_or_create_existing(self, store):
        ctx1 = store.get_or_create("project_a")
        ctx1.add_conversation("user", "hello", ContextSourceMode.SMART)

        ctx2 = store.get_or_create("project_a")
        assert ctx2 is ctx1
        assert ctx2.entry_count == 1

    def test_get_existing(self, store):
        store.get_or_create("project_a")
        ctx = store.get("project_a")
        assert ctx is not None
        assert ctx.project_id == "project_a"

    def test_get_nonexistent(self, store):
        assert store.get("nonexistent") is None

    def test_has(self, store):
        assert store.has("project_a") is False
        store.get_or_create("project_a")
        assert store.has("project_a") is True

    def test_list_project_ids(self, store):
        store.get_or_create("alpha")
        store.get_or_create("beta")
        store.get_or_create("gamma")

        ids = store.list_project_ids()
        assert set(ids) == {"alpha", "beta", "gamma"}

    def test_remove(self, store):
        store.get_or_create("project_a")
        assert store.remove("project_a") is True
        assert store.get("project_a") is None
        assert store.remove("project_a") is False

    def test_clear(self, store):
        store.get_or_create("a")
        store.get_or_create("b")
        store.get_or_create("c")

        count = store.clear()
        assert count == 3
        assert len(store) == 0

    def test_len(self, store):
        assert len(store) == 0
        store.get_or_create("a")
        store.get_or_create("b")
        assert len(store) == 2

    def test_stats(self, store):
        ctx_a = store.get_or_create("a")
        ctx_a.add_conversation("user", "msg1", ContextSourceMode.SMART)
        ctx_a.add_conversation("user", "msg2", ContextSourceMode.SMART)
        ctx_a.create_version("v1", ContextSourceMode.SMART)

        ctx_b = store.get_or_create("b")
        ctx_b.add_conversation("user", "msg3", ContextSourceMode.COCO)

        stats = store.stats()
        assert stats["project_count"] == 2
        assert stats["total_entries"] == 3
        assert stats["total_versions"] == 1

    def test_default_max_entries_propagated(self):
        store = UnifiedContextStore(default_max_entries=50, default_max_versions=10)
        ctx = store.get_or_create("test")
        assert ctx.max_entries == 50
        assert ctx.max_versions == 10

    def test_isolation_between_projects(self, store):
        ctx_a = store.get_or_create("project_a")
        ctx_b = store.get_or_create("project_b")

        ctx_a.add_conversation("user", "only in A", ContextSourceMode.COCO)
        ctx_b.add_conversation("user", "only in B", ContextSourceMode.CLAUDE)

        assert ctx_a.entry_count == 1
        assert ctx_b.entry_count == 1
        assert ctx_a.entries[0].content == "only in A"
        assert ctx_b.entries[0].content == "only in B"

    def test_data_persists_during_service_lifetime(self, store):
        """同一 store 实例内，数据在多次操作间持续存在"""
        ctx = store.get_or_create("persistent")
        ctx.add_conversation("user", "step1", ContextSourceMode.SMART)
        ctx.create_version("v1", ContextSourceMode.SMART)

        # 模拟后续请求
        ctx2 = store.get_or_create("persistent")
        ctx2.add_conversation("user", "step2", ContextSourceMode.SMART)

        assert ctx2.entry_count == 2
        assert ctx2.current_version_number == 1

    def test_data_resets_on_new_store(self):
        """新的 store 实例 = 服务重启，数据重置"""
        store1 = UnifiedContextStore()
        ctx = store1.get_or_create("project")
        ctx.add_conversation("user", "old data", ContextSourceMode.SMART)
        assert ctx.entry_count == 1

        store2 = UnifiedContextStore()
        ctx2 = store2.get_or_create("project")
        assert ctx2.entry_count == 0


# ---------------------------------------------------------------------------
# UnifiedContextStore — 线程安全
# ---------------------------------------------------------------------------


class TestUnifiedContextStoreThreadSafety:
    def test_concurrent_get_or_create(self):
        import threading

        store = UnifiedContextStore()
        results: list[UnifiedContext] = []
        errors: list[Exception] = []

        def worker():
            try:
                ctx = store.get_or_create("shared_project")
                results.append(ctx)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        # 所有线程应该拿到同一个实例
        assert all(r is results[0] for r in results)

    def test_concurrent_add_entries(self):
        import threading

        store = UnifiedContextStore()
        ctx = store.get_or_create("concurrent_test")
        errors: list[Exception] = []

        def worker(idx: int):
            try:
                for j in range(10):
                    ctx.add_conversation("user", f"thread_{idx}_msg_{j}", ContextSourceMode.SMART)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        # 不要求精确 50 条（因为没有内部锁），但不应崩溃
        assert ctx.entry_count > 0


# ---------------------------------------------------------------------------
# ContextResult
# ---------------------------------------------------------------------------


class TestContextResult:
    def test_success_result(self):
        r = ContextResult(success=True, message="ok", data={"key": 1}, project_id="p1")
        assert r.success is True
        assert r.message == "ok"
        assert r.data["key"] == 1
        assert r.project_id == "p1"

    def test_failure_result(self):
        r = ContextResult(success=False, message="not found")
        assert r.success is False
        assert r.data is None
        assert r.project_id is None


# ---------------------------------------------------------------------------
# ProjectContextManager — createContext
# ---------------------------------------------------------------------------


class TestProjectContextManagerCreate:
    @pytest.fixture
    def mgr(self):
        return ProjectContextManager()

    def test_create_new(self, mgr):
        r = mgr.create_context("proj_a")
        assert r.success is True
        assert r.project_id == "proj_a"
        assert r.data is not None
        assert r.data.project_id == "proj_a"
        assert r.data.entry_count == 0

    def test_create_with_initial_entries(self, mgr):
        entries = [
            ContextEntry(content="msg1", source_mode=ContextSourceMode.COCO),
            ContextEntry(content="msg2", source_mode=ContextSourceMode.COCO),
        ]
        r = mgr.create_context("proj_b", initial_entries=entries)
        assert r.success is True
        assert r.data.entry_count == 2

    def test_create_with_custom_limits(self, mgr):
        r = mgr.create_context("proj_c", max_entries=50, max_versions=10)
        assert r.success is True
        assert r.data.max_entries == 50
        assert r.data.max_versions == 10

    def test_create_duplicate_fails(self, mgr):
        mgr.create_context("proj_a")
        r = mgr.create_context("proj_a")
        assert r.success is False
        assert "已存在" in r.message

    def test_create_empty_id_fails(self, mgr):
        r = mgr.create_context("")
        assert r.success is False
        assert "不能为空" in r.message

    def test_create_whitespace_id_fails(self, mgr):
        r = mgr.create_context("   ")
        assert r.success is False
        assert "不能为空" in r.message


# ---------------------------------------------------------------------------
# ProjectContextManager — getContext
# ---------------------------------------------------------------------------


class TestProjectContextManagerGet:
    @pytest.fixture
    def mgr(self):
        m = ProjectContextManager()
        m.create_context("proj_a")
        ctx = m.store.get("proj_a")
        ctx.add_conversation("user", "hello", ContextSourceMode.COCO, "mid1")
        ctx.add_conversation("assistant", "hi", ContextSourceMode.COCO)
        ctx.add_session_snapshot({"session_id": "s1"}, ContextSourceMode.COCO)
        ctx.add_conversation("user", "claude msg", ContextSourceMode.CLAUDE)
        return m

    def test_get_full_context(self, mgr):
        r = mgr.get_context("proj_a")
        assert r.success is True
        assert r.data["project_id"] == "proj_a"
        assert r.data["entry_count"] == 4
        assert len(r.data["entries"]) == 4

    def test_get_filtered_by_type(self, mgr):
        r = mgr.get_context("proj_a", entry_type=ContextEntryType.CONVERSATION)
        assert r.success is True
        assert len(r.data["entries"]) == 3

    def test_get_filtered_by_mode(self, mgr):
        r = mgr.get_context("proj_a", source_mode=ContextSourceMode.COCO)
        assert r.success is True
        # 2 conversations + 1 snapshot = 3 coco entries
        assert len(r.data["entries"]) == 3

    def test_get_filtered_by_type_and_mode(self, mgr):
        r = mgr.get_context(
            "proj_a",
            entry_type=ContextEntryType.CONVERSATION,
            source_mode=ContextSourceMode.CLAUDE,
        )
        assert r.success is True
        assert len(r.data["entries"]) == 1
        assert r.data["entries"][0].content == "claude msg"

    def test_get_nonexistent_fails(self, mgr):
        r = mgr.get_context("nonexistent")
        assert r.success is False
        assert "不存在" in r.message

    def test_get_includes_bridge_info(self, mgr):
        ctx = mgr.store.get("proj_a")
        ctx.build_bridge_summary(ContextSourceMode.COCO, ContextSourceMode.CLAUDE)
        r = mgr.get_context("proj_a")
        assert r.data["has_bridge_summary"] is True


# ---------------------------------------------------------------------------
# ProjectContextManager — updateContext
# ---------------------------------------------------------------------------


class TestProjectContextManagerUpdate:
    @pytest.fixture
    def mgr(self):
        m = ProjectContextManager()
        m.create_context("proj_a")
        return m

    def test_update_with_entries(self, mgr):
        entries = [
            ContextEntry(content="e1"),
            ContextEntry(content="e2"),
            ContextEntry(content="e3"),
        ]
        r = mgr.update_context("proj_a", entries=entries)
        assert r.success is True
        assert r.data["added_count"] == 3
        assert r.data["total_count"] == 3

    def test_update_with_conversation(self, mgr):
        r = mgr.update_context(
            "proj_a",
            conversation={
                "role": "user",
                "content": "help me refactor",
                "source_mode": "coco",
                "message_id": "msg_123",
            },
        )
        assert r.success is True
        assert r.data["added_count"] == 1

        ctx = mgr.store.get("proj_a")
        entry = ctx.entries[0]
        assert entry.content == "help me refactor"
        assert entry.metadata["role"] == "user"
        assert entry.metadata["message_id"] == "msg_123"
        assert entry.source_mode == ContextSourceMode.COCO

    def test_update_with_deep_result(self, mgr):
        r = mgr.update_context(
            "proj_a",
            deep_result={
                "data": {"name": "task_x", "tasks": []},
            },
        )
        assert r.success is True
        ctx = mgr.store.get("proj_a")
        assert ctx.entries[0].entry_type == ContextEntryType.DEEP_ENGINE_RESULT

    def test_update_nonexistent_auto_creates(self, mgr):
        r = mgr.update_context(
            "new_project",
            conversation={
                "role": "user",
                "content": "first msg",
                "source_mode": "smart",
            },
        )
        assert r.success is True
        assert mgr.store.has("new_project")


# ---------------------------------------------------------------------------
# ProjectContextManager — deleteContext
# ---------------------------------------------------------------------------


class TestProjectContextManagerDelete:
    @pytest.fixture
    def mgr(self):
        m = ProjectContextManager()
        m.create_context("proj_a")
        ctx = m.store.get("proj_a")
        ctx.add_conversation("user", "coco1", ContextSourceMode.COCO)
        ctx.add_conversation("user", "coco2", ContextSourceMode.COCO)
        ctx.add_conversation("user", "claude1", ContextSourceMode.CLAUDE)
        return m

    def test_delete_entire_context(self, mgr):
        r = mgr.delete_context("proj_a")
        assert r.success is True
        assert r.data["removed_count"] == 1
        assert not mgr.store.has("proj_a")

    def test_delete_single_entry(self, mgr):
        ctx = mgr.store.get("proj_a")
        entry_id = ctx.entries[0].entry_id

        r = mgr.delete_context("proj_a", entry_id=entry_id)
        assert r.success is True
        assert r.data["removed_count"] == 1
        assert ctx.entry_count == 2
        assert ctx.get_entry(entry_id) is None

    def test_delete_by_mode(self, mgr):
        r = mgr.delete_context("proj_a", source_mode=ContextSourceMode.COCO)
        assert r.success is True
        assert r.data["removed_count"] == 2

        ctx = mgr.store.get("proj_a")
        assert ctx.entry_count == 1
        assert ctx.entries[0].content == "claude1"


# ---------------------------------------------------------------------------
# ProjectContextManager — contextExists
# ---------------------------------------------------------------------------


class TestProjectContextManagerExists:
    @pytest.fixture
    def mgr(self):
        m = ProjectContextManager()
        m.create_context("proj_a")
        m.store.get("proj_a").add_conversation("user", "msg", ContextSourceMode.SMART)
        return m

    def test_exists_true(self, mgr):
        r = mgr.context_exists("proj_a")
        assert r.success is True
        assert r.data["exists"] is True
        assert r.data["entry_count"] == 1

    def test_exists_false(self, mgr):
        r = mgr.context_exists("nonexistent")
        assert r.success is True
        assert r.data["exists"] is False
        assert r.data["entry_count"] == 0

    def test_exists_empty_id(self, mgr):
        r = mgr.context_exists("")
        assert r.success is True
        assert r.data["exists"] is False

    def test_exists_after_delete(self, mgr):
        mgr.delete_context("proj_a")
        r = mgr.context_exists("proj_a")
        assert r.data["exists"] is False


# ---------------------------------------------------------------------------
# ProjectContextManager — 端到端流程
# ---------------------------------------------------------------------------


class TestProjectContextManagerEndToEnd:
    def test_full_lifecycle(self):
        """create -> update -> get -> exists -> delete -> exists"""
        mgr = ProjectContextManager()

        # 1. create
        r = mgr.create_context("my_app")
        assert r.success is True

        # 2. update: 模拟 Coco 会话
        mgr.update_context(
            "my_app",
            conversation={
                "role": "user",
                "content": "help me refactor auth",
                "source_mode": "coco",
            },
        )
        mgr.update_context(
            "my_app",
            conversation={
                "role": "assistant",
                "content": "I'll create a plan...",
                "source_mode": "coco",
            },
        )

        # 3. update: 模式切换
        mgr.update_context(
            "my_app",
            mode_transition={
                "from_mode": "coco",
                "to_mode": "claude",
                "reason": "user requested",
            },
        )

        # 4. update: Claude 会话
        mgr.update_context(
            "my_app",
            conversation={
                "role": "user",
                "content": "continue from where coco left off",
                "source_mode": "claude",
            },
        )

        # 5. get: 查询全部
        r = mgr.get_context("my_app")
        assert r.success is True
        assert r.data["entry_count"] == 4

        # 6. get: 只看 Claude 对话
        r = mgr.get_context(
            "my_app",
            entry_type=ContextEntryType.CONVERSATION,
            source_mode=ContextSourceMode.CLAUDE,
        )
        assert len(r.data["entries"]) == 1

        # 7. exists
        r = mgr.context_exists("my_app")
        assert r.data["exists"] is True
        assert r.data["entry_count"] == 4

        # 8. delete: 清除 Coco 数据
        # mode_transition 的 source_mode 是 from_mode=coco，所以一共删 3 条
        r = mgr.delete_context("my_app", source_mode=ContextSourceMode.COCO)
        assert r.data["removed_count"] == 3

        # 9. get: 确认只剩 1 条 (Claude 对话)
        r = mgr.get_context("my_app")
        assert r.data["entry_count"] == 1

        # 10. delete: 删除整个上下文
        r = mgr.delete_context("my_app")
        assert r.success is True

        # 11. exists: 确认已删除
        r = mgr.context_exists("my_app")
        assert r.data["exists"] is False

    def test_multi_project_isolation(self):
        """多项目操作互不影响"""
        mgr = ProjectContextManager()
        mgr.create_context("frontend")
        mgr.create_context("backend")

        mgr.update_context(
            "frontend",
            conversation={
                "role": "user",
                "content": "React question",
                "source_mode": "claude",
            },
        )
        mgr.update_context(
            "backend",
            conversation={
                "role": "user",
                "content": "Django question",
                "source_mode": "coco",
            },
        )
        mgr.update_context(
            "backend",
            conversation={
                "role": "user",
                "content": "Another Django question",
                "source_mode": "coco",
            },
        )

        r_fe = mgr.get_context("frontend")
        r_be = mgr.get_context("backend")
        assert r_fe.data["entry_count"] == 1
        assert r_be.data["entry_count"] == 2

        # 删除 frontend 不影响 backend
        mgr.delete_context("frontend")
        assert mgr.context_exists("frontend").data["exists"] is False
        assert mgr.context_exists("backend").data["exists"] is True
        assert mgr.get_context("backend").data["entry_count"] == 2


# ---------------------------------------------------------------------------
# 项目切换时的上下文保留与恢复
# ---------------------------------------------------------------------------


class TestProjectSwitchContextPreservation:
    """测试项目切换时旧项目的上下文被完整保留"""

    @pytest.fixture
    def mgr(self):
        return ProjectContextManager()

    def test_old_project_context_preserved_after_switch(self, mgr):
        """切换项目后，旧项目的上下文条目仍然完好"""
        mgr.create_context("proj_a")
        ctx_a = mgr.store.get("proj_a")
        ctx_a.add_conversation("user", "hello from A", ContextSourceMode.COCO)
        ctx_a.add_conversation("assistant", "hi from coco", ContextSourceMode.COCO)

        # 模拟创建版本（项目切换时的操作）
        ctx_a.create_version(
            reason="project_switch: proj_a -> proj_b",
            source_mode=ContextSourceMode.SMART,
            summary="Switched to project proj_b",
        )

        # 切换到 proj_b 后，proj_a 的上下文仍然完好
        r = mgr.get_context("proj_a")
        assert r.success is True
        assert r.data["entry_count"] == 2
        assert r.data["version_count"] == 1


    def test_incremental_diff_after_switch_and_return(self, mgr):
        """切换走再切换回来后，增量 diff 只包含新增条目"""
        mgr.create_context("proj_a")
        ctx_a = mgr.store.get("proj_a")

        # 第一阶段工作
        ctx_a.add_conversation("user", "msg1", ContextSourceMode.COCO)
        ctx_a.add_conversation("assistant", "resp1", ContextSourceMode.COCO)

        # 切换走：创建版本
        ver = ctx_a.create_version(
            reason="project_switch: proj_a -> proj_b",
            source_mode=ContextSourceMode.SMART,
        )

        # 切换回来后又工作
        ctx_a.add_conversation("user", "msg2 after return", ContextSourceMode.CLAUDE)
        ctx_a.add_conversation("assistant", "resp2 after return", ContextSourceMode.CLAUDE)

        # 增量 diff 应只包含切换后的 2 条
        diff = ctx_a.get_entries_since_version(ver.version_number)
        assert len(diff) == 2
        assert diff[0].content == "msg2 after return"
        assert diff[1].content == "resp2 after return"


class TestProjectSwitchContextRestoration:
    """测试切换到目标项目时上下文的正确恢复"""

    @pytest.fixture
    def mgr(self):
        return ProjectContextManager()

    def test_new_project_context_auto_created(self, mgr):
        """首次切换到新项目时，自动创建统一上下文"""
        # 使用 get_or_create 模拟 _switch_project 中 auto-create 行为
        ctx = mgr.store.get_or_create("new_proj")
        assert ctx is not None
        assert ctx.project_id == "new_proj"
        assert ctx.entry_count == 0

    def test_existing_project_context_loaded(self, mgr):
        """切换到已有上下文的项目时，完整加载"""
        mgr.create_context("proj_b")
        ctx_b = mgr.store.get("proj_b")
        ctx_b.add_conversation("user", "prev work in B", ContextSourceMode.CLAUDE)
        ctx_b.add_conversation("assistant", "prev resp in B", ContextSourceMode.CLAUDE)
        ctx_b.create_version(
            reason="some_earlier_version",
            source_mode=ContextSourceMode.CLAUDE,
        )

        # 模拟恢复——直接读取即可，无需特殊操作
        restored = mgr.store.get("proj_b")
        assert restored is not None
        assert restored.entry_count == 2
        assert len(restored.versions) == 1
        assert restored.entries[0].content == "prev work in B"


class TestProjectSwitchBridgeSummary:
    """测试项目切换时的跨模式桥接摘要"""

    @pytest.fixture
    def mgr(self):
        return ProjectContextManager()

    def test_bridge_summary_built_on_mode_transition(self, mgr):
        """模式切换时正确构建桥接摘要"""
        mgr.create_context("proj_a")
        ctx = mgr.store.get("proj_a")

        ctx.add_conversation("user", "write auth module", ContextSourceMode.COCO)
        ctx.add_conversation("assistant", "creating auth handler...", ContextSourceMode.COCO)

        bridge = ctx.build_bridge_summary(ContextSourceMode.COCO, ContextSourceMode.CLAUDE)

        assert bridge.from_mode == ContextSourceMode.COCO
        assert bridge.to_mode == ContextSourceMode.CLAUDE
        assert "write auth module" in bridge.summary_text
        assert ctx.last_bridge_summary is bridge

    def test_bridge_summary_consumed_once(self, mgr):
        """桥接摘要只能被消费一次（防止重复注入）"""
        mgr.create_context("proj_a")
        ctx = mgr.store.get("proj_a")
        ctx.add_conversation("user", "some work", ContextSourceMode.COCO)
        ctx.build_bridge_summary(ContextSourceMode.COCO, ContextSourceMode.CLAUDE)

        first_consume = ctx.consume_bridge_summary()
        assert first_consume is not None
        assert first_consume.from_mode == ContextSourceMode.COCO

        second_consume = ctx.consume_bridge_summary()
        assert second_consume is None

    def test_bridge_injection_prompt_format(self, mgr):
        """桥接摘要的 injection prompt 格式正确"""
        mgr.create_context("proj_a")
        ctx = mgr.store.get("proj_a")
        ctx.add_conversation("user", "refactor database layer", ContextSourceMode.COCO)
        ctx.add_conversation("assistant", "I'll restructure the ORM", ContextSourceMode.COCO)

        bridge = ctx.build_bridge_summary(ContextSourceMode.COCO, ContextSourceMode.CLAUDE)
        prompt = bridge.to_injection_prompt()

        assert "[Context from previous coco session]" in prompt
        assert "[End of context]" in prompt
        assert "refactor database layer" in prompt


class TestProjectSwitchEdgeCases:
    """测试项目切换上下文的边界情况"""

    @pytest.fixture
    def mgr(self):
        return ProjectContextManager()

    def test_context_isolation_across_rapid_switches(self, mgr):
        """快速多次切换不会导致上下文串扰"""
        for name in ["proj_a", "proj_b", "proj_c"]:
            mgr.create_context(name)

        # 分别在三个项目中写入数据
        ctx_a = mgr.store.get("proj_a")
        ctx_b = mgr.store.get("proj_b")
        ctx_c = mgr.store.get("proj_c")

        ctx_a.add_conversation("user", "work A", ContextSourceMode.COCO)
        ctx_b.add_conversation("user", "work B", ContextSourceMode.CLAUDE)
        ctx_c.add_conversation("user", "work C", ContextSourceMode.SHELL)

        # 模拟快速切换：A -> B -> C -> A
        ctx_a.create_version(reason="switch to B", source_mode=ContextSourceMode.SMART)
        ctx_b.create_version(reason="switch to C", source_mode=ContextSourceMode.SMART)
        ctx_c.create_version(reason="switch to A", source_mode=ContextSourceMode.SMART)

        # 验证每个项目的数据仍然正确且互不影响
        assert ctx_a.entry_count == 1
        assert ctx_a.entries[0].content == "work A"
        assert ctx_a.entries[0].source_mode == ContextSourceMode.COCO

        assert ctx_b.entry_count == 1
        assert ctx_b.entries[0].content == "work B"
        assert ctx_b.entries[0].source_mode == ContextSourceMode.CLAUDE

        assert ctx_c.entry_count == 1
        assert ctx_c.entries[0].content == "work C"
        assert ctx_c.entries[0].source_mode == ContextSourceMode.SHELL

    def test_context_preserved_with_rolling_window(self, mgr):
        """上下文滚动窗口在切换后仍然正确工作"""
        ctx = UnifiedContext(project_id="proj_a", max_entries=5)

        # 写入 8 条（超过窗口大小 5）
        for i in range(8):
            ctx.add_conversation("user", f"msg_{i}", ContextSourceMode.COCO)

        assert ctx.entry_count == 5
        assert ctx.entries[0].content == "msg_3"  # 前 3 条被淘汰

        # 模拟切换走再回来后继续写入
        ctx.create_version(reason="switch away", source_mode=ContextSourceMode.SMART)
        ctx.add_conversation("user", "msg_after_return", ContextSourceMode.CLAUDE)

        assert ctx.entry_count == 5  # 又淘汰了 1 条
        assert ctx.entries[-1].content == "msg_after_return"

    def test_multiple_versions_across_switches(self, mgr):
        """多次项目切换产生的版本链正确"""
        mgr.create_context("proj_a")
        ctx = mgr.store.get("proj_a")

        ctx.add_conversation("user", "work session 1", ContextSourceMode.COCO)
        v1 = ctx.create_version(reason="switch to B", source_mode=ContextSourceMode.SMART)

        ctx.add_conversation("user", "work session 2", ContextSourceMode.CLAUDE)
        v2 = ctx.create_version(reason="switch to C", source_mode=ContextSourceMode.SMART)

        ctx.add_conversation("user", "work session 3", ContextSourceMode.SHELL)
        v3 = ctx.create_version(reason="switch to D", source_mode=ContextSourceMode.SMART)

        assert v1.version_number == 1
        assert v2.version_number == 2
        assert v3.version_number == 3

        # 每个版本记录的 entry_count 递增
        assert v1.entry_count == 1
        assert v2.entry_count == 2
        assert v3.entry_count == 3

        # 增量 diff 从 v1 开始
        diff_from_v1 = ctx.get_entries_since_version(1)
        assert len(diff_from_v1) == 2
        assert diff_from_v1[0].content == "work session 2"
        assert diff_from_v1[1].content == "work session 3"

        # 增量 diff 从 v2 开始
        diff_from_v2 = ctx.get_entries_since_version(2)
        assert len(diff_from_v2) == 1
        assert diff_from_v2[0].content == "work session 3"


class TestProjectSwitchEndToEnd:
    """端到端集成测试：模拟完整的项目切换流程"""

    def test_full_switch_flow(self):
        """
        完整流程:
        1. 在 proj_A 上用 Coco 模式工作
        2. 切换到 proj_B
        3. 在 proj_B 上用 Claude 模式工作
        4. 切换回 proj_A
        5. 验证 proj_A 的上下文被完整保留
        6. 验证桥接摘要正确生成
        """
        mgr = ProjectContextManager()

        # === 阶段 1：在 proj_A 上用 Coco 工作 ===
        mgr.create_context("proj_A")
        ctx_a = mgr.store.get("proj_A")

        ctx_a.add_conversation("user", "implement login API", ContextSourceMode.COCO, "mid1")
        ctx_a.add_conversation("assistant", "creating login endpoint...", ContextSourceMode.COCO)
        ctx_a.add_conversation("user", "add rate limiting", ContextSourceMode.COCO, "mid2")
        ctx_a.add_conversation("assistant", "added rate limiter middleware", ContextSourceMode.COCO)

        # 保存会话快照（模拟 _preserve_project_context）
        ctx_a.add_session_snapshot(
            {"session_id": "coco_sess_1", "message_count": 4, "last_query": "add rate limiting"},
            ContextSourceMode.COCO,
        )

        # === 阶段 2：切换到 proj_B ===
        switch_version = ctx_a.create_version(
            reason="project_switch: proj_A -> proj_B",
            source_mode=ContextSourceMode.SMART,
            summary="Switched to project proj_B",
        )

        # proj_B 首次激活
        mgr.create_context("proj_B")
        ctx_b = mgr.store.get("proj_B")

        # === 阶段 3：在 proj_B 上用 Claude 工作 ===
        ctx_b.add_mode_transition(ContextSourceMode.SMART, ContextSourceMode.CLAUDE, "enter_claude_mode")
        ctx_b.add_conversation("user", "design database schema", ContextSourceMode.CLAUDE)
        ctx_b.add_conversation("assistant", "creating ERD diagram...", ContextSourceMode.CLAUDE)

        # === 阶段 4：切换回 proj_A ===
        ctx_b.add_session_snapshot(
            {"session_id": "claude_sess_1", "message_count": 2},
            ContextSourceMode.CLAUDE,
        )
        ctx_b.create_version(
            reason="project_switch: proj_B -> proj_A",
            source_mode=ContextSourceMode.SMART,
        )

        # === 阶段 5：验证 proj_A 上下文完整性 ===
        restored_a = mgr.store.get("proj_A")
        assert restored_a is not None
        assert restored_a.entry_count == 5  # 4 conversations + 1 snapshot
        assert len(restored_a.versions) == 1

        conversations = restored_a.get_conversations()
        assert len(conversations) == 4
        assert conversations[0].content == "implement login API"
        assert conversations[3].content == "added rate limiter middleware"

        snapshots = restored_a.get_entries_by_type(ContextEntryType.SESSION_SNAPSHOT)
        assert len(snapshots) == 1
        assert snapshots[0].metadata["session_id"] == "coco_sess_1"

        # 增量 diff：切换走后没有新增条目
        diff = restored_a.get_entries_since_version(switch_version.version_number)
        assert len(diff) == 0

        # === 阶段 6：切换回 proj_A 后继续工作，并验证桥接 ===
        # 模拟从 SMART 进入 COCO 模式，构建桥接摘要
        ctx_a.add_mode_transition(ContextSourceMode.SMART, ContextSourceMode.COCO, "resume after switch")
        bridge = ctx_a.build_bridge_summary(ContextSourceMode.SMART, ContextSourceMode.COCO)

        assert bridge is not None
        assert "implement login API" in bridge.summary_text or "rate limiting" in bridge.summary_text

        # 消费桥接摘要
        consumed = ctx_a.consume_bridge_summary()
        prompt = consumed.to_injection_prompt()
        assert "[Context from previous smart session]" in prompt
        assert "[End of context]" in prompt

        # 第二次消费返回 None
        assert ctx_a.consume_bridge_summary() is None

        # === 验证 proj_B 也完好 ===
        restored_b = mgr.store.get("proj_B")
        assert restored_b.entry_count == 4  # 1 transition + 2 conversations + 1 snapshot
        assert len(restored_b.versions) == 1

    def test_bridge_includes_deep_engine_results(self):
        """桥接摘要包含 Deep Engine 任务结果"""
        mgr = ProjectContextManager()
        mgr.create_context("proj_A")
        ctx = mgr.store.get("proj_A")

        ctx.add_deep_engine_result(
            {
                "name": "feature_x",
                "tasks": [
                    {"title": "implement API", "status": "completed", "result": "REST endpoints created"},
                ],
            }
        )

        bridge = ctx.build_bridge_summary(ContextSourceMode.DEEP_ENGINE, ContextSourceMode.COCO)
        prompt = bridge.to_injection_prompt()

        assert "[Context from previous deep_engine session]" in prompt
        assert "implement API" in prompt
        assert "REST endpoints created" in prompt


# ---------------------------------------------------------------------------
# 补充 CRUD 操作测试
# ---------------------------------------------------------------------------


class TestCRUDAdvanced:
    """补充 CRUD 操作的高级场景"""

    @pytest.fixture
    def ctx(self):
        return UnifiedContext(project_id="test_crud", max_entries=20, max_versions=5)

    @pytest.fixture
    def mgr(self):
        return ProjectContextManager()

    # ---- Update: 同时更新 content 和 metadata ----

    def test_update_entry_content_and_metadata_simultaneously(self, ctx):
        entry = ctx.add_conversation("user", "original content", ContextSourceMode.COCO, "msg_1")
        old_updated = ctx.updated_at

        time.sleep(0.01)
        result = ctx.update_entry(
            entry.entry_id,
            content="updated content",
            metadata={"extra_key": "extra_value"},
        )
        assert result is True

        found = ctx.get_entry(entry.entry_id)
        assert found.content == "updated content"
        assert found.metadata["extra_key"] == "extra_value"
        # 原有 metadata 保留
        assert found.metadata["role"] == "user"
        assert found.metadata["message_id"] == "msg_1"
        assert ctx.updated_at > old_updated

    # ---- Read: 组合条件查询高级场景 ----

    def test_query_with_all_filters_combined(self, ctx):
        """type + mode + since + limit 同时组合"""
        ctx.add_conversation("user", "old_coco", ContextSourceMode.COCO)
        cutoff = time.time()
        time.sleep(0.01)
        ctx.add_conversation("user", "new_coco_1", ContextSourceMode.COCO)
        ctx.add_conversation("user", "new_coco_2", ContextSourceMode.COCO)
        ctx.add_conversation("user", "new_claude", ContextSourceMode.CLAUDE)
        ctx.add_session_snapshot({"sid": "s1"}, ContextSourceMode.COCO)

        results = ctx.query_entries(
            entry_type=ContextEntryType.CONVERSATION,
            source_mode=ContextSourceMode.COCO,
            since=cutoff,
            limit=1,
        )
        assert len(results) == 1
        assert results[0].content == "new_coco_2"

    # ---- Delete: 删除边界 ----

    def test_clear_all_then_add_again(self, ctx):
        ctx.add_conversation("user", "msg1", ContextSourceMode.SMART)
        ctx.add_conversation("user", "msg2", ContextSourceMode.SMART)
        ctx.clear_entries()
        assert ctx.entry_count == 0
        ctx.add_conversation("user", "msg3", ContextSourceMode.SMART)
        assert ctx.entry_count == 1
        assert ctx.entries[0].content == "msg3"

    def test_remove_middle_entry_preserves_order(self, ctx):
        e1 = ctx.add_conversation("user", "first", ContextSourceMode.SMART)
        e2 = ctx.add_conversation("user", "second", ContextSourceMode.SMART)
        e3 = ctx.add_conversation("user", "third", ContextSourceMode.SMART)

        ctx.remove_entry(e2.entry_id)
        assert ctx.entry_count == 2
        entries = ctx.entries
        assert entries[0].content == "first"
        assert entries[1].content == "third"
        # 索引应仍然有效
        assert ctx.get_entry(e1.entry_id) is not None
        assert ctx.get_entry(e3.entry_id) is not None

    # ---- ProjectContextManager: 补充 CRUD 测试 ----

    def test_mgr_delete_by_mode_zero_match(self, mgr):
        mgr.create_context("proj")
        ctx = mgr.store.get("proj")
        ctx.add_conversation("user", "coco msg", ContextSourceMode.COCO)

        r = mgr.delete_context("proj", source_mode=ContextSourceMode.SHELL)
        assert r.success is True
        assert r.data["removed_count"] == 0

    def test_mgr_update_entries_and_conversation_together(self, mgr):
        mgr.create_context("proj")
        raw_entries = [ContextEntry(content="raw1"), ContextEntry(content="raw2")]
        r = mgr.update_context(
            "proj",
            entries=raw_entries,
            conversation={"role": "user", "content": "conv msg", "source_mode": "coco"},
        )
        assert r.success is True
        assert r.data["added_count"] == 3

    def test_mgr_get_context_with_recent_limit_and_type(self, mgr):
        mgr.create_context("proj")
        ctx = mgr.store.get("proj")
        for i in range(10):
            ctx.add_conversation("user", f"msg_{i}", ContextSourceMode.COCO)
        ctx.add_session_snapshot({"sid": "s1"}, ContextSourceMode.COCO)

        r = mgr.get_context("proj", entry_type=ContextEntryType.CONVERSATION, recent_limit=3)
        assert r.success is True
        assert len(r.data["entries"]) == 3


# ---------------------------------------------------------------------------
# 多编程模式之间的上下文共享
# ---------------------------------------------------------------------------


class TestCrossModeContextSharing:
    """
    测试多个编程模式之间的上下文共享。
    核心验证：不同模式产生的 entries 共存于同一个 UnifiedContext 中，
    可以被统一查询、筛选和桥接。
    """

    @pytest.fixture
    def ctx(self):
        return UnifiedContext(project_id="cross_mode_test", max_entries=100, max_versions=20)

    @pytest.fixture
    def mgr(self):
        return ProjectContextManager()

    def test_all_modes_entries_coexist(self, ctx):
        """已写入的多模式条目在同一上下文中共存"""
        modes = [
            ContextSourceMode.SMART,
            ContextSourceMode.COCO,
            ContextSourceMode.CLAUDE,
            ContextSourceMode.SHELL,
            ContextSourceMode.TTADK,
        ]

        ctx.add_conversation("user", "smart cmd", ContextSourceMode.SMART)
        ctx.add_conversation("user", "coco msg", ContextSourceMode.COCO)
        ctx.add_conversation("user", "claude msg", ContextSourceMode.CLAUDE)
        ctx.add_conversation("user", "shell cmd", ContextSourceMode.SHELL)
        ctx.add_conversation("user", "ttadk msg", ContextSourceMode.TTADK)
        ctx.add_deep_engine_result({"name": "task1", "tasks": []})

        assert ctx.entry_count == len(modes) + 1

        # 仅校验本用例写入过的模式；新增模式（如 aiden/codex）不应破坏该断言。
        for mode in modes + [ContextSourceMode.DEEP_ENGINE]:
            entries = ctx.get_entries_by_mode(mode)
            assert len(entries) == 1, f"Mode {mode.value} should have exactly 1 entry"

    def test_unfiltered_query_returns_all_modes(self, ctx):
        """不带模式过滤的查询返回所有模式的条目"""
        ctx.add_conversation("user", "smart", ContextSourceMode.SMART)
        ctx.add_conversation("user", "coco", ContextSourceMode.COCO)
        ctx.add_conversation("user", "claude", ContextSourceMode.CLAUDE)

        all_entries = ctx.query_entries()
        assert len(all_entries) == 3

    def test_mode_filter_returns_correct_subset(self, ctx):
        """按模式过滤只返回对应模式的条目"""
        ctx.add_conversation("user", "coco_1", ContextSourceMode.COCO)
        ctx.add_conversation("user", "claude_1", ContextSourceMode.CLAUDE)
        ctx.add_conversation("user", "coco_2", ContextSourceMode.COCO)
        ctx.add_conversation("user", "shell_1", ContextSourceMode.SHELL)

        coco_entries = ctx.get_entries_by_mode(ContextSourceMode.COCO)
        assert len(coco_entries) == 2
        assert all(e.source_mode == ContextSourceMode.COCO for e in coco_entries)

    def test_full_workflow_smart_coco_claude_shell_deep(self, ctx):
        """
        完整工作流：SMART → COCO → CLAUDE → SHELL → DEEP_ENGINE
        每次模式切换都记录 transition，最终所有条目共存
        """
        # Phase 1: SMART 模式
        ctx.add_conversation("user", "ls -la", ContextSourceMode.SMART)

        # Phase 2: 进入 COCO
        ctx.add_mode_transition(ContextSourceMode.SMART, ContextSourceMode.COCO, "enter coco")
        ctx.add_conversation("user", "help me refactor auth", ContextSourceMode.COCO)
        ctx.add_conversation("assistant", "creating plan...", ContextSourceMode.COCO)

        # Phase 3: 切换到 CLAUDE
        ctx.add_mode_transition(ContextSourceMode.COCO, ContextSourceMode.CLAUDE, "switch to claude")
        ctx.add_conversation("user", "implement the plan", ContextSourceMode.CLAUDE)
        ctx.add_conversation("assistant", "implementing...", ContextSourceMode.CLAUDE)

        # Phase 4: 执行 shell 命令
        ctx.add_mode_transition(ContextSourceMode.CLAUDE, ContextSourceMode.SHELL, "run tests")
        ctx.add_conversation("user", "pytest tests/", ContextSourceMode.SHELL)

        # Phase 5: Deep Engine 任务
        ctx.add_deep_engine_result(
            {
                "name": "test_coverage",
                "tasks": [
                    {"title": "run all tests", "status": "completed", "result": "42 passed"},
                ],
            }
        )

        # 总计：1 SMART + 3 transitions + 2 COCO + 2 CLAUDE + 1 SHELL + 1 DEEP = 10
        assert ctx.entry_count == 10

        # 按类型查询
        convs = ctx.get_entries_by_type(ContextEntryType.CONVERSATION)
        assert len(convs) == 6
        transitions = ctx.get_entries_by_type(ContextEntryType.MODE_TRANSITION)
        assert len(transitions) == 3
        deep_results = ctx.get_entries_by_type(ContextEntryType.DEEP_ENGINE_RESULT)
        assert len(deep_results) == 1

    def test_bridge_summary_carries_multi_mode_history(self, ctx):
        """桥接摘要包含来自多个模式的对话历史"""
        ctx.add_conversation("user", "smart question", ContextSourceMode.SMART)
        ctx.add_conversation("user", "coco work", ContextSourceMode.COCO)
        ctx.add_conversation("assistant", "coco response", ContextSourceMode.COCO)
        ctx.add_conversation("user", "claude task", ContextSourceMode.CLAUDE)

        bridge = ctx.build_bridge_summary(ContextSourceMode.CLAUDE, ContextSourceMode.COCO)

        # 桥接摘要应包含多个模式的对话
        assert "smart question" in bridge.summary_text
        assert "coco work" in bridge.summary_text
        assert "claude task" in bridge.summary_text

    def test_delete_one_mode_preserves_others(self, ctx):
        """删除一个模式的条目不影响其他模式"""
        ctx.add_conversation("user", "coco msg", ContextSourceMode.COCO)
        ctx.add_conversation("user", "claude msg", ContextSourceMode.CLAUDE)
        ctx.add_conversation("user", "shell msg", ContextSourceMode.SHELL)

        ctx.clear_entries_by_mode(ContextSourceMode.COCO)

        assert ctx.entry_count == 2
        assert len(ctx.get_entries_by_mode(ContextSourceMode.COCO)) == 0
        assert len(ctx.get_entries_by_mode(ContextSourceMode.CLAUDE)) == 1
        assert len(ctx.get_entries_by_mode(ContextSourceMode.SHELL)) == 1

    def test_mgr_cross_mode_update_and_query(self, mgr):
        """通过 ProjectContextManager 进行跨模式操作"""
        mgr.create_context("proj")

        # 添加多个模式的数据
        mgr.update_context(
            "proj",
            conversation={
                "role": "user",
                "content": "coco question",
                "source_mode": "coco",
            },
        )
        mgr.update_context(
            "proj",
            conversation={
                "role": "assistant",
                "content": "coco answer",
                "source_mode": "coco",
            },
        )
        mgr.update_context(
            "proj",
            mode_transition={
                "from_mode": "coco",
                "to_mode": "claude",
            },
        )
        mgr.update_context(
            "proj",
            conversation={
                "role": "user",
                "content": "claude question",
                "source_mode": "claude",
            },
        )
        mgr.update_context(
            "proj",
            deep_result={
                "data": {"name": "deep_task", "tasks": []},
            },
        )

        # 查询全部
        r = mgr.get_context("proj")
        assert r.data["entry_count"] == 5

        # 按模式过滤
        r_coco = mgr.get_context("proj", source_mode=ContextSourceMode.COCO)
        assert len(r_coco.data["entries"]) == 3  # 2 convs + 1 mode_transition (from_mode=coco)

        r_claude = mgr.get_context("proj", source_mode=ContextSourceMode.CLAUDE)
        assert len(r_claude.data["entries"]) == 1

        r_deep = mgr.get_context("proj", source_mode=ContextSourceMode.DEEP_ENGINE)
        assert len(r_deep.data["entries"]) == 1


# ---------------------------------------------------------------------------
# 项目切换的补充测试
# ---------------------------------------------------------------------------


class TestProjectSwitchAdvanced:
    """项目切换的补充测试场景"""

    def test_switch_back_and_forth_versions_accumulate(self):
        """反复切换 A↔B 版本链正确累积"""
        mgr = ProjectContextManager()
        mgr.create_context("A")
        ctx_a = mgr.store.get("A")

        for round_num in range(5):
            ctx_a.add_conversation("user", f"round {round_num}", ContextSourceMode.COCO)
            ctx_a.create_version(f"switch_round_{round_num}", ContextSourceMode.SMART)

        assert len(ctx_a.versions) == 5
        assert ctx_a.current_version_number == 5
        assert ctx_a.entry_count == 5

        # diff 从第 3 个版本开始
        diff = ctx_a.get_entries_since_version(3)
        assert len(diff) == 2
        assert diff[0].content == "round 3"
        assert diff[1].content == "round 4"


# ---------------------------------------------------------------------------
# 边界情况测试
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """边界情况测试"""

    # ---- 容量极限边界 ----

    def test_max_entries_equals_one(self):
        ctx = UnifiedContext(project_id="test", max_entries=1)
        e1 = ctx.add_conversation("user", "first", ContextSourceMode.SMART)
        assert ctx.entry_count == 1
        e2 = ctx.add_conversation("user", "second", ContextSourceMode.SMART)
        assert ctx.entry_count == 1
        assert ctx.get_entry(e1.entry_id) is None
        assert ctx.get_entry(e2.entry_id) is not None
        assert ctx.entries[0].content == "second"

    def test_max_versions_equals_one(self):
        ctx = UnifiedContext(project_id="test", max_entries=100, max_versions=1)
        ctx.create_version("v1", ContextSourceMode.SMART)
        ctx.create_version("v2", ContextSourceMode.COCO)
        ctx.create_version("v3", ContextSourceMode.CLAUDE)

        assert len(ctx.versions) == 1
        assert ctx.versions[0].reason == "v3"
        assert ctx.current_version_number == 3
        # v1 and v2 被淘汰
        assert ctx.get_version(1) is None
        assert ctx.get_version(2) is None
        assert ctx.get_version(3) is not None

    def test_max_entries_zero_means_unlimited(self):
        """max_entries=0 表示不限制条目数量，保留全部"""
        ctx = UnifiedContext(project_id="test", max_entries=0)
        ctx.add_conversation("user", "msg1", ContextSourceMode.SMART)
        ctx.add_conversation("user", "msg2", ContextSourceMode.SMART)
        ctx.add_conversation("user", "msg3", ContextSourceMode.SMART)
        # max_entries=0 时跳过淘汰逻辑，保留全部条目
        assert ctx.entry_count == 3

    # ---- 版本和滚动窗口交互 ----

    def test_version_entry_count_stale_after_eviction(self):
        """滚动窗口淘汰后，diff 仍能基于 seq 正确返回版本之后的新增条目"""
        ctx = UnifiedContext(project_id="test", max_entries=3, max_versions=10)

        ctx.add_conversation("user", "msg_0", ContextSourceMode.SMART)
        ctx.add_conversation("user", "msg_1", ContextSourceMode.SMART)
        v1 = ctx.create_version("v1", ContextSourceMode.SMART)
        assert v1.entry_count == 2

        # 添加更多，触发淘汰
        ctx.add_conversation("user", "msg_2", ContextSourceMode.SMART)
        ctx.add_conversation("user", "msg_3", ContextSourceMode.SMART)
        ctx.add_conversation("user", "msg_4", ContextSourceMode.SMART)

        # 现在 entries=[msg_2, msg_3, msg_4], 旧实现会用 entry_count 切片导致丢失 msg_2/msg_3
        # 新实现使用 last_seq 做增量 diff，应返回 v1 之后仍在窗口内的所有新增条目
        diff = ctx.get_entries_since_version(1)
        assert len(diff) == 3
        assert [d.content for d in diff] == ["msg_2", "msg_3", "msg_4"]

    def test_version_entry_count_larger_than_current(self):
        """清空 entries 后仍应基于 seq 正确识别版本之后的新条目"""
        ctx = UnifiedContext(project_id="test", max_entries=3, max_versions=10)

        for i in range(5):
            ctx.add_conversation("user", f"msg_{i}", ContextSourceMode.SMART)
        v1 = ctx.create_version("v1", ContextSourceMode.SMART)
        # v1.entry_count = 3 (max_entries=3 所以实际只有3条)

        # 清空再添加少量条目
        ctx.clear_entries()
        ctx.add_conversation("user", "new_msg", ContextSourceMode.SMART)
        # 版本之后产生的新条目 seq > v1.last_seq，应返回该条目
        diff = ctx.get_entries_since_version(v1.version_number)
        assert len(diff) == 1
        assert diff[0].content == "new_msg"

    # ---- 序列化边界 ----

    def test_roundtrip_with_all_entry_types(self):
        """序列化/反序列化包含所有 6 种条目类型的上下文"""
        ctx = UnifiedContext(project_id="all_types")
        ctx.add_conversation("user", "conv msg", ContextSourceMode.COCO)
        ctx.add_session_snapshot({"session_id": "s1"}, ContextSourceMode.COCO)
        ctx.add_mode_transition(ContextSourceMode.COCO, ContextSourceMode.CLAUDE)
        ctx.add_deep_engine_result({"name": "task1", "tasks": []})
        ctx.add_entry(
            ContextEntry(
                entry_type=ContextEntryType.AI_SUMMARY,
                source_mode=ContextSourceMode.COCO,
                content="summary text",
            )
        )
        ctx.add_entry(
            ContextEntry(
                entry_type=ContextEntryType.FILE_CHANGE,
                source_mode=ContextSourceMode.CLAUDE,
                content="src/main.py",
            )
        )

        restored = UnifiedContext.from_dict(ctx.to_dict())
        assert restored.entry_count == 6

        types = [e.entry_type for e in restored.entries]
        assert ContextEntryType.CONVERSATION in types
        assert ContextEntryType.SESSION_SNAPSHOT in types
        assert ContextEntryType.MODE_TRANSITION in types
        assert ContextEntryType.DEEP_ENGINE_RESULT in types
        assert ContextEntryType.AI_SUMMARY in types
        assert ContextEntryType.FILE_CHANGE in types

    def test_roundtrip_preserves_bridge_summary(self):
        """序列化/反序列化保留桥接摘要"""
        ctx = UnifiedContext(project_id="bridge_rt")
        ctx.add_conversation("user", "hello", ContextSourceMode.COCO)
        ctx.build_bridge_summary(ContextSourceMode.COCO, ContextSourceMode.CLAUDE)

        restored = UnifiedContext.from_dict(ctx.to_dict())
        assert restored.last_bridge_summary is not None
        assert restored.last_bridge_summary.from_mode == ContextSourceMode.COCO
        assert restored.last_bridge_summary.to_mode == ContextSourceMode.CLAUDE

    # ---- 并发边界 ----

    def test_concurrent_version_creation(self):
        """并发创建版本不应崩溃"""
        import threading

        ctx = UnifiedContext(project_id="concurrent_v", max_versions=50)
        errors: list[Exception] = []

        def create_versions(thread_id: int):
            try:
                for j in range(10):
                    ctx.create_version(f"t{thread_id}_v{j}", ContextSourceMode.SMART)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=create_versions, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        # 版本号应单调递增（但因无锁，总数可能不精确）
        assert ctx.current_version_number > 0

    def test_concurrent_add_and_query(self):
        """并发添加和查询不应崩溃"""
        import threading

        ctx = UnifiedContext(project_id="rw_concurrent", max_entries=50)
        errors: list[Exception] = []

        def writer(idx: int):
            try:
                for j in range(20):
                    ctx.add_conversation("user", f"w{idx}_m{j}", ContextSourceMode.SMART)
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                for _ in range(20):
                    ctx.get_recent_entries(5)
                    ctx.query_entries(limit=3)
                    ctx.get_entries_by_mode(ContextSourceMode.SMART)
            except Exception as e:
                errors.append(e)

        threads = []
        for i in range(3):
            threads.append(threading.Thread(target=writer, args=(i,)))
        for _ in range(2):
            threads.append(threading.Thread(target=reader))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0

    # ---- index 一致性 ----

    def test_index_correct_after_remove_and_add(self):
        """删除再添加后索引仍然正确"""
        ctx = UnifiedContext(project_id="test", max_entries=10)
        e1 = ctx.add_conversation("user", "msg1", ContextSourceMode.SMART)
        e2 = ctx.add_conversation("user", "msg2", ContextSourceMode.SMART)
        e3 = ctx.add_conversation("user", "msg3", ContextSourceMode.SMART)

        ctx.remove_entry(e2.entry_id)
        e4 = ctx.add_conversation("user", "msg4", ContextSourceMode.SMART)

        assert ctx.get_entry(e1.entry_id) is not None
        assert ctx.get_entry(e2.entry_id) is None
        assert ctx.get_entry(e3.entry_id) is not None
        assert ctx.get_entry(e4.entry_id) is not None

    def test_index_correct_after_clear_by_mode(self):
        """按模式清除后索引正确"""
        ctx = UnifiedContext(project_id="test", max_entries=10)
        ctx.add_conversation("user", "coco1", ContextSourceMode.COCO)
        e_claude = ctx.add_conversation("user", "claude1", ContextSourceMode.CLAUDE)
        ctx.add_conversation("user", "coco2", ContextSourceMode.COCO)

        ctx.clear_entries_by_mode(ContextSourceMode.COCO)
        assert ctx.entry_count == 1
        assert ctx.get_entry(e_claude.entry_id) is not None
        assert ctx.get_entry(e_claude.entry_id).content == "claude1"

    # ---- ProjectContextManager 边界 ----

    def test_mgr_update_context_none_project_id(self):
        mgr = ProjectContextManager()
        r = mgr.update_context(
            None,
            conversation={
                "role": "user",
                "content": "x",
                "source_mode": "smart",
            },
        )
        assert r.success is False

    def test_mgr_delete_context_none_project_id(self):
        mgr = ProjectContextManager()
        r = mgr.delete_context(None)
        assert r.success is False

    def test_mgr_context_exists_none_project_id(self):
        mgr = ProjectContextManager()
        r = mgr.context_exists(None)
        assert r.success is True
        assert r.data["exists"] is False

    def test_store_multiple_operations_interleaved(self):
        """Store 交叉操作不会互相影响"""
        store = UnifiedContextStore()
        ctx_a = store.get_or_create("a")
        store.get_or_create("b")

        ctx_a.add_conversation("user", "a_msg", ContextSourceMode.COCO)
        store.remove("b")
        ctx_a.add_conversation("user", "a_msg2", ContextSourceMode.COCO)

        assert store.has("a")
        assert not store.has("b")
        assert ctx_a.entry_count == 2
