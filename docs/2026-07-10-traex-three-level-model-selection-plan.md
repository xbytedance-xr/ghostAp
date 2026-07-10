# Traex Three-Level Model Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore Traex model/Profile/Effort selection and apply the selected Profile and Effort to real ACP sessions across all execution strategies.

**Architecture:** Keep one `ACPModelOption` per Traex model family and attach immutable, explicit selection variants for rendering. Build those variants from the live adapter whitelist plus Traex metadata, then preserve the composite selection until `SyncACPSession` resolves it into ordered `model` and `reasoning_effort` config writes.

**Tech Stack:** Python 3.11+, ACP SDK 0.11, dataclasses, Feishu CardKit JSON, pytest, uv.

## Global Constraints

- Use `uv`; never use pip or conda.
- Persist Traex choices as compatible `model/profile/effort` strings without changing `ProjectContext` schema.
- Only expose adapter-whitelisted models and adapter-accepted Profile/Effort combinations.
- Standard Profile writes the bare Traex `config_name`; max Profile writes metadata `max_key`.
- Startup and online switch fail-close if either ACP config write fails.
- Preserve Codex model/Effort behavior and all non-Traex provider behavior.
- Keep card dependency direction `handler -> session -> render` and `handler -> session -> delivery`.
- Update `ux/acp-model-cascade.html` before production card behavior.
- Update `.Memory/2026-07-10.md` and `.Memory/Abstract.md` after verification.
- Commit messages follow `docs/commit-message-guidelines.md`.

---

## File Structure

- `src/ttadk/models.py`: immutable generic ACP selection-variant DTO attached to `ACPModelOption`.
- `src/acp/traex_selection.py`: Traex metadata parsing, composite encoding/decoding, runtime selection resolution, and button-list expansion.
- `src/acp/helper.py`: live Traex capability probing plus shared cache invalidation/generation.
- `src/card/render/model_cascade.py`: render explicit variants before legacy suffix inference.
- `src/card/builders/system.py`: preserve explicit variant metadata and expand variants for button-only callers.
- `src/worktree_engine/tool_discovery.py`: carry explicit variants to Workflow and other strategy selectors.
- `src/feishu/handlers/worktree.py`, `src/feishu/handlers/spec.py`: expand structured variants before existing paginated button renderers.
- `src/feishu/handlers/workflow.py`: deep-copy explicit variants and key its short cache by shared generation.
- `src/feishu/handlers/system.py`: make the refresh action invalidate shared probe state.
- `src/acp/sync_adapter.py`: apply Traex model/Profile/Effort on startup and online switch.
- `src/acp/manager.py`, `src/agent_session/factory.py`, `src/feishu/handlers/programming.py`: preserve raw Traex selections until the sync-session boundary.
- `ux/acp-model-cascade.html`: reviewed preview of the restored Traex three-level card.
- `tests/test_traex_model_selection.py`: parser, metadata, resolver, and expansion contracts.
- `tests/test_acp_model_probe_timeout.py`: real-shape Traex config-option extraction and cache contracts.
- `tests/test_model_cascade.py`: explicit-variant cascade rendering and remembered defaults.
- `tests/test_switch_model.py`, `tests/test_acp_sync_adapter.py`, `tests/test_acp_model_normalization.py`: startup/live runtime application and fail-close behavior.
- `tests/test_worktree_tool_discovery.py`, `tests/test_workflow_model_selection.py`, `tests/test_model_command.py`: cross-surface propagation and refresh invalidation.

---

### Task 1: Traex selection types and metadata resolver

**Files:**
- Create: `src/acp/traex_selection.py`
- Modify: `src/ttadk/models.py`
- Create: `tests/test_traex_model_selection.py`

**Interfaces:**
- Produces: `ACPModelVariantOption(name, profile, effort, display_name, is_variant_default)`.
- Produces: `ACPModelOption.selection_variants`, an immutable tuple of `ACPModelVariantOption` values.
- Produces: `compose_traex_model_selection(model_id, profile, effort) -> str`.
- Produces: `split_traex_model_selection(value) -> tuple[str, str, Optional[str]]`.
- Produces: `resolve_traex_runtime_selection(value, metadata_path=None) -> TraexRuntimeSelection`.
- Produces: `expand_acp_model_options(models) -> list[ACPModelOption]`.

- [ ] **Step 1: Write failing parser and metadata tests**

```python
import json


def _traex_cache_model():
    levels = [{"effort": value} for value in ("low", "medium", "high", "max")]
    return {
        "slug": "Test-O-New-Thinking",
        "config_name": "c_o_new_thinking",
        "supported_reasoning_levels": levels,
        "business_metadata": {
            "variants": {
                "standard_key": "c_o_new_thinking__dev",
                "standard_supported_reasoning_levels": levels,
                "max_key": "c_o_new_thinking__max",
                "max_supported_reasoning_levels": levels,
            }
        },
    }


def _write_cache(tmp_path, *, model):
    path = tmp_path / "models_cache.json"
    path.write_text(json.dumps({"models": [model]}), encoding="utf-8")
    return path


def test_traex_selection_round_trips_explicit_profile_and_effort():
    value = compose_traex_model_selection("c_o_new_thinking", "max", "max")
    assert value == "c_o_new_thinking/max/max"
    assert split_traex_model_selection(value) == (
        "c_o_new_thinking", "max", "max"
    )


def test_traex_selection_keeps_legacy_suffix_semantics():
    assert split_traex_model_selection("c_o_new_thinking/high") == (
        "c_o_new_thinking", "standard", "high"
    )
    assert split_traex_model_selection("c_o_new_thinking/max") == (
        "c_o_new_thinking", "max", None
    )


def test_runtime_selection_maps_max_profile_to_hidden_backend_key(tmp_path):
    cache = _write_cache(tmp_path, model=_traex_cache_model())
    selection = resolve_traex_runtime_selection(
        "c_o_new_thinking/max/max", metadata_path=cache
    )
    assert selection.model_id == "c_o_new_thinking"
    assert selection.backend_model_value == "c_o_new_thinking__max"
    assert selection.profile == "max"
    assert selection.effort == "max"
```

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run python -m pytest tests/test_traex_model_selection.py -q`

Expected: collection/import failure because `traex_selection` and `ACPModelVariantOption` do not exist.

- [ ] **Step 3: Implement immutable DTOs and resolver**

```python
@dataclass(frozen=True)
class ACPModelVariantOption:
    name: str
    profile: str
    effort: str = "default"
    display_name: str = ""
    is_variant_default: bool = False


@dataclass(frozen=True)
class TraexRuntimeSelection:
    model_id: str
    backend_model_value: str
    profile: str
    effort: Optional[str]


def compose_traex_model_selection(
    model_id: str, profile: str, effort: Optional[str]
) -> str:
    model = str(model_id or "").strip()
    selected_profile = str(profile or "standard").strip().lower()
    selected_effort = str(effort or "").strip().lower()
    if selected_effort:
        return f"{model}/{selected_profile}/{selected_effort}"
    if selected_profile != "standard":
        return f"{model}/{selected_profile}"
    return model
```

Implement strict metadata parsing against `~/.trae/cli/models_cache.json`, match both `config_name` and `slug`, use bare `config_name` for standard, use `max_key` for max, and reject unsupported Profile/Effort with `ValueError`.

- [ ] **Step 4: Implement expansion for button-only callers**

```python
def expand_acp_model_options(models: list[ACPModelOption]) -> list[ACPModelOption]:
    expanded: list[ACPModelOption] = []
    for model in models:
        if not model.selection_variants:
            expanded.append(dataclasses.replace(model))
            continue
        for variant in model.selection_variants:
            expanded.append(ACPModelOption(
                name=variant.name,
                description=variant.display_name or model.description,
                is_default=bool(model.is_default and variant.is_variant_default),
            ))
    return expanded
```

- [ ] **Step 5: Run tests and verify GREEN**

Run: `uv run python -m pytest tests/test_traex_model_selection.py tests/test_acp_model_normalization.py -q`

Expected: all tests pass, including legacy Traex normalization tests.

- [ ] **Step 6: Commit**

```bash
git add src/acp/traex_selection.py src/ttadk/models.py tests/test_traex_model_selection.py
git commit -m "feat(acp): model Traex profile selections"
```

---

### Task 2: Live Traex capability discovery and cache invalidation

**Files:**
- Modify: `src/acp/helper.py`
- Modify: `tests/test_acp_model_probe_timeout.py`

**Interfaces:**
- Consumes: `ACPModelVariantOption`, Traex composition and metadata helpers from Task 1.
- Produces: `_extract_traex_model_capabilities(conn, resp, current_model, metadata_path=None)`.
- Produces: `invalidate_acp_model_cache(tool_name, cwd) -> None`.
- Produces: `get_acp_model_cache_generation(tool_name, cwd) -> int`.

- [ ] **Step 1: Write failing real-shape probe tests**

```python
from types import SimpleNamespace


def _select_root(config_id, current_value, values):
    root = SimpleNamespace(
        id=config_id,
        category="model" if config_id == "model" else "thought_level",
        current_value=current_value,
        options=[SimpleNamespace(name=value, value=value) for value in values],
    )
    return SimpleNamespace(root=root)


def _traex_response(*, session_id, current_model, efforts):
    return SimpleNamespace(
        session_id=session_id,
        config_options=[
            _select_root("model", current_model, ["c_o_new_thinking"]),
            _select_root("reasoning_effort", efforts[-1], efforts),
        ],
    )


class _FakeTraexConnection:
    def __init__(self):
        self.writes = []

    async def set_config_option(self, *, session_id, config_id, value):
        self.writes.append((config_id, value))
        efforts = ["low", "medium", "high", "max"]
        return _traex_response(
            session_id=session_id,
            current_model="c_o_new_thinking",
            efforts=efforts,
        )


def test_probe_traex_builds_profile_effort_variants(monkeypatch, tmp_path):
    fake_conn = _FakeTraexConnection()
    response = _traex_response(
        session_id="session-1",
        current_model="c_o_new_thinking",
        efforts=["low", "medium", "high", "max"],
    )
    cache_path = _write_cache(tmp_path, model=_traex_cache_model())
    models = asyncio.run(_extract_traex_model_capabilities(
        fake_conn,
        response,
        current_model=None,
        metadata_path=cache_path,
    ))
    target = next(model for model in models if model.name == "c_o_new_thinking")
    assert {variant.name for variant in target.selection_variants} >= {
        "c_o_new_thinking/standard/high",
        "c_o_new_thinking/max/max",
    }
    assert fake_conn.writes[:2] == [
        ("model", "c_o_new_thinking"),
        ("model", "c_o_new_thinking__max"),
    ]


def test_missing_traex_cache_only_exposes_verified_standard_profile(tmp_path):
    fake_conn = _FakeTraexConnection()
    response = _traex_response(
        session_id="session-1",
        current_model="c_o_new_thinking",
        efforts=["low", "medium", "high", "max"],
    )
    models = asyncio.run(_extract_traex_model_capabilities(
        fake_conn,
        response,
        current_model=None,
        metadata_path=tmp_path / "missing.json",
    ))
    target = next(model for model in models if model.name == "c_o_new_thinking")
    assert all(variant.profile == "standard" for variant in target.selection_variants)


def test_invalidate_probe_cache_increments_generation():
    before = get_acp_model_cache_generation("traex", "/repo")
    invalidate_acp_model_cache("traex", "/repo")
    assert get_acp_model_cache_generation("traex", "/repo") == before + 1
```

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run python -m pytest tests/test_acp_model_probe_timeout.py -k 'traex and (profile or generation or cache)' -q`

Expected: failures because Traex still uses `_extract_models_from_config_options()` and no generation API exists.

- [ ] **Step 3: Implement provider-specific extraction**

In `probe_acp_models()`, route Traex before the legacy model extraction:

```python
if tool_name == "traex":
    return await _extract_traex_model_capabilities(
        conn, resp, current_model=current_model
    )
```

For each adapter model, set the standard bare model and optional metadata `max_key`, read the returned `reasoning_effort` options, intersect non-empty cache capabilities with live capabilities, and build immutable explicit variants. Skip a rejected Profile instead of fabricating it.

- [ ] **Step 4: Implement generation-aware invalidation**

```python
_acp_probe_generation: dict[tuple[str, str], int] = {}


def invalidate_acp_model_cache(tool_name: str, cwd: Optional[str]) -> None:
    key = _probe_key(tool_name, cwd)
    with _acp_probe_cache_lock:
        _acp_probe_cache.pop(key, None)
        _acp_neg_cache.pop(key, None)
        _acp_probe_generation[key] = _acp_probe_generation.get(key, 0) + 1
```

Keep `_copy_models()` safe by storing selection variants as immutable tuples.

- [ ] **Step 5: Run tests and verify GREEN**

Run: `uv run python -m pytest tests/test_acp_model_probe_timeout.py tests/test_traex_model_selection.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/acp/helper.py tests/test_acp_model_probe_timeout.py
git commit -m "fix(acp): discover Traex profile efforts"
```

---

### Task 3: Restore the three-level card and cross-surface candidates

**Files:**
- Modify: `ux/acp-model-cascade.html`
- Modify: `src/card/render/model_cascade.py`
- Modify: `src/card/builders/system.py`
- Modify: `src/worktree_engine/tool_discovery.py`
- Modify: `src/feishu/handlers/worktree.py`
- Modify: `src/feishu/handlers/spec.py`
- Modify: `src/feishu/handlers/workflow.py`
- Modify: `src/feishu/handlers/system.py`
- Modify: `tests/test_model_cascade.py`
- Modify: `tests/test_worktree_tool_discovery.py`
- Modify: `tests/test_workflow_model_selection.py`
- Modify: `tests/test_model_command.py`

**Interfaces:**
- Consumes: explicit selection variants and expansion helper from Tasks 1–2.
- Produces: `build_model_groups()` entries with exact `name/profile/effort` from explicit variants.
- Produces: generation-aware Workflow cache entries.

- [ ] **Step 1: Update the HTML preview first**

Change the Traex preview to select `c_o_new_thinking`, Profile `max`, Effort `max`, show confirmation value `c_o_new_thinking/max/max`, and state that the backend applies `c_o_new_thinking__max + reasoning_effort=max`.

- [ ] **Step 2: Write failing cascade and propagation tests**

```python
def _explicit_traex_model_dict():
    return {
        "name": "c_o_new_thinking",
        "display_name": "Test-O-New-Thinking",
        "selection_variants": [
            {
                "name": "c_o_new_thinking/standard/high",
                "profile": "standard",
                "effort": "high",
                "is_variant_default": True,
            },
            {
                "name": "c_o_new_thinking/max/max",
                "profile": "max",
                "effort": "max",
                "is_variant_default": False,
            },
        ],
    }


def test_explicit_traex_variants_render_profile_and_effort_dropdowns():
    _, card_json = SystemBuilder.build_acp_model_cascade_card(
        [_explicit_traex_model_dict()], "traex", current_model=None
    )
    card = json.loads(card_json)
    by_action = {item["value"]["action"]: item for item in _walk_selects(card)}
    assert by_action[action_ids.SELECT_ACP_MODEL_PROFILE]["initial_option"] == "standard"
    assert "max" in [
        option["value"]
        for option in by_action[action_ids.SELECT_ACP_MODEL_PROFILE]["options"]
    ]
    assert "max" in [
        option["value"]
        for option in by_action[action_ids.SELECT_ACP_MODEL_EFFORT]["options"]
    ]
    assert "c_o_new_thinking/max/max" in card_json


def test_workflow_cache_preserves_explicit_selection_variants(monkeypatch):
    handler = WorkflowHandler.__new__(WorkflowHandler)
    handler._workflow_model_cache = {}
    monkeypatch.setattr(
        WorktreeToolDiscovery,
        "get_models_for_tool",
        lambda *args, **kwargs: [_explicit_traex_model_dict()],
    )
    models = handler._get_workflow_models_for_tool("traex", "/repo")
    assert models[0]["selection_variants"][0]["profile"] == "standard"


def test_refresh_invalidates_shared_probe_cache(monkeypatch):
    project = SimpleNamespace(project_id="project-1", root_path="/repo")
    context = MagicMock()
    context.project_manager.get_project_for_chat.return_value = project
    handler = SystemHandler(context)
    handler._show_acp_model_selection_flow = MagicMock()
    invalidate = MagicMock()
    monkeypatch.setattr("src.feishu.handlers.system.invalidate_acp_model_cache", invalidate)
    handler.handle_refresh_acp_models(
        "message-1",
        "chat-1",
        "traex",
        "project-1",
        value={"tool_name": "traex"},
    )
    invalidate.assert_called_once_with("traex", "/repo")
```

- [ ] **Step 3: Run tests and verify RED**

Run: `uv run python -m pytest tests/test_model_cascade.py tests/test_worktree_tool_discovery.py tests/test_workflow_model_selection.py tests/test_model_command.py -q`

Expected: explicit variants are ignored, strategy selectors only contain the base model, and refresh does not invalidate cache.

- [ ] **Step 4: Implement explicit rendering and button expansion**

Make `build_model_groups()` prefer `selection_variants`:

```python
explicit = list(model.get("selection_variants") or [])
if explicit:
    for variant in explicit:
        groups[name]["variants"].append({
            "name": variant["name"],
            "display_name": variant.get("display_name") or display,
            "profile": variant["profile"],
            "effort": variant.get("effort") or "default",
            "tokens": (),
            "is_variant_default": bool(variant.get("is_variant_default")),
            "is_default": bool(model.get("is_default"))
                and bool(variant.get("is_variant_default")),
        })
    continue
```

Serialize explicit variants in `SystemBuilder`, preserve them through Worktree discovery and Workflow copies, and call `expand_acp_model_options()` only for button-only Slock/Worktree/Spec render paths.

- [ ] **Step 5: Implement cache invalidation wiring**

`handle_refresh_acp_models()` invalidates only a real refresh action; pagination payloads containing `model_page` only repaint. Workflow cache entries store the shared generation and are discarded on mismatch.

- [ ] **Step 6: Run tests and verify GREEN**

Run: `uv run python -m pytest tests/test_model_cascade.py tests/test_worktree_tool_discovery.py tests/test_workflow_model_selection.py tests/test_model_command.py tests/test_worktree_selection_flow.py tests/test_spec_review_agent_selection.py -q`

Expected: all tests pass and existing CardKit node-budget assertions remain green.

- [ ] **Step 7: Commit**

```bash
git add ux/acp-model-cascade.html src/card/render/model_cascade.py src/card/builders/system.py src/worktree_engine/tool_discovery.py src/feishu/handlers/worktree.py src/feishu/handlers/spec.py src/feishu/handlers/workflow.py src/feishu/handlers/system.py tests/test_model_cascade.py tests/test_worktree_tool_discovery.py tests/test_workflow_model_selection.py tests/test_model_command.py
git commit -m "fix(card): restore Traex three-level selection"
```

---

### Task 4: Apply Traex Profile and Effort at the ACP boundary

**Files:**
- Modify: `src/acp/sync_adapter.py`
- Modify: `src/acp/manager.py`
- Modify: `src/agent_session/factory.py`
- Modify: `src/feishu/handlers/programming.py`
- Modify: `tests/test_switch_model.py`
- Modify: `tests/test_acp_sync_adapter.py`
- Modify: `tests/test_acp_model_normalization.py`

**Interfaces:**
- Consumes: `resolve_traex_runtime_selection()` from Task 1.
- Produces: `SyncACPSession._apply_traex_selection(selection) -> bool`.
- Preserves: `SyncACPSession._model_name` as the raw persisted selection.

- [ ] **Step 1: Write failing startup and live-switch tests**

```python
def test_traex_set_model_applies_profile_then_effort():
    session = SyncACPSession.__new__(SyncACPSession)
    session._agent_type = "traex"
    session._agent_args = ["acp", "serve"]
    session._model_name = None
    session._acp_session = MagicMock()
    session._acp_session.set_config_option = AsyncMock(return_value=True)
    session._loop = MagicMock()
    with patch(
        "src.acp.sync_adapter.resolve_traex_runtime_selection",
        return_value=TraexRuntimeSelection(
            model_id="c_o_new_thinking",
            backend_model_value="c_o_new_thinking__max",
            profile="max",
            effort="max",
        ),
    ):
        assert session.set_model("c_o_new_thinking/max/max") is True
    assert session._acp_session.set_config_option.await_args_list == [
        call("model", "c_o_new_thinking__max"),
        call("reasoning_effort", "max"),
    ]
    assert session._model_name == "c_o_new_thinking/max/max"


async def test_traex_startup_fails_closed_when_effort_is_rejected():
    session = SyncACPSession.__new__(SyncACPSession)
    session._agent_type = "traex"
    session._agent_cmd = "traex"
    session._agent_args = ["acp", "serve"]
    session._cwd = "/repo"
    session._model_name = "c_o_new_thinking/max/max"
    fake_acp = MagicMock()
    fake_acp.start = AsyncMock(return_value="session-1")
    fake_acp.close = AsyncMock()
    with patch("src.acp.sync_adapter.ACPSession", return_value=fake_acp), patch.object(
        session, "_apply_traex_selection", AsyncMock(return_value=False)
    ):
        with pytest.raises(RuntimeError, match="Traex ACP rejected"):
            await session._start_session()
    fake_acp.close.assert_awaited_once()


def test_engine_factory_preserves_raw_traex_selection():
    with patch("src.acp.sync_adapter.start_session_with_retry") as start:
        start.return_value = MagicMock()
        create_engine_session(
            "traex", "/repo", model_name="c_o_new_thinking/max/max"
        )
    assert start.call_args.kwargs["agent_type"] == "traex"
    assert start.call_args.kwargs["cwd"] == "/repo"
    assert start.call_args.kwargs["model_name"] == "c_o_new_thinking/max/max"
```

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run python -m pytest tests/test_switch_model.py tests/test_acp_sync_adapter.py tests/test_acp_model_normalization.py -k 'traex or codex' -q`

Expected: Traex calls only `set_model()` with the stripped slug and factories pass `Test-O-New-Thinking` instead of the composite value.

- [ ] **Step 3: Implement one ordered application path**

```python
async def _apply_traex_selection(self, selection: str) -> bool:
    if not self._acp_session:
        return False
    resolved = resolve_traex_runtime_selection(selection)
    if not await self._acp_session.set_config_option(
        "model", resolved.backend_model_value
    ):
        return False
    if resolved.effort is None:
        return True
    return bool(await self._acp_session.set_config_option(
        "reasoning_effort", resolved.effort
    ))
```

Call this after `new_session` and from `set_model()`. Close the ACP session and raise on startup failure. Keep Codex on its existing official-adapter path.

- [ ] **Step 4: Preserve raw Traex selections upstream**

Skip early normalization for `agent == "traex"` in manager and factory helpers. In `ProgrammingModeHandler.switch_model()`, pass the raw Traex selection to `SyncACPSession.set_model()` while retaining existing normalization for other providers. `resolve_agent_spec()` continues to normalize only the launch command.

- [ ] **Step 5: Run tests and verify GREEN**

Run: `uv run python -m pytest tests/test_switch_model.py tests/test_acp_sync_adapter.py tests/test_acp_model_normalization.py tests/test_handlers.py -q`

Expected: all tests pass; Codex ordered config writes remain unchanged.

- [ ] **Step 6: Commit**

```bash
git add src/acp/sync_adapter.py src/acp/manager.py src/agent_session/factory.py src/feishu/handlers/programming.py tests/test_switch_model.py tests/test_acp_sync_adapter.py tests/test_acp_model_normalization.py
git commit -m "fix(acp): apply Traex profile and effort"
```

---

### Task 5: Review, full verification, memory, and dev push

**Files:**
- Modify: `.Memory/2026-07-10.md`
- Modify: `.Memory/Abstract.md`
- Modify as required by review: only files already listed in Tasks 1–4.

**Interfaces:**
- Consumes: completed implementation and all regression tests.
- Produces: verified commit history on `dev` and updated project memory.

- [ ] **Step 1: Run targeted regression groups**

```bash
uv run python -m pytest \
  tests/test_traex_model_selection.py \
  tests/test_acp_model_probe_timeout.py \
  tests/test_model_cascade.py \
  tests/test_switch_model.py \
  tests/test_acp_sync_adapter.py \
  tests/test_acp_model_normalization.py \
  tests/test_worktree_tool_discovery.py \
  tests/test_workflow_model_selection.py \
  tests/test_worktree_selection_flow.py \
  tests/test_spec_review_agent_selection.py \
  tests/test_model_command.py -q
```

Expected: all targeted tests pass.

- [ ] **Step 2: Review the implementation against the approved spec**

Check that adapter whitelist, Profile mapping, Effort intersection, legacy parsing, cache invalidation, session startup, live switch, cross-mode propagation, and fail-close behavior each have a passing test. Remove unrelated refactors.

- [ ] **Step 3: Run static and configuration verification**

```bash
uv run ruff check src tests
uv run python -m src.main --validate
git diff --check
```

Expected: ruff reports `All checks passed`, validation succeeds, and diff check is silent.

- [ ] **Step 4: Run the full test suite**

Run: `uv run python -m pytest tests/ -q`

Expected: zero failures.

- [ ] **Step 5: Run a real Traex smoke**

Use one temporary ACP session, select `c_o_new_thinking/max/max`, execute the prompt `Reply exactly OK. Do not call tools.`, then inspect only the generated session metadata fields.

Expected:

```text
model = Test-O-New-Thinking
model_backend_variant = max
reasoning_effort = max
stop_reason = end_turn
```

- [ ] **Step 6: Update project memory**

Append a detailed section to `.Memory/2026-07-10.md` covering root cause, implementation, grill decisions, targeted/full test counts, real smoke evidence, and residual dependency on Traex metadata. Add a roughly 20-character summary line with the date reference to `.Memory/Abstract.md`.

- [ ] **Step 7: Commit memory and any review fixes**

```bash
git add .Memory/2026-07-10.md .Memory/Abstract.md
git commit -m "docs(memory): record Traex selection restoration"
```

- [ ] **Step 8: Confirm dev state and push**

```bash
git status --short
git branch --show-current
git log --oneline --decorate -6
git push origin dev
```

Expected: worktree clean, branch is `dev`, and push updates `origin/dev` without force.
