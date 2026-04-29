# Worktree TTADK 独立入口 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `/wt` 将 TTADK 作为单独入口展示，并把 TTADK 的工具/模型选择与原生 ACP 工具的模型来源彻底隔离。

**Architecture:** 保持现有 Worktree 执行层不变，只调整工具发现、选择流程和卡片渲染。主工具列表新增 TTADK 聚合入口；进入后展示 TTADK 子工具列表，再进入对应模型列表，最终仍落成具体 `(provider, tool_name, model_name)` 选择项。

**Tech Stack:** Python 3.11、pytest、现有 WorktreeHandler / WorktreeToolDiscovery / CardBuilder 体系

---

### Task 1: 锁定主列表与 TTADK 子流程测试

**Files:**
- Modify: `tests/test_worktree_tool_discovery.py`
- Modify: `tests/test_worktree_selection_flow.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_returns_ttadk_as_single_aggregate_entry():
    ...


def test_worktree_select_tool_shows_ttadk_subtool_card():
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_worktree_tool_discovery.py tests/test_worktree_selection_flow.py -q`
Expected: FAIL because TTADK tools are still mixed into the top-level list and there is no TTADK subtool selection card yet.

- [ ] **Step 3: Implement the minimal production changes**

```python
tools.append(
    WorktreeToolOption(
        provider="ttadk",
        tool_name="ttadk",
        display_name="TTADK",
        description="TTADK 多工具入口",
        supports_model=False,
    ).__dict__
)
```

- [ ] **Step 4: Re-run tests to verify they pass**

Run: `uv run python -m pytest tests/test_worktree_tool_discovery.py tests/test_worktree_selection_flow.py -q`
Expected: PASS

### Task 2: 实现 TTADK 子工具卡与选择流程

**Files:**
- Modify: `src/worktree_engine/tool_discovery.py`
- Modify: `src/feishu/handlers/worktree.py`
- Modify: `src/card/builders/worktree.py`
- Modify: `src/card/styles.py`
- Test: `tests/test_worktree_selection_flow.py`

- [ ] **Step 1: Extend handler flow with TTADK aggregate branch**

```python
if provider == "ttadk" and tool_name == "ttadk":
    ttadk_tools = self._get_ttadk_worktree_tools()
    msg_type, card = CardBuilder.build_worktree_ttadk_tool_select_card(
        ttadk_tools,
        selected_dicts,
        pid,
        goal=goal,
    )
    self.patch_message(message_id, card, msg_type=msg_type)
    return
```

- [ ] **Step 2: Add TTADK-only discovery helper**

```python
def get_ttadk_tools(self) -> list[dict]:
    ...
```

- [ ] **Step 3: Add TTADK subtool card builder**

```python
def build_worktree_ttadk_tool_select_card(...):
    ...
```

- [ ] **Step 4: Add UI text entries for the new card**

```python
"worktree_select_ttadk_tool_title": "🌳 Worktree — 选择 TTADK 工具",
"worktree_select_ttadk_tool_prompt": "**请选择一个 TTADK 工具加入 Worktree 组合：**\n",
```

- [ ] **Step 5: Run targeted tests**

Run: `uv run python -m pytest tests/test_worktree_selection_flow.py tests/test_worktree_tool_discovery.py -q`
Expected: PASS

### Task 3: 回归验证与记忆更新

**Files:**
- Modify: `.Memory/Abstract.md`
- Modify: `.Memory/2026-04-29.md`

- [ ] **Step 1: Run broader worktree regression**

Run: `uv run python -m pytest tests/test_worktree_tool_discovery.py tests/test_worktree_selection_flow.py tests/test_worktree_card_flow.py tests/test_worktree_command_routing.py -q`
Expected: PASS

- [ ] **Step 2: Update Memory files**

```markdown
- **/wt TTADK 独立入口** — 主列表改为 TTADK 聚合入口，原生模型与 TTADK 模型来源解耦，相关 worktree 测试通过 → [详细记录](2026-04-29.md)
```

- [ ] **Step 3: Final verification**

Run: `uv run python -m pytest -x -q tests/test_worktree_tool_discovery.py tests/test_worktree_selection_flow.py tests/test_worktree_card_flow.py tests/test_worktree_command_routing.py`
Expected: PASS
