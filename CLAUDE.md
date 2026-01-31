# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

GhostAP is a Feishu (Lark) chatbot service that provides safe remote Shell command execution and AI-assisted development via WebSocket long-connection. Users interact through Feishu chat to execute shell commands, use Coco AI (Bytedance ARK model) or Claude for remote development, and manage multiple projects. No public IP or domain is needed — the bot connects outbound via WebSocket.

## Commands

```bash
# Install dependencies
uv sync --group dev

# Run the service
uv run python -m src.main

# Run all tests
uv run python -m pytest tests/ -v

# Run a single test file
uv run python -m pytest tests/test_claude.py -v

# Run a single test
uv run python -m pytest tests/test_claude.py::TestClaudeSession::test_create_session -v
```

**Package manager**: Always use `uv`, never pip/conda.

## Architecture

### Message Flow

```
Feishu WebSocket message
  → FeishuWSClient._handle_message (validation, dedup, expiry check)
    → IntentRecognizer (ReAct-based LLM intent classification)
      → Route by intent:
          SHELL_COMMAND → SandboxExecutor (safety check → execute)
          ENTER_COCO    → CocoSessionManager (multi-turn AI dev session)
          ENTER_CLAUDE  → ClaudeSessionManager (Claude AI session)
          CREATE_PROJECT → ProjectManager
          ...
    → CardBuilder / StreamingCardManager (build response card)
    → Feishu API reply + EmojiReaction feedback
```

### Key Modules

- **`src/feishu/ws_client.py`** — Core hub (~1700 lines). Handles WebSocket events, routes messages to appropriate handlers based on current mode (SMART/COCO/CLAUDE/SHELL), manages all component interactions. This is the central orchestrator.
- **`src/mode/manager.py`** — `ModeManager` state machine: SMART ↔ COCO/CLAUDE/SHELL mode transitions.
- **`src/agent/intent_recognizer.py`** — LLM-powered ReAct intent recognition. Classifies user messages into ~30 intent types and generates shell commands from natural language.
- **`src/coco/session.py`** — `CocoSessionManager` manages Coco AI dev sessions with lifecycle control and session resume.
- **`src/claude/session.py`** — `ClaudeSessionManager` manages Claude AI sessions (UUID-based).
- **`src/sandbox/executor.py`** — `SandboxExecutor` runs shell commands with safety checks (20+ dangerous pattern regexes, blacklist, timeout, output truncation).
- **`src/project/`** — Multi-project workspace: `ProjectManager`, `ProjectContext` (status tracking, conversation history), `MessageProjectMapper`.
- **`src/card/builder.py`** — `CardBuilder` creates Feishu interactive cards (buttons, menus).
- **`src/card/streaming.py`** — `StreamingCardManager` for real-time card updates during long operations.
- **`src/deep_engine/`** — Orchestrates complex multi-step tasks: parser → planner → executor → reporter.
- **`src/config.py`** — `Settings` singleton via `get_settings()`, backed by pydantic-settings + `.env`.

### Design Patterns

- **State machine**: `InteractionMode` enum governs SMART/COCO/CLAUDE/SHELL transitions
- **Singleton**: `get_settings()` for configuration
- **Session managers**: `CocoSessionManager` and `ClaudeSessionManager` handle isolated sessions per chat
- **Thread safety**: Critical sections use `threading.Lock`; message dedup uses `OrderedDict` + lock

## Project Conventions

- Python 3.11+ required
- Configuration: all secrets/settings from `.env` via pydantic-settings — never hardcode
- AI Native first: prefer model capabilities for generic problems over hardcoded rules
- All features must have unit test coverage
- Maintain `Memory.md` as a development log (reverse chronological, format: YYYY-MM-DD HH:MM:SS)
- Tests and temp code go in `tests/`, keep root directory clean
- When solving problems, consider at least two approaches and pick the best one
- **编程模式兼容**: 所有编程模式（Coco/Claude/Shell 等）相关的后续功能，默认必须实现所有编程模式的兼容，除非用户明确指定不需要兼容
