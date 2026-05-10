# 卡片精简 FLOW · TurnActivityDigest 设计

- **Status**: Active
- **Date**: 2026-05-10
- **Supplements**: `2026-05-10-card-redesign-design.md` §4.3
- **Problem**: 当前卡片信息冗余 × 3，卡片过长

## 1. 问题诊断

当前每次工具调用的信息在卡片中出现 **三次**：

| # | 位置 | 来源 | 展示方式 |
|---|------|------|---------|
| 1 | body text 内联 | `ACPEventRenderer._format_tool_run_line()` | `📖 Read \`foo.py\` ✅` 嵌入 text chunk |
| 2 | appendix tool_panel | `render_tool_panel()` via `flatten_to_atoms` | collapsible_panel 含完整 input/output JSON |
| 3 | footer now_tool_hint | `render_now_tool_hint()` | `⚙ Read · 读取 src/foo.py` 小字 |

同时，`tool_panel` 被放在 appendix（仅末页），`reasoning` 在 body，导致"思考→工具→再思考→再工具"的循环节奏不可见。

## 2. 目标状态

```
┌─ reasoning (折叠面板) ────────────┐
│ 读完入口后判断错误处理在 dispatch │
│ 层，准备 grep 匹配并打补丁。      │
└──────────────────────────────────┘
▣ 已探索 3 项, 已编辑 1 个文件      ← activity_digest（一行 markdown）
┌─ reasoning (折叠面板) ────────────┐
│ 找到了关键的路由分发逻辑，         │
│ 需要修改异常分支处理…              │
└──────────────────────────────────┘
▣ 已编辑 2 个文件, 已运行 1 条命令   ← activity_digest
⏳ 正在执行: grep "dispatch"         ← 运行中工具（仅当有 active 时）
```

关键原则：
- **Reasoning** 和 **activity_digest** 在 body 区交替出现
- **activity_digest** 是一行统计（调了什么工具类别，各多少次），**不展示具体 input/output**
- 运行中的工具单独一行，显示工具名+目标
- 去掉 body text 中的内联工具摘要（冗余源 #1）
- 去掉 appendix 中的 tool_panel（冗余源 #2）
- footer now_tool_hint 保留（作为全局定位信号，不冗余因为 digest 在 body 区可能跨页不可见）

## 3. 数据模型变更

### 3.1 新增 AtomKind: `activity_digest`

```python
# atoms.py
AtomKind = Literal[
    "text", "tool_panel", "tool_history", "reasoning", "plan",
    "criteria_panel", "phase_panel", "warning_banner", "progress_bar",
    "worktree_panel", "task_list", "phase_banner",
    "subagent_dispatch", "activity_digest",  # ← NEW
]
```

### 3.2 RenderAtom 补充字段

`activity_digest` atom 的 `content` 存储渲染好的一行 markdown 统计文本。

### 3.3 SectionLayout 归属

```python
# renderer.py
_BODY_ATOM_KINDS = frozenset({
    "text", "reasoning", "plan", "worktree_panel",
    "subagent_dispatch", "activity_digest",  # ← 加入 body
})
_APPENDIX_ATOM_KINDS = frozenset({"tool_panel", "tool_history"})  # 保留但很少命中
```

> `tool_panel` 仍保留在 appendix 作为降级兜底（万一有零散未聚合的工具）；正常流程中 flatten 会产出 activity_digest 而非 tool_panel。

## 4. flatten_to_atoms 改造

### 4.1 当前逻辑

```
for each block:
  if tool_call:
    if completed and consecutive >= threshold → tool_history atom
    else → individual tool_panel atom
  else → handler dispatch
```

### 4.2 新逻辑

```
for each block:
  if tool_call:
    if status == "active":
      → flush pending completed tools as activity_digest
      → emit tool_panel atom for this active tool (stays in body as running indicator)
    elif status in ("completed", "failed"):
      → accumulate into pending_tools buffer
      → when next non-tool block arrives or end of blocks → flush as activity_digest
  else:
    → flush pending_tools as activity_digest
    → handler dispatch as before
```

**Flush 规则**：
- `pending_tools` 非空时，生成 1 个 `activity_digest` atom
- content = `render_activity_digest_line(pending_tools)` — 一行 markdown

### 4.3 render_activity_digest_line

复用 `tools.py` 的分类常量：

```python
def render_activity_digest_line(blocks: list[ContentBlock]) -> str:
    """一行统计，如 '▣ **已探索 3 项, 已编辑 1 个文件, 已运行 2 条命令**'"""
    explored, edited, commands, other = 0, 0, 0, 0
    failed = 0
    for b in blocks:
        name = (b.tool_name or "").lower()
        if b.status == "failed": failed += 1
        if name in _EXPLORE_TOOLS: explored += 1
        elif name in _EDIT_TOOLS: edited += 1
        elif name in _COMMAND_TOOLS: commands += 1
        else: other += 1
    parts = []
    if explored: parts.append(f"已探索 {explored} 项")
    if edited: parts.append(f"已编辑 {edited} 个文件")
    if commands: parts.append(f"已运行 {commands} 条命令")
    if other: parts.append(f"{other} 次其他调用")
    if failed: parts.append(f"{failed} 项失败")
    return f"▣ **{', '.join(parts)}**" if parts else ""
```

### 4.4 运行中工具渲染

active tool 不再渲染为 collapsible_panel（太占空间），改为一行 markdown：

```
⏳ **Read** · src/card/render/tools.py
```

用 `generate_tool_summary(block)` 已有的摘要逻辑。放在 body 中。

## 5. 去除冗余

### 5.1 ACPEventRenderer 内联摘要（冗余源 #1）

`ACPEventRenderer._ingest_event` 中，当 tool completed 时调用 `_format_tool_run_line` 将 `📖 Read \`foo.py\` ✅` 注入 `_text_content`。

**改动**：删除 `_format_tool_run_line` 的调用。tool 完成后不再往 text 里注入摘要行。

影响文件：`src/acp/renderer.py`

### 5.2 Appendix tool_panel 成为空区（冗余源 #2）

正常流程中 `flatten_to_atoms` 不再产出 `tool_panel` atom（改为 `activity_digest`）。appendix 自然变空。

唯一例外：active 工具仍产出 `tool_panel`，但归入 body。

**改动**：将 `tool_panel` 也加入 `_BODY_ATOM_KINDS`，让 active 工具面板在 body 中显示。

### 5.3 Footer now_tool_hint（保留）

不改。作为跨页全局定位信号有独立价值。

## 6. activity_digest 渲染器

新增 `_render_atom_activity_digest` 到 `_ATOM_RENDERERS`：

```python
def _render_atom_activity_digest(atom, state, budget, block_index) -> dict:
    """Render activity digest as a plain markdown line with muted text."""
    return {"tag": "markdown", "content": atom.content, "text_size": "notation"}
```

简洁一行 markdown，`text_size: notation` 让它小字显示，视觉上区分于 reasoning。

## 7. 影响范围

| 文件 | 改动 |
|------|------|
| `src/card/render/atoms.py` | AtomKind +1, flatten 逻辑改为 digest 聚合 |
| `src/card/render/tools.py` | +`render_activity_digest_line()`, +`render_active_tool_line()` |
| `src/card/render/renderer.py` | +`_render_atom_activity_digest`, `_BODY_ATOM_KINDS` += `activity_digest` + `tool_panel` |
| `src/acp/renderer.py` | 删除内联工具摘要注入 |
| `src/card/state/models.py` | 无变更 |
| `src/card/state/block_registry.py` | 无变更（tool_call block 的 `_atom_kind` 仍为 `tool_panel`，由 flatten 覆盖） |

## 8. 不在范围

- Header / task_list / 切卡连贯性 — 按原 spec 独立推进
- 并行 subagent 卡片 — 按原 spec 独立推进
- reasoning 面板样式调整 — 保持现有 collapsible_panel
- tool_panel 删除 — 保留代码但正常流程不产出

## 9. 测试

- `test_flatten_produces_activity_digest`: 3 completed tools → 1 activity_digest atom
- `test_active_tool_in_body`: active tool → body 区的 tool_panel
- `test_digest_interleaves_with_reasoning`: reasoning + tools + reasoning → 3 body atoms 交替
- `test_no_inline_tool_summary_in_text`: ACPEventRenderer text 不含工具摘要行
- `test_appendix_empty_in_normal_flow`: 正常流程 appendix 无 atom
