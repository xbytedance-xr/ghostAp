import time

import pytest

from src.project.unified_context import (
    ContextBridgeSummary,
    ContextEntry,
    ContextEntryType,
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

    def test_list_project_ids(self, store):
        store.get_or_create("alpha")
        store.get_or_create("beta")
        store.get_or_create("gamma")

        ids = store.list_project_ids()
        # Composite keys with default empty chat_id: ":alpha", ":beta", ":gamma"
        assert set(ids) == {":alpha", ":beta", ":gamma"}

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



# ---------------------------------------------------------------------------
# UnifiedContextStore — 复合键隔离 (chat_id:project_id)
# ---------------------------------------------------------------------------


class TestCompositeKeyIsolation:
    """同一 project_id 在不同 chat_id 下应拥有独立的上下文实例。"""

    def test_same_project_different_chats_are_isolated(self):
        store = UnifiedContextStore()
        ctx_chat1 = store.get_or_create("proj_a", chat_id="chat_1")
        ctx_chat2 = store.get_or_create("proj_a", chat_id="chat_2")

        assert ctx_chat1 is not ctx_chat2

        ctx_chat1.add_conversation("user", "msg from chat1", ContextSourceMode.COCO)
        assert ctx_chat1.entry_count == 1
        assert ctx_chat2.entry_count == 0

    def test_same_chat_different_projects_are_isolated(self):
        store = UnifiedContextStore()
        ctx_a = store.get_or_create("proj_a", chat_id="chat_1")
        ctx_b = store.get_or_create("proj_b", chat_id="chat_1")

        assert ctx_a is not ctx_b

        ctx_a.add_conversation("user", "proj_a msg", ContextSourceMode.COCO)
        ctx_b.add_conversation("user", "proj_b msg", ContextSourceMode.CLAUDE)
        assert ctx_a.entry_count == 1
        assert ctx_b.entry_count == 1
        assert ctx_a.entries[0].content == "proj_a msg"
        assert ctx_b.entries[0].content == "proj_b msg"

    def test_get_respects_chat_id(self):
        store = UnifiedContextStore()
        store.get_or_create("proj", chat_id="c1")

        assert store.get("proj", chat_id="c1") is not None
        assert store.get("proj", chat_id="c2") is None
        # Default empty chat_id also yields None
        assert store.get("proj") is None

    def test_has_respects_chat_id(self):
        store = UnifiedContextStore()
        store.get_or_create("proj", chat_id="c1")

        assert store.has("proj", chat_id="c1") is True
        assert store.has("proj", chat_id="c2") is False
        assert store.has("proj") is False

    def test_remove_respects_chat_id(self):
        store = UnifiedContextStore()
        store.get_or_create("proj", chat_id="c1")
        store.get_or_create("proj", chat_id="c2")

        assert store.remove("proj", chat_id="c1") is True
        assert store.has("proj", chat_id="c1") is False
        # c2 still exists
        assert store.has("proj", chat_id="c2") is True

    def test_list_project_ids_returns_composite_keys(self):
        store = UnifiedContextStore()
        store.get_or_create("proj_a", chat_id="c1")
        store.get_or_create("proj_a", chat_id="c2")
        store.get_or_create("proj_b", chat_id="c1")

        keys = set(store.list_project_ids())
        assert keys == {"c1:proj_a", "c2:proj_a", "c1:proj_b"}

    def test_backward_compat_empty_chat_id(self):
        """Default empty chat_id is backward compatible — same as no chat_id."""
        store = UnifiedContextStore()
        ctx1 = store.get_or_create("proj_x")
        ctx2 = store.get_or_create("proj_x", chat_id="")

        assert ctx1 is ctx2

    def test_stats_counts_all_composite_entries(self):
        store = UnifiedContextStore()
        ctx1 = store.get_or_create("proj", chat_id="c1")
        ctx2 = store.get_or_create("proj", chat_id="c2")
        ctx1.add_conversation("user", "m1", ContextSourceMode.SMART)
        ctx2.add_conversation("user", "m2", ContextSourceMode.SMART)
        ctx2.add_conversation("user", "m3", ContextSourceMode.SMART)

        stats = store.stats()
        assert stats["project_count"] == 2
        assert stats["total_entries"] == 3


class TestProjectContextManagerCompositeKey:
    """ProjectContextManager CRUD methods respect chat_id."""

    def test_create_and_get_with_chat_id(self):
        mgr = ProjectContextManager()
        r = mgr.create_context("proj", chat_id="c1")
        assert r.success is True

        r2 = mgr.get_context("proj", chat_id="c1")
        assert r2.success is True
        assert r2.data["entry_count"] == 0

        # Different chat_id should NOT find it
        r3 = mgr.get_context("proj", chat_id="c2")
        assert r3.success is False

    def test_update_with_chat_id(self):
        mgr = ProjectContextManager()
        mgr.create_context("proj", chat_id="c1")

        r = mgr.update_context(
            "proj",
            conversation={"role": "user", "content": "hello", "source_mode": "coco"},
            chat_id="c1",
        )
        assert r.success is True
        assert r.data["added_count"] == 1

    def test_delete_with_chat_id(self):
        mgr = ProjectContextManager()
        mgr.create_context("proj", chat_id="c1")
        mgr.create_context("proj", chat_id="c2")

        r = mgr.delete_context("proj", chat_id="c1")
        assert r.success is True

        assert mgr.context_exists("proj", chat_id="c1").data["exists"] is False
        assert mgr.context_exists("proj", chat_id="c2").data["exists"] is True

    def test_context_exists_with_chat_id(self):
        mgr = ProjectContextManager()
        mgr.create_context("proj", chat_id="c1")

        assert mgr.context_exists("proj", chat_id="c1").data["exists"] is True
        assert mgr.context_exists("proj", chat_id="c2").data["exists"] is False


