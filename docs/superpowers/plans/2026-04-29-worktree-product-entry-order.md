# Worktree 产品入口驱动主列表 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `/wt` 主列表按产品入口而不是实现协议展示，将 Coco、Aiden、Codex、Claude、TTADK 作为同一级入口，并固定原生主工具排在前面。

**Architecture:** 保持现有 Worktree 执行层与 TTADK 子工具流程不变，只调整顶层工具发现与排序。顶层列表同时保留原生 Coco/Aiden/Codex/Claude 与 TTADK 聚合入口，后续路径分别进入“原生模型选择”或“TTADK 子工具 → 模型”。

**Tech Stack:** Python 3.11、pytest、现有 WorktreeToolDiscovery / WorktreeHandler / CardBuilder

---

### Task 1: 锁定 /wt 顶层入口集合与排序

**Files:**
- Modify: `tests/test_worktree_tool_discovery.py`
- Modify: `tests/test_worktree_selection_flow.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_top_level_tools_keep_native_entries_and_ttadk_at_same_level():
    ...


def test_top_level_tools_prioritize_coco_aiden_codex_claude_before_ttadk():
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_worktree_tool_discovery.py tests/test_worktree_selection_flow.py -q`
Expected: FAIL because the current top-level list does not yet enforce the agreed product-entry ordering/shape.

- [ ] **Step 3: Write minimal implementation**

```python
priority_order = {
    ("acp", "coco"): 0,
    ("acp", "aiden"): 1,
    ("acp", "codex"): 2,
    ("cli", "claude"): 3,
    ("ttadk", "ttadk"): 90,
}
```

- [ ] **Step 4: Re-run tests to verify they pass**

Run: `uv run python -m pytest tests/test_worktree_tool_discovery.py tests/test_worktree_selection_flow.py -q`
Expected: PASS

### Task 2: 实现产品入口驱动的顶层工具发现

**Files:**
- Modify: `src/worktree_engine/tool_discovery.py`
- Modify: `src/feishu/handlers/worktree.py`
- Test: `tests/test_worktree_tool_discovery.py`

- [ ] **Step 1: Preserve top-level product entries**

```python
tools.append(
    WorktreeToolOption(
        provider="acp",
        tool_name=name,
        display_name=name.capitalize(),
        ...
    ).__dict__
)
```

- [ ] **Step 2: Sort by product priority, not protocol grouping**

```python
def _sort_top_level_tools(self, tools: list[dict]) -> list[dict]:
    ...
```

- [ ] **Step 3: Keep TTADK as a sibling entry**

```python
if ttadk_tools:
    tools.append(
        WorktreeToolOption(
            provider="ttadk",
            tool_name="ttadk",
            display_name="TTADK",
            supports_model=False,
        ).__dict__
    )
```

- [ ] **Step 4: Keep existing TTADK subtool branch untouched**

```python
if provider == "ttadk" and tool_name == "ttadk":
    ...
```

- [ ] **Step 5: Run targeted tests**

Run: `uv run python -m pytest tests/test_worktree_tool_discovery.py tests/test_worktree_selection_flow.py -q`
Expected: PASS

### Task 3: 回归验证与记录

**Files:**
- Modify: `.Memory/Abstract.md`
- Modify: `.Memory/2026-04-29.md`

- [ ] **Step 1: Run worktree regression**

Run: `uv run python -m pytest tests/test_worktree_tool_discovery.py tests/test_worktree_selection_flow.py tests/test_worktree_card_flow.py tests/test_worktree_command_routing.py -q`
Expected: PASS

- [ ] **Step 2: Update Memory**

```markdown
- **/wt 主列表改为产品入口驱动** — 顶层并列展示 Coco/Aiden/Codex/Claude/TTADK，按用户入口优先级排序；相关 worktree 测试通过 → [详细记录](2026-04-29.md)
```

- [ ] **Step 3: Final verification**

Run: `uv run python -m pytest -x -q tests/test_worktree_tool_discovery.py tests/test_worktree_selection_flow.py tests/test_worktree_card_flow.py tests/test_worktree_command_routing.py`
Expected: PASS
