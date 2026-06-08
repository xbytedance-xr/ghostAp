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
- `src/deep_engine/`, `src/spec_engine/`, `src/worktree_engine/`,
  `src/workflow_engine/`: long-running execution strategies.
  - `src/workflow_engine/`: JS-orchestrated multi-agent parallel execution.
    Key modules: `commands.py` (SSOT command set), `engine.py` (bridge + runtime),
    `executor.py` (per-agent-call session), `tool_registry.py` (dynamic discovery),
    `script_gen.py` (prompt template + validation), `renderer.py` (Feishu card).
    Requires Node.js >= `NODE_MIN_VERSION` (defined in `src/workflow_engine/constants.py`);
    all user-facing "Node too old" messages derive from that constant.
- `src/card/`: Feishu card builders, render pipeline, session orchestration, and
  delivery.
- `src/project/`, `src/project_chat/`, `src/thread/`: project context, project
  chat bindings, and thread context.
- `src/chat_lock.py`, `src/repo_lock.py`, `src/utils/lock_order.py`: chat/repo
  locking and lock-order enforcement.
- `src/config/`: pydantic settings package and config validation.
- `src/slock_engine/activation_guard.py`: permission check and rate limiting guard for
  passive auto-activation.
- `src/slock_engine/autonomous_resolver.py`: autonomous resolver for uncertain intents.
- `src/slock_engine/role_bootstrap.py`: automatic role creation bootstrap when creating
  new slock groups.
- `src/slock_engine/task_classifier.py`: message classifier (task/chat/uncertain).
- `src/slock_engine/task_queue.py`: task queue management.
- `src/slock_engine/safe_error.py`: safe error message utility (re-export from
  `src/utils/errors`).

## Strategy And Transport

GhostAP has two independent axes:

- Execution strategies: Normal programming, Deep, Spec, Worktree, and Workflow.
- Tool transport: ACP direct mode, shell CLI bridge mode, and TTADK CLI bridge.

Keep these axes separate. A new programming feature should usually work across
Coco, Claude, Aiden, Codex, Gemini, and TTADK unless the user explicitly scopes
it down or the backend cannot support it.

State scoping is also a product contract:

- SMART is the default chat/project state and may route simple intents or
  shell-like commands directly.
- Normal tool entries such as `/coco`, `/codex`, `/aiden`, `/claude`, `/gemini`,
  and `/ttadk` set a persistent chat+project programming state until `/exit`.
- Deep, Spec, Worktree, and Workflow are engine strategies scoped to the Feishu
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

## Workflow Mode (`/wf`)

The `WorkflowHandler` owns the `/wf` command, which lets users describe a
multi-step task in natural language and have an orchestrator agent generate
and execute a Node.js workflow script. The flow is:

1. **Send a requirement** — `/wf <need-to-add-cli-parsing>` or `/wf` followed
   by free-form text. The requirement surfaces in all subsequent cards.
2. **Step 1 — Orchestrator agent selection** — pick a tool + model combo that
   will drive script generation. The combined card lets you expand a tool to
   reveal its model panel, or click "+ 添加 <tool>" directly to use its
   default model. Multi-selection is not needed here: the orchestrator is a
   single chosen agent.
3. **Step 2 — Review agent selection** — same combined card. You may pick one
   or more tool + model combos to act as independent reviewers, or click the
   **Auto** shortcut to skip independent review and have the orchestrator
   self-review. Skipping is useful for low-risk changes and avoids the cost
   of extra agent calls.
4. **Script generation & confirmation** — once both steps are non-empty (or
   Auto toggled on step 2), the engine builds a JS workflow via
   `src/workflow_engine/script_gen.py`, validates the output (meta export,
   balanced brackets, at least one `agent()`/`workflow()` call, no forbidden
   `require('fs'|child_process|net|...)` escapes), and shows a confirmation
   card listing phases, tools, and a short preview before execution.
5. **Execution & progress** — after user confirmation, the JS runtime runs
   the script, streaming phase and per-agent progress through
   `WorkflowProgressRenderer`.

Error handling:

- Empty selection on either step surfaces an inline error in the card; the
  user picks at least one tool / model and retries.
- Scripts that fail validation are rejected with a structured error list
  (missing meta, unsafe patterns, etc.) — the user regenerates from the
  confirmation card.
- Running workflows block new `/wf` invocations and must be stopped with
  `/stop_wf` or the cancel button on the progress card.

Command quick reference:

| Command            | Purpose                                          |
| ------------------ | ------------------------------------------------ |
| `/wf <desc>`       | Start a new workflow from a requirement          |
| `/wf <template>`   | (Optional) launch from a saved template name     |
| `/stop_wf`         | Abort the currently running workflow             |
| `/wf_status`       | Show active workflow progress and selected tools |
| `/wf_help`         | In-chat help text                                |

### Detailed Usage Guide

#### Two-Step Selection Flow
The workflow uses a combined card interface for both orchestrator and review
selection steps:

- **Orchestrator Step (Step 1)**: Select exactly one tool + model combination
  that will generate the workflow script. Use the stepper indicator at the top
  to track progress (current=1).

- **Review Step (Step 2)**: Select one or more tool + model combinations to
  review the generated script, or use the **Auto** button to skip independent
  review. The stepper indicator shows current=2.

#### Combined Card Features
- **Tool + Model Inline Expansion**: Click on any tool to expand and view its
  available models inline without navigating to a separate card.
- **Stepper Indicator**: Shows current step (1/2) and overall progress.
- **Auto Option**: In review step, skips independent review and uses the
  orchestrator agent for self-review.
- **Remove/Clear Buttons**: Remove individual selections or clear all selections
  with a single click.
- **Empty Selection Validation**: Prevents proceeding with empty selections by
  showing inline error messages.

#### Skipping Review
Use the **Auto** button in the review step to skip independent review when:
- Making low-risk changes (e.g., minor bug fixes, documentation updates)
- Working in a rapid prototyping mode
- Trusting the orchestrator agent's self-review capabilities

#### Script Generation & Confirmation
- **Dynamic Role Assignment**: Roles (Orchestrator/Reviewer) are dynamically
  inferred from the task description by the LLM, not statically selected by
  the user.
- **Script Preview**: The confirmation card shows a preview of the generated
  workflow script with key details:
  - Orchestrator tool/model
  - Reviewer tools/models (or "Auto" if review was skipped)
  - Phase breakdown
  - Estimated token usage
- **Execution Control**: Confirm to execute the script, or regenerate if changes
  are needed.

#### Agent() Call Execution
When the workflow runs `agent()` calls:
- Each agent call uses the selected tool/model combination
- Review agents provide feedback on the orchestrator's work
- The final output combines all agent results into a cohesive deliverable
- Progress is streamed in real-time through the workflow progress card
