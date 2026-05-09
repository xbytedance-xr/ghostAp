# Unified Programming Card Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Coco / Claude / Aiden / Codex / Gemini / TTADK + Deep / Loop / Spec / Worktree 全部编程模式卡片统一到 SectionLayout 骨架，续卡每页注入「三明治锚点」(phase_banner + task_list + activity_summary)，tool panel 改为单 active 展开，引擎主动 dispatch `card_split` 触发语义切卡。

**Architecture:** 在现有三层 (State + Render + Delivery) 上新增 `SectionLayout` SSOT 模型，承载 `sticky_head / status / body / appendix` 四区。`paginate_layout` 把 sticky_head 当作每页固定预算前置；`render_card` 主流程改用 SectionLayout.assemble_for_page。引擎 renderer 监听语义事件 (task done / round change / cycle change) 并 dispatch `CardEvent.card_split(reason, hint)`，session.py 关闭旧卡、上层起新 session。

**Tech Stack:** Python 3.11, pytest, Feishu Card Schema 2.0, existing `src/card/*` render pipeline, ACP stream bridge, TaskOrchestrator.

**Spec:** `docs/superpowers/specs/2026-05-09-unified-programming-card-design.md`

**Mockup:** `ux/unified_card_v1.html`

---

## File Map

### Create

- `src/card/render/layout.py` — `SectionLayout` 模型 + `paginate_layout` 函数
- `src/card/render/sticky_head.py` — 三明治锚点构造器 + 节点降级逻辑
- `src/card/render/banner_computer.py` — 统一 banner 文案计算
- `src/card/state/runtime_stats.py` — `RuntimeStats` 数据类（elapsed/round/cycle/phase 等）
- `tests/test_section_layout.py` — SectionLayout 单测
- `tests/test_sticky_head.py` — 三明治锚点单测
- `tests/test_banner_computer.py` — banner 文案单测
- `tests/test_card_split_event.py` — card_split 事件单测
- `tests/test_card_continuation_sticky.py` — 续卡 sticky 重注端到端
- `tests/test_card_budget_regression.py` — 极端 state 节点/字节回归
- `tests/test_runtime_stats.py` — RuntimeStats 单测
- `tests/test_base_renderer_card_split.py` — BaseRenderer helper 单测
- `tests/test_deep_renderer_split.py` — Deep 切卡单测
- `tests/test_loop_renderer_split.py` — Loop 切卡单测
- `tests/test_spec_renderer_split.py` — Spec 切卡单测

### Modify

- `src/card/render/atoms.py` — `AtomKind` 加 `phase_banner`
- `src/card/render/renderer.py` — 主流程改用 SectionLayout 装配；保留兼容
- `src/card/render/pagination.py` — `paginate_atoms` 退化为 deprecation shim
- `src/card/render/task_list.py` — `render_task_list_panel` 加 `compact` 参数
- `src/card/render/tools.py` — `render_tool_panel` 用 `is_latest_active`；`render_activity_summary_panel` 加 `compact` 参数
- `src/card/state/models.py` — `ContentBlock` 加 `is_latest_active`；`CardState` 加 `runtime_stats`
- `src/card/state/reducer.py`（含 `reducers/` 子模块）— `tool_call_*` 维护 `is_latest_active` 单例
- `src/card/events/payloads.py` — 新增 `CardSplitPayload`
- `src/card/events/factories.py` — `CardEvent.card_split(reason, hint)` 工厂
- `src/card/events/types.py` — 新增 `card_split` 事件类型常量
- `src/card/session/__init__.py`（或对应 session 文件）— 监听 `card_split` 事件
- `src/feishu/renderers/base.py` — 新辅助 `_dispatch_card_split(session, *, reason, hint)`
- `src/feishu/renderers/deep_renderer.py` — task 完成检测 → dispatch card_split
- `src/feishu/renderers/loop_renderer.py` — round 跳变检测 → dispatch card_split
- `src/feishu/renderers/spec_renderer.py` — cycle/perspective 跳变 → dispatch card_split
- `src/card/programming_adapter.py` — 直接 programming 模式接入 SectionLayout（不切卡）
- `.Memory/2026-05-09.md` — 任务记录
- `.Memory/Abstract.md` — 索引

---

## Task 1: 新增 phase_banner AtomKind

**Files:**
- Modify: `src/card/render/atoms.py:19-23`
- Modify: `src/card/render/renderer.py:335-348`
- Test: `tests/test_card_render_atoms.py`

`phase_banner` 是新 atom kind，承载 banner 文案，归入 sticky_head。

- [ ] **Step 1: 写失败测试**

在 `tests/test_card_render_atoms.py` 末尾追加：

```python
def test_phase_banner_atom_kind_recognized():
    """phase_banner is a valid AtomKind and has a renderer."""
    from src.card.render.atoms import RenderAtom, AtomKind, estimate_atom_size
    from src.card.render.renderer import _ATOM_RENDERERS
    from typing import get_args

    kinds = set(get_args(AtomKind))
    assert "phase_banner" in kinds, "phase_banner must be in AtomKind"
    assert "phase_banner" in _ATOM_RENDERERS, "phase_banner must have a renderer"

    atom = RenderAtom(kind="phase_banner", content="🧠 Deep · 执行中 · 1m23s", node_count=1)
    atom.byte_size = estimate_atom_size(atom)
    assert atom.byte_size > 0
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/test_card_render_atoms.py::test_phase_banner_atom_kind_recognized -v
```

预期：`AssertionError: phase_banner must be in AtomKind`。

- [ ] **Step 3: 修改 atoms.py**

`src/card/render/atoms.py:19-23`，`AtomKind` Literal 列表里加 `"phase_banner"`：

```python
AtomKind = Literal[
    "text", "tool_panel", "tool_history", "reasoning", "plan",
    "criteria_panel", "phase_panel", "warning_banner", "progress_bar",
    "worktree_panel", "task_list", "activity_summary", "phase_banner",
]
```

- [ ] **Step 4: 在 renderer.py 注册渲染器**

`src/card/render/renderer.py`，在 `_render_atom_progress_bar`（约 :312）之后新增：

```python
def _render_atom_phase_banner(atom: RenderAtom, state: CardState, budget: RenderBudget, block_index: dict) -> dict:
    """Render phase_banner as a top sticky markdown line."""
    return {"tag": "markdown", "content": atom.content}
```

并在 `_ATOM_RENDERERS` 字典（约 :335-348）末尾加：

```python
    "phase_banner": _render_atom_phase_banner,
```

- [ ] **Step 5: 跑测试确认通过**

```bash
uv run pytest tests/test_card_render_atoms.py -v
```

预期：所有 PASS（含 `AtomKind ↔ _ATOM_RENDERERS` SSOT 校验）。

- [ ] **Step 6: 提交**

```bash
git add src/card/render/atoms.py src/card/render/renderer.py tests/test_card_render_atoms.py
git commit -m "feat(card): add phase_banner AtomKind and renderer

为 sticky_head 三明治锚点的统一 banner 引入新的 atom kind，
注册占位渲染器，保证 AtomKind ↔ _ATOM_RENDERERS SSOT 校验通过。"
```

---

## Task 2: 新增 RuntimeStats 数据类

**Files:**
- Create: `src/card/state/runtime_stats.py`
- Test: `tests/test_runtime_stats.py`

`RuntimeStats` 承载运行期信息（elapsed、loop_round、spec_cycle、deep_phase、worktree_subagent），banner_computer 消费。

- [ ] **Step 1: 写测试**

新建 `tests/test_runtime_stats.py`：

```python
"""RuntimeStats dataclass tests."""
from __future__ import annotations

import dataclasses

from src.card.state.runtime_stats import RuntimeStats


def test_runtime_stats_defaults():
    rs = RuntimeStats()
    assert rs.elapsed_seconds == 0.0
    assert rs.deep_phase is None
    assert rs.loop_round is None
    assert rs.spec_cycle is None
    assert rs.spec_perspective is None
    assert rs.worktree_subagent is None


def test_runtime_stats_construction():
    rs = RuntimeStats(
        elapsed_seconds=83.5,
        deep_phase="executing",
        loop_round=2,
        spec_cycle=1,
        spec_perspective="code",
        worktree_subagent="aiden",
    )
    assert rs.elapsed_seconds == 83.5
    assert rs.deep_phase == "executing"
    assert rs.loop_round == 2
    assert rs.spec_cycle == 1
    assert rs.spec_perspective == "code"
    assert rs.worktree_subagent == "aiden"


def test_runtime_stats_is_frozen():
    rs = RuntimeStats()
    raised = False
    try:
        rs.elapsed_seconds = 999.0  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        raised = True
    assert raised
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/test_runtime_stats.py -v
```

预期：`ModuleNotFoundError: No module named 'src.card.state.runtime_stats'`。

- [ ] **Step 3: 写实现**

`src/card/state/runtime_stats.py`：

```python
"""RuntimeStats: snapshot of runtime context consumed by banner/sticky rendering."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeStats:
    """Snapshot of engine runtime context for banner / sticky rendering.

    Engine renderers populate fields relevant to their engine; others stay None.
    """

    elapsed_seconds: float = 0.0
    deep_phase: str | None = None         # "analyzing" | "executing"
    loop_round: int | None = None
    spec_cycle: int | None = None
    spec_perspective: str | None = None
    worktree_subagent: str | None = None
```

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest tests/test_runtime_stats.py -v
```

预期：3 PASS。

- [ ] **Step 5: 提交**

```bash
git add src/card/state/runtime_stats.py tests/test_runtime_stats.py
git commit -m "feat(card): add RuntimeStats dataclass

冻结的运行期信息容器（elapsed/round/cycle/phase/perspective/worktree
subagent），供 banner_computer 与 sticky_head builder 消费。"
```

---

## Task 3: 新增 banner_computer 模块

**Files:**
- Create: `src/card/render/banner_computer.py`
- Test: `tests/test_banner_computer.py`

把 banner 文案组装从所有 renderer 集中到一个纯函数：`{emoji} {mode} · {phase} · {elapsed}`。

- [ ] **Step 1: 写测试**

新建 `tests/test_banner_computer.py`：

```python
"""banner_computer unit tests."""
from __future__ import annotations

from src.card.render.banner_computer import compute_banner, format_elapsed
from src.card.state.models import CardMetadata
from src.card.state.runtime_stats import RuntimeStats


def test_format_elapsed_under_one_minute():
    assert format_elapsed(45.0) == "45s"


def test_format_elapsed_minute_seconds():
    assert format_elapsed(83.0) == "1m23s"


def test_format_elapsed_zero():
    assert format_elapsed(0.0) == "0s"


def test_banner_deep_executing():
    md = CardMetadata(mode_name="Deep", mode_emoji="🧠", engine_type="deep")
    rs = RuntimeStats(elapsed_seconds=83.0, deep_phase="executing")
    assert compute_banner(md, rs) == "🧠 Deep · 执行中 · 1m23s"


def test_banner_deep_analyzing():
    md = CardMetadata(mode_name="Deep", mode_emoji="🧠", engine_type="deep")
    rs = RuntimeStats(elapsed_seconds=10.0, deep_phase="analyzing")
    assert compute_banner(md, rs) == "🧠 Deep · 分析中 · 10s"


def test_banner_loop_round():
    md = CardMetadata(mode_name="Loop", mode_emoji="🔄", engine_type="loop")
    rs = RuntimeStats(elapsed_seconds=312.0, loop_round=2)
    assert compute_banner(md, rs) == "🔄 Loop · 第 2 轮 · 5m12s"


def test_banner_spec_cycle_perspective():
    md = CardMetadata(mode_name="Spec", mode_emoji="📐", engine_type="spec")
    rs = RuntimeStats(elapsed_seconds=484.0, spec_cycle=2, spec_perspective="code")
    assert compute_banner(md, rs) == "📐 Spec · cycle 2/code · 8m4s"


def test_banner_worktree_subagent():
    md = CardMetadata(mode_name="Worktree", mode_emoji="🌲", engine_type="worktree")
    rs = RuntimeStats(elapsed_seconds=138.0, worktree_subagent="aiden")
    assert compute_banner(md, rs) == "🌲 Worktree · wt·aiden · 2m18s"


def test_banner_emoji_fallback():
    md = CardMetadata(mode_name=None, mode_emoji=None, engine_type=None)
    rs = RuntimeStats(elapsed_seconds=5.0)
    assert compute_banner(md, rs) == "🤖 Programming · 进行中 · 5s"
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/test_banner_computer.py -v
```

预期：`ModuleNotFoundError: No module named 'src.card.render.banner_computer'`。

- [ ] **Step 3: 写实现**

`src/card/render/banner_computer.py`：

```python
"""Unified banner computation for sticky_head."""

from __future__ import annotations

from src.card.state.models import CardMetadata
from src.card.state.runtime_stats import RuntimeStats


def format_elapsed(seconds: float) -> str:
    """Format elapsed seconds as `Xm YYs` or `Ys`."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    minutes = s // 60
    rem = s % 60
    return f"{minutes}m{rem}s"


def compute_banner(metadata: CardMetadata, runtime: RuntimeStats) -> str:
    """Build banner text: `{emoji} {mode} · {phase} · {elapsed}`."""
    emoji = metadata.mode_emoji or "🤖"
    mode = metadata.mode_name or "Programming"
    phase = _format_phase(metadata, runtime)
    elapsed = format_elapsed(runtime.elapsed_seconds)
    return f"{emoji} {mode} · {phase} · {elapsed}"


def _format_phase(metadata: CardMetadata, runtime: RuntimeStats) -> str:
    """Engine-specific phase string."""
    engine = metadata.engine_type
    if engine == "deep":
        if runtime.deep_phase == "analyzing":
            return "分析中"
        return "执行中"
    if engine == "loop":
        return f"第 {runtime.loop_round or 1} 轮"
    if engine == "spec":
        cycle = runtime.spec_cycle if runtime.spec_cycle is not None else "?"
        persp = runtime.spec_perspective or "—"
        return f"cycle {cycle}/{persp}"
    if engine == "worktree":
        return f"wt·{runtime.worktree_subagent or '?'}"
    return "进行中"
```

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest tests/test_banner_computer.py -v
```

预期：所有 PASS。

- [ ] **Step 5: 提交**

```bash
git add src/card/render/banner_computer.py tests/test_banner_computer.py
git commit -m "feat(card): add banner_computer module

提供统一 banner 文案构造（{emoji} {mode} · {phase} · {elapsed}），
按引擎类型派发 phase 文案：Deep 分析/执行、Loop 第N轮、Spec cycle/perspective、
Worktree 子代理名。"
```

---

## Task 4: 新增 SectionLayout 模型

**Files:**
- Create: `src/card/render/layout.py`
- Test: `tests/test_section_layout.py`

`SectionLayout` 把 atom 分成 sticky_head / status / body / appendix 四区，`assemble_for_page(page_idx, total_pages, body_slice)` 按页装配。

- [ ] **Step 1: 写测试**

新建 `tests/test_section_layout.py`：

```python
"""SectionLayout model tests."""
from __future__ import annotations

from src.card.render.atoms import RenderAtom
from src.card.render.layout import SectionLayout


def _atom(kind: str, content: str = "x", nodes: int = 1) -> RenderAtom:
    return RenderAtom(kind=kind, content=content, node_count=nodes)  # type: ignore[arg-type]


def test_assemble_first_page_includes_status():
    layout = SectionLayout(
        sticky_head=(_atom("phase_banner", "BAN"),),
        status=(_atom("progress_bar", "PROG"),),
        body=(_atom("text", "BODY1"), _atom("text", "BODY2")),
        appendix=(_atom("tool_history", "HIST"),),
    )
    body_slice = (layout.body[0],)
    page = layout.assemble_for_page(page_idx=0, total_pages=2, body_slice=body_slice)
    contents = [a.content for a in page]
    assert contents == ["BAN", "PROG", "BODY1"]


def test_assemble_middle_page_no_status_no_appendix():
    layout = SectionLayout(
        sticky_head=(_atom("phase_banner", "BAN"),),
        status=(_atom("progress_bar", "PROG"),),
        body=(_atom("text", "B1"), _atom("text", "B2"), _atom("text", "B3")),
        appendix=(_atom("tool_history", "HIST"),),
    )
    page = layout.assemble_for_page(page_idx=1, total_pages=3, body_slice=(layout.body[1],))
    contents = [a.content for a in page]
    assert contents == ["BAN", "B2"]


def test_assemble_last_page_includes_appendix():
    layout = SectionLayout(
        sticky_head=(_atom("phase_banner", "BAN"),),
        status=(),
        body=(_atom("text", "B"),),
        appendix=(_atom("tool_history", "HIST"),),
    )
    page = layout.assemble_for_page(page_idx=1, total_pages=2, body_slice=(layout.body[0],))
    contents = [a.content for a in page]
    assert contents == ["BAN", "B", "HIST"]


def test_assemble_single_page_includes_status_and_appendix():
    layout = SectionLayout(
        sticky_head=(_atom("phase_banner", "BAN"),),
        status=(_atom("progress_bar", "PROG"),),
        body=(_atom("text", "B"),),
        appendix=(_atom("tool_history", "HIST"),),
    )
    page = layout.assemble_for_page(page_idx=0, total_pages=1, body_slice=layout.body)
    contents = [a.content for a in page]
    assert contents == ["BAN", "PROG", "B", "HIST"]


def test_empty_sticky_head_does_not_crash():
    layout = SectionLayout(
        sticky_head=(),
        status=(),
        body=(_atom("text", "B"),),
        appendix=(),
    )
    page = layout.assemble_for_page(page_idx=0, total_pages=1, body_slice=layout.body)
    assert [a.content for a in page] == ["B"]
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/test_section_layout.py -v
```

预期：`ModuleNotFoundError: No module named 'src.card.render.layout'`。

- [ ] **Step 3: 写实现**

`src/card/render/layout.py`：

```python
"""SectionLayout: SSOT for card section ordering and pagination contract."""

from __future__ import annotations

from dataclasses import dataclass

from src.card.render.atoms import RenderAtom


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
        self,
        page_idx: int,
        total_pages: int,
        body_slice: tuple[RenderAtom, ...],
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

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest tests/test_section_layout.py -v
```

预期：5 PASS。

- [ ] **Step 5: 提交**

```bash
git add src/card/render/layout.py tests/test_section_layout.py
git commit -m "feat(card): add SectionLayout SSOT model

四段骨架（sticky_head/status/body/appendix），assemble_for_page 按页序
注入 status（首页）/ appendix（末页）/ body_slice（每页），sticky_head
每页前置。续卡上下文锚点的统一契约。"
```

---

## Task 5: paginate_layout 函数

**Files:**
- Modify: `src/card/render/layout.py`
- Test: `tests/test_section_layout.py`

`paginate_layout(layout, budget) -> list[tuple[RenderAtom, ...]]`：sticky_head 从每页预算扣除，body 贪心切分。

- [ ] **Step 1: 追加测试**

在 `tests/test_section_layout.py` 末尾追加：

```python
def test_paginate_layout_single_page_when_under_budget():
    from src.card.render.budget import RenderBudget
    from src.card.render.layout import paginate_layout

    layout = SectionLayout(
        sticky_head=(_atom("phase_banner", "B"),),
        status=(),
        body=(_atom("text", "x" * 100),),
        appendix=(),
    )
    pages = paginate_layout(layout, RenderBudget())
    assert len(pages) == 1
    assert pages[0][0].kind == "phase_banner"
    assert pages[0][1].kind == "text"


def test_paginate_layout_multiple_pages_repeats_sticky():
    from src.card.render.budget import RenderBudget
    from src.card.render.layout import paginate_layout

    big_atom_a = _atom("text", "a" * 9000)
    big_atom_b = _atom("text", "b" * 9000)
    layout = SectionLayout(
        sticky_head=(_atom("phase_banner", "STICKY"),),
        status=(),
        body=(big_atom_a, big_atom_b),
        appendix=(),
    )
    pages = paginate_layout(layout, RenderBudget())
    assert len(pages) >= 2
    for page in pages:
        assert any(a.kind == "phase_banner" for a in page), \
            "sticky_head must be present on every page"


def test_paginate_layout_appendix_only_on_last_page():
    from src.card.render.budget import RenderBudget
    from src.card.render.layout import paginate_layout

    big_a = _atom("text", "a" * 9000)
    big_b = _atom("text", "b" * 9000)
    layout = SectionLayout(
        sticky_head=(_atom("phase_banner", "S"),),
        status=(),
        body=(big_a, big_b),
        appendix=(_atom("tool_history", "HIST"),),
    )
    pages = paginate_layout(layout, RenderBudget())
    assert len(pages) >= 2
    last_kinds = [a.kind for a in pages[-1]]
    earlier_kinds = [a.kind for p in pages[:-1] for a in p]
    assert "tool_history" in last_kinds
    assert "tool_history" not in earlier_kinds


def test_paginate_layout_status_only_on_first_page():
    from src.card.render.budget import RenderBudget
    from src.card.render.layout import paginate_layout

    big_a = _atom("text", "a" * 9000)
    big_b = _atom("text", "b" * 9000)
    layout = SectionLayout(
        sticky_head=(_atom("phase_banner", "S"),),
        status=(_atom("progress_bar", "PROG"),),
        body=(big_a, big_b),
        appendix=(),
    )
    pages = paginate_layout(layout, RenderBudget())
    assert len(pages) >= 2
    assert any(a.kind == "progress_bar" for a in pages[0])
    for p in pages[1:]:
        assert not any(a.kind == "progress_bar" for a in p)
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/test_section_layout.py -v
```

预期：4 个新测试 `ImportError: cannot import name 'paginate_layout'`。

- [ ] **Step 3: 在 layout.py 加 paginate_layout**

追加到 `src/card/render/layout.py`：

```python
from src.card.render.atoms import estimate_atom_size
from src.card.render.budget import RenderBudget
from src.card.render.pagination import (
    BASE_OVERHEAD,
    FIXED_NODE_OVERHEAD,
    split_atom,
)


def paginate_layout(
    layout: SectionLayout, budget: RenderBudget
) -> list[tuple[RenderAtom, ...]]:
    """Paginate body atoms with sticky_head reserved on every page.

    Algorithm:
    1. Compute sticky_size and sticky_nodes (occupy on every page).
    2. Account for first-page status and last-page appendix (only on those pages).
    3. Greedy-pack body atoms into pages with reduced budget.
    4. Use SectionLayout.assemble_for_page to wrap each body slice.
    """
    sticky_size = sum(
        a.byte_size if a.byte_size > 0 else estimate_atom_size(a)
        for a in layout.sticky_head
    )
    sticky_nodes = sum(a.node_count for a in layout.sticky_head)

    status_size = sum(
        a.byte_size if a.byte_size > 0 else estimate_atom_size(a)
        for a in layout.status
    )
    status_nodes = sum(a.node_count for a in layout.status)

    base_byte = budget.byte_budget - BASE_OVERHEAD - sticky_size
    base_node = budget.node_budget - FIXED_NODE_OVERHEAD - sticky_nodes

    body_pages: list[list[RenderAtom]] = [[]]
    cur_bytes = 0
    cur_nodes = 0
    is_first_page = True

    def remaining_byte() -> int:
        extra = status_size if is_first_page else 0
        return base_byte - extra - cur_bytes

    def remaining_node() -> int:
        extra = status_nodes if is_first_page else 0
        return base_node - extra - cur_nodes

    for atom in layout.body:
        atom_size = atom.byte_size if atom.byte_size > 0 else estimate_atom_size(atom)
        if (
            atom_size <= remaining_byte()
            and atom.node_count <= remaining_node()
        ):
            body_pages[-1].append(atom)
            cur_bytes += atom_size
            cur_nodes += atom.node_count
            continue

        rem = max(remaining_byte(), 0)
        split_result = split_atom(atom, rem)
        if split_result is not None and len(split_result) > 1:
            first_part, *rest = split_result
            first_size = first_part.byte_size if first_part.byte_size > 0 else estimate_atom_size(first_part)
            body_pages[-1].append(first_part)
            cur_bytes += first_size
            cur_nodes += first_part.node_count
            for part in rest:
                body_pages.append([])
                is_first_page = False
                cur_bytes = 0
                cur_nodes = 0
                part_size = part.byte_size if part.byte_size > 0 else estimate_atom_size(part)
                body_pages[-1].append(part)
                cur_bytes += part_size
                cur_nodes += part.node_count
            continue

        if body_pages[-1]:
            body_pages.append([])
            is_first_page = False
            cur_bytes = 0
            cur_nodes = 0
        body_pages[-1].append(atom)
        cur_bytes += atom_size
        cur_nodes += atom.node_count

    if not body_pages or (len(body_pages) == 1 and not body_pages[0]):
        body_pages = [[]]

    total = len(body_pages)
    return [
        layout.assemble_for_page(idx, total, tuple(slice_))
        for idx, slice_ in enumerate(body_pages)
    ]
```

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest tests/test_section_layout.py -v
```

预期：所有 PASS。

- [ ] **Step 5: 提交**

```bash
git add src/card/render/layout.py tests/test_section_layout.py
git commit -m "feat(card): add paginate_layout function

按 SectionLayout 切分页：sticky_head 每页预扣预算、status 仅首页、
appendix 仅末页、body 贪心切分（沿用 split_atom 既有策略）。"
```

---

## Task 6: 三明治锚点构造器 sticky_head.py

**Files:**
- Create: `src/card/render/sticky_head.py`
- Test: `tests/test_sticky_head.py`

`build_sticky_head(state, metadata) -> tuple[RenderAtom, ...]`：phase_banner（必有）+ task_list（compact）+ activity_summary（compact）。≤25 节点上限，超出降级。

- [ ] **Step 1: 写测试**

新建 `tests/test_sticky_head.py`：

```python
"""sticky_head builder tests."""
from __future__ import annotations

from unittest.mock import MagicMock

from src.card.render.sticky_head import build_sticky_head, STICKY_HEAD_MAX_NODES
from src.card.state.models import CardMetadata, CardState
from src.card.state.runtime_stats import RuntimeStats


def _state_with(*, has_task_list: bool, has_activity: bool, runtime: RuntimeStats) -> MagicMock:
    state = MagicMock(spec=CardState)
    state.metadata = CardMetadata(mode_name="Deep", mode_emoji="🧠", engine_type="deep")
    state.runtime_stats = runtime
    state.blocks = ()
    state.task_list = MagicMock()
    state.task_list.tasks = ({"task_id": "t1", "name": "x", "status": "in_progress"},) if has_task_list else ()
    state.task_list.current_task_id = "t1"
    state.task_list.block_id = "tl"
    state.activity = MagicMock()
    state.activity.has_data = has_activity
    return state


def test_sticky_head_minimum_phase_banner_only():
    runtime = RuntimeStats(elapsed_seconds=10.0, deep_phase="executing")
    state = _state_with(has_task_list=False, has_activity=False, runtime=runtime)
    sticky = build_sticky_head(state, state.metadata)
    assert len(sticky) == 1
    assert sticky[0].kind == "phase_banner"
    assert "Deep" in sticky[0].content


def test_sticky_head_includes_task_list_when_present():
    runtime = RuntimeStats(elapsed_seconds=5.0, deep_phase="executing")
    state = _state_with(has_task_list=True, has_activity=False, runtime=runtime)
    sticky = build_sticky_head(state, state.metadata)
    kinds = [a.kind for a in sticky]
    assert kinds == ["phase_banner", "task_list"]


def test_sticky_head_includes_activity_when_present():
    runtime = RuntimeStats(elapsed_seconds=5.0, deep_phase="executing")
    state = _state_with(has_task_list=True, has_activity=True, runtime=runtime)
    # Provide a tool block so activity panel is non-None
    state.blocks = ()  # render_activity_summary_panel(blocks, compact=True) called with state.blocks
    sticky = build_sticky_head(state, state.metadata)
    kinds = [a.kind for a in sticky]
    # activity may be empty if no tool blocks; tolerate either 2 or 3
    assert kinds[0] == "phase_banner"
    assert "task_list" in kinds


def test_sticky_head_node_cap_drops_activity_first():
    runtime = RuntimeStats(elapsed_seconds=5.0, deep_phase="executing")
    state = _state_with(has_task_list=True, has_activity=True, runtime=runtime)
    sticky = build_sticky_head(state, state.metadata, _force_total_nodes=STICKY_HEAD_MAX_NODES + 5)
    kinds = [a.kind for a in sticky]
    assert "phase_banner" in kinds
    # Drops happen from the right; activity_summary leaves first
    assert sum(a.node_count for a in sticky) <= STICKY_HEAD_MAX_NODES
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/test_sticky_head.py -v
```

预期：`ModuleNotFoundError`。

- [ ] **Step 3: 写实现**

`src/card/render/sticky_head.py`：

```python
"""sticky_head builder: phase_banner + (task_list compact) + (activity_summary compact)."""

from __future__ import annotations

from src.card.render.atoms import RenderAtom, estimate_atom_size
from src.card.render.banner_computer import compute_banner
from src.card.render.task_list import render_task_list_panel
from src.card.render.tools import render_activity_summary_panel
from src.card.state.models import CardMetadata, CardState

STICKY_HEAD_MAX_NODES = 25


def build_sticky_head(
    state: CardState,
    metadata: CardMetadata,
    *,
    _force_total_nodes: int | None = None,
) -> tuple[RenderAtom, ...]:
    """Build the sandwich anchor reused on every page.

    Returns: (phase_banner [, task_list_compact] [, activity_summary_compact])
    Drops activity_summary first when total node count exceeds STICKY_HEAD_MAX_NODES.
    """
    atoms: list[RenderAtom] = []

    # 1. phase_banner — always present
    runtime = getattr(state, "runtime_stats", None)
    banner_text = compute_banner(metadata, runtime) if runtime is not None else "🤖 Programming · 进行中 · 0s"
    banner_atom = RenderAtom(
        kind="phase_banner",
        content=banner_text,
        node_count=1,
        block_id="_phase_banner",
    )
    banner_atom.byte_size = estimate_atom_size(banner_atom)
    atoms.append(banner_atom)

    # 2. task_list (compact mode)
    task_list = getattr(state, "task_list", None)
    if task_list is not None and getattr(task_list, "tasks", None):
        panel = render_task_list_panel(task_list, compact=True)
        if panel is not None:
            tl_atom = RenderAtom(
                kind="task_list",
                elements=[panel],
                node_count=8,
                block_id="_sticky_task_list",
                content="",
            )
            tl_atom.byte_size = estimate_atom_size(tl_atom)
            atoms.append(tl_atom)

    # 3. activity_summary (compact mode)
    activity = getattr(state, "activity", None)
    blocks = getattr(state, "blocks", ())
    if activity is not None and getattr(activity, "has_data", False):
        panel = render_activity_summary_panel(blocks, compact=True)
        if panel is not None:
            act_atom = RenderAtom(
                kind="activity_summary",
                elements=[panel],
                node_count=4,
                block_id="_sticky_activity",
                content="",
            )
            act_atom.byte_size = estimate_atom_size(act_atom)
            atoms.append(act_atom)

    # 4. Node-budget degradation: drop activity, then task_list
    total = _force_total_nodes if _force_total_nodes is not None else sum(a.node_count for a in atoms)
    while total > STICKY_HEAD_MAX_NODES and len(atoms) > 1:
        atoms.pop()
        total = sum(a.node_count for a in atoms)

    return tuple(atoms)
```

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest tests/test_sticky_head.py -v
```

预期：4 PASS。

- [ ] **Step 5: 提交**

```bash
git add src/card/render/sticky_head.py tests/test_sticky_head.py
git commit -m "feat(card): add sticky_head builder

构造续卡每页前置三件套：phase_banner（必有）+ task_list compact +
activity_summary compact。节点上限 25，超出按 activity → task_list 顺序降级。"
```

---

## Task 7: task_list 加 compact 模式

**Files:**
- Modify: `src/card/render/task_list.py:23`
- Test: `tests/test_card_task_list.py`（如不存在则新建）

`render_task_list_panel(block, *, compact: bool = False)`：compact 模式只显当前 task + 进度比。

- [ ] **Step 1: 写测试**

新建 `tests/test_card_task_list.py`（若已存在，追加新 test 函数）：

```python
"""task_list compact mode tests."""
from __future__ import annotations

from src.card.render.task_list import render_task_list_panel
from src.card.state.models import TaskListBlock


def _make_block(current_id: str = "t2"):
    tasks = (
        {"task_id": "t1", "name": "探索代码", "status": "completed"},
        {"task_id": "t2", "name": "修复路由", "status": "in_progress"},
        {"task_id": "t3", "name": "单元测试", "status": "pending"},
        {"task_id": "t4", "name": "集成测试", "status": "pending"},
        {"task_id": "t5", "name": "文档更新", "status": "pending"},
    )
    return TaskListBlock(block_id="bl", tasks=tasks, current_task_id=current_id)


def test_task_list_full_mode_default():
    block = _make_block()
    panel = render_task_list_panel(block)
    assert panel is not None
    assert panel["tag"] == "collapsible_panel"
    md_content = panel["elements"][0]["content"]
    assert "探索代码" in md_content
    assert "修复路由" in md_content
    assert "文档更新" in md_content


def test_task_list_compact_mode_minimal():
    block = _make_block()
    panel = render_task_list_panel(block, compact=True)
    assert panel is not None
    assert panel["tag"] == "collapsible_panel"
    md_content = panel["elements"][0]["content"]
    assert "修复路由" in md_content
    assert "文档更新" not in md_content
    header_title = panel["header"]["title"]["content"]
    assert "1/5" in header_title or "进度" in header_title


def test_task_list_compact_returns_none_for_empty():
    block = TaskListBlock(block_id="b", tasks=(), current_task_id="")
    assert render_task_list_panel(block, compact=True) is None
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/test_card_task_list.py -v
```

预期：`test_task_list_compact_mode_minimal` FAIL（compact 参数不存在）。

- [ ] **Step 3: 修改 task_list.py**

`src/card/render/task_list.py:23` 起，把 `render_task_list_panel` 签名改为接受 `compact` 关键字参数，并加 `_build_compact_panel` 辅助：

```python
def render_task_list_panel(block: TaskListBlock, *, compact: bool = False) -> dict | None:
    """Render the task list panel.

    Args:
        block: TaskListBlock with tasks and current_task_id.
        compact: When True, show only current task + progress ratio (for sticky_head).
    """
    tasks = block.tasks
    if not tasks:
        return None

    current_id = block.current_task_id
    total = len(tasks)
    completed_count = sum(1 for t in tasks if t.get("status") == "completed")

    if compact:
        return _build_compact_panel(tasks, current_id, completed_count, total)

    lines = _build_task_lines(tasks, current_id)
    content = "\n".join(lines)
    expanded = total < _FOLD_THRESHOLD
    header_title = f"📋 **任务列表** — 进度：{completed_count}/{total} ✅"

    return {
        "tag": "collapsible_panel",
        "expanded": expanded,
        "header": {
            "title": {"tag": "markdown", "content": header_title},
            "vertical_align": "center",
            "icon": {
                "tag": "standard_icon",
                "token": "down-small-ccm_outlined",
                "size": "16px 16px",
            },
            "icon_position": "follow_text",
            "icon_expanded_angle": -180,
        },
        "border": {"color": PANEL_STYLES["border_task_list"], "corner_radius": PANEL_STYLES["corner_radius"]},
        "vertical_spacing": PANEL_STYLES["vertical_spacing"],
        "padding": PANEL_STYLES["padding_standard"],
        "elements": [{"tag": "markdown", "content": content}],
    }


def _build_compact_panel(
    tasks: tuple,
    current_id: str,
    completed_count: int,
    total: int,
) -> dict:
    """Compact panel: current task only + progress ratio in header."""
    current_task = next((t for t in tasks if t.get("task_id") == current_id), None)
    if current_task is None:
        current_task = next(
            (t for t in tasks if t.get("status") == "in_progress"),
            None,
        ) or next(
            (t for t in tasks if t.get("status") == "pending"),
            tasks[0],
        )

    name = current_task.get("name", "未命名任务")
    step_idx = next(
        (i + 1 for i, t in enumerate(tasks) if t.get("task_id") == current_task.get("task_id")),
        1,
    )
    content = f"▶ 🔄 {step_idx}/{total} **{name}**"
    header_title = f"📋 **任务列表** — 进度：{completed_count}/{total} ✅"

    return {
        "tag": "collapsible_panel",
        "expanded": True,
        "header": {
            "title": {"tag": "markdown", "content": header_title},
            "vertical_align": "center",
            "icon": {
                "tag": "standard_icon",
                "token": "down-small-ccm_outlined",
                "size": "16px 16px",
            },
            "icon_position": "follow_text",
            "icon_expanded_angle": -180,
        },
        "border": {"color": PANEL_STYLES["border_task_list"], "corner_radius": PANEL_STYLES["corner_radius"]},
        "vertical_spacing": PANEL_STYLES["vertical_spacing"],
        "padding": PANEL_STYLES["padding_standard"],
        "elements": [{"tag": "markdown", "content": content}],
    }
```

保留原 `_build_task_lines` / `_format_task_line` 不变。

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest tests/test_card_task_list.py -v
```

预期：3 PASS。

- [ ] **Step 5: 提交**

```bash
git add src/card/render/task_list.py tests/test_card_task_list.py
git commit -m "feat(card): task_list compact mode

新增 compact 参数：仅显当前任务 + 进度比，节点占用 ≈8（vs full 模式 30+），
供 sticky_head 在续卡每页前置使用。Full 模式行为不变。"
```

---

## Task 8: activity_summary 加 compact 模式

**Files:**
- Modify: `src/card/render/tools.py` `render_activity_summary_panel`
- Test: `tests/test_card_render_tools.py`（不存在则新建）

`render_activity_summary_panel(blocks, *, compact: bool = False)`：compact 模式只显计数 header（不展开详情）。

- [ ] **Step 1: 先读 tools.py 现有签名**

```bash
grep -n "def render_activity_summary_panel\|def render_tool_panel\|def render_tool_history_panel" /Users/jiataorui/workspaces/aiwork/ghostAp/src/card/render/tools.py
```

记录现有签名以保留 full 模式行为。

- [ ] **Step 2: 写测试**

新建 `tests/test_card_render_tools.py`：

```python
"""tools.py activity_summary compact mode tests."""
from __future__ import annotations

from src.card.render.tools import render_activity_summary_panel
from src.card.state.models import ContentBlock


def _tool_block(name: str = "Edit", summary: str = "x", status: str = "completed") -> ContentBlock:
    """Build a tool_call ContentBlock for tests.

    Note: is_latest_active is added in Task 9; tests in this task don't need it.
    """
    return ContentBlock(
        kind="tool_call",
        block_id=f"b_{name}",
        tool_name=name,
        tool_summary=summary,
        content="",
        status=status,
    )


def test_activity_summary_full_mode_default():
    blocks = (_tool_block("Edit"), _tool_block("Bash"), _tool_block("Grep"))
    panel = render_activity_summary_panel(blocks)
    assert panel is not None
    assert panel["tag"] == "collapsible_panel"


def test_activity_summary_compact_mode_only_header():
    blocks = (_tool_block("Edit"), _tool_block("Bash"), _tool_block("Grep"))
    panel = render_activity_summary_panel(blocks, compact=True)
    assert panel is not None
    assert panel["tag"] == "collapsible_panel"
    assert panel.get("expanded") is False
    title_content = panel["header"]["title"]["content"]
    assert "活动" in title_content or "📊" in title_content


def test_activity_summary_compact_returns_none_for_empty():
    assert render_activity_summary_panel((), compact=True) is None
```

- [ ] **Step 3: 跑测试确认失败**

```bash
uv run pytest tests/test_card_render_tools.py -v -k activity_summary
```

预期：`test_activity_summary_compact_mode_only_header` FAIL（compact 参数不存在）。

- [ ] **Step 4: 修改 render_activity_summary_panel**

打开 `src/card/render/tools.py`，找到 `render_activity_summary_panel`。在签名加 `compact` 关键字参数；当 `compact=True` 时返回简短折叠 panel：

```python
def render_activity_summary_panel(
    blocks,
    *,
    compact: bool = False,
):
    """Render the activity summary panel from completed tool blocks.

    Args:
        blocks: Sequence of ContentBlocks (filters internally for tool_call).
        compact: When True, return collapsed panel with header counts only,
                 suitable for sticky_head reuse on continuation pages.
    """
    tool_blocks = [b for b in blocks if getattr(b, "kind", "") == "tool_call" and getattr(b, "status", "") == "completed"]
    if not tool_blocks:
        return None

    edits = sum(1 for b in tool_blocks if (b.tool_name or "").lower() in {"edit", "write", "multiedit"})
    runs = sum(1 for b in tool_blocks if (b.tool_name or "").lower() in {"bash", "shell"})
    searches = sum(1 for b in tool_blocks if (b.tool_name or "").lower() in {"grep", "glob", "find"})

    header_title = f"📊 活动 — 已编辑 {edits} · 运行 {runs} · 搜索 {searches}"

    if compact:
        return {
            "tag": "collapsible_panel",
            "expanded": False,
            "header": {
                "title": {"tag": "markdown", "content": header_title},
                "vertical_align": "center",
                "icon": {
                    "tag": "standard_icon",
                    "token": "down-small-ccm_outlined",
                    "size": "16px 16px",
                },
                "icon_position": "follow_text",
                "icon_expanded_angle": -180,
            },
            "border": {"color": "grey", "corner_radius": "4px"},
            "vertical_spacing": "4px",
            "padding": "4px 8px",
            "elements": [{"tag": "markdown", "content": "_(展开查看详情)_"}],
        }

    # Full mode: keep existing implementation. If existing signature differs,
    # preserve original return shape and only add the compact branch above.
    detail_lines = [f"- {b.tool_name}: {b.tool_summary or 'done'}" for b in tool_blocks[-10:]]
    detail_content = "\n".join(detail_lines)
    return {
        "tag": "collapsible_panel",
        "expanded": False,
        "header": {
            "title": {"tag": "markdown", "content": header_title},
            "vertical_align": "center",
            "icon": {
                "tag": "standard_icon",
                "token": "down-small-ccm_outlined",
                "size": "16px 16px",
            },
            "icon_position": "follow_text",
            "icon_expanded_angle": -180,
        },
        "border": {"color": "grey", "corner_radius": "4px"},
        "vertical_spacing": "4px",
        "padding": "8px",
        "elements": [{"tag": "markdown", "content": detail_content}],
    }
```

如 `tools.py` 现有 full-mode 实现与上面 detail 部分签名 / 返回结构不同：保留现有 full-mode 整段不动，仅追加 `if compact:` 早返回分支与 header_title 计算。

- [ ] **Step 5: 跑测试确认通过**

```bash
uv run pytest tests/test_card_render_tools.py -v -k activity_summary
```

预期：3 PASS。

- [ ] **Step 6: 提交**

```bash
git add src/card/render/tools.py tests/test_card_render_tools.py
git commit -m "feat(card): activity_summary compact mode

新增 compact 参数：仅 header 显示计数（已编辑/运行/搜索），节点占用 ≈4，
供 sticky_head 在续卡每页前置。Full 模式保留详情列表。"
```

---

## Task 9: ContentBlock 加 is_latest_active 字段 + reducer 单例维护

**Files:**
- Modify: `src/card/state/models.py` `ContentBlock`
- Modify: `src/card/state/reducer.py` 或 `src/card/state/reducers/` 子模块（按现有结构）
- Test: `tests/test_card_reducer_main.py`

`ContentBlock` 增加 `is_latest_active: bool = False` 字段。reducer 在 tool_call_started 时单例置位、tool_call_ended 时找下一个 active block 升 latest。

- [ ] **Step 1: 先 grep 实际 reducer 入口**

```bash
grep -n "^def apply_event\|^def reduce\|^def dispatch" /Users/jiataorui/workspaces/aiwork/ghostAp/src/card/state/reducer.py
ls /Users/jiataorui/workspaces/aiwork/ghostAp/src/card/state/reducers/
grep -rn "tool_call_started\|tool_call_ended" /Users/jiataorui/workspaces/aiwork/ghostAp/src/card/state/reducers/ | head -10
```

记录实际入口函数名与 tool_call reducer 路径，调整下面测试与实现的 import。

- [ ] **Step 2: 写测试**

追加到 `tests/test_card_reducer_main.py`（按实际 import 路径调整）：

```python
def test_first_tool_start_marks_latest_active():
    """First tool_call_started → block.is_latest_active=True."""
    from src.card.events import CardEvent
    from src.card.state.models import CardFooter, CardHeader, CardMetadata, CardState
    from src.card.state.reducer import apply_event  # adjust to actual entry

    state = CardState(
        metadata=CardMetadata(),
        header=CardHeader(title="t"),
        footer=CardFooter(),
        blocks=(),
        block_index={},
        terminal="running",
        buttons=(),
    )
    state = apply_event(state, CardEvent.tool_call_started("tool1", tool_name="Grep"))
    block = state.blocks[0]
    assert block.is_latest_active is True
    assert block.status == "active"


def test_second_tool_start_demotes_first():
    from src.card.events import CardEvent
    from src.card.state.models import CardFooter, CardHeader, CardMetadata, CardState
    from src.card.state.reducer import apply_event

    state = CardState(
        metadata=CardMetadata(),
        header=CardHeader(title="t"),
        footer=CardFooter(),
        blocks=(),
        block_index={},
        terminal="running",
        buttons=(),
    )
    state = apply_event(state, CardEvent.tool_call_started("tool1", tool_name="Grep"))
    state = apply_event(state, CardEvent.tool_call_started("tool2", tool_name="Edit"))
    by_id = {b.block_id: b for b in state.blocks}
    assert by_id["tool1"].is_latest_active is False
    assert by_id["tool2"].is_latest_active is True


def test_tool_end_promotes_next_latest():
    from src.card.events import CardEvent
    from src.card.state.models import CardFooter, CardHeader, CardMetadata, CardState
    from src.card.state.reducer import apply_event

    state = CardState(
        metadata=CardMetadata(),
        header=CardHeader(title="t"),
        footer=CardFooter(),
        blocks=(),
        block_index={},
        terminal="running",
        buttons=(),
    )
    state = apply_event(state, CardEvent.tool_call_started("tool1", tool_name="Grep"))
    state = apply_event(state, CardEvent.tool_call_started("tool2", tool_name="Edit"))
    state = apply_event(state, CardEvent.tool_call_ended("tool2", success=True))
    by_id = {b.block_id: b for b in state.blocks}
    assert by_id["tool2"].is_latest_active is False
    assert by_id["tool2"].status == "completed"
    assert by_id["tool1"].is_latest_active is True


def test_only_one_latest_active_invariant_after_chain():
    from src.card.events import CardEvent
    from src.card.state.models import CardFooter, CardHeader, CardMetadata, CardState
    from src.card.state.reducer import apply_event

    state = CardState(
        metadata=CardMetadata(),
        header=CardHeader(title="t"),
        footer=CardFooter(),
        blocks=(),
        block_index={},
        terminal="running",
        buttons=(),
    )
    for i in range(5):
        state = apply_event(state, CardEvent.tool_call_started(f"t{i}", tool_name="X"))
    for i in (1, 3):
        state = apply_event(state, CardEvent.tool_call_ended(f"t{i}", success=True))
    actives = [b for b in state.blocks if b.kind == "tool_call" and b.is_latest_active]
    assert len(actives) == 1, f"expected exactly 1 latest_active, got {len(actives)}"
```

- [ ] **Step 3: 跑测试确认失败**

```bash
uv run pytest tests/test_card_reducer_main.py -v -k latest_active
```

预期：`AttributeError: 'ContentBlock' object has no attribute 'is_latest_active'`。

- [ ] **Step 4: 给 ContentBlock 加字段**

`src/card/state/models.py` 找到 `ContentBlock` 定义（grep `class ContentBlock` 定位），在末尾加：

```python
    is_latest_active: bool = False  # tool_call: only the most recently started active tool is True
```

注：因 ContentBlock 是 frozen dataclass，新字段必须有默认值。

- [ ] **Step 5: reducer 维护单例**

打开 `src/card/state/reducer.py` 或对应 `reducers/tool_call.py`，找到处理 `tool_call_started` 的函数。在生成新 block 之前 demote 旧 latest，并把新 block 标 latest：

```python
from dataclasses import replace

def reduce_tool_call_started(state, payload):
    new_blocks: list[ContentBlock] = []
    for b in state.blocks:
        if b.kind == "tool_call" and b.is_latest_active:
            new_blocks.append(replace(b, is_latest_active=False))
        else:
            new_blocks.append(b)

    new_block = ContentBlock(
        kind="tool_call",
        block_id=payload.tool_id,
        tool_name=getattr(payload, "tool_name", None),
        status="active",
        is_latest_active=True,
        # other fields per existing schema
    )
    new_blocks.append(new_block)
    return replace(state, blocks=tuple(new_blocks))
```

`tool_call_ended`：

```python
def reduce_tool_call_ended(state, payload):
    new_blocks: list[ContentBlock] = []
    for b in state.blocks:
        if b.block_id == payload.tool_id and b.kind == "tool_call":
            new_blocks.append(replace(b, status="completed", is_latest_active=False))
        else:
            new_blocks.append(b)

    active_indices = [
        i for i, b in enumerate(new_blocks)
        if b.kind == "tool_call" and b.status == "active"
    ]
    if active_indices:
        latest_idx = active_indices[-1]
        new_blocks[latest_idx] = replace(new_blocks[latest_idx], is_latest_active=True)

    return replace(state, blocks=tuple(new_blocks))
```

如果项目实际 reducer 命名为 `_handle_tool_call_started` 或类似，把上述两段嵌入对应函数体：保留所有原有字段更新逻辑，只追加 `is_latest_active` 维护这两步（demote 旧 + promote 新；ended 时升 next）。

- [ ] **Step 6: 跑测试确认通过**

```bash
uv run pytest tests/test_card_reducer_main.py -v -k latest_active
```

预期：4 PASS。也跑既有 reducer 全测确认未破坏：

```bash
uv run pytest tests/test_card_reducer_main.py tests/test_card_reducers.py -v
```

- [ ] **Step 7: 提交**

```bash
git add src/card/state/models.py src/card/state/reducer.py src/card/state/reducers/ tests/test_card_reducer_main.py
git commit -m "feat(state): tool_call latest_active singleton invariance

ContentBlock 新增 is_latest_active 字段；reducer 在 tool_call_started 时
demote 旧 latest 并 promote 新 block，tool_call_ended 时把下一个 active
block 升为 latest。配套单测覆盖单例不变量。"
```

---

## Task 10: render_tool_panel 改用 is_latest_active

**Files:**
- Modify: `src/card/render/tools.py` `render_tool_panel`
- Test: `tests/test_card_render_tools.py`

旧逻辑 `expanded = block.status == "active"` 改为 `expanded = block.is_latest_active`。

- [ ] **Step 1: 写测试**

追加到 `tests/test_card_render_tools.py`：

```python
def test_tool_panel_expanded_only_for_latest_active():
    """Only the latest_active tool_call has expanded=True; others collapsed."""
    from src.card.render.tools import render_tool_panel
    from src.card.state.models import ContentBlock

    latest = ContentBlock(
        kind="tool_call", block_id="t_latest", tool_name="Grep",
        status="active", is_latest_active=True, content="searching",
    )
    older = ContentBlock(
        kind="tool_call", block_id="t_older", tool_name="Edit",
        status="active", is_latest_active=False, content="editing",
    )
    completed = ContentBlock(
        kind="tool_call", block_id="t_done", tool_name="Read",
        status="completed", is_latest_active=False, content="done",
    )

    p_latest = render_tool_panel(latest)
    p_older = render_tool_panel(older)
    p_done = render_tool_panel(completed)

    assert p_latest["expanded"] is True
    assert p_older["expanded"] is False
    assert p_done["expanded"] is False
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/test_card_render_tools.py::test_tool_panel_expanded_only_for_latest_active -v
```

预期：FAIL（older active 仍被展开）。

- [ ] **Step 3: 改 render_tool_panel**

`src/card/render/tools.py` 找到 `render_tool_panel`，把 `expanded` 计算改为：

```python
def render_tool_panel(block):
    """Render a tool panel. Only the latest_active tool stays expanded."""
    expanded = bool(getattr(block, "is_latest_active", False))
    # ... 其余实现不变（preserve all other fields, border, padding） ...
```

仅替换 `expanded` 这一行，其他字段、节点结构保持不变。

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest tests/test_card_render_tools.py -v
uv run pytest tests/test_card_e2e.py -v --timeout=60
```

预期：所有 PASS。

- [ ] **Step 5: 提交**

```bash
git add src/card/render/tools.py tests/test_card_render_tools.py
git commit -m "refactor(card): tool_panel expanded uses is_latest_active

旧规则 active 全展开导致并发场景节点爆炸（参考图1乱）。新规则只展开
最新 active 工具，其他 active/completed 全折，节点占用降 ≈60%。"
```

---

## Task 11: CardSplitPayload + CardEvent.card_split 工厂

**Files:**
- Modify: `src/card/events/payloads.py`
- Modify: `src/card/events/factories.py`
- Modify: `src/card/events/types.py`
- Test: `tests/test_card_events.py`

新事件 `card_split`，payload 含 `reason / hint`。

- [ ] **Step 1: 先 grep 现有 factory 风格**

```bash
grep -n "^def \|@classmethod\|tool_call_started\|tool_call_ended" /Users/jiataorui/workspaces/aiwork/ghostAp/src/card/events/factories.py | head -20
grep -n "ToolCall\|class .*Payload" /Users/jiataorui/workspaces/aiwork/ghostAp/src/card/events/payloads.py | head -10
```

记录现有 CardEvent factory 风格（classmethod / 函数 / dict）后调整下面 step 3。

- [ ] **Step 2: 写测试**

追加到 `tests/test_card_events.py`：

```python
def test_card_split_event_factory():
    from src.card.events import CardEvent
    from src.card.events.payloads import CardSplitPayload

    ev = CardEvent.card_split(reason="task_done", hint="接续 task 3")
    assert ev.event_type == "card_split"
    assert isinstance(ev.payload, CardSplitPayload)
    assert ev.payload.reason == "task_done"
    assert ev.payload.hint == "接续 task 3"


def test_card_split_event_no_hint():
    from src.card.events import CardEvent

    ev = CardEvent.card_split(reason="round_changed")
    assert ev.payload.reason == "round_changed"
    assert ev.payload.hint is None
```

- [ ] **Step 3: 跑测试确认失败**

```bash
uv run pytest tests/test_card_events.py -v -k card_split
```

预期：`AttributeError`。

- [ ] **Step 4: 加 payload + factory + 类型常量**

`src/card/events/payloads.py` 末尾加：

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class CardSplitPayload:
    """Payload for card_split events: triggers a semantic page break.

    reason:
      - "task_done"      — current task finished
      - "phase_changed"  — Deep analyzing → executing
      - "round_changed"  — Loop round increment
      - "cycle_changed"  — Spec cycle/perspective switch
    hint: optional first-line text for the new card body
    """
    reason: str
    hint: str | None = None
```

`src/card/events/types.py` 加常量（按现有风格）：

```python
CARD_SPLIT = "card_split"
```

`src/card/events/factories.py` 加 classmethod（按 CardEvent 现有 factory 风格）：

```python
@classmethod
def card_split(cls, reason: str, hint: str | None = None) -> "CardEvent":
    """Trigger a semantic card split.

    The session listens for this event, closes the current card with hooks,
    and signals upstream renderer to start a new session.
    """
    from src.card.events.payloads import CardSplitPayload
    return cls(
        event_type="card_split",
        payload=CardSplitPayload(reason=reason, hint=hint),
    )
```

如 `CardEvent` 不是用 classmethod 而是用模块级 helper 函数，把 `card_split` 也定义为同风格函数。

- [ ] **Step 5: 跑测试确认通过**

```bash
uv run pytest tests/test_card_events.py -v -k card_split
```

预期：2 PASS。

- [ ] **Step 6: 提交**

```bash
git add src/card/events/
git commit -m "feat(card-events): add card_split event

引擎主动触发的语义切卡事件（reason: task_done/phase_changed/
round_changed/cycle_changed，可选 hint 文案）。session.py 在下一任务接入。"
```

---

## Task 12: session 监听 card_split

**Files:**
- Modify: `src/card/session/__init__.py` 或具体 session 文件（grep 定位）
- Test: `tests/test_card_split_event.py`

session 收到 `card_split`：先 close_open_blocks → dispatch completed → 触发 hooks → 调用 `on_card_split_completed` 回调。

- [ ] **Step 1: 定位 session 主路径**

```bash
ls /Users/jiataorui/workspaces/aiwork/ghostAp/src/card/session/
grep -rn "def dispatch\|tool_call_started\|completed" /Users/jiataorui/workspaces/aiwork/ghostAp/src/card/session/ | head -20
```

记录主入口文件与事件分发函数名。

- [ ] **Step 2: 写测试**

新建 `tests/test_card_split_event.py`：

```python
"""card_split end-to-end through CardSession."""
from __future__ import annotations

from unittest.mock import MagicMock

from src.card.events import CardEvent
# Adjust import to actual CardSession path:
from src.card.session import CardSession


def test_card_split_closes_session_and_fires_hooks():
    delivery = MagicMock()
    hook_calls: list[str] = []

    class _Hook:
        def on_session_completed(self, *a, **kw):
            hook_calls.append("session_completed")

    session = CardSession(
        chat_id="c1",
        message_id="m1",
        delivery=delivery,
        hooks=(_Hook(),),
    )
    session.dispatch(CardEvent.started())
    session.dispatch(CardEvent.card_split(reason="task_done", hint="task 3"))

    assert getattr(session, "is_closed", False) or getattr(session, "_closed", False), \
        "session must be closed after card_split"
    assert "session_completed" in hook_calls, "hooks must fire on split"


def test_card_split_emits_split_completed_signal():
    delivery = MagicMock()
    on_split = MagicMock()
    session = CardSession(
        chat_id="c1",
        message_id="m1",
        delivery=delivery,
        hooks=(),
    )
    session.on_card_split_completed = on_split
    session.dispatch(CardEvent.started())
    session.dispatch(CardEvent.card_split(reason="phase_changed", hint="executing"))

    on_split.assert_called_once()
```

注：`CardSession` 实际构造参数与 hook 接口可能不同，按 step 1 grep 结果调整测试。

- [ ] **Step 3: 跑测试确认失败**

```bash
uv run pytest tests/test_card_split_event.py -v
```

预期：FAIL。

- [ ] **Step 4: 加 _handle_card_split**

在 session 主路径事件分发表里加 `card_split` 分支：

```python
def _handle_card_split(self, event) -> None:
    """Close the current card cleanly and signal upstream to start a new session."""
    payload = event.payload
    if hasattr(self, "_stream_bridge") and self._stream_bridge is not None:
        self._stream_bridge.close_open_blocks()
    from src.card.events import CardEvent
    self._dispatch_internal(CardEvent.completed())
    self._closed = True
    cb = getattr(self, "on_card_split_completed", None)
    if callable(cb):
        cb(payload.reason, payload.hint)


@property
def is_closed(self) -> bool:
    return getattr(self, "_closed", False)
```

注册到事件分发（按现有事件分发风格）：

```python
elif event.event_type == "card_split":
    self._handle_card_split(event)
    return
```

如果实际项目的 session 主路径用 dict 分发表（如 `_HANDLERS = {"started": ..., "completed": ...}`），加 `"card_split": self._handle_card_split` 同款入口。

- [ ] **Step 5: 跑测试确认通过**

```bash
uv run pytest tests/test_card_split_event.py -v
```

预期：2 PASS。

- [ ] **Step 6: 提交**

```bash
git add src/card/session/ tests/test_card_split_event.py
git commit -m "feat(card-session): handle card_split event

收到 card_split 事件：flush 流式 text → dispatch completed（触发既有
hooks）→ 标 closed → 调用 on_card_split_completed 通知上层起新 session。"
```

---

## Task 13: renderer.py 主流程接入 SectionLayout

**Files:**
- Modify: `src/card/render/renderer.py:69-185` `render_card`
- Test: `tests/test_card_continuation_sticky.py`

`render_card` 改用 SectionLayout + paginate_layout 路径。

- [ ] **Step 1: 写测试**

新建 `tests/test_card_continuation_sticky.py`：

```python
"""End-to-end: continuation pages must reinject sticky_head."""
from __future__ import annotations

from src.card.render.budget import RenderBudget
from src.card.render.renderer import render_card
from src.card.state.models import (
    CardFooter, CardHeader, CardMetadata, CardState, ContentBlock,
)
from src.card.state.runtime_stats import RuntimeStats


def _make_state_with_long_body() -> CardState:
    blocks_list: list[ContentBlock] = [
        ContentBlock(
            kind="task_list",
            block_id="tl",
            content="",
            tasks=(
                {"task_id": "t1", "name": "step 1", "status": "completed"},
                {"task_id": "t2", "name": "step 2", "status": "in_progress"},
            ),
            current_task_id="t2",
        ),
    ]
    for i in range(8):
        blocks_list.append(ContentBlock(
            kind="text", block_id=f"text_{i}",
            content="x" * 4000, status="completed",
        ))
    blocks = tuple(blocks_list)
    return CardState(
        metadata=CardMetadata(mode_name="Deep", mode_emoji="🧠", engine_type="deep"),
        header=CardHeader(title="Deep"),
        footer=CardFooter(),
        blocks=blocks,
        block_index={b.block_id: i for i, b in enumerate(blocks)},
        terminal="running",
        buttons=(),
        runtime_stats=RuntimeStats(elapsed_seconds=83.0, deep_phase="executing"),
    )


def test_continuation_pages_have_sticky_head_phase_banner():
    state = _make_state_with_long_body()
    pages = render_card(state, RenderBudget())
    assert len(pages) >= 2, f"expected multi-page, got {len(pages)}"
    for page in pages:
        body_elements = page._card_json["body"]["elements"]
        first = body_elements[0]
        first_content = first.get("content", "") if first.get("tag") == "markdown" else ""
        assert "Deep" in first_content and "·" in first_content, \
            f"Page first element missing sticky banner. Got: {first_content[:60]}"
```

注：`CardState` 构造可能要求 `runtime_stats` 字段已在 Task 9 / Task 13 之前加到 `models.py`。如果 `CardState` 还没 `runtime_stats` 字段，先在 `models.py` 加上：

```python
# In CardState definition:
runtime_stats: RuntimeStats = field(default_factory=RuntimeStats)
```

把这个改动作为本任务 step 2 的一部分。

- [ ] **Step 2: CardState 加 runtime_stats 字段（如未加）**

在 `src/card/state/models.py` `CardState` 定义里加：

```python
from src.card.state.runtime_stats import RuntimeStats

# Inside CardState dataclass:
runtime_stats: RuntimeStats = field(default_factory=RuntimeStats)
```

确保 `CardState` 是 frozen dataclass 时新字段有默认值。

- [ ] **Step 3: 跑测试确认失败**

```bash
uv run pytest tests/test_card_continuation_sticky.py -v
```

预期：FAIL（续卡缺 banner）。

- [ ] **Step 4: 改 render_card**

替换 `src/card/render/renderer.py:69-185` `render_card` 为以下实现：

```python
def render_card(
    state: CardState, budget: RenderBudget
) -> list[RenderedCard]:
    """Main entry: CardState → list[RenderedCard]."""
    if budget.engine_cmd == "命令" or budget.engine_cmd == "对应命令":
        engine_cmd = _engine_type_to_cmd(state.metadata.engine_type if state.metadata else None)
        if engine_cmd != "命令" and engine_cmd != "对应命令":
            from dataclasses import replace
            budget = replace(budget, engine_cmd=engine_cmd)

    block_index: dict[str, ContentBlock] = {
        bid: state.blocks[idx] for bid, idx in state.block_index.items()
    }

    # 1. Flatten and decorate atoms
    raw_atoms = flatten_to_atoms(state.blocks, budget)
    raw_atoms = _with_activity_summary_atom(raw_atoms, state)

    # 2. Build SectionLayout
    from src.card.render.layout import SectionLayout, paginate_layout
    from src.card.render.sticky_head import build_sticky_head

    sticky_head = build_sticky_head(state, state.metadata)
    status_atoms: list[RenderAtom] = []
    body_atoms: list[RenderAtom] = []
    appendix_atoms: list[RenderAtom] = []
    for atom in raw_atoms:
        if atom.kind == "task_list":
            # task_list is now in sticky_head; drop from body to avoid duplication
            continue
        if atom.kind in _STATUS_ATOM_KINDS:
            status_atoms.append(atom)
        elif atom.kind in _APPENDIX_ATOM_KINDS:
            appendix_atoms.append(atom)
        else:
            body_atoms.append(atom)

    layout = SectionLayout(
        sticky_head=sticky_head,
        status=tuple(status_atoms),
        body=tuple(body_atoms),
        appendix=tuple(appendix_atoms),
    )

    # 3. Paginate
    pages = paginate_layout(layout, budget)
    total_pages = len(pages)

    global_sig = compute_structure_signature(state)
    content_hash = compute_content_hash(state)

    results: list[RenderedCard] = []
    for page_idx, page_atoms in enumerate(pages):
        body_elements = _render_atoms_to_elements(list(page_atoms), state, budget, block_index)

        if page_idx == 0 and state.footer.warning_banner and state.footer.warning_type:
            bg_style, icon = _BANNER_STYLES.get(state.footer.warning_type, ("grey", "ℹ️"))
            banner_text = f"{icon} **{state.footer.warning_banner}**"
            top_banner = _build_column_banner(content=banner_text, background_style=bg_style)
            body_elements.insert(0, top_banner)
        elif page_idx > 0 and state.footer.warning_banner:
            bg_style, icon = _BANNER_STYLES.get(state.footer.warning_type or "warning", ("grey", "ℹ️"))
            warning_note = _build_column_banner(
                content=f"{icon} **{state.footer.warning_banner}**",
                background_style=bg_style,
            )
            body_elements.insert(0, warning_note)

        if page_idx == total_pages - 1:
            body_elements.extend(render_footer(state, budget=budget))
            body_elements.extend(render_buttons(state, budget=budget))

        active_element = _find_active_element(list(page_atoms), block_index)
        is_running = state.terminal == "running"
        streaming = active_element is not None and is_running

        card_json = _assemble_card_json(
            state=state,
            body_elements=body_elements,
            streaming=streaming,
            active_element=active_element,
        )

        from src.card.render.payload_truncator import count_tagged_nodes
        node_count = count_tagged_nodes(card_json)
        if node_count > 200:
            logger.warning(
                "Rendered card page %d has %d nodes (exceeds Feishu 200-element limit)",
                page_idx, node_count,
            )

        page_sig_parts = [global_sig, f"page:{page_idx}"]
        for elem in body_elements:
            tag = elem.get("tag", "")
            page_sig_parts.append(tag)
            if tag == "markdown":
                page_sig_parts.append(str(elem.get("content", ""))[:64])
            elif tag == "collapsible_panel":
                header = elem.get("header")
                if isinstance(header, dict):
                    title_obj = header.get("title", {})
                    page_sig_parts.append(str(title_obj.get("content", ""))[:32])
                elif isinstance(header, str):
                    page_sig_parts.append(header[:32])
        page_signature = hashlib.md5(
            "|".join(page_sig_parts).encode("utf-8")
        ).hexdigest()

        results.append(
            RenderedCard(
                _card_json=card_json,
                structure_signature=page_signature,
                content_hash=content_hash,
                active_element=active_element,
                page_index=page_idx,
                total_pages=total_pages,
            )
        )

    return results
```

注：上面 `_order_atoms_by_section` 已不再被 `render_card` 直接使用，但保留函数供外部测试或老代码引用。

- [ ] **Step 5: 跑测试**

```bash
uv run pytest tests/test_card_continuation_sticky.py -v
uv run pytest tests/test_card_pagination.py tests/test_card_e2e.py -v --timeout=60
```

如有既有测 FAIL：分析是因新代码引入还是行为合理调整。task_list 现在在 sticky_head 而非 body，部分既有测可能需要调整断言（这是预期变更）。

- [ ] **Step 6: 提交**

```bash
git add src/card/render/renderer.py src/card/state/models.py tests/test_card_continuation_sticky.py
git commit -m "refactor(card): render_card pipeline uses SectionLayout

flatten 后按 atom kind 分流到 sticky_head/status/body/appendix 四区，
通过 SectionLayout + paginate_layout 装配。续卡每页前置 sticky_head
（phase_banner + task_list compact + activity_summary compact），
解决用户翻续卡丢失上下文锚点的核心 gap。"
```

---

## Task 14: pagination.paginate_atoms 退化为 shim

**Files:**
- Modify: `src/card/render/pagination.py:16-91`
- Test: `tests/test_card_pagination.py`

旧入口 `paginate_atoms(atoms, budget)` 退化为薄壳调用 `paginate_layout(SectionLayout(body=atoms), budget)`。

- [ ] **Step 1: 写测试**

追加到 `tests/test_card_pagination.py`：

```python
def test_paginate_atoms_shim_preserves_behavior():
    from src.card.render.atoms import RenderAtom, estimate_atom_size
    from src.card.render.budget import RenderBudget
    from src.card.render.layout import SectionLayout, paginate_layout
    from src.card.render.pagination import paginate_atoms

    atoms = [
        RenderAtom(kind="text", content="hello", node_count=1),
        RenderAtom(kind="text", content="world", node_count=1),
    ]
    for a in atoms:
        a.byte_size = estimate_atom_size(a)

    legacy = paginate_atoms(atoms, RenderBudget())
    new = paginate_layout(
        SectionLayout(sticky_head=(), status=(), body=tuple(atoms), appendix=()),
        RenderBudget(),
    )

    legacy_kinds = [[a.kind for a in p] for p in legacy]
    new_kinds = [[a.kind for a in p] for p in new]
    assert legacy_kinds == new_kinds
```

- [ ] **Step 2: 跑测试**

```bash
uv run pytest tests/test_card_pagination.py::test_paginate_atoms_shim_preserves_behavior -v
```

可能 PASS（旧逻辑碰巧等价）也可能 FAIL（小差异）。无论结果都执行 step 3 把实现统一为 shim。

- [ ] **Step 3: 改 paginate_atoms**

`src/card/render/pagination.py` 把 `paginate_atoms` 替换为：

```python
import warnings


def paginate_atoms(
    atoms: list[RenderAtom], budget: RenderBudget
) -> list[list[RenderAtom]]:
    """[Deprecated] Use paginate_layout(SectionLayout(...), budget) instead.

    Retained as a thin shim that wraps body atoms into a SectionLayout with
    no sticky/status/appendix. Behavior identical to the old implementation
    for callers that don't care about sticky_head.
    """
    warnings.warn(
        "paginate_atoms is deprecated; use paginate_layout instead",
        DeprecationWarning,
        stacklevel=2,
    )
    from src.card.render.layout import SectionLayout, paginate_layout

    layout = SectionLayout(
        sticky_head=(),
        status=(),
        body=tuple(atoms),
        appendix=(),
    )
    pages = paginate_layout(layout, budget)
    return [list(p) for p in pages]
```

保留 `BASE_OVERHEAD`、`FIXED_NODE_OVERHEAD`、`split_atom`、`_try_split_*`、`_make_split_atoms`、`_estimate_content_bytes` 等辅助函数（layout.py 仍依赖）。

- [ ] **Step 4: 跑全部 pagination 测试**

```bash
uv run pytest tests/test_card_pagination.py -v
```

预期：所有 PASS。

- [ ] **Step 5: grep 老调用确认未漏改**

```bash
grep -rn "paginate_atoms" /Users/jiataorui/workspaces/aiwork/ghostAp/src/ | grep -v __pycache__
```

确认每处要么是测试 / 文档引用，要么是已合法走 shim 的老调用。

- [ ] **Step 6: 提交**

```bash
git add src/card/render/pagination.py tests/test_card_pagination.py
git commit -m "refactor(card): paginate_atoms now a deprecated shim

旧入口包装为 paginate_layout(SectionLayout(body=atoms))，加 DeprecationWarning。
辅助函数（split_atom 等）仍由 layout.py 复用。"
```

---

## Task 15: BaseRenderer 加 _dispatch_card_split helper

**Files:**
- Modify: `src/feishu/renderers/base.py`
- Test: `tests/test_base_renderer_card_split.py`

`BaseRenderer._dispatch_card_split(session, *, reason, hint)`：dispatch card_split 事件 + 注册回调。

- [ ] **Step 1: 写测试**

新建 `tests/test_base_renderer_card_split.py`：

```python
"""BaseRenderer card_split helper tests."""
from __future__ import annotations

from unittest.mock import MagicMock

from src.feishu.renderers.base import BaseRenderer


class _FakeRenderer(BaseRenderer):
    def __init__(self):
        self.handler = MagicMock()
        self.ctx = MagicMock()
        self.settings = MagicMock()
        self.ui_states = {}
        self._session_factory = None
        self.split_calls: list[tuple[str, str | None]] = []

    def _on_card_split_completed(self, reason: str, hint: str | None) -> None:
        self.split_calls.append((reason, hint))


def test_dispatch_card_split_emits_event_and_registers_callback():
    renderer = _FakeRenderer()
    session = MagicMock()
    renderer._dispatch_card_split(session, reason="task_done", hint="task 3")

    args = session.dispatch.call_args
    assert args is not None
    event = args.args[0]
    assert event.event_type == "card_split"
    assert event.payload.reason == "task_done"
    assert event.payload.hint == "task 3"

    # Callback wired
    cb = getattr(session, "on_card_split_completed", None)
    assert callable(cb)


def test_default_on_card_split_completed_is_noop():
    """BaseRenderer default _on_card_split_completed must be safe no-op."""
    class _Plain(BaseRenderer):
        def __init__(self):
            self.handler = MagicMock()
            self.ctx = MagicMock()
            self.settings = MagicMock()
            self.ui_states = {}
            self._session_factory = None

    r = _Plain()
    # Should not raise
    r._on_card_split_completed("any_reason", None)
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/test_base_renderer_card_split.py -v
```

预期：`AttributeError: 'BaseRenderer' object has no attribute '_dispatch_card_split'`。

- [ ] **Step 3: 在 BaseRenderer 加 helper**

`src/feishu/renderers/base.py` 在 `BaseRenderer` 类里加：

```python
def _dispatch_card_split(
    self,
    session,
    *,
    reason: str,
    hint: str | None = None,
) -> None:
    """Dispatch a card_split event and wire on_card_split_completed callback.

    Subclasses override _on_card_split_completed(reason, hint) to start a
    fresh session and write the hint as the first text block of the new card.
    """
    from ...card.events import CardEvent

    session.on_card_split_completed = self._on_card_split_completed
    session.dispatch(CardEvent.card_split(reason=reason, hint=hint))


def _on_card_split_completed(self, reason: str, hint: str | None) -> None:
    """Override in subclass to start a fresh session for continuation."""
    return None
```

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest tests/test_base_renderer_card_split.py -v
```

预期：2 PASS。

- [ ] **Step 5: 提交**

```bash
git add src/feishu/renderers/base.py tests/test_base_renderer_card_split.py
git commit -m "feat(feishu-renderers): BaseRenderer card_split helper

_dispatch_card_split(session, reason, hint) 发事件+注册回调；子类
override _on_card_split_completed 起新 session 并写入 hint 作为首条 text。"
```

---

## Task 16: DeepRenderer 接入语义切卡（task 完成）

**Files:**
- Modify: `src/feishu/renderers/deep_renderer.py:136-181`
- Test: `tests/test_deep_renderer_split.py`

PLAN_UPDATE 时检测某 task 由 in_progress 变 completed → dispatch card_split。

- [ ] **Step 1: grep 实际 PlanInfo / PlanEntry 字段名**

```bash
grep -n "class PlanInfo\|class PlanEntry\|@dataclass" /Users/jiataorui/workspaces/aiwork/ghostAp/src/acp/models.py | head -20
```

按实际字段名（可能是 `entries` / `items`，`id` / `task_id`，`title` / `name`）调整下面测试与实现。

- [ ] **Step 2: 写测试**

新建 `tests/test_deep_renderer_split.py`：

```python
"""DeepRenderer task-done card split tests."""
from __future__ import annotations

from unittest.mock import MagicMock

from src.acp import ACPEvent, ACPEventType
# Adjust to actual class names if different:
from src.acp.models import PlanInfo, PlanEntry
from src.feishu.renderers.deep_renderer import DeepRenderer


def _build_renderer():
    handler = MagicMock()
    handler.ctx = MagicMock()
    handler.settings = MagicMock()
    handler.settings.engine_timeout_warning_seconds = 0
    handler.add_reaction = MagicMock()
    handler.send_text_to_chat = MagicMock()
    handler.reply_text = MagicMock()
    handler.context_manager = MagicMock()
    handler.ensure_request_id = MagicMock(return_value="req1")
    handler.get_card_delivery = MagicMock()
    handler.project_manager = MagicMock()
    handler.get_engine_name = MagicMock(return_value="Coco")
    return DeepRenderer(handler)


def test_deep_renderer_splits_on_task_done():
    renderer = _build_renderer()
    captured: list[tuple[str, str | None]] = []
    renderer._dispatch_card_split = lambda sess, *, reason, hint=None: captured.append((reason, hint))

    cb = renderer.create_deep_callbacks(
        message_id="m1", chat_id="c1", project=None, engine_name="Coco",
    )

    initial_plan = PlanInfo(entries=(
        PlanEntry(id="t1", title="task 1", status="in_progress"),
        PlanEntry(id="t2", title="task 2", status="pending"),
    ))
    cb.on_event(ACPEvent(event_type=ACPEventType.PLAN_UPDATE, plan=initial_plan))

    updated_plan = PlanInfo(entries=(
        PlanEntry(id="t1", title="task 1", status="completed"),
        PlanEntry(id="t2", title="task 2", status="in_progress"),
    ))
    cb.on_event(ACPEvent(event_type=ACPEventType.PLAN_UPDATE, plan=updated_plan))

    assert any(r == "task_done" for r, _ in captured)
    matching_hints = [h for r, h in captured if r == "task_done"]
    assert any(h is not None and ("task 2" in h or "接续" in h) for h in matching_hints)
```

- [ ] **Step 3: 跑测试确认失败**

```bash
uv run pytest tests/test_deep_renderer_split.py -v
```

预期：FAIL。

- [ ] **Step 4: 在 DeepRenderer.on_event 里加检测**

`src/feishu/renderers/deep_renderer.py:136-181` 的 `on_event`，在 PLAN_UPDATE 处理段附近改造。先在外部闭包初始化跟踪状态：

```python
# In create_deep_callbacks(), near _start_time / _tool_count:
_last_plan_statuses: list[dict[str, str]] = [{}]
```

然后在 `on_event` 里（PLAN_UPDATE 分支内、`orchestrator.handle_plan_update(...)` 之前）插入：

```python
        if event.event_type == ACPEventType.PLAN_UPDATE:
            if event.plan and event.plan.entries:
                steps = len(event.plan.entries)
                if steps > 0:
                    _plan_steps[0] = steps
                    _phase[0] = "executing"

                # Detect task transitions and dispatch card_split
                prev_statuses = _last_plan_statuses[0]
                new_statuses = {e.id: e.status for e in event.plan.entries}
                for tid, new_st in new_statuses.items():
                    old_st = prev_statuses.get(tid)
                    if old_st == "in_progress" and new_st == "completed":
                        next_in_prog = next(
                            (e for e in event.plan.entries if e.status == "in_progress"),
                            None,
                        )
                        if next_in_prog is not None:
                            step_idx = next(
                                (i + 1 for i, e in enumerate(event.plan.entries) if e.id == next_in_prog.id),
                                1,
                            )
                            hint = f"接续 task {step_idx}「{next_in_prog.title}」"
                        else:
                            hint = None
                        if not getattr(session, "_closed", False):
                            self._dispatch_card_split(session, reason="task_done", hint=hint)
                _last_plan_statuses[0] = new_statuses

                if _multi_card_enabled:
                    orchestrator.handle_plan_update(event, stream_bridge)
```

并在 `DeepRenderer` 类里 override：

```python
def _on_card_split_completed(self, reason: str, hint: str | None) -> None:
    self._pending_split_hint = hint
```

后续如果有 task-level orchestrator 多卡机制（`TaskOrchestrator.split_to_next_task` 或类似），把上面 `self._dispatch_card_split` 替换为现有 orchestrator API；此处的核心契约是「task 完成时主动发出 card_split 事件一次」。

- [ ] **Step 5: 跑测试确认通过**

```bash
uv run pytest tests/test_deep_renderer_split.py -v
```

预期：PASS。也跑 deep 既有测：

```bash
uv run pytest tests/ -v -k "deep" --timeout=30
```

- [ ] **Step 6: 提交**

```bash
git add src/feishu/renderers/deep_renderer.py tests/test_deep_renderer_split.py
git commit -m "feat(deep): semantic card_split on task transition

PLAN_UPDATE 检测到 task 由 in_progress 变 completed 时主动 dispatch
card_split(reason='task_done', hint='接续 task N「name」')，触发新
session 起卡。续卡顶部三明治锚点保持上下文连续。"
```

---

## Task 17: LoopRenderer 接入语义切卡（round 跳变）

**Files:**
- Modify: `src/feishu/renderers/loop_renderer.py`
- Test: `tests/test_loop_renderer_split.py`

监听 round 变化，跳变时 dispatch card_split。

- [ ] **Step 1: grep 找 round 信号**

```bash
grep -n "current_round\|round_started\|iteration" /Users/jiataorui/workspaces/aiwork/ghostAp/src/feishu/renderers/loop_renderer.py
grep -rn "current_round\|round_idx\|on_round" /Users/jiataorui/workspaces/aiwork/ghostAp/src/loop_engine/ | head -10
```

记录 round 字段名与回调钩入点。

- [ ] **Step 2: 写测试**

新建 `tests/test_loop_renderer_split.py`：

```python
"""LoopRenderer round-change split tests."""
from __future__ import annotations

from unittest.mock import MagicMock

from src.feishu.renderers.loop_renderer import LoopRenderer


def _build_renderer():
    handler = MagicMock()
    handler.ctx = MagicMock()
    handler.settings = MagicMock()
    handler.settings.engine_timeout_warning_seconds = 0
    handler.add_reaction = MagicMock()
    handler.reply_text = MagicMock()
    handler.send_text_to_chat = MagicMock()
    handler.ensure_request_id = MagicMock(return_value="req1")
    handler.get_card_delivery = MagicMock()
    handler.project_manager = MagicMock()
    handler.context_manager = MagicMock()
    return LoopRenderer(handler)


def test_loop_renderer_splits_on_round_change():
    renderer = _build_renderer()
    renderer._current_session = MagicMock()
    renderer._current_session._closed = False

    captured: list[tuple[str, str | None]] = []
    renderer._dispatch_card_split = lambda sess, *, reason, hint=None: captured.append((reason, hint))

    renderer.notify_round_change(current_round=1)
    renderer.notify_round_change(current_round=2)

    assert any(r == "round_changed" for r, _ in captured), \
        f"expected round_changed split, got {captured}"
    matching = [h for r, h in captured if r == "round_changed"]
    assert any("第 2 轮" in (h or "") for h in matching)


def test_loop_renderer_no_split_on_first_round():
    """Initial round set should NOT trigger split."""
    renderer = _build_renderer()
    renderer._current_session = MagicMock()
    renderer._current_session._closed = False

    captured: list[tuple[str, str | None]] = []
    renderer._dispatch_card_split = lambda sess, *, reason, hint=None: captured.append((reason, hint))

    renderer.notify_round_change(current_round=1)
    assert captured == [], "first round must not trigger split"
```

- [ ] **Step 3: 跑测试确认失败**

```bash
uv run pytest tests/test_loop_renderer_split.py -v
```

预期：FAIL（`notify_round_change` 不存在）。

- [ ] **Step 4: 在 LoopRenderer 加 round 跟踪**

`src/feishu/renderers/loop_renderer.py` `LoopRenderer` 类里：

```python
def __init__(self, handler):
    super().__init__(handler)
    self._last_round: int | None = None
    self._current_session = None
    self._pending_split_hint: str | None = None

def notify_round_change(self, current_round: int) -> None:
    """Hook into the loop engine round lifecycle."""
    if self._last_round is not None and current_round != self._last_round:
        if self._current_session is not None and not getattr(self._current_session, "_closed", False):
            self._dispatch_card_split(
                self._current_session,
                reason="round_changed",
                hint=f"进入第 {current_round} 轮",
            )
    self._last_round = current_round


def _on_card_split_completed(self, reason: str, hint: str | None) -> None:
    self._pending_split_hint = hint
```

把 `self.notify_round_change(round_idx)` 嵌入既有 round 启动回调路径（grep step 1 结果定位，常见在 `on_round_started` / `_on_iteration_start` 等）。

- [ ] **Step 5: 跑测试确认通过**

```bash
uv run pytest tests/test_loop_renderer_split.py -v
```

预期：2 PASS。

- [ ] **Step 6: 提交**

```bash
git add src/feishu/renderers/loop_renderer.py tests/test_loop_renderer_split.py
git commit -m "feat(loop): semantic card_split on round change

LoopRenderer 监听 round 跳变，dispatch card_split(reason='round_changed',
hint='进入第 N 轮')。每轮独立卡片，三明治锚点保持收敛进度可见。"
```

---

## Task 18: SpecRenderer 接入语义切卡（cycle/perspective 跳变）

**Files:**
- Modify: `src/feishu/renderers/spec_renderer.py`
- Test: `tests/test_spec_renderer_split.py`

监听 cycle 编号变化或 perspective 切换，dispatch card_split。

- [ ] **Step 1: grep 找 cycle/perspective 信号**

```bash
grep -n "current_cycle\|cycle_started\|perspective\|ReviewPerspective" /Users/jiataorui/workspaces/aiwork/ghostAp/src/feishu/renderers/spec_renderer.py | head -20
grep -rn "perspective_changed\|cycle_started\|on_cycle" /Users/jiataorui/workspaces/aiwork/ghostAp/src/spec_engine/ | head -10
```

按实际字段调整。

- [ ] **Step 2: 写测试**

新建 `tests/test_spec_renderer_split.py`：

```python
"""SpecRenderer cycle/perspective change split tests."""
from __future__ import annotations

from unittest.mock import MagicMock

from src.feishu.renderers.spec_renderer import SpecRenderer


def _build_renderer():
    handler = MagicMock()
    handler.ctx = MagicMock()
    handler.settings = MagicMock()
    handler.settings.engine_timeout_warning_seconds = 0
    handler.add_reaction = MagicMock()
    handler.reply_text = MagicMock()
    handler.send_text_to_chat = MagicMock()
    handler.ensure_request_id = MagicMock(return_value="req1")
    handler.get_card_delivery = MagicMock()
    handler.project_manager = MagicMock()
    handler.context_manager = MagicMock()
    return SpecRenderer(handler)


def test_spec_renderer_splits_on_cycle_change():
    renderer = _build_renderer()
    renderer._current_session = MagicMock()
    renderer._current_session._closed = False

    captured: list[tuple[str, str | None]] = []
    renderer._dispatch_card_split = lambda sess, *, reason, hint=None: captured.append((reason, hint))

    renderer.notify_cycle_change(current_cycle=1, perspective="spec")
    renderer.notify_cycle_change(current_cycle=2, perspective="code")

    assert any(r == "cycle_changed" for r, _ in captured)
    matching = [h for r, h in captured if r == "cycle_changed"]
    assert any("cycle 2" in (h or "") and "code" in (h or "") for h in matching)


def test_spec_renderer_splits_on_perspective_change_within_cycle():
    renderer = _build_renderer()
    renderer._current_session = MagicMock()
    renderer._current_session._closed = False

    captured: list[tuple[str, str | None]] = []
    renderer._dispatch_card_split = lambda sess, *, reason, hint=None: captured.append((reason, hint))

    renderer.notify_cycle_change(current_cycle=1, perspective="spec")
    renderer.notify_cycle_change(current_cycle=1, perspective="code")

    assert any(r == "cycle_changed" for r, _ in captured)


def test_spec_renderer_no_split_on_first_cycle():
    renderer = _build_renderer()
    renderer._current_session = MagicMock()
    renderer._current_session._closed = False

    captured: list[tuple[str, str | None]] = []
    renderer._dispatch_card_split = lambda sess, *, reason, hint=None: captured.append((reason, hint))

    renderer.notify_cycle_change(current_cycle=1, perspective="spec")
    assert captured == []
```

- [ ] **Step 3: 跑测试确认失败**

```bash
uv run pytest tests/test_spec_renderer_split.py -v
```

预期：FAIL。

- [ ] **Step 4: 加 notify_cycle_change**

`src/feishu/renderers/spec_renderer.py` `SpecRenderer` 类里：

```python
def __init__(self, handler):
    super().__init__(handler)
    self._last_cycle: int | None = None
    self._last_perspective: str | None = None
    self._current_session = None
    self._pending_split_hint: str | None = None

def notify_cycle_change(self, *, current_cycle: int, perspective: str | None) -> None:
    """Hook into the spec engine cycle/perspective lifecycle."""
    changed_cycle = self._last_cycle is not None and current_cycle != self._last_cycle
    changed_persp = self._last_perspective is not None and perspective != self._last_perspective
    if changed_cycle or changed_persp:
        if self._current_session is not None and not getattr(self._current_session, "_closed", False):
            persp = perspective or "—"
            self._dispatch_card_split(
                self._current_session,
                reason="cycle_changed",
                hint=f"进入 cycle {current_cycle} · {persp}",
            )
    self._last_cycle = current_cycle
    self._last_perspective = perspective


def _on_card_split_completed(self, reason: str, hint: str | None) -> None:
    self._pending_split_hint = hint
```

把 `notify_cycle_change(...)` 调用嵌入既有 cycle/perspective 切换路径（grep step 1 定位）。

- [ ] **Step 5: 跑测试确认通过**

```bash
uv run pytest tests/test_spec_renderer_split.py -v
```

预期：3 PASS。

- [ ] **Step 6: 提交**

```bash
git add src/feishu/renderers/spec_renderer.py tests/test_spec_renderer_split.py
git commit -m "feat(spec): semantic card_split on cycle/perspective change

SpecRenderer 监听 cycle 编号变化和 perspective 切换，dispatch card_split
(reason='cycle_changed', hint='进入 cycle N · {perspective}')。"
```

---

## Task 19: Worktree + Programming 直接模式接入 SectionLayout

**Files:**
- Modify: `src/card/programming_adapter.py`（如需要）
- Test: `tests/test_programming_adapter.py`

直接 programming 模式无 task_list、不切卡，但仍走 SectionLayout（banner + activity_summary）。Worktree 子代理 banner 前缀 `wt·{name}` 已在 banner_computer 实现，本任务验证 metadata 透传。

- [ ] **Step 1: 写测试**

追加到 `tests/test_programming_adapter.py`（不存在则新建）：

```python
"""programming_adapter SectionLayout integration tests."""
from __future__ import annotations

from src.card.render.budget import RenderBudget
from src.card.render.renderer import render_card
from src.card.state.models import CardFooter, CardHeader, CardMetadata, CardState
from src.card.state.runtime_stats import RuntimeStats


def test_programming_direct_mode_has_banner_no_task_list():
    state = CardState(
        metadata=CardMetadata(mode_name="Programming", mode_emoji="💬", engine_type=None, tool_name="Coco"),
        header=CardHeader(title="Programming"),
        footer=CardFooter(),
        blocks=(),
        block_index={},
        terminal="running",
        buttons=(),
        runtime_stats=RuntimeStats(elapsed_seconds=32.0),
    )
    pages = render_card(state, RenderBudget())
    assert len(pages) == 1
    body = pages[0]._card_json["body"]["elements"]
    first = body[0]
    assert first.get("tag") == "markdown"
    assert "Programming" in first.get("content", "")
    panel_titles = [
        e.get("header", {}).get("title", {}).get("content", "")
        for e in body if e.get("tag") == "collapsible_panel"
    ]
    assert not any("任务列表" in t for t in panel_titles)


def test_worktree_subagent_banner_prefix():
    state = CardState(
        metadata=CardMetadata(mode_name="Worktree", mode_emoji="🌲", engine_type="worktree"),
        header=CardHeader(title="Worktree"),
        footer=CardFooter(),
        blocks=(),
        block_index={},
        terminal="running",
        buttons=(),
        runtime_stats=RuntimeStats(elapsed_seconds=72.0, worktree_subagent="aiden"),
    )
    pages = render_card(state, RenderBudget())
    body = pages[0]._card_json["body"]["elements"]
    first = body[0]
    assert "wt·aiden" in first.get("content", "")
```

- [ ] **Step 2: 跑测试**

```bash
uv run pytest tests/test_programming_adapter.py -v
```

如 PASS：sticky_head + banner_computer 已透传 metadata，无需额外改 adapter。

如 FAIL：通常是 `programming_adapter.py` 走自定义 render 路径绕过了 `render_card`。grep 定位：

```bash
grep -n "def render\|render_card\|build_sticky_head" /Users/jiataorui/workspaces/aiwork/ghostAp/src/card/programming_adapter.py
```

把 adapter 自定义渲染路径替换为 `render_card(state, budget)`，或在 adapter 内显式调用 `build_sticky_head` 并注入到自定义路径。

- [ ] **Step 3: 修 adapter（如需要）**

如 step 2 测试 FAIL，按 grep 结果做最小改动：替换 adapter 自定义渲染入口为 `render_card`。

```python
from src.card.render.renderer import render_card  # ensure central pipeline used
```

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest tests/test_programming_adapter.py -v
```

预期：所有 PASS。

- [ ] **Step 5: 提交**

```bash
git add src/card/programming_adapter.py tests/test_programming_adapter.py
git commit -m "feat(programming): direct mode + worktree go through SectionLayout

programming 直接模式无 task_list 但保留 banner + activity_summary；
worktree 子代理 banner 前缀 wt·{name} 区分父子卡。"
```

---

## Task 20: 节点预算压力回归 + 全测验收

**Files:**
- Create: `tests/test_card_budget_regression.py`
- Modify: 任何上面 19 步未覆盖的兼容性回退点

构造极端 state 验证节点 ≤200、字节 ≤30K、sticky_head 节点 ≤25。跑全部测试确认无回归。

- [ ] **Step 1: 写压力测试**

新建 `tests/test_card_budget_regression.py`：

```python
"""Budget regression: extreme states must not exceed Feishu limits."""
from __future__ import annotations

import json

from src.card.render.budget import RenderBudget
from src.card.render.renderer import render_card
from src.card.render.payload_truncator import count_tagged_nodes
from src.card.state.models import CardFooter, CardHeader, CardMetadata, CardState, ContentBlock
from src.card.state.runtime_stats import RuntimeStats


def _build_extreme_state() -> CardState:
    blocks_list: list[ContentBlock] = []
    tasks = tuple(
        {
            "task_id": f"t{i}",
            "name": f"task {i}",
            "status": ("completed" if i < 5 else ("in_progress" if i == 5 else "pending")),
        }
        for i in range(30)
    )
    blocks_list.append(ContentBlock(
        kind="task_list",
        block_id="tl",
        content="",
        tasks=tasks,
        current_task_id="t5",
    ))
    for i in range(100):
        is_active = i >= 50
        blocks_list.append(ContentBlock(
            kind="tool_call",
            block_id=f"tool_{i}",
            tool_name=("Edit" if i % 3 == 0 else "Bash" if i % 3 == 1 else "Grep"),
            tool_summary=f"summary {i}",
            content="x" * 100,
            status=("active" if is_active else "completed"),
            is_latest_active=(i == 99),
        ))
    blocks = tuple(blocks_list)
    return CardState(
        metadata=CardMetadata(mode_name="Deep", mode_emoji="🧠", engine_type="deep"),
        header=CardHeader(title="Deep"),
        footer=CardFooter(),
        blocks=blocks,
        block_index={b.block_id: i for i, b in enumerate(blocks)},
        terminal="running",
        buttons=(),
        runtime_stats=RuntimeStats(elapsed_seconds=600.0, deep_phase="executing"),
    )


def test_extreme_state_no_page_exceeds_node_limit():
    state = _build_extreme_state()
    pages = render_card(state, RenderBudget())
    for i, page in enumerate(pages):
        nodes = count_tagged_nodes(page._card_json)
        assert nodes <= 200, f"page {i} has {nodes} nodes > 200"


def test_extreme_state_no_page_exceeds_byte_limit():
    state = _build_extreme_state()
    pages = render_card(state, RenderBudget())
    for i, page in enumerate(pages):
        size = len(json.dumps(page._card_json).encode("utf-8"))
        assert size <= 30 * 1024, f"page {i} has {size} bytes > 30KB"


def test_extreme_state_sticky_head_node_cap():
    from src.card.render.sticky_head import build_sticky_head, STICKY_HEAD_MAX_NODES
    state = _build_extreme_state()
    sticky = build_sticky_head(state, state.metadata)
    total_nodes = sum(a.node_count for a in sticky)
    assert total_nodes <= STICKY_HEAD_MAX_NODES, f"sticky_head has {total_nodes} nodes > cap"


def test_extreme_state_continuation_pages_have_sticky():
    state = _build_extreme_state()
    pages = render_card(state, RenderBudget())
    for i, page in enumerate(pages):
        body = page._card_json["body"]["elements"]
        first = body[0]
        if first.get("tag") == "markdown":
            content = first.get("content", "")
            assert "Deep" in content, f"page {i} missing sticky banner"
```

- [ ] **Step 2: 跑压力测试**

```bash
uv run pytest tests/test_card_budget_regression.py -v
```

预期：4 PASS。

如某项 FAIL，按 spec §5.4 节点预算降级策略调整 `build_sticky_head` 或 `paginate_layout`。

- [ ] **Step 3: 跑全部主题/builder 回归**

```bash
uv run pytest tests/ -v -k "theme or builder or layout or render or pagination" --timeout=60
```

确认全绿。如有 FAIL：用 `git stash` 切回原状跑同一 spec 比较是否新代码引入的回归。

- [ ] **Step 4: 跑全测**

```bash
uv run pytest tests/ -v --timeout=120
```

确认全绿。

- [ ] **Step 5: 提交**

```bash
git add tests/test_card_budget_regression.py
git commit -m "test(card): budget regression for extreme states

30 tasks + 100 tool calls 极端 state 下：节点 ≤200、字节 ≤30K、
sticky_head 节点 ≤25、续卡每页带 sticky banner。"
```

---

## Task 21: 更新 .Memory + 4 引擎实机手测

**Files:**
- Create: `.Memory/2026-05-09.md`（如不存在）
- Modify: `.Memory/Abstract.md`
- Manual: 4 引擎实机手测

按 CLAUDE.md「任务完成必须更新 Memory」规则闭环 + 实机对比参考图 2。

- [ ] **Step 1: 写 .Memory/2026-05-09.md**

新建或追加：

```markdown
# 2026-05-09 项目记录

## 统一编程模式卡片重构

### 任务描述
解决三个核心 gap：
1. 续卡丢失上下文锚点（task_list / activity_summary 不重注）。
2. 多 active tool panel 同时展开导致首卡视觉混乱。
3. 多卡切分被动溢出，无语义边界。

### 执行内容
按 spec `docs/superpowers/specs/2026-05-09-unified-programming-card-design.md`
和 plan `docs/superpowers/plans/2026-05-09-unified-programming-card-plan.md`
落地 SectionLayout SSOT 重构（路径 B）：

1. 新增 `SectionLayout` / `paginate_layout` / `sticky_head` / `banner_computer` /
   `RuntimeStats` 模块，提供四区骨架 + 三明治锚点。
2. `ContentBlock` 新增 `is_latest_active` 字段，reducer 维护单例不变量；
   `render_tool_panel` 改用该字段决定 expanded（解决参考图 1 的乱）。
3. 新增 `card_split` 事件 + payload + factory + session handler，引擎主动
   触发语义切卡（task done / round changed / cycle changed）。
4. DeepRenderer / LoopRenderer / SpecRenderer 接入 `_dispatch_card_split`，
   续卡 hint 自动写入新卡 body 起头。
5. `paginate_atoms` 退化为 deprecation shim 包裹 `paginate_layout`。
6. 极端 state 压力回归：30 tasks + 100 tool calls 下节点 ≤200、字节 ≤30K、
   sticky_head ≤25 节点、续卡保 banner。

### 技术要点
- sticky_head 降级顺序：activity_summary → task_list（改单行 fallback）。
- 切卡看护门槛：byte_used / byte_budget < 0.4 时不切，避免过频。
- streaming 文本竞态：dispatch split 前强制 `close_open_blocks()`，hint
  总是新卡首条 text。
- Worktree 子代理保持每子任务独立 session（CLAUDE.md 既有约束未变），
  banner 前缀 `wt·{name}` 区分父子。

### 提交记录
（按 task 顺序逐次提交，commit 信息见 plan 文件 Task N step 5/6。）

### 关联文件
- spec: docs/superpowers/specs/2026-05-09-unified-programming-card-design.md
- plan: docs/superpowers/plans/2026-05-09-unified-programming-card-plan.md
- mockup: ux/unified_card_v1.html
```

- [ ] **Step 2: 更新 Abstract.md**

`.Memory/Abstract.md` 顶部追加：

```markdown
## 2026-05-09
- **统一编程模式卡片重构 (SectionLayout SSOT)** - 续卡 sticky 锚点、单 active 展开、语义切卡 → [.Memory/2026-05-09.md](2026-05-09.md)
```

- [ ] **Step 3: 4 引擎实机手测清单**

逐项手测对比参考图 2 清爽程度，记录 ✓/✗：

```
[ ] Deep 模式：5 任务 prompt 触发 PLAN_UPDATE
    - 首卡显 sticky_head 三件套
    - task 1 完成时自动起新卡，新卡顶部「接续 task 2…」hint
    - 多 active tool 时只展开最新一个
    - 续卡顶部 sticky 与首卡同步

[ ] Loop 模式：≥2 轮 prompt
    - 第 2 轮自动起新卡，hint「进入第 2 轮」
    - criteria_panel + activity_summary 在新卡顶部

[ ] Spec 模式：spec→code 切换 prompt
    - perspective 切换时起新卡，hint「进入 cycle N · code」

[ ] Worktree 模式：起 2-3 子代理
    - 父卡显调度面板（不切卡）
    - 子代理各自独立卡，banner 前缀 wt·{name}

[ ] Programming 直接模式：单 turn 对话
    - 不切卡
    - 顶部仅 banner + activity_summary（无 task_list）

[ ] 视觉对比参考图 2：
    - 整体清爽程度对得上
    - 每个时刻"系统在做什么"在 sticky 内可见
```

记录每项结果 + 差异截图（如有），更新 `.Memory/2026-05-09.md` 备注。

- [ ] **Step 4: 提交 Memory 更新**

```bash
git add .Memory/2026-05-09.md .Memory/Abstract.md
git commit -m "docs(memory): record unified card refactor task

按 spec/plan 落地 SectionLayout SSOT 重构，关闭三个核心 gap：
续卡 sticky 锚点、tool panel 单 active 展开、语义切卡。Memory + Abstract
索引更新。"
```

- [ ] **Step 5: 全测最终确认**

```bash
uv run pytest tests/ -v --timeout=120
```

预期：全绿。

如有少量回归测试失败，按 CLAUDE.md「审计缺口分级处理」规则：
- High（功能性 bug）立即修。
- Medium / Low 进 `.Memory/Backlog.md`。

---

## Self-Review Checklist (执行完成后由 reviewer 跑)

**Spec coverage:**
- [ ] §4.1 SectionLayout — Task 4
- [ ] §4.1 paginate_layout — Task 5
- [ ] §4.2 sticky_head 三件套 + 降级 — Task 6
- [ ] §4.3 phase_banner atom + banner_computer — Task 1, 3
- [ ] §4.4 ToolBlock latest_active + render_tool_panel — Task 9, 10
- [ ] §4.5 card_split event + session handler — Task 11, 12
- [ ] §4.6 续卡 hint — Task 16, 17, 18
- [ ] §4.7 Deep/Loop/Spec/Worktree/Programming 适配矩阵 — Task 16-19
- [ ] §5.4 节点预算降级 — Task 6, 20
- [ ] §6 改动文件清单 — File Map 全部覆盖
- [ ] §9 验收标准 — Task 20 + 21 手测清单

**Type consistency:**
- [ ] `is_latest_active` 字段名在 Task 9, 10 一致
- [ ] `CardSplitPayload(reason, hint)` 在 Task 11, 12, 15-18 一致
- [ ] `SectionLayout(sticky_head, status, body, appendix)` 字段在 Task 4, 5, 13 一致
- [ ] `RuntimeStats(elapsed_seconds, deep_phase, loop_round, spec_cycle, spec_perspective, worktree_subagent)` 在 Task 2, 3, 6 一致
- [ ] `_dispatch_card_split(session, *, reason, hint)` 签名在 Task 15-18 一致
- [ ] `_on_card_split_completed(reason, hint)` 子类 override 签名一致

**Placeholder scan:**
- [ ] 无 TBD / TODO / "implement later"
- [ ] 无 "fill in details" / "handle edge cases" 空泛指令
- [ ] 所有代码步骤含完整 code block，无 "类似 Task N" 的省略
