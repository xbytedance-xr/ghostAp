# Slock Usability Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Slock teams start cleanly, create usable default Traex-based roles, render only Feishu Schema 2.0-safe cards, and expose diagnostics when a team cannot run.

**Architecture:** Fix the runtime blockers before UX polish: first add a Slock card schema contract and remove unsupported card nodes, then align default role bootstrap with Traex, then harden disk restore against stale/test markers and missing-role plans. Keep Slock execution strategy intact; changes should be small, test-first, and reversible per subsystem.

**Tech Stack:** Python 3.13, pytest, ruff, Feishu interactive card Schema 2.0, GhostAP Slock engine, ACP tool registry.

---

## Evidence Snapshot

- Current branch was already committed and pushed before this analysis: `e46e9cb fix(workflow): avoid unsupported note cards`, `slock` is synced with `origin/slock`.
- `logs.log` shows Slock startup restores 9 engines from `~/.ghostap/slock/groups`, including test/demo ids: `test_chat_006`, `test_chat_007`, `t_claim_1`, `t_claim_2`, `t_claim_3`, `ch_templates`, `ch_workspace`.
- `logs.log` shows the real team `oc_8db1260b409f93efa03a2a87681ea285` repeatedly fails plans with no matching roles:
  `No agent for role planner/coder/reviewer/tester`, then `Plan e74b97cf marked FAILED: all steps skipped/timed out`.
- `~/.ghostap/slock/groups/oc_8db1260b409f93efa03a2a87681ea285/workspace/.plans.json` has pending/failed collaboration plans with empty `agent_id`.
- `~/.ghostap/slock/agents/*/identity.json` currently has only `writer` agents for that real team, while the default chain needs `planner -> coder -> reviewer -> tester`.
- `logs.log` shows card send failures: `not support tag: progress`. Slock templates still emit `progress`, `note`, and legacy `action` nodes.
- Slock bootstrap still accepts `ttadk` and rejects `traex`: `src/slock_engine/role_bootstrap.py` and `src/config/settings.py` allow `{"codex", "claude", "coco", "aiden", "gemini", "ttadk"}`.

## File Map

- Modify: `src/slock_engine/card_templates/common.py` — add shared Schema 2.0-safe progress/hint helpers.
- Modify: `src/slock_engine/card_templates/progress.py` — replace native `progress` with markdown progress.
- Modify: `src/slock_engine/card_templates/discussion.py` — replace native `progress` and `note` with markdown.
- Modify: `src/slock_engine/card_templates/queue_feedback.py` — replace `note` with markdown.
- Modify: `src/slock_engine/card_templates/command.py` — remove legacy `action` containers from extended command card or route to slash-command examples instead of inline inputs.
- Modify: `src/feishu/handlers/slock.py` — remove inline dissolve-confirm `action` container; use responsive button rows.
- Modify: `src/config/settings.py` — make default roles Traex-first and validate `traex`; remove `ttadk` from Slock default-role validation.
- Modify: `src/slock_engine/role_bootstrap.py` — support `traex`, reject `ttadk`, and expose a single supported-tool constant.
- Modify: `src/slock_engine/manager.py` — skip non-Feishu/test markers on restore by default and log skipped markers.
- Modify: `src/slock_engine/collaboration_orchestrator.py` — block or pause plans with missing required roles instead of auto-failing every step.
- Create: `src/slock_engine/doctor.py` — read-only diagnostics for storage, role coverage, stale plans, and card-safe state.
- Modify: `src/feishu/handlers/slock.py` — add `/slock doctor` or `/team doctor` command routing to diagnostics.
- Test: `tests/test_slock_schema_v2_contract.py` — recursive card contract for Slock templates.
- Test: `tests/test_slock_config.py`, `tests/test_slock_bootstrap_safety.py`, `tests/test_slock_passive.py` — Traex default roles and bootstrap behavior.
- Test: `tests/test_slock_runtime_restore.py` — restore hygiene for non-`oc_` markers.
- Test: `tests/test_slock_collaboration_missing_roles.py` — missing roles produce actionable blocked state, not silent failed plan.
- Test: `tests/test_slock_doctor.py` — diagnostics output.
- Docs: `.env.example`, `README.md`, `.Memory/{date}.md`, `.Memory/Abstract.md`.

---

### Task 1: Add a Slock Schema 2.0 Contract and Remove Unsupported Card Nodes

**Files:**
- Create: `tests/test_slock_schema_v2_contract.py`
- Modify: `src/slock_engine/card_templates/common.py`
- Modify: `src/slock_engine/card_templates/progress.py`
- Modify: `src/slock_engine/card_templates/discussion.py`
- Modify: `src/slock_engine/card_templates/queue_feedback.py`
- Modify: `src/slock_engine/card_templates/command.py`
- Modify: `src/feishu/handlers/slock.py`

- [ ] **Step 1: Write the failing schema contract**

```python
# tests/test_slock_schema_v2_contract.py
from __future__ import annotations

from src.slock_engine.card_templates import (
    build_activation_confirm_card,
    build_command_panel_card,
    build_command_panel_extended_card,
    build_discussion_live_card,
    build_progress_overview_card,
    build_queue_full_card,
    build_queue_wait_card,
)
from src.slock_engine.models import CollaborationPlan, CollaborationPlanStatus

UNSUPPORTED_TAGS = {"action", "note", "progress"}


def _walk(node: object):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _assert_card_safe(card: dict) -> None:
    assert card["schema"] == "2.0"
    tags = [node.get("tag") for node in _walk(card) if isinstance(node, dict)]
    blocked = [tag for tag in tags if tag in UNSUPPORTED_TAGS]
    assert blocked == []


def test_core_slock_cards_do_not_emit_unsupported_schema_v2_tags() -> None:
    plan = CollaborationPlan(
        plan_id="plan_1",
        task_id="task_1",
        task_content="run task",
        status=CollaborationPlanStatus.PENDING_APPROVAL,
    )
    cards = [
        build_queue_wait_card(position=1, busy_count=1, message_preview="hello"),
        build_activation_confirm_card(team_name="Team", agent_count=4),
        build_queue_full_card(message_preview="hello", max_size=8),
        build_discussion_live_card(
            thread_id="thread_1",
            participants=["planner", "coder"],
            messages=[{"sender": "planner", "content": "ok", "round_num": 1}],
            current_round=1,
            max_rounds=3,
            channel_id="oc_test",
        ),
        build_progress_overview_card([plan], [], channel_id="oc_test"),
        build_command_panel_card(channel_id="oc_test"),
        build_command_panel_extended_card(channel_id="oc_test"),
    ]
    for card in cards:
        _assert_card_safe(card)
```

- [ ] **Step 2: Run the red test**

Run: `uv run python -m pytest tests/test_slock_schema_v2_contract.py -q`

Expected: FAIL with unsupported tags including `progress`, `note`, and `action`.

- [ ] **Step 3: Add shared safe helpers**

Add these helpers to `src/slock_engine/card_templates/common.py`:

```python
def build_markdown_hint(text: str, *, color: str = "grey") -> dict:
    return {"tag": "markdown", "content": f"<font color='{color}'>{text}</font>"}


def build_markdown_progress(current: int, total: int, *, label: str = "进度") -> dict:
    total = max(1, int(total or 1))
    current = max(0, min(int(current or 0), total))
    filled = round(current / total * 10)
    bar = "●" * filled + "○" * (10 - filled)
    return {
        "tag": "markdown",
        "content": f"{label} {bar} ({current}/{total})",
    }


def build_percent_progress(percent: int, *, label: str = "进度") -> dict:
    percent = max(0, min(100, int(percent or 0)))
    filled = round(percent / 10)
    bar = "●" * filled + "○" * (10 - filled)
    return {"tag": "markdown", "content": f"{label} {bar} {percent}%"}
```

- [ ] **Step 4: Replace unsupported nodes in Slock card templates**

Implement these replacements:

- `progress.py::_build_native_progress()` returns `build_percent_progress(pct)` instead of `{"tag": "progress", ...}`.
- `discussion.py` uses `build_markdown_progress(current_round, max_rounds)` for the round bar.
- Short discussion messages use markdown: `{"tag": "markdown", "content": f"💬 **{sender}** (R{round_num}): {raw_content}"}`.
- `queue_feedback.py` replaces every `note` with `build_markdown_hint(...)`.
- `command.py::build_command_panel_extended_card()` must not emit input-containing `action` nodes. For this pass, replace inline input forms with markdown usage examples and callback buttons that open existing slash-command flows:
  - `/new-team <团队名>`
  - `/new-role <角色名>`
  - `/council <议题>`
- `SlockHandler._dispatch_cmd_panel_action()` dissolve confirm card must build buttons with `build_responsive_layout()` or `build_mobile_card_row()`, not `{"tag": "action"}`.

- [ ] **Step 5: Run contract and adjacent card tests**

Run:

```bash
uv run python -m pytest \
  tests/test_slock_schema_v2_contract.py \
  tests/test_slock_card_templates.py \
  tests/test_slock_card_mobile.py \
  tests/test_slock_cmd_panel_dispatch.py \
  tests/test_slock_dissolve_confirm.py \
  -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/slock_engine/card_templates src/feishu/handlers/slock.py tests/test_slock_schema_v2_contract.py tests/test_slock_card_templates.py tests/test_slock_card_mobile.py tests/test_slock_cmd_panel_dispatch.py tests/test_slock_dissolve_confirm.py
git commit -m "fix(slock): render schema-safe cards"
```

---

### Task 2: Make Slock Default Roles Traex-First and Remove TTADK From Bootstrap

**Files:**
- Modify: `src/config/settings.py`
- Modify: `src/slock_engine/role_bootstrap.py`
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `tests/test_slock_config.py`
- Modify: `tests/test_slock_bootstrap_safety.py`
- Modify: `tests/test_slock_passive.py`

- [ ] **Step 1: Write failing config and bootstrap tests**

Add/adjust tests:

```python
def test_slock_default_roles_default_is_traex_first(monkeypatch):
    monkeypatch.delenv("SLOCK_DEFAULT_ROLES", raising=False)
    from src.config.settings import Settings
    s = Settings(_env_file=None)
    assert s.slock_default_roles == "planner:traex,coder:traex,reviewer:traex,tester:traex"


def test_slock_default_roles_accepts_traex(monkeypatch):
    monkeypatch.setenv("SLOCK_DEFAULT_ROLES", "planner:traex,coder:traex")
    from src.config.settings import Settings
    s = Settings()
    assert "traex" in s.slock_default_roles


def test_slock_default_roles_rejects_ttadk(monkeypatch):
    monkeypatch.setenv("SLOCK_DEFAULT_ROLES", "planner:ttadk")
    from pydantic import ValidationError
    from src.config.settings import Settings
    with pytest.raises(ValidationError):
        Settings()
```

Update `tests/test_slock_bootstrap_safety.py`:

```python
def test_supported_tool_types_use_traex_not_ttadk():
    from src.slock_engine.role_bootstrap import SUPPORTED_TOOL_TYPES
    assert "traex" in SUPPORTED_TOOL_TYPES
    assert "ttadk" not in SUPPORTED_TOOL_TYPES
```

- [ ] **Step 2: Run red tests**

Run:

```bash
uv run python -m pytest tests/test_slock_config.py tests/test_slock_bootstrap_safety.py -q
```

Expected: FAIL because current defaults are empty and Traex is not allowed.

- [ ] **Step 3: Update settings and bootstrap constants**

In `src/config/settings.py`:

```python
slock_default_roles: str = Field(
    default="planner:traex,coder:traex,reviewer:traex,tester:traex",
    description=(
        "新建 slock 群时自动创建的预置角色（格式: role:tool_type,role:tool_type）。"
        " 合法 tool_type: traex, codex, claude, coco, aiden, gemini"
    ),
)
```

Use a shared set in validation:

```python
valid_tool_types = {"traex", "codex", "claude", "coco", "aiden", "gemini"}
```

In `src/slock_engine/role_bootstrap.py`:

```python
SUPPORTED_TOOL_TYPES: frozenset[str] = frozenset(
    {"traex", "codex", "claude", "coco", "aiden", "gemini"}
)
```

- [ ] **Step 4: Update docs and examples**

In `.env.example` and `README.md`, replace the Slock default-role example with:

```bash
SLOCK_DEFAULT_ROLES=planner:traex,coder:traex,reviewer:traex,tester:traex
```

Document supported tool types as:

```text
traex, codex, claude, coco, aiden, gemini
```

- [ ] **Step 5: Run config and passive bootstrap tests**

Run:

```bash
uv run python -m pytest \
  tests/test_slock_config.py \
  tests/test_slock_bootstrap_safety.py \
  tests/test_slock_passive.py \
  tests/test_slock_role_creation.py \
  -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/config/settings.py src/slock_engine/role_bootstrap.py .env.example README.md tests/test_slock_config.py tests/test_slock_bootstrap_safety.py tests/test_slock_passive.py tests/test_slock_role_creation.py
git commit -m "fix(slock): bootstrap traex default roles"
```

---

### Task 3: Harden Runtime Restore and Stop Restoring Test Markers by Default

**Files:**
- Modify: `src/config/settings.py`
- Modify: `src/slock_engine/manager.py`
- Modify: `tests/test_slock_runtime_restore.py`

- [ ] **Step 1: Write failing restore tests**

Add tests to `tests/test_slock_runtime_restore.py`:

```python
def test_restore_skips_non_feishu_chat_markers_by_default(tmp_path):
    storage = str(tmp_path / "slock")
    root = str(tmp_path / "repo")
    _write_marker(storage, "test_chat_006", {"channel_id": "test_chat_006", "team_name": "TestTeam"})
    _write_marker(storage, "t_claim_1", {"channel_id": "t_claim_1", "team_name": "T"})

    manager = SlockEngineManager(storage_base_path=storage)
    restored = manager.restore_from_disk(root)

    assert restored == 0
    assert manager.list_activated_engines() == []


def test_restore_keeps_real_feishu_group_markers(tmp_path):
    storage = str(tmp_path / "slock")
    root = str(tmp_path / "repo")
    _write_marker(storage, "oc_real", {"channel_id": "oc_real", "team_name": "Team"})

    manager = SlockEngineManager(storage_base_path=storage)
    restored = manager.restore_from_disk(root)

    assert restored == 1
    assert manager.get_activated_engine("oc_real") is not None
```

- [ ] **Step 2: Run red tests**

Run: `uv run python -m pytest tests/test_slock_runtime_restore.py -q`

Expected: first new test fails because current restore loads all markers.

- [ ] **Step 3: Add restore filter**

In `src/config/settings.py`, add:

```python
slock_restore_non_feishu_markers: bool = Field(
    default=False,
    description="是否恢复非 oc_ 前缀的历史 Slock marker；默认关闭以避免测试/旧数据污染生产运行态",
)
```

In `src/slock_engine/manager.py`, add a constructor kwarg with default:

```python
def __init__(self, storage_base_path: str = "", restore_non_feishu_markers: bool = False) -> None:
    ...
    self._restore_non_feishu_markers = restore_non_feishu_markers
```

Before restoring each marker:

```python
if not self._restore_non_feishu_markers and not str(channel_id).startswith("oc_"):
    logger.info("restore_from_disk: skipping non-Feishu marker chat=%s", channel_id)
    continue
```

Wire `ctx.settings.slock_restore_non_feishu_markers` into the manager creation site if it exists centrally; otherwise keep the default false.

- [ ] **Step 4: Run restore and startup-adjacent tests**

Run:

```bash
uv run python -m pytest tests/test_slock_runtime_restore.py tests/test_slock_new_team.py -q
```

Expected: tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/config/settings.py src/slock_engine/manager.py tests/test_slock_runtime_restore.py
git commit -m "fix(slock): skip stale test markers on restore"
```

---

### Task 4: Block Missing-Role Plans With an Actionable State Instead of Auto-Failing

**Files:**
- Modify: `src/slock_engine/collaboration_orchestrator.py`
- Modify: `src/slock_engine/models.py`
- Modify: `src/slock_engine/card_templates/progress.py`
- Modify: `tests/test_slock_collaboration_missing_roles.py`

- [ ] **Step 1: Write failing missing-role behavior test**

Create `tests/test_slock_collaboration_missing_roles.py`:

```python
from src.slock_engine.collaboration_orchestrator import CollaborationOrchestrator
from src.slock_engine.models import CollaborationPlanStatus, PlanStepStatus


def test_missing_roles_blocks_plan_without_marking_all_steps_skipped():
    events = []
    orchestrator = CollaborationOrchestrator(
        list_agents_fn=lambda channel_id=None: [],
        create_task_fn=lambda *args, **kwargs: None,
        dispatch_task_fn=lambda *args, **kwargs: None,
        notify_overview_fn=lambda plan_id: events.append(plan_id),
    )
    plan = orchestrator.create_plan(
        task_id="task_1",
        task_content="大家介绍下自己",
        channel_id="oc_real",
        chain_template="planner->coder->reviewer->tester",
    )

    orchestrator.start_plan(plan.plan_id)

    assert plan.status == CollaborationPlanStatus.BLOCKED
    assert all(step.status == PlanStepStatus.TODO for step in plan.steps)
    assert plan.missing_roles == ["planner", "coder", "reviewer", "tester"]
```

- [ ] **Step 2: Run red test**

Run: `uv run python -m pytest tests/test_slock_collaboration_missing_roles.py -q`

Expected: FAIL because current behavior marks all steps skipped and the plan failed.

- [ ] **Step 3: Add blocked state and preflight role coverage**

In `src/slock_engine/models.py`, add:

```python
class CollaborationPlanStatus(str, Enum):
    ...
    BLOCKED = "blocked"
```

Add optional field on `CollaborationPlan`:

```python
missing_roles: list[str] = field(default_factory=list)
```

In `CollaborationOrchestrator.start_plan()` or the first dispatch phase, preflight required roles:

```python
required_roles = [step.role for step in plan.steps]
missing_roles = [
    role for role in dict.fromkeys(required_roles)
    if self._resolve_agent(role, channel_id) is None
]
if missing_roles:
    plan.status = CollaborationPlanStatus.BLOCKED
    plan.missing_roles = missing_roles
    logger.warning("Plan %s blocked: missing roles %s", plan.plan_id[:8], missing_roles)
    self._notify_overview(plan.plan_id)
    return
```

Do not mutate step statuses to `SKIPPED` in this case.

- [ ] **Step 4: Render blocked plan guidance**

In `src/slock_engine/card_templates/progress.py`, map `BLOCKED` to orange and show:

```text
缺少角色：planner, coder, reviewer, tester
可执行：/new-role planner 或配置 SLOCK_DEFAULT_ROLES 后重试
```

- [ ] **Step 5: Run collaboration tests**

Run:

```bash
uv run python -m pytest \
  tests/test_slock_collaboration_missing_roles.py \
  tests/test_slock_autonomous_collaboration.py \
  tests/test_slock_collaboration_insights.py \
  -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/slock_engine/collaboration_orchestrator.py src/slock_engine/models.py src/slock_engine/card_templates/progress.py tests/test_slock_collaboration_missing_roles.py
git commit -m "fix(slock): block plans with missing roles"
```

---

### Task 5: Add `/slock doctor` Diagnostics

**Files:**
- Create: `src/slock_engine/doctor.py`
- Modify: `src/feishu/handlers/slock.py`
- Test: `tests/test_slock_doctor.py`
- Docs: `README.md`

- [ ] **Step 1: Write failing diagnostics tests**

Create `tests/test_slock_doctor.py`:

```python
from src.slock_engine.doctor import diagnose_slock_team


def test_doctor_reports_missing_roles_and_stale_markers(tmp_path):
    result = diagnose_slock_team(
        storage_base_path=str(tmp_path / "slock"),
        channel_id="oc_real",
        configured_default_roles="planner:traex,coder:traex,reviewer:traex,tester:traex",
        existing_roles=["writer"],
        restored_chat_ids=["oc_real", "test_chat_006"],
        active_plan_missing_roles=["planner", "coder"],
    )

    assert result.ok is False
    assert "缺少角色" in result.summary
    assert "test_chat_006" in "\n".join(result.findings)
    assert any("/new-role planner" in action for action in result.actions)
```

- [ ] **Step 2: Implement read-only doctor model**

In `src/slock_engine/doctor.py`:

```python
from dataclasses import dataclass, field


@dataclass
class SlockDoctorResult:
    ok: bool
    summary: str
    findings: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)


def diagnose_slock_team(
    *,
    storage_base_path: str,
    channel_id: str,
    configured_default_roles: str,
    existing_roles: list[str],
    restored_chat_ids: list[str],
    active_plan_missing_roles: list[str],
) -> SlockDoctorResult:
    findings: list[str] = []
    actions: list[str] = []
    stale = [cid for cid in restored_chat_ids if not cid.startswith("oc_")]
    if stale:
        findings.append("发现非 Feishu 群 marker: " + ", ".join(stale))
        actions.append("运行维护清理或启用 restore 过滤后重启")
    required = [part.split(":", 1)[0] for part in configured_default_roles.split(",") if ":" in part]
    missing = [role for role in required if role and role not in existing_roles]
    missing.extend([role for role in active_plan_missing_roles if role not in missing])
    if missing:
        findings.append("缺少角色: " + ", ".join(missing))
        actions.append("执行 /new-role " + missing[0])
        actions.append("配置 SLOCK_DEFAULT_ROLES 后创建新团队或重新 bootstrap")
    ok = not findings
    return SlockDoctorResult(
        ok=ok,
        summary="Slock 状态正常" if ok else "Slock 需要处理后才能顺滑运行",
        findings=findings,
        actions=actions,
    )
```

- [ ] **Step 3: Add handler command**

In `src/feishu/handlers/slock.py`, route `/slock doctor` and `/team doctor` to a card that shows:

- storage path
- restored engine count
- agent count and role list for current team
- missing roles for pending/blocked plans
- next actions

Use only `markdown`, `hr`, `column_set`, `button`, and `collapsible_panel`.

- [ ] **Step 4: Run diagnostics tests**

Run:

```bash
uv run python -m pytest tests/test_slock_doctor.py tests/test_slock_cmd_panel_dispatch.py -q
```

Expected: tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/slock_engine/doctor.py src/feishu/handlers/slock.py tests/test_slock_doctor.py README.md
git commit -m "feat(slock): add runtime doctor diagnostics"
```

---

### Task 6: Verification, Memory, Restart, Push

**Files:**
- Modify: `.Memory/{YYYY-MM-DD}.md`
- Modify: `.Memory/Abstract.md`

- [ ] **Step 1: Run Slock focused tests**

Run:

```bash
uv run python -m pytest tests/test_slock*.py -q
```

Expected: all Slock tests pass.

- [ ] **Step 2: Run global checks**

Run:

```bash
uv run ruff check .
uv run python -m src.main --validate
git diff --check
```

Expected: all pass.

- [ ] **Step 3: Update Memory**

Add `.Memory/{YYYY-MM-DD}.md` entry with:

- schema V2 card fixes
- Traex default role bootstrap
- restore hygiene
- missing-role blocked state
- doctor command
- verification outputs

Add `.Memory/Abstract.md` summary line.

- [ ] **Step 4: Commit memory and final polish**

```bash
git add .Memory/Abstract.md .Memory/$(date +%F).md
git commit -m "docs(memory): record slock usability hardening"
```

- [ ] **Step 5: Restart service**

Run:

```bash
./restart.sh restart
./restart.sh status
tail -n 80 logs.log
```

Expected:

- one new GhostAP process is running
- Feishu long connection reconnects
- no new `not support tag`, `unsupported tag`, or restore of `test_chat_*` markers after restart

- [ ] **Step 6: Push**

Run:

```bash
git push origin slock
git status --short --branch
```

Expected: branch is synced with `origin/slock` and worktree is clean.

---

## Rollout Order

1. Task 1 is first because Feishu card rejection makes every other improvement invisible.
2. Task 2 is second because Slock currently has no usable default team composition and Traex migration is incomplete.
3. Task 3 is third because polluted restore state makes startup noisy and misleading.
4. Task 4 is fourth because it changes execution semantics and should be isolated after roles/defaults are fixed.
5. Task 5 is last because diagnostics should reflect the final state model.
6. Task 6 closes the loop with focused Slock tests, validation, restart, and push.

## Acceptance Criteria

- New Slock team can be created without manual role setup and gets `planner/coder/reviewer/tester` Traex-backed roles by default.
- Slock cards sent by core templates do not contain `action`, `note`, or `progress` tags.
- Startup no longer restores test/demo markers like `test_chat_006` by default.
- A task that requires missing roles is blocked with actionable guidance instead of silently failing every plan step.
- `/slock doctor` explains missing roles, stale markers, and next actions in one card.
- `uv run python -m pytest tests/test_slock*.py -q`, `uv run ruff check .`, `uv run python -m src.main --validate`, and `git diff --check` pass.
