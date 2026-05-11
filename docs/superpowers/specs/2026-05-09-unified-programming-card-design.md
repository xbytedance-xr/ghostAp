# 统一编程模式卡片设计 Spec

**Date:** 2026-05-09
**Owner:** jiataorui
**Status:** Draft (pending user review)
**Supersedes:** `docs/superpowers/plans/2026-05-09-unified-programming-card-plan.md`（旧 plan，路径选择已变更）

---

## 1. 目的

让 Coco / Claude / Aiden / Codex / Gemini / TTADK + Deep / Spec / Worktree 全部编程模式卡片共享同一套骨架，解决以下三个直接问题：

1. **续卡丢失上下文锚点**：当前 `src/card/render/renderer.py` 在 `page_idx > 0` 时只重注 `warning_banner`，task_list / activity_summary 双双消失，用户翻续卡时不知道当前在哪个 task、做了什么。
2. **多 active 工具同时展开导致首卡混乱**：`src/card/render/tools.py:render_tool_panel` 用 `expanded = block.status == "active"`，并发场景所有 active 全展开，参考图 1 的"乱"由此产生。
3. **多卡切分被动溢出**：`src/card/render/pagination.py` 贪心按字节/节点预算切，切点常落在 atom 中间，无语义边界。

## 2. 范围

### In scope

- 新增 `SectionLayout` SSOT 模型，承载 `sticky_head / status / body / appendix` 四区。
- 续卡每页前置「三明治锚点」：phase_banner + task_list + activity_summary。
- tool panel 默认折叠规则改为「只展开最新一个 active」。
- 引擎主动 `dispatch(CardEvent.card_split(...))` 触发语义切卡（task 完成 / phase 跳变 / round 跳变 / cycle/perspective 跳变）。
- 4 引擎 + Worktree 子卡 + 直接 programming 模式全部接入 SectionLayout。
- 18 主题 + 12 builder 模块的回归覆盖。

### Out of scope

- 新增 banner 文案的国际化（沿用 `UI_TEXT` 中文）。
- 卡片交互按钮新设计（保留现有 `[停止] / [模式切换]`）。
- Feishu Schema 2.0 节点扩展（仍只用 `markdown / collapsible_panel / div / column_set` 等已有 tag）。
- ACP 协议本身的事件扩展。
- token 使用量在 activity_summary 的展示（进 Backlog）。

## 3. 现状（参考资料）

- **三层架构**：State (Reducer) + Render (pure func) + Delivery，atom 化渲染管线 flatten → paginate → assemble。
- **既有 atom kind**（`src/card/render/renderer.py:31-33`）：
  - status: `warning_banner / progress_bar / phase_panel / criteria_panel / task_list`
  - body:   `text / reasoning / plan / worktree_panel / activity_summary`
  - appendix: `tool_panel / tool_history`
- **节点预算**：Feishu Card Schema 2.0 ≤200 节点 / 30K bytes。`pagination.py` `BASE_OVERHEAD = 500`、`FIXED_NODE_OVERHEAD = 20`。
- **续卡逻辑**（`src/card/render/renderer.py` `_with_continuation_atoms`）：仅 `state.footer.warning_banner` 跨页重注。
- **Worktree 子卡**：CLAUDE.md 已规定「并发 subagent 时每个子任务独立维护自己的消息卡片并持续更新」，每子代理一个独立 session，本 spec 不改这个规则，只让子卡同样接入新骨架。

## 4. 设计

### 4.1 SectionLayout 模型

新增 `src/card/render/layout.py`：

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class SectionLayout:
    """Single source of truth for card section ordering and pagination.

    sticky_head: repeated on every page, never moved by pagination.
    status:      first page only; secondary status panels (progress, criteria).
    body:        primary content; subject to greedy pagination.
    appendix:    last page only; tool_history, references.
    """
    sticky_head: tuple[RenderAtom, ...]
    status:      tuple[RenderAtom, ...]
    body:        tuple[RenderAtom, ...]
    appendix:    tuple[RenderAtom, ...]

    def assemble_for_page(
        self, page_idx: int, total_pages: int, body_slice: tuple[RenderAtom, ...]
    ) -> tuple[RenderAtom, ...]:
        """Build full atom sequence for one page."""
        result: list[RenderAtom] = list(self.sticky_head)
        if page_idx == 0:
            result.extend(self.status)
        result.extend(body_slice)
        if page_idx == total_pages - 1:
            result.extend(self.appendix)
        return tuple(result)
```

`paginate_atoms` 升级为 `paginate_layout(layout, budget) -> list[tuple[RenderAtom, ...]]`：

1. 计算 `sticky_size = sum(estimate_atom_size(a) for a in layout.sticky_head)`，每页固定占用。
2. 对 `body` 做贪心分页，每页可用预算 = `byte_budget - BASE_OVERHEAD - sticky_size`、节点预算 = `node_budget - FIXED_NODE_OVERHEAD - sticky_node_count`。
3. 调用 `assemble_for_page` 拼装最终原子序列。
4. 保留旧入口 `paginate_atoms(atoms, budget)` 作为 deprecation shim：内部包装为 `SectionLayout(body=atoms)` 调用新路径。

### 4.2 三明治锚点（sticky_head）

`src/card/render/sticky_head.py`（新）：

```python
def build_sticky_head(state: CardState, metadata: CardMetadata) -> tuple[RenderAtom, ...]:
    atoms: list[RenderAtom] = []

    # 1. phase_banner — 始终存在
    banner = build_phase_banner_atom(state, metadata)
    atoms.append(banner)

    # 2. task_list — 仅当 state 有 task_list block
    if state.task_list and state.task_list.tasks:
        atoms.append(build_task_list_atom(state.task_list, compact=True))

    # 3. activity_summary — 仅当有 tool 调用统计
    if state.activity and state.activity.has_data:
        atoms.append(build_activity_summary_atom(state.activity, compact=True))

    # 节点预算硬上限：sticky_head ≤ 25 节点，超出降级（去 activity_summary）
    while total_nodes(atoms) > 25 and len(atoms) > 1:
        atoms.pop()
    return tuple(atoms)
```

**节点预算估算**：
- phase_banner：1 markdown atom = 1 节点
- task_list（compact）：collapsible_panel 折叠态 ≈ 5–10 节点
- activity_summary（compact）：collapsible_panel 折叠态 ≈ 3–5 节点
- 合计上限 ≈ 18 节点（< 25 上限），`paginate_layout` 计算 body 预算时按 25 预留以保安全。

### 4.3 phase_banner atom

新 atom kind `phase_banner`，归入 `_STATUS_ATOM_KINDS`，在 `_order_atoms_by_section` 中强制最前。

`src/card/render/banner_computer.py`（新）：

```python
def compute_banner(metadata: CardMetadata, runtime: RuntimeStats) -> str:
    """Unified banner template: {emoji} {mode} · {phase} · {elapsed}"""
    emoji = metadata.mode_emoji or "🤖"
    mode = metadata.mode_name or "Programming"
    phase = _format_phase(metadata, runtime)  # 引擎特定
    elapsed = _format_elapsed(runtime.elapsed_seconds)
    return f"{emoji} {mode} · {phase} · {elapsed}"


def _format_phase(metadata: CardMetadata, runtime: RuntimeStats) -> str:
    engine = metadata.engine_type
    if engine == "deep":
        return runtime.deep_phase or "执行中"  # "分析中" / "执行中"
    if engine == "spec":
        return f"cycle {runtime.spec_cycle}/{runtime.spec_perspective or '—'}"
    if engine == "worktree":
        return f"wt·{runtime.worktree_subagent or '?'}"
    return "进行中"
```

### 4.4 Tool panel 单 active 展开规则

`src/card/state/models.py` `ToolBlock` 加字段 `is_latest_active: bool`。

`src/card/state/reducer.py`：
- `on_tool_call_start`：把所有 `is_latest_active=True` 的 block 改为 False，新 block 设 True。
- `on_tool_call_end`：终结的 block `is_latest_active=False`、`status="completed"`；从仍 active 的中按 `started_at` 取最新一个标 `is_latest_active=True`。

`src/card/render/tools.py:render_tool_panel`：

```python
expanded = block.is_latest_active  # 旧逻辑：block.status == "active"
```

非 latest 的 active block 也折叠，title 加 `🔄` 表明在跑但内容收起。

### 4.5 语义切卡

新事件 `CardEvent.card_split(reason: str, hint: str | None)`：

`src/card/events/payloads.py`：

```python
@dataclass(frozen=True)
class CardSplitPayload:
    reason: str            # "task_done" | "phase_changed" | "round_changed" | "cycle_changed"
    hint: str | None       # 用于新卡 body 起头标注，如 "接续 task 3「单元测试」"
```

`src/card/session/session.py` 增加 `_handle_card_split(event)`：
1. dispatch split 前 `stream_bridge.close_open_blocks()`，确保流式 text 正确收尾。
2. 把当前 session 标 `state.completed=True` 并 dispatch 一次 final 渲染。
3. 关闭当前 CardSession，触发 hooks。
4. 上层 renderer 监听 `card_split.completed` 信号，自行起新 session（已有的 task-level 多卡逻辑可复用）。

各引擎 dispatch 时机：
- **Deep**：`DeepRenderer.create_deep_callbacks()` 内 `on_event`，检测到 PLAN_UPDATE 且某 task `status` 由 in_progress 变 completed → dispatch card_split。
- **Spec**：`SpecRenderer` 监听 cycle/perspective 跳变。
- **Worktree**：每子代理已独立 session，不额外切卡。
- **Programming（直接对话）**：不切（多 task 概念由 plan 驱动，没有 plan 时不切）。

切卡看护门槛（防止过度切卡）：
- 当前卡 `byte_used / byte_budget < 0.4` 时不切（信息太少不值得新开一卡）。
- 同一引擎 60 秒内 ≥3 次 split 触发警告日志（潜在 bug）。

### 4.6 续卡 body 起头标注

新卡 body 第一个 text atom 为 hint：

```
续卡 2/3 — 接续 task 3「单元测试」
```

由 `card_split.hint` 提供文案，`session.py` 创建新 session 时 dispatch 一次 `text_delta` 写入。

### 4.7 引擎适配矩阵

| 引擎 | sticky_head 内容 | 切卡触发 | 切卡 hint 示例 |
|---|---|---|---|
| Deep | banner(phase) + task_list + activity_summary | task 完成 / phase 跳变 | "接续 task 3「单元测试」" |
| Spec | banner(cycle/perspective) + task_list + activity_summary | cycle 完成 / perspective 切换 | "进入 cycle 2 · 验收视角" |
| Worktree | banner(子代理名) + 单子任务 task_list + activity_summary | （不切，已独立 session）| — |
| Programming（直接） | banner(mode) + activity_summary | 不切 | — |

## 5. 行为契约

### 5.1 首卡

- sticky_head：phase_banner（必有）+ task_list（有 plan 时）+ activity_summary（有工具调用时）。
- status：progress_bar / criteria_panel / phase_panel（如有）。
- body：text / reasoning / plan / 当前活跃 tool_panel（仅最新 active 展开）。
- appendix：tool_history（已完成工具折叠总览）。

### 5.2 续卡（page_idx > 0）

- sticky_head：与首卡相同内容，自动重注。
- status：**不重注**（progress_bar 已在首卡）。
- body：起头一行 hint（若由 card_split 触发），然后接续内容。
- appendix：仅最末页出现。

### 5.3 切卡（card_split）

- 旧卡 dispatch `completed`，触发 hooks（emoji 反应、context 持久化）。
- 新卡作为独立消息发送，飞书中表现为顺次的两张卡片。
- 新卡 metadata 里 `continuation_seq` 自增，方便后续追溯。

### 5.4 节点预算超限保护

- sticky_head 总节点 > 25 时，按优先级降级：先去 activity_summary，再把 task_list 改为单行 fallback（仅显当前 task 名 + 进度比，无展开按钮）。
- pagination 后单页节点仍超 180（200 - 20 fixed overhead）时，强制把 body 末段切到下一页。

## 6. 改动文件清单

### 新增（5 文件 + 测试）

- `src/card/render/layout.py` — SectionLayout 模型 + paginate_layout
- `src/card/render/sticky_head.py` — 三明治锚点构造器
- `src/card/render/banner_computer.py` — 统一 banner 文案计算
- `src/card/state/runtime_stats.py` — RuntimeStats 数据类（elapsed/round/cycle/phase 等运行期信息）
- `tests/card/render/test_layout.py`
- `tests/card/render/test_sticky_head.py`
- `tests/card/render/test_banner_computer.py`

### 改造（17 文件）

- `src/card/render/renderer.py` — `_order_atoms_by_section` 退役，改调用 `SectionLayout.assemble_for_page`；`_with_continuation_atoms` 退役，sticky 在 paginate_layout 阶段处理。
- `src/card/render/pagination.py` — `paginate_atoms` 改为 deprecation shim，新主路径 `paginate_layout`。
- `src/card/render/atoms.py` — 加 `phase_banner` atom kind 与 estimate 规则。
- `src/card/render/tools.py` — `render_tool_panel` 用 `is_latest_active` 决定 expanded。
- `src/card/render/task_list.py` — 加 `compact` 参数，sticky_head 用 compact 模式（只显当前 task + 进度比，不展全列表）。
- `src/card/render/activity_summary.py`（新拆出，原代码可能在 renderer.py 内）— 加 compact 模式。
- `src/card/state/models.py` — `ToolBlock.is_latest_active` 字段；`CardState` 加 `runtime_stats`。
- `src/card/state/reducer.py` — `on_tool_call_*` 维护 latest_active 单例；新事件 `on_card_split`。
- `src/card/events/payloads.py` — `CardSplitPayload`。
- `src/card/events/event.py` — `CardEvent.card_split(reason, hint)` 工厂方法。
- `src/card/session/session.py` — 监听 `card_split` 事件，关闭当前 session、暴露 `card_split_completed` 信号给上层。
- `src/feishu/renderers/base.py` — 新辅助 `_dispatch_card_split(session, reason, hint)`。
- `src/feishu/renderers/deep_renderer.py` — task 完成检测 → dispatch card_split；新 session 写 hint。
- `src/feishu/renderers/worktree_renderer.py` — 子代理状态变化 → dispatch card_split。
- `src/feishu/renderers/spec_renderer.py` — cycle/perspective 跳变 → dispatch card_split。
- `src/card/programming_adapter.py`（或对应文件）— 直接 programming 模式接入 SectionLayout（无切卡、无 task_list）。
- `src/card/builders/layout.py` — `UnifiedCardLayout` 适配新 SectionLayout 输出。

### 测试（10 文件）

- `tests/card/render/test_layout.py`（新）
- `tests/card/render/test_sticky_head.py`（新）
- `tests/card/render/test_banner_computer.py`（新）
- `tests/card/render/test_renderer.py` — 续卡 sticky 验证、order 验证。
- `tests/card/render/test_pagination.py` — paginate_layout 切分验证、sticky 不被切散。
- `tests/card/render/test_tools.py` — single-active-expanded 验证。
- `tests/card/render/test_task_list.py` — compact 模式验证。
- `tests/card/state/test_reducer.py` — latest_active 单例维护、card_split 事件处理。
- `tests/feishu/renderers/test_deep_renderer.py` — task 完成 → split 验证。
- `tests/feishu/renderers/test_worktree_renderer.py` — 子代理状态 → split 验证。
- `tests/feishu/renderers/test_spec_renderer.py` — cycle 跳变 → split 验证。

### UX 资产

- `ux/unified_card_v1.html` — 全引擎 mockup（首卡 + 续卡 + 切卡前后对比）。

## 7. 迁移策略

3 步骤独立 commit、每步全测通过再下一步：

### Step 1：新模块 + 单测，不接入

新增 `layout.py` / `sticky_head.py` / `banner_computer.py` / `runtime_stats.py`，配套 3 个测试文件。**不修改 renderer.py/pagination.py**，线上不受影响。

### Step 2：renderer 与 pagination 接入

- `renderer.py` 改用 `SectionLayout.assemble_for_page`。
- `pagination.py` 新增 `paginate_layout`，旧 `paginate_atoms` 退化为 shim。
- 跑 18 主题（`src/card/themes/`）+ 12 builder（`src/card/builders/`）回归。
- tools.py / task_list.py 升级 compact 模式 + single-active-expanded。

### Step 3：3 引擎接入 + Worktree 验证

- DeepRenderer / WorktreeRenderer / SpecRenderer 改 dispatch `card_split`。
- Worktree 子卡复用新骨架（不改切卡逻辑）。
- 直接 Programming 模式接入 SectionLayout（不切卡）。
- E2E 手测每引擎一遍。

## 8. 风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| pagination shim 漏改老调用 | 中 | 中 | shim 加 deprecation warning + grep 全仓老调用 |
| sticky_head 总节点超 25 | 中 | 中 | 硬上限降级（去 activity_summary 优先）+ 单测 |
| tool_history 跨页位置漂 | 低 | 低 | appendix 仅最末页，单测覆盖 |
| Worktree 子卡 banner 与父卡混淆 | 中 | 中 | 子代理名前缀 `wt·{name}`，避免歧义 |
| 18 主题 sticky 分隔线视觉冲突 | 低 | 低 | 主题加 `sticky_separator` 默认色，回退老主题用 fallback |
| 切卡过频（< 0.4 byte_used） | 中 | 中 | 看护门槛 + 60s 警告日志 |
| `card_split` 事件竞态（split 时还有 streaming 文本） | 中 | 高 | dispatch split 前 `stream_bridge.close_open_blocks()` |
| 主题 mode_emoji 缺失 | 低 | 低 | banner_computer fallback 到 `🤖` |
| activity_summary 数据来源滞后 | 中 | 低 | activity 计数由 reducer 维护，与 tool_call 事件原子绑定 |

## 9. 验收标准

### 功能

- [ ] 首卡渲染含 phase_banner + task_list（有 plan）+ activity_summary（有工具调用）三件套，顺序固定。
- [ ] 续卡每页前置三明治锚点，内容与首卡同步（task_list 高亮当前 task、activity_summary 计数最新）。
- [ ] 多 active 工具时只有最新一个 tool_panel 展开。
- [ ] Deep 模式 task 完成时主动切卡，新卡 body 起头标注接续 task。
- [ ] Spec 模式 cycle/perspective 跳变时主动切卡。
- [ ] Worktree 子代理保持每子任务独立卡，骨架与新设计一致。
- [ ] Programming 直接模式不切卡，但骨架一致。
- [ ] phase_banner 跨 4 引擎文案符合统一模板。

### 节点预算与字节预算

- [ ] sticky_head 节点 ≤25，超出按优先级降级。
- [ ] 续卡 body 可用预算 = `byte_budget - BASE_OVERHEAD - sticky_size`，不溢出。
- [ ] 单卡总节点 ≤180（200 - 20 fixed overhead）。

### 回归

- [ ] 18 主题渲染通过 mock 测试。
- [ ] 12 builder 模块单测通过。
- [ ] `uv run python -m pytest tests/ -v` 全绿。

### 视觉

- [ ] `ux/unified_card_v1.html` 包含 4 引擎首卡 + 续卡 + 切卡前后 mockup，与本 spec 描述一致。
- [ ] 飞书实机测试：参考图 2 的清爽程度对得上。

## 10. 测试策略

- **单测**：新模块全覆盖（layout / sticky_head / banner / reducer 新事件）。
- **回归**：renderer / pagination / tools / task_list 改造点全覆盖。
- **集成**：每引擎 renderer 单测 + 一条 E2E 路径（mock ACP 事件流 → 完整渲染输出）。
- **节点/字节预算压力测**：`tests/card/render/test_budget_regression.py` 构造极端场景（30 个 task、100 个 tool 调用），验证降级策略。
- **手测清单**：4 引擎各起一次真实任务，肉眼对比参考图 2。

## 11. 开放问题

- Q：phase_banner 的 elapsed 文案需要 i18n 吗？  
  A：暂不需要，沿用中文 `Xm Ys` 格式。
- Q：activity_summary 是否需要展示 token 使用量？  
  A：超本期范围，进 Backlog。
- Q：切卡时旧卡的 streaming text 还没关，hint 怎么处理？  
  A：dispatch split 前强制 `stream_bridge.close_open_blocks()`，hint 总是首条 text 进入新卡。
- Q：是否要给 sticky_head 加可手动折叠开关？  
  A：暂不加，sticky 即"始终重要"，可折叠会破坏锚点价值。

## 12. 附录：相关代码引用

- `src/card/render/renderer.py:31-33` — _STATUS/_BODY/_APPENDIX_ATOM_KINDS
- `src/card/render/pagination.py:9-13` — BASE_OVERHEAD / FIXED_NODE_OVERHEAD
- `src/card/render/task_list.py:20` — _FOLD_THRESHOLD = 5
- `src/card/render/tools.py:render_tool_panel` — `expanded = block.status == "active"`（待改）
- `src/feishu/renderers/deep_renderer.py:30` — _MIN_TASKS_FOR_MULTI_CARD = 2
- CLAUDE.md "飞书卡片任务级展示原则" — 父子卡规则约束
