# AGENTS.md

This is the compact harness guide for AI coding agents working in GhostAP.
Keep it short: this file is startup context, not project documentation. Add a
rule here only when it prevents a known failure or points agents to the right
tooling faster.

## Project In One Paragraph

GhostAP is a Feishu/Lark bot service for safe remote shell execution and
AI-assisted development over outbound WebSocket connections. Users can run shell
commands, manage projects, and drive programming tools such as Coco, Claude,
Aiden, Codex, Gemini, and TTADK through chat.

## Commands

Use `uv` only; never use pip/conda in this repo.

```bash
uv sync --group dev
uv run python -m src.main
uv run python -m src.main --validate
uv run python -m pytest tests/ -q
uv run python -m pytest tests/test_acp_client.py -q
```

For focused changes, run the narrowest relevant test first, then broaden if the
change touches shared routing, card rendering, locks, config, or session code.

## Working Rules

- Read `.Memory/Abstract.md` before changing behavior; it is the project-local
  index of recent decisions and pitfalls.
- Check existing patterns with `rg` before editing an established module.
- Keep changes scoped. Do not refactor unrelated code while fixing a local issue.
- All feature and bug-fix changes need tests. Prefer targeted regression tests
  for the touched contract.
- Secrets and environment-specific values come from `.env` via `src/config/`.
  Never hardcode credentials or tokens.
- Tests, probes, and temporary helpers belong under `tests/`, `scripts/`, `ux/`,
  or `/tmp`; keep the repo root clean.
- After completing a meaningful task, update `.Memory/{YYYY-MM-DD}.md` with
  detailed entries: what changed, why, validation, and any follow-up risk.
  Also append a one-line summary (~20 chars) to `.Memory/Abstract.md` with the
  date reference so readers can locate the full record in the day file.
- Medium/Low audit findings go to `.Memory/Backlog.md`; High correctness or
  security findings should be fixed immediately. Remove backlog items when fixed.
- Commit messages must follow `docs/commit-message-guidelines.md`.

## Harness Principles

Use this file like a harness, not a wiki:

- Put durable rules here; put history and evidence in `.Memory/`.
- Prefer specific failure-derived rules over generic advice.
- If a rule can be enforced by tests, hooks, or typed APIs, enforce it there and
  keep only the short pointer here.
- Delete stale rules when the codebase or tooling no longer needs them.
- Treat Coco/Claude/Aiden/Codex/Gemini/TTADK as tool backends behind GhostAP's
  programming abstractions. Avoid adding backend-specific branches unless the
  transport or capability really differs.
- Bot admin bootstrap is intentionally one-way: `/setadmin` is accepted from
  anyone only while `ADMIN_USER_IDS` is empty; afterward only the configured
  admin may replace the single admin in `.env`.
- Worktree mode should produce directly usable code without manual conflict
  repair. Merge conflicts created by WT output are resolved automatically in
  favor of the WT branch, and the card must disclose that impact so users can
  decide whether to start an extra repair task.

## Architecture Pointers

Start from these modules instead of reading the whole tree:

- `src/feishu/ws_client.py` and `src/feishu/dispatcher.py`: WebSocket ingress,
  message routing, and interaction-mode dispatch.
- `src/feishu/handlers/`: command handlers. Use `BaseHandler` messaging helpers:
  `reply_text`, `reply_card`, `update_card`, `send_card_to_chat`,
  `send_text_to_chat`.
- `src/mode/`: `InteractionMode` and per-chat/project mode state.
- `src/acp/`: ACP sessions, providers, diagnostics, and model/tool discovery for
  ACP-capable programming tools.
- `src/ttadk/`: TTADK tool/model selection and startup strategy. `ttadk_*`
  agent types are CLI-bridge only; do not start ACP directly for them.
- `src/deep_engine/`, `src/spec_engine/`, `src/worktree_engine/`: long-running
  execution strategies.
- `src/card/`: Feishu card builders, render pipeline, session orchestration, and
  delivery.
- `src/project/`, `src/project_chat/`, `src/thread/`: project context, project
  chat bindings, and thread context.
- `src/chat_lock.py`, `src/repo_lock.py`, `src/utils/lock_order.py`: chat/repo
  locking and lock-order enforcement.
- `src/config/`: pydantic settings package and config validation.

## Strategy And Transport

GhostAP has two independent axes:

- Execution strategies: Normal programming, Deep, Spec, and Worktree.
- Tool transport: ACP direct mode, shell CLI bridge mode, and TTADK CLI bridge.

Keep these axes separate. A new programming feature should usually work across
Coco, Claude, Aiden, Codex, Gemini, and TTADK unless the user explicitly scopes
it down or the backend cannot support it.

State scoping is also a product contract:

- SMART is the default chat/project state and may route simple intents or
  shell-like commands directly.
- Normal tool entries such as `/coco`, `/codex`, `/aiden`, `/claude`, `/gemini`,
  and `/ttadk` set a persistent chat+project programming state until `/exit`.
- Deep, Spec, and Worktree are engine strategies scoped to the Feishu
  topic/root thread; they must not replace chat+project programming state.
- Shell-like text in SMART must remain shell execution, including commands such
  as `./restart.sh rr`, instead of being stolen by project-chat free text
  programming routing.

## Card And UI Rules

- Programming cards follow one task per card. Show the overall task list and the
  current active task at the top.
- Subagents get separate task cards and keep updating their own messages.
- When a card exceeds limits, create a continuation card and preserve the
  previous card content.
- Avoid empty tool/detail blocks.
- For UI design changes, create or update an HTML preview under `ux/` before
  implementation, then align production code to the reviewed preview.
- Respect card layering: handlers use session/protocol APIs; session orchestrates
  render and delivery; render stays pure; delivery does not import session.

## Import Boundaries

The card pipeline has a strict one-way dependency direction:

```text
handler -> session -> render
                  -> delivery
```

- `render` must not import `delivery`.
- `delivery` must not import `session`.
- Handlers should depend on protocols and facades, not concrete renderer internals.
- Shared cross-layer types belong in `src/card/protocols.py` or
  `src/card/events/`.
- Use `TYPE_CHECKING` or local lazy imports only when they preserve this direction.

## Current Gotchas

- `CardBuilder.build_engine_card()` is removed. Static cards use
  `build_info_card()`; engine/progress cards go through the `CardSession`
  pipeline.
- Spec persists context through `SpecManager.persist_result`; Deep uses
  `ContextPersistenceHook`; Worktree persists through its reporter path.
- `ACPSessionManager` owns session-key parsing and locking. Do not hand-parse
  session keys in business code.
- Feishu card JSON is strict. If a schema error appears in `logs.log`, fix the
  emitted structure and add a regression test around the builder or renderer.
- For restart/startup issues, inspect `logs.log` and `[RESTART]` markers before
  changing app code; separate script latency from Python cold start.
