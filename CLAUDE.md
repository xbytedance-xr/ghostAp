# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

GhostAP is a Feishu (Lark) chatbot service that provides safe remote Shell command execution and AI-assisted development via WebSocket long-connection. Users interact through Feishu chat to execute shell commands, use Coco/Claude/Aiden/Codex/Gemini/TTADK for remote development, and manage multiple projects. No public IP or domain is needed — the bot connects outbound via WebSocket.

### Programming Strategy Layers

GhostAP has two orthogonal dimensions:

- **Execution strategy layer (3 levels, complexity increasing)**
  - **Normal programming mode**: multi-turn coding dialogue mode
  - **Deep mode**: one-shot deep execution (single requirement → autonomous plan+execute)
  - **Spec mode**: structured iterative methodology (`Spec → Plan → Task → Build → Review`)
- **Tool transport layer (how tools are actually driven)**
  - **ACP direct mode**: tools with ACP support are connected through ACP (JSON-RPC 2.0 over stdio)
  - **Shell CLI mode**: tools without ACP are executed via CLI subprocess bridge
  - **TTADK bridge mode**: TTADK-wrapped tools (including Claude/Codex in TTADK context) are forced to CLI bridge and **must not** directly start ACP server under `ttadk_*` agent types

## Commands

```bash
# Install dependencies
uv sync --group dev

# Run the service
uv run python -m src.main

# Run all tests
uv run python -m pytest tests/ -v

# Run a single test file
uv run python -m pytest tests/test_acp_client.py -v

# Run a single test
uv run python -m pytest tests/test_acp_client.py::TestGhostAPClient::test_request_permission_auto_approve -v
```

**Package manager**: Always use `uv`, never pip/conda.

## Architecture

### Message Flow

```
Feishu WebSocket message
  → FeishuWSClient._handle_message (validation, dedup, expiry check)
    → ChatLockGate (lock check: readonly commands pass, others blocked if locked)
      → TaskScheduler.submit (per-chat ordered, concurrency-limited)
        → IntentRecognizer (ReAct-based LLM intent classification)
          → Route by intent:
              SHELL_COMMAND → SandboxExecutor (safety check → execute)
              ENTER_COCO    → ACPSessionManager("coco") (multi-turn AI dev session via ACP)
              ENTER_CLAUDE  → ACPSessionManager("claude") (Claude CLI session backend)
              ENTER_TTADK   → TTADKManager + ACPSessionManager("ttadk") (CLI bridge only)
              DEEP_COMMAND  → DeepEngine (ACP-driven single-prompt deep execution)
              SPEC_COMMAND  → SpecEngine (structured iterative execution)
              WORKTREE_CMD  → WorktreeEngine (parallel multi-tool worktree execution)
              CREATE_PROJECT → ProjectManager
              ...
        → Renderer layer (BaseRenderer → DeepRenderer / SpecRenderer)
        → ACPEventRenderer (structured tool/plan/text rendering)
        → CardBuilder / CardDelivery (build and deliver response card)
        → Feishu API reply + EmojiReaction feedback
```

### Key Modules

- **`src/acp/`** — ACP (Agent Client Protocol) layer. Provides structured communication for ACP-capable agents via JSON-RPC 2.0 over stdio, plus session routing/degrade diagnostics, provider registry, and telemetry. Includes:
  - `models.py` — `ACPEvent`, `ACPEventType`, `ToolCallInfo`, `PlanInfo`, `ACPSessionState`, `PromptResult`
  - `client.py` — `GhostAPClient` implements ACP `Client` interface, converts raw session updates to `ACPEvent` objects
  - `session.py` — `ACPSession` manages async ACP connection lifecycle (start/prompt/cancel/close)
  - `sync_adapter.py` — `SyncACPSession` wraps async ACPSession for use from synchronous threads
  - `manager.py` — `ACPSessionManager` manages per-chat ACP sessions with timeout/cleanup
  - `renderer.py` — `ACPEventRenderer` converts ACP events to Feishu Markdown (tool status, plan checklist, text accumulation)
  - `provider.py` — `ACPProvider` protocol + `ToolRegistry` with availability caching and background preheating
  - `providers/` — Provider implementations with availability checking, LRU-cached help blob loading
  - `session_factory.py` — Session creation factory
  - `diagnostics.py` — Session diagnostics
  - `telemetry.py` — Telemetry collection
- **`src/ttadk/`** — TTADK (Multi-Tool AI Development Kit) layer. Manages multi-tool AI programming sessions with startup strategies. Includes:
  - `models.py` — `TTADKTool`, `TTADKModel` data models for tool/model definitions
  - `manager.py` — `TTADKManager` singleton for managing current tool/model state
  - `startup.py` — Startup orchestration
  - `strategies/` — Startup strategies (official_cli, interactive, local_config, probe)
  - `cache.py` — Cache management
  - `model_fetcher.py` — Model list fetching
  - `env_sandbox.py` — Environment isolation sandbox
- **`src/feishu/`** — Feishu integration hub. WebSocket client, 12 handlers, renderer layer, control plane, routing, user cache, lock gate. Key files:
  - `ws_client.py` — Core hub. Handles WebSocket events, routes messages to handlers.
  - `ws_health.py` — `WSHealthMonitor` watchdog for WebSocket connection health.
  - `control_plane.py` — `ControlPlane` for pending exit handling, event queue, system command gate.
  - `router.py` — `FORWARDING_MAP` dispatch table routing methods to handler registries.
  - `handlers/` — 11 handler modules: `base`, `engine_base`, `programming`, `deep`, `spec`, `worktree`, `project`, `system`, `diagnostics`, `lock_helper`, `diagnostics_helper`.
  - `renderers/` — Renderer layer: `BaseRenderer`, `DeepRenderer`, `SpecRenderer` (with retry callback support).
  - `user_cache.py` — LRU cache (500 capacity, 1h TTL) for Feishu user display name resolution.
  - `chat_lock_gate.py` — Lock interception gate.
  - `session_hub.py` — Session hub.
- **`src/deep_engine/`** — ACP-driven deep execution engine. Single prompt with full requirement → agent self-plans and executes. Tracks progress via ACP plan updates and tool calls.
- **`src/spec_engine/`** — Structured iterative engine (`Spec→Plan→Task→Build→Review`) with 26 modules. Review subsystem decomposed into: `ReviewOrchestrator` (orchestration), `review_pipeline` (parallel assembly), `review_strategy` (strategy pattern), `review_retry` (in-cycle retry), `review_parsing` (LLM output parsing), `review_types` (shared types), `perspective_worker` (single-perspective workers), `cycle_budget` (wall-clock cap). Structured retry via `RetryStatus`/`RetryEvent`. UI text constants in `constants.py`. State persistence in `persistence.py`.
- **`src/worktree_engine/`** — Git worktree-based parallel multi-tool execution engine. Manages worktree creation, tool discovery, selection control, and dispatching.
- **`src/chat_lock.py`** — Chat-level lock manager with `ChatLockCode` enum for structured result codes. Defines `READONLY_COMMANDS` and `SAFE_INTERRUPT_COMMANDS`.
- **`src/repo_lock.py`** — Repo-level mutex with `SimpleEvent` (multi-subscriber observer), reentrant acquire, P2P privilege bypass, idle-timeout auto-release.
- **`src/engine_base.py`** — Base engine with `EngineRunState` enum, `ReviewPerspective` enum (with `register_display_names()` DI), ordered lock integration.
- **`src/card/`** — Card rendering with theme system (18 themes), `UnifiedCardLayout` builder, 12 card builder modules, truncation strategies, flow control. `styles.py` merges `SPEC_UI_TEXT` + `LOCK_UI_TEXT` into global `UI_TEXT`.
- **`src/project/`** — Multi-project workspace: `ProjectManager`, `ProjectContext`, `UnifiedContext` (cross-mode bridging with version snapshots), `MessageProjectMapper`.
- **`src/coco_model/`** — `CocoModelManager` with model cache (5min TTL), YAML config reader.
- **`src/thread/`** — `ThreadContextManager` with TTL-based eviction, alias support, background cleanup.
- **`src/tasking/`** — `TaskScheduler` (per-chat ordered execution, global concurrency) + `ServiceRegistry` DI container (singleton/transient/factory, scoped registries).
- **`src/utils/`** — Infrastructure layer (30 modules): `circuit_breaker` (CLOSED/OPEN/HALF_OPEN), `gc_monitor` (psutil/gc memory monitoring), `hooks` (7 event types), `lock_order` (6-level hierarchy with runtime violation detection), `rate_limit` (token bucket), `registry` (DI container), `shutdown` (graceful shutdown + signal handling), `cleanup` (async cleanup registry), and more.
- **`src/config.py`** — `Settings` singleton via `get_settings()`, backed by pydantic-settings + `.env`. Supports `--validate` pre-check mode.

### Design Patterns

- **Strategy layering**: user-facing development workflow is split into 3 execution strategies (Normal/Deep/Spec), independent from backend tool transport.
- **Hybrid transport (ACP + CLI)**: ACP-capable tools use ACP direct mode; non-ACP tools use Shell CLI bridge; `ttadk_*` is strictly CLI-bridged and isolated from ACP direct startup.
- **TTADK multi-tool layer**: TTADK provides a unified interface for multiple AI tools (Coco/Claude/Cursor/Gemini/etc) and models, with `TTADKManager` singleton managing tool/model switching and state.
- **Event-driven rendering**: `ACPEventRenderer` processes `ACPEvent` stream (text chunks, tool calls, plan updates) into Feishu Markdown. Handlers register `on_event` callbacks to drive real-time card updates.
- **Renderer hierarchy**: `BaseRenderer` → `DeepRenderer` / `SpecRenderer` encapsulate engine-specific rendering logic.
- **State machine**: `InteractionMode` enum governs SMART/COCO/CLAUDE/AIDEN/CODEX/GEMINI/SHELL/TTADK transitions; `EngineRunState` enum tracks IDLE/RUNNING/STOPPING for engines.
- **Singleton**: `get_settings()` for configuration, `get_ttadk_manager()` for TTADK tool/model management
- **Session managers**: `ACPSessionManager` unifies per-chat/project session lifecycle across ACP and CLI backends (`coco/aiden/gemini/codex` ACP-direct, `claude` CLI backend, `ttadk_*` forced CLI bridge).
- **Task scheduling**: `TaskScheduler` provides per-chat ordered execution with global concurrency control. Long-running tasks (e.g. Deep Engine) use separate `queue_key` to avoid blocking control commands.
- **Multi-tier locking**: 6-level lock ordering hierarchy (ENGINE_MANAGER → ENGINE_INSTANCE → PROJECT_MANAGER → CHAT_LOCK_CTX → CHAT_LOCK_MGR → REPO_LOCK) with runtime deadlock detection via `utils/lock_order.py`. All `threading.Lock()`/`RLock()` instances annotated as leaf locks.
- **DI container**: `ServiceRegistry` supports singleton/transient/factory patterns, hierarchical scopes, thread-safe, with `close()` for cleanup.
- **Review pipeline**: Spec review decomposed into strategy selection → pipeline assembly → parallel workers → retry → output parsing (6 independent modules). Circuit breaker state tracking per review cycle.
- **Circuit breaker**: Sliding window failure counting, CLOSED → OPEN → HALF_OPEN three-state transition for fault isolation.
- **Hook system**: 7 event types (pre/post shell, session start/end, engine start/stop, iteration done) with `register_hook()`/`fire_hooks()`.
- **Thread safety**: Critical sections use `threading.Lock`; message dedup uses `MessageCache` with background cleanup thread. Every lock annotated with ordering level.
- **Sync-async bridge**: `SyncACPSession` runs an asyncio event loop in a daemon thread, uses `asyncio.run_coroutine_threadsafe()` to allow synchronous callers to interact with async ACP sessions.

## Project Conventions

- Python 3.11+ required
- Configuration: all secrets/settings from `.env` via pydantic-settings — never hardcode
- AI Native first: prefer model capabilities for generic problems over hardcoded rules
- All features must have unit test coverage
- Before and After The Task, You Need Maintain `Memory.md` as a development log (reverse chronological, format: YYYY-MM-DD HH:MM:SS)
- Tests and temp code go in `tests/`, keep root directory clean
- When solving problems, consider at least two approaches and pick the best one
- **编程模式兼容**: 所有编程模式（Coco/Claude/Aiden/Codex/Gemini/TTADK）相关的后续功能，默认必须实现全模式兼容，除非用户明确指定不需要兼容
- **飞书卡片任务级展示原则**: 编程相关卡片默认遵循“一个任务一张卡片”；卡片开头必须先展示“整体任务列表”和“当前进行中”任务；发生并发 subagent 时每个子任务独立维护自己的消息卡片并持续更新；单卡超限时必须新开续卡且保留原卡内容，新卡开头继续展示整体任务列表与当前进行中；工具调用详情保持现有展示方式，但应尽量避免空数据块
- **提交信息规范**: 提交信息必须准确描述所有修改的文件和变更范围，详细规范请参考 [docs/commit-message-guidelines.md](docs/commit-message-guidelines.md)
- **Git Hooks**: 项目已配置 pre-commit 和 commit-msg hooks 来帮助确保提交信息质量，这些 hooks 会在提交时自动运行并提供提示
- **UI 效果图规范**: 所有涉及 UI 设计的变更，必须先使用 HTML 绘制效果图进行 Review 确认，再进行代码实现。效果图统一存放在 `ux/` 目录下，作为设计参考和历史存档

## Workflow Rules

1. **逐步验证** - 每完成一个独立变更后立即运行测试（`uv run pytest -x -q`），确认通过后再继续下一步。不要批量实现多个变更后才验证。
2. **实现完整性** - 实现功能时覆盖所有子操作（如 CRUD 全覆盖、收发都实现），不要只做 happy path。如果 scope 不明确，先问清楚再动手。
3. **先研究后实现** - 修改已有模块前，先用 Grep/Read 查看该模块现有模式和约定，确保新代码与既有风格一致。
4. **任务完成必须更新 Memory** - 每次任务完成后，必须更新 `.Memory/{YYYY-MM-DD}.md` 记录执行内容，并同步更新 `.Memory/Abstract.md` 索引。这是任务闭环的一部分，未更新 Memory 等于任务未完成。
5. **审计缺口分级处理** - Review/Audit 产出的改进建议按 severity 分级：**High**（影响正确性/安全性）立即修复；**Medium**（可观测性/可运维性缺口）和 **Low**（代码风格/文档一致性）录入 `.Memory/Backlog.md`，集中在维护窗口批量处理，不打断主线开发节奏。
6. **Backlog 清理** - 当 Backlog.md 中的条目已被修复时，**必须立即从 Backlog.md 中删除该条目**。保持 Backlog 只包含未解决的问题，避免已修复项堆积造成混淆。

## Project Memory System (重要)

项目使用 `.Memory/` 目录存储历史决策和执行经验，**必须在 git 中跟踪**。

**记忆目录**: `.Memory/` (项目根目录下)

**文件结构**:
```
.Memory/
├── Abstract.md       # 摘要索引（行动前必查入口）
├── 2026-02-04.md     # 按日期组织的详细记录
└── 2026-02-05.md
```

**核心规则**:
1. **行动前先查 Abstract.md** - 快速定位历史决策，避免重复犯错
2. **任务完成后必须更新** - 在 `{YYYY-MM-DD}.md` 中记录，并更新 `Abstract.md` 索引
3. **提交到 git** - Memory 是项目的一部分，必须版本控制

**记录内容**:
- 完成的任务和关键决策
- 踩过的坑和解决方案
- 技术要点和最佳实践
- 提交记录

**更新时机**:
- 创建新模块/技能后
- 发现并修复 bug 后
- 学到新的最佳实践后
- 完成重要重构后

**Abstract.md 格式**:
```markdown
## {YYYY-MM-DD}
- **任务简述** - 一句话描述 → [链接到详细文件]
```

**日期文件格式**:
```markdown
# {YYYY-MM-DD} 项目记录

## 任务名称
### 任务描述
### 执行内容
### 技术要点
### 提交记录
```
