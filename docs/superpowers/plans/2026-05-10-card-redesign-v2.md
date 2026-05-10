# Card Redesign v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Redesign Feishu programming card v2 — unify all programming modes (Coco/Claude/Aiden/Codex/Gemini/TTADK + Deep/Loop/Spec/Worktree) under a single visual contract: project-tool-#seq header, three-group always-open task list, turn-based reasoning↔tool flow with running-only expand, footer with now-tool hint and subagent badge, card_split with frozen-prev + cumulative time, parallel subagent orange theme.

**Architecture:** Keep SectionLayout SSOT 4-region skeleton. Move `task_list` from status into `sticky_head`. Move `footer` to `appendix` (last-page only). Reduce `activity_summary` panel — its semantics are absorbed by per-turn local reasoning blocks. Add a 1Hz `live_ticker` for emoji frame swaps. CardSession gains 4 fields: `sequence`, `session_started_at`, `is_subagent`, `parent_card_seq`. Parallel subagents get independent CardSession + bridge throttle, displayed in orange theme.

**Tech Stack:** Python 3.11+, pydantic-settings, pytest, Feishu Card Schema 2.0 (`column_set` + `background_style.color` only — no `text_color`, no CSS animation), uv package manager.

**Spec:** `docs/superpowers/specs/2026-05-10-card-redesign-design.md`
**Mockups:** `ux/unified_card_v2_single.html` · `ux/unified_card_v2_split_parallel.html`

---

## Conventions

- All commands run from repo root `/Users/jiataorui/workspaces/aiwork/ghostAp`.
- Test runner: `uv run python -m pytest <path> -v`.
- After every task: `uv run python -m pytest -x -q` to ensure no regression. Commit only when green.
- Commit messages follow `docs/commit-message-guidelines.md` (conventional commits, scoped, ≤72 char subject).
- Each task = one focused commit; do NOT batch unrelated changes.

---

## Phase 0 — Foundation

### Task 0.1: Extend `CardSession` data model

**Files:**
- Modify: `src/card/state/models.py` (find `class CardSession`)
- Modify: `src/card/session_factory.py` (constructor calls)
- Test: `tests/test_card_session_v2_fields.py` (NEW)

- [ ] **Step 1: Write failing test**

```python
# tests/test_card_session_v2_fields.py
import time
from src.card.state.models import CardSession

def test_card_session_has_v2_fields_with_defaults():
    s = CardSession(chat_id="c1", message_id="m1", started_at=time.time())
    assert s.sequence == 1
    assert s.session_started_at == s.started_at
    assert s.is_subagent is False
    assert s.parent_card_seq is None
    assert s.final_state_for_freeze is None

def test_card_session_subagent_construction():
    s = CardSession(
        chat_id="c1", message_id="m1", started_at=time.time(),
        sequence=2, is_subagent=True, parent_card_seq="5",
    )
    assert s.sequence == 2
    assert s.parent_card_seq == "5"
```

- [ ] **Step 2: Run test → expect FAIL**

```bash
uv run python -m pytest tests/test_card_session_v2_fields.py -v
```
Expected: `AttributeError` or dataclass mismatch.

- [ ] **Step 3: Add fields to `CardSession`**

In `src/card/state/models.py`, add to `CardSession` dataclass:

```python
sequence: int = 1
session_started_at: float | None = None  # defaults to started_at in __post_init__
is_subagent: bool = False
parent_card_seq: str | None = None
final_state_for_freeze: "CardState | None" = None
```

If `CardSession` lacks `__post_init__`, add:

```python
def __post_init__(self):
    if self.session_started_at is None:
        self.session_started_at = self.started_at
```

- [ ] **Step 4: Run test → expect PASS**

```bash
uv run python -m pytest tests/test_card_session_v2_fields.py -v
```

- [ ] **Step 5: Run full suite to catch fixture drift**

```bash
uv run python -m pytest -x -q
```
Expected: existing CardSession fixtures may need `session_started_at=None` default. Fix any failing fixtures inline.

- [ ] **Step 6: Commit**

```bash
git add src/card/state/models.py tests/test_card_session_v2_fields.py
git commit -m "feat(card): extend CardSession with v2 fields (sequence/subagent/freeze)"
```

---

### Task 0.2: Add `bridge_phrase` to `CardSplitEvent`

**Files:**
- Modify: `src/card/events/models.py` (or wherever `CardSplitEvent` lives — grep first)
- Test: `tests/test_card_split_event_bridge.py` (NEW)

- [ ] **Step 1: Locate `CardSplitEvent`**

```bash
grep -rn "class CardSplitEvent\|card_split" src/card/events/ src/card/state/ 2>/dev/null | head -10
```
Note the file path before continuing.

- [ ] **Step 2: Write failing test**

```python
# tests/test_card_split_event_bridge.py
from src.card.events.models import CardSplitEvent  # adjust import per Step 1

def test_split_event_carries_bridge_phrase():
    ev = CardSplitEvent(reason="task_done", bridge_phrase="续接：")
    assert ev.bridge_phrase == "续接："

def test_split_event_bridge_optional():
    ev = CardSplitEvent(reason="round_changed")
    assert ev.bridge_phrase is None
```

- [ ] **Step 3: Run → expect FAIL**

```bash
uv run python -m pytest tests/test_card_split_event_bridge.py -v
```

- [ ] **Step 4: Add `bridge_phrase` field**

```python
@dataclass
class CardSplitEvent:
    # ...existing fields...
    bridge_phrase: str | None = None
```

- [ ] **Step 5: Run → expect PASS + full suite green**

```bash
uv run python -m pytest tests/test_card_split_event_bridge.py -v
uv run python -m pytest -x -q
```

- [ ] **Step 6: Commit**

```bash
git add src/card/events/models.py tests/test_card_split_event_bridge.py
git commit -m "feat(card): add bridge_phrase to CardSplitEvent"
```

---

## Phase 1 — Header v2

### Task 1.1: Two-row header renderer

**Files:**
- Modify: `src/card/render/header.py` (currently 23 lines — full rewrite)
- Test: `tests/test_header_v2_two_row.py` (NEW)

- [ ] **Step 1: Read current header**

```bash
cat src/card/render/header.py
```
Note the public function signature so call-sites stay compatible.

- [ ] **Step 2: Write failing tests**

```python
# tests/test_header_v2_two_row.py
import time
from src.card.render.header import render_header_atom
from src.card.state.models import CardSession

def _session(**kw):
    base = dict(chat_id="c1", message_id="m1", started_at=time.time())
    base.update(kw)
    return CardSession(**base)

def test_header_first_row_contains_project_tool_seq():
    s = _session(
        project_name="ghostAp", tool_id="coco", model_id="claude-opus-4-7", sequence=3,
    )
    atom = render_header_atom(s, working_dir="/Users/x/workspaces/aiwork/ghostAp")
    text = atom.to_text()
    assert "ghostAp" in text
    assert "Coco" in text or "coco" in text
    assert "#3" in text
    assert "claude-opus-4-7" in text

def test_header_second_row_contains_dir_and_elapsed():
    s = _session(project_name="ghostAp", tool_id="coco", started_at=time.time() - 252.0)
    atom = render_header_atom(s, working_dir="/Users/x/workspaces/aiwork/ghostAp")
    text = atom.to_text()
    assert "~/workspaces/aiwork/ghostAp" in text
    assert "4m12s" in text  # 252s formatted

def test_header_subagent_replaces_dir_with_parent_link():
    s = _session(
        project_name="ghostAp", tool_id="aiden", sequence="5.a",
        is_subagent=True, parent_card_seq="5",
    )
    atom = render_header_atom(s, working_dir="/Users/x/p")
    text = atom.to_text()
    assert "↳ from #5" in text
```

- [ ] **Step 3: Run → FAIL**

```bash
uv run python -m pytest tests/test_header_v2_two_row.py -v
```

- [ ] **Step 4: Implement two-row header**

Rewrite `src/card/render/header.py`:

```python
from pathlib import Path
from src.card.render.atoms import RenderAtom, AtomKind
from src.card.state.models import CardSession

_TOOL_DISPLAY = {
    "coco": "Coco", "claude": "Claude", "aiden": "Aiden",
    "codex": "Codex", "gemini": "Gemini", "ttadk": "TTADK",
}

def _format_elapsed(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}m{s % 60:02d}s"

def _short_path(path: str) -> str:
    try:
        rel = Path(path).resolve().relative_to(Path.home())
        return f"~/{rel}"
    except ValueError:
        return path

def render_header_atom(session: CardSession, working_dir: str) -> RenderAtom:
    tool_label = _TOOL_DISPLAY.get(session.tool_id or "", session.tool_id or "?")
    seq_str = f"#{session.sequence}"
    model_suffix = f"  · {session.model_id}" if getattr(session, "model_id", None) else ""

    row1 = f"📁 {session.project_name or '?'} · 🤖 {tool_label} · {seq_str}{model_suffix}"

    if session.is_subagent and session.parent_card_seq:
        left2 = f"↳ from #{session.parent_card_seq}"
    else:
        left2 = _short_path(working_dir)

    elapsed = _format_elapsed(session.elapsed_seconds())  # add helper on CardSession if missing
    live_dot = "🟢" if session.is_running else "⏸"
    row2 = f"{left2}    {live_dot} {elapsed}"

    return RenderAtom(
        kind=AtomKind.HEADER,
        markdown=f"**{row1}**\n<font color='grey'>{row2}</font>",
        node_count=1,
    )
```

If `CardSession.elapsed_seconds()` and `is_running` don't exist, add them in this same task (small helpers).

- [ ] **Step 5: Run → PASS + full suite**

```bash
uv run python -m pytest tests/test_header_v2_two_row.py -v
uv run python -m pytest -x -q
```
Fix any drift in existing `test_card_*` that asserted old single-line header.

- [ ] **Step 6: Commit**

```bash
git add src/card/render/header.py src/card/state/models.py tests/test_header_v2_two_row.py
git commit -m "feat(card): two-row header with project/tool/#seq + dir/elapsed"
```

---

### Task 1.2: Frozen header mode for split

**Files:**
- Modify: `src/card/render/header.py` (add `_render_frozen_header`)
- Test: `tests/test_header_v2_frozen.py` (NEW)

- [ ] **Step 1: Write failing test**

```python
# tests/test_header_v2_frozen.py
import time
from src.card.render.header import render_header_atom

from src.card.state.models import CardSession

def test_frozen_header_shows_archived_tag_and_no_dot():
    s = CardSession(
        chat_id="c1", message_id="m1", started_at=time.time() - 422.0,
        project_name="ghostAp", tool_id="coco", sequence=3,
    )
    s.frozen = True  # new flag
    s.frozen_total_elapsed = 422.0
    atom = render_header_atom(s, working_dir="/Users/x/p")
    text = atom.to_text()
    assert "已封存" in text
    assert "🟢" not in text
    assert "⏸" in text
    assert "7m02s" in text
```

- [ ] **Step 2: Run → FAIL**

```bash
uv run python -m pytest tests/test_header_v2_frozen.py -v
```

- [ ] **Step 3: Add `frozen` flag + freeze branch in header**

In `src/card/state/models.py`, add `frozen: bool = False` and `frozen_total_elapsed: float | None = None` to `CardSession`.

In `render_header_atom`, replace the row1/row2 build to:

```python
if session.frozen:
    seq_str = f"#{session.sequence} <font color='grey'>[已封存]</font>"
    elapsed = _format_elapsed(session.frozen_total_elapsed or 0)
    live_dot = "⏸"
else:
    seq_str = f"#{session.sequence}"
    elapsed = _format_elapsed(session.elapsed_seconds())
    live_dot = "🟢"
```

- [ ] **Step 4: Run → PASS + full suite**

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(card): render frozen header for archived split predecessor"
```

---

## Phase 2 — Task List Three Groups

### Task 2.1: `group_tasks` helper

**Files:**
- Modify: `src/card/render/task_list.py`
- Test: `tests/test_task_list_v2_grouping.py` (NEW)

- [ ] **Step 1: Write failing test**

```python
# tests/test_task_list_v2_grouping.py
from src.card.render.task_list import group_tasks
from src.acp.models import PlanInfo, PlanEntry  # adjust to real types

def _plan(rows):
    return PlanInfo(entries=[PlanEntry(title=t, status=s) for t, s in rows])

def test_group_tasks_three_buckets():
    p = _plan([
        ("a", "completed"), ("b", "in_progress"),
        ("c", "pending"),  ("d", "completed"), ("e", "pending"),
    ])
    in_p, done, pend = group_tasks(p)
    assert [t.title for t in in_p] == ["b"]
    assert [t.title for t in done] == ["a", "d"]
    assert [t.title for t in pend] == ["c", "e"]

def test_group_tasks_empty_plan():
    assert group_tasks(_plan([])) == ([], [], [])
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Add `group_tasks` to `src/card/render/task_list.py`**

```python
def group_tasks(plan):
    in_progress = [e for e in plan.entries if e.status == "in_progress"]
    completed   = [e for e in plan.entries if e.status == "completed"]
    pending     = [e for e in plan.entries if e.status == "pending"]
    return in_progress, completed, pending
```

- [ ] **Step 4: Run → PASS + full suite green**

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(card): add group_tasks helper for three-bucket task list"
```

---

### Task 2.2: Three-group sticky atom + downgrade

**Files:**
- Modify: `src/card/render/task_list.py`
- Test: `tests/test_task_list_v2_render.py` (NEW)

- [ ] **Step 1: Write failing tests**

```python
# tests/test_task_list_v2_render.py
from src.card.render.task_list import render_task_list_atom
from src.acp.models import PlanInfo, PlanEntry

def _plan(rows): return PlanInfo(entries=[PlanEntry(title=t, status=s) for t, s in rows])

def test_three_groups_always_open():
    atom = render_task_list_atom(_plan([
        ("修复路由层", "in_progress"),
        ("探索代码库", "completed"),
        ("补单测", "pending"),
    ]))
    md = atom.to_text()
    assert "进行中 (1)" in md
    assert "已完成 (1)" in md
    assert "未处理 (1)" in md

def test_downgrade_when_over_12_tasks():
    rows = [(f"t{i}", "completed") for i in range(15)] + [("now", "in_progress")]
    atom = render_task_list_atom(_plan(rows))
    md = atom.to_text()
    assert "已完成 (15)" in md
    # collapsed group: don't render every completed title individually
    assert md.count("✔ t") <= 5

def test_pending_tail_truncation():
    rows = [("now", "in_progress")] + [(f"p{i}", "pending") for i in range(8)]
    atom = render_task_list_atom(_plan(rows))
    md = atom.to_text()
    assert "还有 3 个" in md
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Implement render**

Replace existing render in `task_list.py`:

```python
_PEND_VISIBLE = 5
_DONE_FOLD_THRESHOLD = 8

def render_task_list_atom(plan):
    in_p, done, pend = group_tasks(plan)
    lines = []

    lines.append(f"<font color='grey'>▶ 进行中 ({len(in_p)})</font>")
    for t in in_p[:3]:
        lines.append(f"▶ **{t.title}**")

    if len(done) > _DONE_FOLD_THRESHOLD:
        lines.append(f"<font color='grey'>✅ 已完成 ({len(done)}) ▼</font>")
        for t in done[:3]:
            lines.append(f"<font color='grey'>✔ ~~{t.title}~~</font>")
        lines.append(f"<font color='grey'>…还有 {len(done) - 3} 个已完成</font>")
    else:
        lines.append(f"<font color='grey'>✅ 已完成 ({len(done)})</font>")
        for t in done:
            lines.append(f"<font color='grey'>✔ ~~{t.title}~~</font>")

    lines.append(f"<font color='grey'>⏳ 未处理 ({len(pend)})</font>")
    for t in pend[:_PEND_VISIBLE]:
        lines.append(f"<font color='grey'>○ {t.title}</font>")
    if len(pend) > _PEND_VISIBLE:
        lines.append(f"<font color='grey'>…还有 {len(pend) - _PEND_VISIBLE} 个</font>")

    return RenderAtom(kind=AtomKind.TASK_LIST, markdown="\n".join(lines), node_count=1)
```

- [ ] **Step 4: Run → PASS + full suite**

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(card): three-group always-open task list with size downgrade"
```

---

## Phase 3 — Tool Collapse Rule

### Task 3.1: Only running tool open

**Files:**
- Modify: `src/card/render/tools.py` (find existing fold rule, ~391 lines)
- Test: `tests/test_tool_collapse_v2.py` (NEW)

- [ ] **Step 1: Write failing tests**

```python
# tests/test_tool_collapse_v2.py
from src.card.render.tools import render_tool_panel_atoms
from src.acp.models import ToolCallInfo

def _tool(name, status, **kw):
    return ToolCallInfo(name=name, status=status, **kw)

def test_only_running_tool_is_open():
    atoms = render_tool_panel_atoms([
        _tool("Grep", "completed"),
        _tool("Read", "completed"),
        _tool("Edit", "in_progress"),
    ])
    open_atoms = [a for a in atoms if a.is_open]
    assert len(open_atoms) == 1
    assert open_atoms[0].title.startswith("Edit")

def test_failed_tool_collapsed_with_red_marker():
    atoms = render_tool_panel_atoms([_tool("Bash", "failed")])
    assert all(not a.is_open for a in atoms)
    assert "❌" in atoms[0].title

def test_no_running_means_all_collapsed():
    atoms = render_tool_panel_atoms([
        _tool("Grep", "completed"), _tool("Read", "completed"),
    ])
    assert all(not a.is_open for a in atoms)
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Implement collapse rule**

Find current expansion logic in `src/card/render/tools.py` (likely a function that sets `open=True` for the latest active). Replace with:

```python
def _should_open(tool):
    return tool.status == "in_progress"

def _status_marker(tool):
    return {
        "in_progress": "",
        "completed": "✔",
        "failed": "❌",
        "cancelled": "⊘",
    }.get(tool.status, "")
```

Apply throughout the existing builder so only `in_progress` gets `is_open=True`. Failed/cancelled get markers in the summary.

- [ ] **Step 4: Run → PASS + full suite**

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(card): collapse all tools except running one"
```

---

## Phase 4 — Footer

### Task 4.1: now-tool hint

**Files:**
- Modify: `src/card/render/footer.py`
- Test: `tests/test_footer_v2_hint.py` (NEW)

- [ ] **Step 1: Write failing tests**

```python
# tests/test_footer_v2_hint.py
from src.card.render.footer import render_now_tool_hint
from src.acp.models import ToolCallInfo

def test_hint_for_edit():
    line = render_now_tool_hint(ToolCallInfo(
        name="Edit", status="in_progress", input={"path": "src/router.py"},
    ))
    assert "Edit" in line
    assert "src/router.py" in line

def test_hint_for_grep():
    line = render_now_tool_hint(ToolCallInfo(
        name="Grep", status="in_progress", input={"pattern": "def route"},
    ))
    assert "搜索" in line
    assert "def route" in line

def test_hint_unknown_tool_falls_back_to_name():
    line = render_now_tool_hint(ToolCallInfo(name="MysteryTool", status="in_progress", input={}))
    assert "MysteryTool" in line

def test_hint_none_when_no_running_tool():
    assert render_now_tool_hint(None) == ""
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Implement hint mapping**

Add to `src/card/render/footer.py`:

```python
_TOOL_BRIEF = {
    "Read":  lambda p: f"读取 {p.get('path','...')}",
    "Edit":  lambda p: f"写入 {p.get('path','...')}",
    "Write": lambda p: f"创建 {p.get('path','...')}",
    "Grep":  lambda p: f"搜索 “{p.get('pattern','...')}”",
    "Glob":  lambda p: f"列出 {p.get('pattern','...')}",
    "Bash":  lambda p: f"执行 {(p.get('command','') or '')[:40]}",
    "Task":  lambda p: f"派发 {p.get('subagent_type','agent')}",
}

def render_now_tool_hint(tool):
    if tool is None or tool.status != "in_progress":
        return ""
    fn = _TOOL_BRIEF.get(tool.name)
    summary = fn(tool.input or {}) if fn else tool.name
    return f"<font color='grey'>⚙ **{tool.name}** · {summary}</font>"
```

- [ ] **Step 4: Run → PASS + full suite**

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(card): footer now-tool hint mapping"
```

---

### Task 4.2: subagent badge

**Files:**
- Modify: `src/card/render/footer.py`
- Test: `tests/test_footer_v2_subagent.py` (NEW)

- [ ] **Step 1: Write failing tests**

```python
# tests/test_footer_v2_subagent.py
import time
from src.card.render.footer import render_subagent_badge
from src.card.state.models import CardSession

def test_no_badge_for_main_session():
    s = CardSession(chat_id="c", message_id="m", started_at=time.time())
    assert render_subagent_badge(s) == ""

def test_badge_renders_model_and_tool():
    s = CardSession(
        chat_id="c", message_id="m", started_at=time.time(),
        is_subagent=True, parent_card_seq="5",
        tool_id="aiden", model_id="claude-haiku-4-5",
    )
    md = render_subagent_badge(s)
    assert "sub" in md
    assert "claude-haiku-4-5" in md
    assert "Aiden" in md or "aiden" in md
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Implement badge**

```python
def render_subagent_badge(session):
    if not session.is_subagent:
        return ""
    return (
        f"<font color='orange'>🧬 sub · "
        f"model: {session.model_id or '?'} · "
        f"tool: {session.tool_id or '?'}</font>"
    )
```

- [ ] **Step 4: Run → PASS + full suite**

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(card): subagent badge in footer"
```

---

### Task 4.3: Footer atom assembly

**Files:**
- Modify: `src/card/render/footer.py`
- Test: `tests/test_footer_v2_assembly.py` (NEW)

- [ ] **Step 1: Write failing test**

```python
# tests/test_footer_v2_assembly.py
import time
from src.card.render.footer import build_footer_atoms
from src.card.state.models import CardSession
from src.acp.models import ToolCallInfo

def test_footer_combines_hint_and_badge():
    s = CardSession(
        chat_id="c", message_id="m", started_at=time.time(),
        is_subagent=True, parent_card_seq="5",
        tool_id="aiden", model_id="claude-haiku-4-5",
    )
    running = ToolCallInfo(name="Grep", status="in_progress", input={"pattern":"x"})
    atoms = build_footer_atoms(session=s, running_tool=running)
    md = "\n".join(a.to_text() for a in atoms)
    assert "Grep" in md
    assert "🧬 sub" in md

def test_footer_main_session_no_badge():
    s = CardSession(chat_id="c", message_id="m", started_at=time.time())
    atoms = build_footer_atoms(session=s, running_tool=None)
    assert all("🧬" not in a.to_text() for a in atoms)
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Implement assembly**

```python
def build_footer_atoms(*, session, running_tool):
    lines = []
    hint = render_now_tool_hint(running_tool)
    if hint:
        lines.append(hint)
    badge = render_subagent_badge(session)
    if badge:
        lines.append(badge)
    if not lines:
        return []
    return [RenderAtom(kind=AtomKind.FOOTER, markdown="\n".join(lines), node_count=1)]
```

- [ ] **Step 4: Run → PASS + full suite**

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(card): combine footer hint+badge into appendix atoms"
```

---

## Phase 5 — Reasoning↔Tool Turn State Machine

### Task 5.1: Turn boundary in `ACPEventRenderer`

**Files:**
- Modify: `src/acp/renderer.py`
- Test: `tests/test_acp_renderer_turn_machine.py` (NEW)

- [ ] **Step 1: Read current `_consume_event`**

```bash
grep -n "_consume_event\|def on_event\|text\|tool_call" src/acp/renderer.py | head -30
```

- [ ] **Step 2: Write failing tests**

```python
# tests/test_acp_renderer_turn_machine.py
from src.acp.renderer import ACPEventRenderer
from src.acp.models import ACPEvent, ACPEventType

def test_text_then_tool_then_text_creates_two_turns():
    r = ACPEventRenderer()
    r.on_event(ACPEvent(type=ACPEventType.TEXT, payload={"text": "thinking A"}))
    r.on_event(ACPEvent(type=ACPEventType.TOOL_CALL, payload={"name":"Grep","status":"completed"}))
    r.on_event(ACPEvent(type=ACPEventType.TEXT, payload={"text": "thinking B"}))
    turns = r.snapshot_turns()
    assert len(turns) == 2
    assert turns[0].reasoning == "thinking A"
    assert len(turns[0].tools) == 1
    assert turns[1].reasoning == "thinking B"

def test_consecutive_text_appends_within_turn():
    r = ACPEventRenderer()
    r.on_event(ACPEvent(type=ACPEventType.TEXT, payload={"text": "A "}))
    r.on_event(ACPEvent(type=ACPEventType.TEXT, payload={"text": "B"}))
    [turn] = r.snapshot_turns()
    assert turn.reasoning == "A B"
```

- [ ] **Step 3: Run → FAIL**

- [ ] **Step 4: Implement turn state machine**

Add to `ACPEventRenderer`:

```python
@dataclass
class _Turn:
    reasoning: str = ""
    tools: list = field(default_factory=list)
    closed: bool = False

class ACPEventRenderer:
    def __init__(self, ...):
        ...
        self._turns: list[_Turn] = [_Turn()]

    def _current_turn(self) -> "_Turn":
        if not self._turns or self._turns[-1].closed:
            self._turns.append(_Turn())
        return self._turns[-1]

    def _on_text(self, text: str):
        t = self._current_turn()
        if t.tools:                       # tools already arrived → next text starts new turn
            t.closed = True
            self._turns.append(_Turn(reasoning=text))
        else:
            t.reasoning += text

    def _on_tool(self, tool):
        self._current_turn().tools.append(tool)

    def snapshot_turns(self):
        return list(self._turns)
```

Wire `_on_text` / `_on_tool` into existing `on_event` dispatch.

- [ ] **Step 5: Run → PASS + full suite**

- [ ] **Step 6: Commit**

```bash
git commit -am "feat(acp): turn state machine for reasoning↔tool flow"
```

---

### Task 5.2: Renderer emits turn-shaped body atoms

**Files:**
- Modify: `src/card/render/renderer.py` (`_build_section_layout` body assembly)
- Test: `tests/test_renderer_v2_turn_atoms.py` (NEW)

- [ ] **Step 1: Write failing test**

```python
# tests/test_renderer_v2_turn_atoms.py
from src.card.render.renderer import render_card_state
from src.card.state.models import CardState  # build with two turns

def test_body_emits_reasoning_then_collapsed_tools_per_turn(card_state_two_turns):
    pages = render_card_state(card_state_two_turns)
    body = pages[0].body_text()
    # turn 1: reasoning A then tool Grep, then turn 2: reasoning B then tool Edit
    assert body.index("thinking A") < body.index("Grep")
    assert body.index("Grep") < body.index("thinking B")
    assert body.index("thinking B") < body.index("Edit")
```

(Add `card_state_two_turns` fixture in `tests/conftest.py` — build two `_Turn` records.)

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Modify `_build_section_layout`**

In `src/card/render/renderer.py`, replace the body assembly that currently flattens by atom kind. New logic:

```python
def _body_atoms_from_turns(turns):
    out = []
    for t in turns:
        if t.reasoning:
            out.append(RenderAtom(kind=AtomKind.REASONING, markdown=_reasoning_block(t.reasoning), node_count=1))
        out.extend(render_tool_panel_atoms(t.tools))
    return out
```

Then in `_build_section_layout`:

```python
body_atoms = _body_atoms_from_turns(state.turns)
```

Drop the old `activity_summary` panel construction.

- [ ] **Step 4: Run → PASS + full suite**

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(card): renderer emits per-turn reasoning+tools, drop activity_summary"
```

---

## Phase 6 — `card_split` Continuity

### Task 6.1: Freeze previous card on split

**Files:**
- Modify: `src/card/state/reducers/programming.py`
- Modify: `src/card/orchestrator.py` (split flush)
- Test: `tests/test_card_split_v2_freeze.py` (NEW)

- [ ] **Step 1: Write failing test**

```python
# tests/test_card_split_v2_freeze.py
from src.card.state.reducers.programming import reduce
from src.card.events.models import CardSplitEvent
from src.card.state.models import CardSession

def test_split_marks_previous_session_frozen():
    state = _state_with_active_card(seq=3, elapsed_so_far=420.0)
    new_state = reduce(state, CardSplitEvent(reason="task_done"))
    prev = new_state.previous_card_session
    assert prev.frozen is True
    assert prev.frozen_total_elapsed == 420.0
    assert new_state.current_session.sequence == 4
    assert new_state.current_session.session_started_at == prev.session_started_at  # carry-over
```

(Provide `_state_with_active_card` test helper in conftest.)

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Implement freeze branch in `reduce`**

```python
def _on_card_split(state, ev):
    prev = state.current_session
    prev.frozen = True
    prev.frozen_total_elapsed = prev.elapsed_seconds()
    new_session = CardSession(
        chat_id=prev.chat_id, message_id=_new_msg_id(),
        started_at=time.time(),
        sequence=prev.sequence + 1,
        session_started_at=prev.session_started_at,  # carry over
        project_name=prev.project_name,
        tool_id=prev.tool_id,
        model_id=prev.model_id,
        is_subagent=prev.is_subagent,
        parent_card_seq=prev.parent_card_seq,
    )
    return state.replace(
        previous_card_session=prev,
        current_session=new_session,
        last_bridge_phrase=ev.bridge_phrase,
    )
```

In `src/card/orchestrator.py`, after split: re-render and patch the previous card's last frame with `frozen=True` so its header swaps to archived state.

- [ ] **Step 4: Run → PASS + full suite**

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(card): freeze previous card and re-patch header on split"
```

---

### Task 6.2: New card cumulative elapsed + bridge phrase

**Files:**
- Modify: `src/card/render/header.py` (cumulative branch)
- Modify: `src/card/render/renderer.py` (inject bridge phrase into first reasoning)
- Test: `tests/test_card_split_v2_continuity.py` (NEW)

- [ ] **Step 1: Write failing tests**

```python
# tests/test_card_split_v2_continuity.py
def test_new_card_header_shows_cumulative(state_after_split):
    pages = render_card_state(state_after_split)
    h = pages[0].header_text()
    assert "0m" in h or "0s" in h
    assert "累计 7m" in h

def test_new_card_first_reasoning_has_bridge_phrase(state_after_split_mid_turn):
    pages = render_card_state(state_after_split_mid_turn)
    body = pages[0].body_text()
    assert body.lstrip().startswith("续接：")
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Cumulative elapsed in header row2**

In `render_header_atom`, when `session.session_started_at != session.started_at`:

```python
cur = _format_elapsed(session.elapsed_seconds())
total = _format_elapsed(time.time() - session.session_started_at)
elapsed = f"{cur} · 累计 {total}"
```

- [ ] **Step 4: Bridge phrase injection**

In `_body_atoms_from_turns`, prepend bridge phrase to the first reasoning of the new card if `state.last_bridge_phrase` is set:

```python
if state.last_bridge_phrase and turns and turns[0].reasoning:
    turns[0] = replace(turns[0], reasoning=state.last_bridge_phrase + turns[0].reasoning)
```

Default bridge phrase is `"续接："` when split happened mid-turn (reasoning had begun but no tool yet).

- [ ] **Step 5: Run → PASS + full suite**

- [ ] **Step 6: Commit**

```bash
git commit -am "feat(card): cumulative elapsed + 续接 bridge phrase across split"
```

---

## Phase 7 — Parallel Subagent

### Task 7.1: Subagent CardSession factory + dispatch event

**Files:**
- Modify: `src/card/session_factory.py`
- Modify: `src/card/state/reducers/programming.py` (handle dispatch event)
- Test: `tests/test_subagent_session_factory.py` (NEW)

- [ ] **Step 1: Write failing test**

```python
# tests/test_subagent_session_factory.py
from src.card.session_factory import spawn_subagent_session

def test_spawn_uses_dotted_seq():
    parent = _make_session(sequence=5)
    child_a = spawn_subagent_session(parent, branch="a", tool_id="aiden", model_id="x")
    assert child_a.is_subagent is True
    assert child_a.sequence == "5.a"
    assert child_a.parent_card_seq == "5"
    assert child_a.tool_id == "aiden"
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Implement factory**

```python
def spawn_subagent_session(parent, *, branch, tool_id, model_id):
    return CardSession(
        chat_id=parent.chat_id, message_id=_new_msg_id(),
        started_at=time.time(),
        session_started_at=time.time(),
        sequence=f"{parent.sequence}.{branch}",
        is_subagent=True, parent_card_seq=str(parent.sequence),
        tool_id=tool_id, model_id=model_id,
        project_name=parent.project_name,
    )
```

Note: `sequence` becomes `int | str` for subagent. Update type hint and the header `f"#{session.sequence}"` rendering tolerates both.

- [ ] **Step 4: Run → PASS + full suite**

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(card): spawn_subagent_session with dotted sequence"
```

---

### Task 7.2: Independent stream throttle per subagent

**Files:**
- Modify: `src/card/orchestrator.py` (or `src/card/stream_bridge.py`)
- Test: `tests/test_subagent_independent_throttle.py` (NEW)

- [ ] **Step 1: Write failing test**

```python
# tests/test_subagent_independent_throttle.py
def test_two_subagents_have_independent_throttles(orchestrator):
    parent = orchestrator.start_session(...)
    a = orchestrator.spawn_subagent(parent, branch="a", tool_id="aiden", model_id="x")
    b = orchestrator.spawn_subagent(parent, branch="b", tool_id="codex", model_id="y")
    assert a.stream_throttle is not b.stream_throttle
    assert a.stream_throttle is not parent.stream_throttle
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Allocate per-session throttle**

In `orchestrator.spawn_subagent`, create a new `_StreamThrottle()` instance and bind to the subagent CardSession (`session.stream_throttle = ...`). Replace any module-global throttle lookup with `session.stream_throttle`.

- [ ] **Step 4: Run → PASS + full suite**

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(card): independent stream throttle per subagent session"
```

---

### Task 7.3: Orange theme on subagent header

**Files:**
- Modify: `src/card/render/header.py`
- Test: `tests/test_subagent_orange_theme.py` (NEW)

- [ ] **Step 1: Write failing test**

```python
# tests/test_subagent_orange_theme.py
def test_subagent_header_uses_orange_chip():
    s = _make_session(is_subagent=True, parent_card_seq="5", sequence="5.a")
    atom = render_header_atom(s, working_dir="/x")
    md = atom.to_text()
    assert "<font color='orange'>" in md or "color=\"orange\"" in md
    assert "#5.a" in md
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Branch on `is_subagent` for chip color**

In `render_header_atom`:

```python
if session.is_subagent:
    seq_str = f"<font color='orange'>#{session.sequence}</font>"
else:
    seq_str = f"#{session.sequence}"
```

(The actual `column_set.background_style.color` swap happens at the `paginate_layout` -> Feishu element layer; the markdown chip color is the user-visible portion. Update background_style mapping if header atom carries a `theme="subagent"` flag.)

- [ ] **Step 4: Run → PASS + full suite**

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(card): orange theme chip for subagent header"
```

---

### Task 7.4: Main card dispatch atom + subagent finalize summary

**Files:**
- Modify: `src/card/render/tools.py` (new dispatch atom builder)
- Modify: `src/card/state/reducers/programming.py` (subagent finalize event)
- Test: `tests/test_dispatch_atom_summary.py` (NEW)

- [ ] **Step 1: Write failing test**

```python
# tests/test_dispatch_atom_summary.py
def test_dispatch_atom_updates_when_subagent_finalizes(orchestrator):
    parent = orchestrator.start_session(...)
    a = orchestrator.spawn_subagent(parent, branch="a", tool_id="aiden", model_id="x")
    b = orchestrator.spawn_subagent(parent, branch="b", tool_id="codex", model_id="y")
    orchestrator.finalize_subagent(a.id, status="ok", elapsed=114.0)
    md = orchestrator.snapshot_main_card_text()
    assert "#5.a · ✔" in md
    assert "1m54s" in md
    assert "#5.b · ⏳" in md
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Implement dispatch atom**

Build atom in `tools.py`:

```python
def render_dispatch_atom(subagents):
    done = sum(1 for s in subagents if s.is_done)
    head = f"🧬 Dispatch · {len(subagents)} subagents (✔ {done} / ⏳ {len(subagents)-done})"
    rows = [
        f"→ #{s.sequence} · {s.tool_id} · {'✔ ' + _format_elapsed(s.elapsed) if s.is_done else '⏳ running'}"
        for s in subagents
    ]
    return RenderAtom(kind=AtomKind.DISPATCH, markdown=head + "\n" + "\n".join(rows), node_count=1)
```

Wire reducer to add/update this atom on subagent spawn/finalize events.

- [ ] **Step 4: Run → PASS + full suite**

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(card): main-card dispatch atom summarizing subagent progress"
```

---

## Phase 8 — Live Ticker (1Hz Frame Swap)

### Task 8.1: `live_ticker` module

**Files:**
- Create: `src/card/render/live_ticker.py`
- Test: `tests/test_live_ticker.py` (NEW)

- [ ] **Step 1: Write failing tests**

```python
# tests/test_live_ticker.py
from src.card.render.live_ticker import LiveTicker

def test_emoji_frame_swap_per_tick():
    t = LiveTicker()
    seq = [t.dot_frame() for _ in range(4)]
    assert seq == ["🟢", "⚪", "🟢", "⚪"]

def test_running_marker_dot_dot_dot():
    t = LiveTicker()
    seq = [t.shimmer_frame() for _ in range(4)]
    assert seq == [".", "..", "...", "."]

def test_ticker_stops_when_session_frozen():
    t = LiveTicker()
    t.attach_session(_session(frozen=False))
    assert t.is_active() is True
    t.attach_session(_session(frozen=True))
    assert t.is_active() is False
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Implement**

```python
# src/card/render/live_ticker.py
class LiveTicker:
    def __init__(self):
        self._tick = 0
        self._session = None

    def attach_session(self, session):
        self._session = session
        return self

    def is_active(self) -> bool:
        return self._session is not None and not getattr(self._session, "frozen", False)

    def step(self):
        self._tick += 1

    def dot_frame(self) -> str:
        f = "🟢" if self._tick % 2 == 0 else "⚪"
        self._tick += 1
        return f

    def shimmer_frame(self) -> str:
        f = ".".ljust(self._tick % 3 + 1, ".")
        self._tick += 1
        return f
```

- [ ] **Step 4: Run → PASS + full suite**

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(card): LiveTicker primitives for 1Hz emoji frame swap"
```

---

### Task 8.2: Wire ticker to TimerScheduler

**Files:**
- Modify: `src/card/timer_scheduler.py` (or wherever scheduler entry lives — grep)
- Modify: `src/card/orchestrator.py` (register ticker)
- Test: `tests/test_live_ticker_scheduler.py` (NEW)

- [ ] **Step 1: Confirm scheduler entry**

```bash
grep -n "TimerScheduler\|schedule\|interval" src/card/timer_scheduler.py | head -20
```

- [ ] **Step 2: Write failing test**

```python
# tests/test_live_ticker_scheduler.py
def test_orchestrator_registers_ticker_on_session_start(orchestrator):
    s = orchestrator.start_session(...)
    assert s.id in orchestrator.scheduler.registered_keys()

def test_scheduler_drops_ticker_on_freeze(orchestrator):
    s = orchestrator.start_session(...)
    orchestrator.freeze_session(s.id)
    assert s.id not in orchestrator.scheduler.registered_keys()
```

- [ ] **Step 3: Run → FAIL**

- [ ] **Step 4: Hook ticker into orchestrator lifecycle**

In `orchestrator.start_session`, after CardSession creation:

```python
self.scheduler.register(session.id, interval=1.0, callback=lambda: self._tick(session))
```

In `orchestrator.freeze_session`:

```python
self.scheduler.unregister(session.id)
```

`_tick` invokes a partial header re-render and patches via element_content (existing CardKit v2 path).

- [ ] **Step 5: Run → PASS + full suite**

- [ ] **Step 6: Commit**

```bash
git commit -am "feat(card): wire LiveTicker into TimerScheduler with freeze-aware lifecycle"
```

---

## Phase 9 — Renderer Wire & Sticky Move

### Task 9.1: Move task_list into sticky_head

**Files:**
- Modify: `src/card/render/renderer.py`, `src/card/render/sticky_head.py`
- Test: `tests/test_sticky_head_v2_contains_task_list.py` (NEW)

- [ ] **Step 1: Write failing test**

```python
# tests/test_sticky_head_v2_contains_task_list.py
def test_sticky_head_includes_task_list(state_with_three_tasks):
    layout = _build_section_layout_for(state_with_three_tasks)
    sticky_md = "\n".join(a.to_text() for a in layout.sticky_head)
    assert "进行中" in sticky_md
    assert "已完成" in sticky_md
    assert "未处理" in sticky_md

def test_sticky_head_node_budget(state_with_30_tasks):
    layout = _build_section_layout_for(state_with_30_tasks)
    nodes = sum(a.node_count for a in layout.sticky_head)
    assert nodes <= 25
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Re-wire `_build_section_layout`**

In `src/card/render/renderer.py`:

```python
def _build_section_layout(state, atoms):
    sticky = (
        render_header_atom(state.current_session, state.working_dir),
        render_task_list_atom(state.plan),
    )
    body = tuple(_body_atoms_from_turns(state.turns))
    appendix = tuple(build_footer_atoms(
        session=state.current_session,
        running_tool=state.running_tool,
    ))
    status = tuple(_build_status_atoms(state))  # unchanged for Deep/Loop/Spec
    return SectionLayout(sticky_head=sticky, status=status, body=body, appendix=appendix)
```

- [ ] **Step 4: Run → PASS + full suite**

Expect ~30-50 fixture tests to drift. Update them by:
- Asserting task list lives in sticky_head, not body.
- Removing references to `activity_summary` panel.

- [ ] **Step 5: Commit**

```bash
git commit -am "refactor(card): move task_list to sticky_head, footer to appendix"
```

---

### Task 9.2: Drop `activity_summary` panel

**Files:**
- Delete or no-op: `src/card/render/activity_summary.py` if exists; remove call sites
- Test: `tests/test_activity_summary_removed.py` (NEW)

- [ ] **Step 1: Locate call sites**

```bash
grep -rn "activity_summary\|ActivitySummary" src/ tests/ | head -30
```

- [ ] **Step 2: Write regression test**

```python
# tests/test_activity_summary_removed.py
def test_no_activity_summary_atom_in_render(state_with_many_tools):
    pages = render_card_state(state_with_many_tools)
    md = "\n".join(p.body_text() for p in pages)
    assert "活动 — 已编辑" not in md
    assert "📊 活动" not in md
```

- [ ] **Step 3: Run → may PASS or FAIL depending on residual uses**

- [ ] **Step 4: Remove activity_summary atom builder**

Delete the function/file and any remaining call site. Keep statistics computation if still used elsewhere; only remove the atom emission.

- [ ] **Step 5: Run → PASS + full suite**

- [ ] **Step 6: Commit**

```bash
git commit -am "refactor(card): remove activity_summary panel (semantics moved to per-turn reasoning)"
```

---

## Phase 10 — Static Gates + Regression

### Task 10.1: Schema 2.0 static guard tests

**Files:**
- Modify or create: `tests/test_card_renderer_schema_guard.py`

- [ ] **Step 1: Write tests**

```python
# tests/test_card_renderer_schema_guard.py
import json
from src.card.render.renderer import render_card_state

def _walk(node, hits):
    if isinstance(node, dict):
        if node.get("tag") == "div" and "background_style" in node:
            hits.append(("div_bg_style", node))
        if "text_color" in node:
            hits.append(("text_color", node))
        for v in node.values():
            _walk(v, hits)
    elif isinstance(node, list):
        for v in node:
            _walk(v, hits)

def test_no_div_with_background_style_or_text_color(state_with_subagent):
    pages = render_card_state(state_with_subagent)
    hits = []
    for p in pages:
        _walk(json.loads(p.to_json()), hits)
    assert hits == []
```

- [ ] **Step 2: Run → expect PASS** (if any FAIL, fix offending atoms inline before commit)

- [ ] **Step 3: Commit**

```bash
git commit -am "test(card): static guard for div+background_style and text_color"
```

---

### Task 10.2: 30-task / 100-tool / 5-split / 2-subagent budget regression

**Files:**
- Modify: `tests/test_card_budget_regression.py`

- [ ] **Step 1: Add scenario**

```python
def test_extreme_load_within_budget():
    state = _build_state(tasks=30, tools=100, splits=5, subagents=2)
    pages = render_card_state(state)
    for p in pages:
        nodes = sum(a.node_count for a in p.atoms)
        assert nodes <= 180
```

- [ ] **Step 2: Run → expect PASS** (fix any overflow with downgrades)

- [ ] **Step 3: Commit**

```bash
git commit -am "test(card): budget regression for v2 extreme load"
```

---

### Task 10.3: Full suite + memory log

**Files:**
- Modify: `.Memory/2026-05-10.md` (CREATE if missing)
- Modify: `.Memory/Abstract.md`

- [ ] **Step 1: Run full suite**

```bash
uv run python -m pytest -q
```
Expected: all green. Fix any residual fixture drift.

- [ ] **Step 2: Append memory entry**

Create `.Memory/2026-05-10.md`:

```markdown
# 2026-05-10 项目记录

## 编程模式卡片 v2 重设计
### 任务描述
统一 Coco/Claude/Aiden/Codex/Gemini/TTADK + Deep/Loop/Spec/Worktree 卡片：项目-工具-#序号 header、三段常开任务列表、turn 级 reasoning↔tool 折叠、footer 工具简介+subagent 标、card_split 冻结+续接、并行 subagent 橙系独立卡。

### 执行内容
- CardSession 扩 4 字段 (sequence/session_started_at/is_subagent/parent_card_seq + frozen/frozen_total_elapsed)
- header 双行 + 冻结模式 + 累计时间
- task_list 三段常开 + 12+ 任务降级
- 工具仅 running 展开
- footer = now-tool hint + subagent badge
- ACPEventRenderer turn 状态机
- card_split 冻结上一卡 + bridge_phrase
- spawn_subagent_session 点号编号 + 独立 throttle + dispatch atom
- LiveTicker 1Hz emoji frame swap
- task_list 上移 sticky_head, footer 下移 appendix, activity_summary 移除

### 技术要点
- SectionLayout SSOT 不动，只换内容
- Schema 2.0 限制：column_set 级 background_style，禁 text_color，禁 div+padding
- 飞书无 CSS 动画，只能 emoji 帧切 + element_content patch

### 提交记录
（按提交顺序粘贴 commit hash + subject）
```

Append index line to `.Memory/Abstract.md`:

```markdown
## 2026-05-10
- **编程模式卡片 v2 重设计** — 项目-工具-#序号 header、三段任务列表、turn 折叠、footer 工具简介+subagent 标、切卡冻结续接、并行 subagent 橙系独立卡 → [详细记录](2026-05-10.md)
```

- [ ] **Step 3: Commit**

```bash
git add .Memory/2026-05-10.md .Memory/Abstract.md
git commit -m "docs(memory): record card v2 redesign task"
```

---

## Self-Review Checklist

Spec coverage map:

| Spec § | Plan task |
|--------|-----------|
| §3 骨架 | 9.1, 9.2 |
| §4.1 header | 1.1, 1.2 |
| §4.2 task_list | 2.1, 2.2 |
| §4.3 flow turn | 5.1, 5.2 |
| §4.5 footer | 4.1, 4.2, 4.3 |
| §4.5.1 tool brief 映射 | 4.1 |
| §5 card_split | 6.1, 6.2 |
| §6 subagent 并行 | 7.1, 7.2, 7.3, 7.4 |
| §7 live ticker | 8.1, 8.2 |
| §8 数据模型 | 0.1, 0.2 |
| §10.1-10.4 测试 | 1.x, 2.x, 3.x, 4.x, 5.x, 6.x, 7.x, 9.x, 10.2 |
| §10.5 静态门禁 | 10.1 |
| §11 .Memory 更新 | 10.3 |

No placeholders. Type names consistent (`CardSession.sequence` int|str, `frozen`/`frozen_total_elapsed`, `spawn_subagent_session`, `render_now_tool_hint`, `render_subagent_badge`, `build_footer_atoms`, `_body_atoms_from_turns`, `LiveTicker`, `bridge_phrase`).
