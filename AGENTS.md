# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

GhostAP is a Feishu (Lark) chatbot service that provides safe remote Shell command execution and AI-assisted development via WebSocket long-connection. Users interact through Feishu chat to execute shell commands, use Coco/Claude/Aiden/Codex/Gemini/TTADK for remote development, and manage multiple projects. No public IP or domain is needed — the bot connects outbound via WebSocket.

### Programming Strategy Layers

GhostAP has two orthogonal dimensions:

- **Execution strategy layer (4 levels, complexity increasing)**
  - **Normal programming mode**: multi-turn coding dialogue mode
  - **Deep mode**: one-shot deep execution (single requirement → autonomous plan+execute)
  - **Loop mode**: iterative closed-loop execution with acceptance criteria
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
    → TaskScheduler.submit (per-chat ordered, concurrency-limited)
      → IntentRecognizer (ReAct-based LLM intent classification)
        → Route by intent:
            SHELL_COMMAND → SandboxExecutor (safety check → execute)
            ENTER_COCO    → ACPSessionManager("coco") (multi-turn AI dev session via ACP)
            ENTER_CLAUDE  → ACPSessionManager("claude") (Claude CLI session backend)
            ENTER_TTADK   → TTADKManager + ACPSessionManager("ttadk") (CLI bridge only)
            DEEP_COMMAND  → DeepEngine (ACP-driven single-prompt deep execution)
            LOOP_COMMAND  → LoopEngine (ACP-driven iterative closed-loop)
            SPEC_COMMAND  → SpecEngine (structured iterative execution)
            CREATE_PROJECT → ProjectManager
            ...
      → ACPEventRenderer (structured tool/plan/text rendering)
      → CardBuilder / StreamingCardManager (build response card)
      → Feishu API reply + EmojiReaction feedback
```

### Key Modules

- **`src/acp/`** — ACP (Agent Client Protocol) layer. Provides structured communication for ACP-capable agents via JSON-RPC 2.0 over stdio, plus session routing/degrade diagnostics. Includes:
  - `models.py` — `ACPEvent`, `ACPEventType`, `ToolCallInfo`, `PlanInfo`, `ACPSessionState`, `PromptResult`
  - `client.py` — `GhostAPClient` implements ACP `Client` interface, converts raw session updates to `ACPEvent` objects
  - `session.py` — `ACPSession` manages async ACP connection lifecycle (start/prompt/cancel/close)
  - `sync_adapter.py` — `SyncACPSession` wraps async ACPSession for use from synchronous threads
  - `manager.py` — `ACPSessionManager` manages per-chat ACP sessions with timeout/cleanup
  - `renderer.py` — `ACPEventRenderer` converts ACP events to Feishu Markdown (tool status, plan checklist, text accumulation)
- **`src/ttadk/`** — TTADK (Multi-Tool AI Development Kit) layer. Manages multi-tool AI programming sessions, supporting switching between different AI tools and models. Includes:
  - `models.py` — `TTADKTool`, `TTADKModel` data models for tool/model definitions
  - `manager.py` — `TTADKManager` singleton for managing current tool/model state, switching between available tools (coco/claude/cursor/gemini/codex/tmates/trae/opencode) and models (gpt-5.2/gpt-4.1/claude-3-opus/claude-3.5-sonnet/claude-3.7-sonnet/doubao-1.5-pro/gemini-2.0-pro/gemini-2.5-pro)
- **`src/feishu/ws_client.py`** — Core hub. Handles WebSocket events, routes messages to handlers by mode (SMART/COCO/CLAUDE/AIDEN/CODEX/GEMINI/SHELL/TTADK), manages component interactions. Uses `_FORWARDING_MAP` dict + `__getattr__` dispatch for handler delegation.
- **`src/tasking/scheduler.py`** — `TaskScheduler`: thread-based task scheduler with per-chat ordered execution, global concurrency limit, priority queues, cancellation tokens, and progress tracking.
- **`src/mode/manager.py`** — `ModeManager` state machine: SMART ↔ programming/shell mode transitions with chat/project scopes.
- **`src/agent/intent_recognizer.py`** — LLM-powered ReAct intent recognition. Classifies user messages into ~30 intent types and generates shell commands from natural language.
- **`src/sandbox/executor.py`** — `SandboxExecutor` runs shell commands with safety checks (20+ dangerous pattern regexes, blacklist, timeout, output truncation).
- **`src/project/`** — Multi-project workspace: `ProjectManager`, `ProjectContext` (status tracking, conversation history), `MessageProjectMapper`.
- **`src/card/builder.py`** — `CardBuilder` creates Feishu interactive cards (buttons, menus). Engine-aware header colors (Coco=blue, Claude=purple).
- **`src/card/streaming.py`** — `StreamingCardManager` for real-time card updates during long operations. Supports desktop/mobile/responsive button layouts.
- **`src/deep_engine/`** — ACP-driven deep execution engine. Single prompt with full requirement → agent self-plans and executes. Tracks progress via ACP plan updates and tool calls. Includes: `engine.py` (DeepEngine, DeepEngineManager), `models.py` (DeepProject, EngineRunState), `progress.py` (DeepProgress).
- **`src/loop_engine/`** — ACP-driven iterative closed-loop engine. Multi-round prompts in a single ACP session, with convergence detection and acceptance criteria evaluation. Includes: `engine.py` (LoopEngine, LoopEngineManager), `models.py` (LoopProject, IterationRecord, CriteriaTracker), `tracker.py` (IterationTracker), `reporter.py` (LoopReporter).
- **`src/spec_engine/`** — structured iterative engine (`Spec→Plan→Task→Build→Review`) with pause/resume/recover, task persistence, and phase-level progress reporting.
- **`src/config.py`** — `Settings` singleton via `get_settings()`, backed by pydantic-settings + `.env`.

### Design Patterns

- **Strategy layering**: user-facing development workflow is split into 4 execution strategies (Normal/Deep/Loop/Spec), independent from backend tool transport.
- **Hybrid transport (ACP + CLI)**: ACP-capable tools use ACP direct mode; non-ACP tools use Shell CLI bridge; `ttadk_*` is strictly CLI-bridged and isolated from ACP direct startup.
- **TTADK multi-tool layer**: TTADK provides a unified interface for multiple AI tools (Coco/Claude/Cursor/Gemini/etc) and models, with `TTADKManager` singleton managing tool/model switching and state.
- **Event-driven rendering**: `ACPEventRenderer` processes `ACPEvent` stream (text chunks, tool calls, plan updates) into Feishu Markdown. Handlers register `on_event` callbacks to drive real-time card updates.
- **State machine**: `InteractionMode` enum governs SMART/COCO/CLAUDE/AIDEN/CODEX/GEMINI/SHELL/TTADK transitions; `EngineRunState` enum tracks IDLE/RUNNING/STOPPING for Deep and Loop engines.
- **Singleton**: `get_settings()` for configuration, `get_ttadk_manager()` for TTADK tool/model management
- **Session managers**: `ACPSessionManager` unifies per-chat/project session lifecycle across ACP and CLI backends (`coco/aiden/gemini/codex` ACP-direct, `claude` CLI backend, `ttadk_*` forced CLI bridge).
- **Task scheduling**: `TaskScheduler` provides per-chat ordered execution with global concurrency control. Long-running tasks (e.g. Deep Engine, Loop Engine) use separate `queue_key` to avoid blocking control commands.
- **Thread safety**: Critical sections use `threading.Lock`; message dedup uses `MessageCache` with background cleanup thread
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
- **提交信息规范**: 提交信息必须准确描述所有修改的文件和变更范围，详细规范请参考 [docs/commit-message-guidelines.md](docs/commit-message-guidelines.md)
- **Git Hooks**: 项目已配置 pre-commit 和 commit-msg hooks 来帮助确保提交信息质量，这些 hooks 会在提交时自动运行并提供提示

## Workflow Rules

1. **逐步验证** - 每完成一个独立变更后立即运行测试（`uv run pytest -x -q`），确认通过后再继续下一步。不要批量实现多个变更后才验证。
2. **实现完整性** - 实现功能时覆盖所有子操作（如 CRUD 全覆盖、收发都实现），不要只做 happy path。如果 scope 不明确，先问清楚再动手。
3. **先研究后实现** - 修改已有模块前，先用 Grep/Read 查看该模块现有模式和约定，确保新代码与既有风格一致。
4. **任务完成必须更新 Memory** - 每次任务完成后，必须更新 `.Memory/{YYYY-MM-DD}.md` 记录执行内容，并同步更新 `.Memory/Abstract.md` 索引。这是任务闭环的一部分，未更新 Memory 等于任务未完成。
5. **审计缺口分级处理** - Review/Audit 产出的改进建议按 severity 分级：**High**（影响正确性/安全性）立即修复；**Medium**（可观测性/可运维性缺口）和 **Low**（代码风格/文档一致性）录入 `.Memory/Backlog.md`，集中在维护窗口批量处理，不打断主线开发节奏。

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
