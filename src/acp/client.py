"""ACP Client implementation — handles agent callbacks.

GhostAPClient implements the ACP Client interface and converts raw ACP session
updates into ACPEvent objects, forwarding them to the registered event handler.
"""

from __future__ import annotations

import logging
import os
import json
import time
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from acp.interfaces import Client, Agent
from acp.schema import (
    AgentMessageChunk,
    AgentPlanUpdate,
    AgentThoughtChunk,
    AllowedOutcome,
    CreateTerminalResponse,
    DeniedOutcome,
    KillTerminalCommandResponse,
    ReadTextFileResponse,
    ReleaseTerminalResponse,
    RequestPermissionResponse,
    TerminalExitStatus,
    TerminalOutputResponse,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    WaitForTerminalExitResponse,
    WriteTextFileResponse,
)

from ..sandbox.executor import SandboxExecutor

from .models import (
    ACPEvent,
    ACPEventType,
    PlanEntryInfo,
    PlanInfo,
    ToolCallInfo,
)

logger = logging.getLogger(__name__)


_MAX_FILE_CHARS = 200_000


def _safe_session_filename(session_id: str) -> str:
    """Make a safe filename from session_id."""
    sid = (session_id or "").strip() or "unknown"
    sid = re.sub(r"[^a-zA-Z0-9._-]+", "_", sid)
    return sid[:120]


class ACPHistoryStore:
    """Local persistence for ACP session history (jsonl).

    Stores command execution results and file operations so that GhostAP can
    display/recover historical info even if the agent-side session store is
    unavailable.
    """

    def __init__(self, base_dir: Optional[str] = None):
        env_dir = os.getenv("GHOSTAP_ACP_HISTORY_DIR", "").strip()
        root = base_dir or env_dir
        if not root:
            root = str(Path.home() / ".ghostap" / "acp_history")
        self._base = Path(root).expanduser()

    def _path_for(self, session_id: str) -> Path:
        name = _safe_session_filename(session_id)
        return self._base / f"{name}.jsonl"

    def append(self, session_id: str, entry: dict) -> None:
        if not session_id:
            return
        try:
            self._base.mkdir(parents=True, exist_ok=True)
            p = self._path_for(session_id)
            payload = dict(entry or {})
            payload.setdefault("ts", time.time())
            payload.setdefault("session_id", session_id)
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug("[ACP] history append failed: %s", e)

    def load(self, session_id: str, limit: int = 200) -> list[dict]:
        if not session_id:
            return []
        p = self._path_for(session_id)
        if not p.exists():
            return []

        try:
            raw = p.read_text(encoding="utf-8")
        except Exception as e:
            logger.info("[ACP] history read failed: %s", e)
            return []

        raw_strip = raw.lstrip()
        # Backward compatibility: accept a single JSON array file.
        if raw_strip.startswith("["):
            try:
                data = json.loads(raw)
                if isinstance(data, list):
                    items = [x for x in data if isinstance(x, dict)]
                    return items[-limit:] if limit > 0 else items
            except Exception:
                # Fall through to jsonl parsing
                pass

        items: list[dict] = []
        for line in raw.splitlines():
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
                if isinstance(obj, dict):
                    items.append(obj)
            except Exception:
                # Corrupt line: skip
                continue

        return items[-limit:] if limit > 0 else items


def _safe_resolve_path(root_dir: str, user_path: str) -> Path:
    """Resolve a user-supplied path inside root_dir.

    Supports both absolute and relative paths, but always enforces:
    resolved_path must be within root_dir.
    """
    root = Path(root_dir).expanduser().resolve()
    p = Path(user_path).expanduser()
    if not p.is_absolute():
        p = root / p
    resolved = p.resolve()
    try:
        resolved.relative_to(root)
    except Exception as e:
        raise PermissionError(f"path escapes root_dir: {user_path}") from e
    return resolved


@dataclass
class _TerminalRecord:
    output: str
    exit_code: int
    truncated: bool
    cursor: int = 0


def _parse_tool_call(update: ToolCallStart | ToolCallProgress) -> ToolCallInfo:
    """Extract ToolCallInfo from a ToolCallStart or ToolCallProgress."""
    locations: list[str] = []
    if update.locations:
        locations = [loc.path for loc in update.locations]

    return ToolCallInfo(
        id=update.tool_call_id,
        title=update.title or "",
        kind=update.kind or "other",
        status=update.status or "in_progress",
        locations=locations,
    )


def _parse_plan(update: AgentPlanUpdate) -> PlanInfo:
    """Extract PlanInfo from an AgentPlanUpdate."""
    entries: list[PlanEntryInfo] = []
    for entry in update.entries:
        try:
            raw_content = getattr(entry, "content", None)
            content = ("" if raw_content is None else str(raw_content)).strip()
        except Exception:
            content = ""
        # Some agents may emit placeholder entries with empty content; skip them to
        # avoid rendering "✅" lines without text.
        if not content:
            continue

        entries.append(
            PlanEntryInfo(
                content=content,
                priority=getattr(entry, "priority", None) or "medium",
                status=getattr(entry, "status", None) or "pending",
            )
        )
    return PlanInfo(entries=entries)


class GhostAPClient(Client):
    """ACP Client implementation — processes agent session updates."""

    def __init__(
        self,
        on_event: Callable[[ACPEvent], None],
        auto_approve: bool = True,
        root_dir: str = ".",
        sandbox: Optional[SandboxExecutor] = None,
        history_store: Optional[ACPHistoryStore] = None,
    ):
        self._on_event = on_event
        self._auto_approve = auto_approve
        self._root_dir = os.path.abspath(os.path.expanduser(root_dir or "."))
        self._sandbox = sandbox or SandboxExecutor()
        self._terminals: dict[str, _TerminalRecord] = {}
        self._history = history_store or ACPHistoryStore()

    def _record(self, session_id: str, kind: str, data: dict) -> None:
        try:
            payload = {"kind": kind, "data": data or {}}
            self._history.append(session_id, payload)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Core callback: session_update
    # ------------------------------------------------------------------
    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        """Receive agent's streaming updates — the core event handler."""
        try:
            if isinstance(update, AgentMessageChunk):
                self._handle_message_chunk(update)
            elif isinstance(update, AgentThoughtChunk):
                self._handle_thought_chunk(update)
            elif isinstance(update, ToolCallStart):
                self._handle_tool_call_start(update)
            elif isinstance(update, ToolCallProgress):
                self._handle_tool_call_progress(update)
            elif isinstance(update, AgentPlanUpdate):
                self._handle_plan_update(update)
            # Other update types (UsageUpdate, etc.) are silently ignored
        except Exception as e:
            logger.debug("Error processing ACP session_update: %s", e)

    def _handle_message_chunk(self, update: AgentMessageChunk) -> None:
        content = update.content
        if isinstance(content, TextContentBlock):
            self._on_event(ACPEvent(
                event_type=ACPEventType.TEXT_CHUNK,
                text=content.text,
            ))

    def _handle_thought_chunk(self, update: AgentThoughtChunk) -> None:
        content = update.content
        if isinstance(content, TextContentBlock):
            self._on_event(ACPEvent(
                event_type=ACPEventType.THOUGHT_CHUNK,
                text=content.text,
            ))

    def _handle_tool_call_start(self, update: ToolCallStart) -> None:
        tool_info = _parse_tool_call(update)
        self._on_event(ACPEvent(
            event_type=ACPEventType.TOOL_CALL_START,
            tool_call=tool_info,
        ))

    def _handle_tool_call_progress(self, update: ToolCallProgress) -> None:
        tool_info = _parse_tool_call(update)
        status = tool_info.status
        if status in ("completed", "failed"):
            event_type = ACPEventType.TOOL_CALL_DONE
        else:
            event_type = ACPEventType.TOOL_CALL_UPDATE
        self._on_event(ACPEvent(
            event_type=event_type,
            tool_call=tool_info,
        ))

    def _handle_plan_update(self, update: AgentPlanUpdate) -> None:
        plan = _parse_plan(update)
        self._on_event(ACPEvent(
            event_type=ACPEventType.PLAN_UPDATE,
            plan=plan,
        ))

    # ------------------------------------------------------------------
    # Permission handling
    # ------------------------------------------------------------------
    async def request_permission(self, options, session_id: str, tool_call, **kwargs: Any) -> RequestPermissionResponse:
        """Handle permission requests from agent."""
        if not self._auto_approve:
            self._record(session_id, "permission", {"outcome": "cancelled", "reason": "auto_approve_disabled"})
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

        if not options:
            # Defensive: some agents might ask with empty options.
            self._record(session_id, "permission", {"outcome": "cancelled", "reason": "empty_options"})
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

        # Best-effort safety gate for execute operations.
        try:
            kind = getattr(tool_call, "kind", None)
            if kind == "execute":
                raw_input = getattr(tool_call, "raw_input", None)
                command: Optional[str] = None
                if isinstance(raw_input, dict):
                    command = (
                        raw_input.get("command")
                        or raw_input.get("cmd")
                        or raw_input.get("shell_command")
                    )
                elif isinstance(raw_input, str):
                    command = raw_input
                if command:
                    ok, reason = self._sandbox.is_command_safe(command)
                    if not ok:
                        logger.info("[ACP] Reject unsafe command: %s (%s)", command, reason)
                        self._record(session_id, "permission", {
                            "outcome": "cancelled",
                            "reason": "unsafe_execute",
                            "command": command,
                            "detail": reason,
                        })
                        return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        except Exception as e:
            # Fail open to avoid breaking agent flows; runtime handlers still enforce.
            logger.debug("[ACP] Permission safety check failed: %s", e)

        # Find an "allow_once" option, or use the first option.
        allow_option_id = ""
        for opt in options:
            if getattr(opt, "kind", None) == "allow_once":
                allow_option_id = opt.option_id
                break
        if not allow_option_id and options:
            allow_option_id = options[0].option_id

        return RequestPermissionResponse(outcome=AllowedOutcome(option_id=allow_option_id, outcome="selected"))

    # ------------------------------------------------------------------
    # File operations (delegate to agent's own filesystem)
    # ------------------------------------------------------------------
    async def read_text_file(self, path: str, session_id: str, **kwargs: Any) -> ReadTextFileResponse:
        try:
            resolved = _safe_resolve_path(self._root_dir, path)
            content = resolved.read_text(encoding="utf-8")
            if len(content) > _MAX_FILE_CHARS:
                content = content[:_MAX_FILE_CHARS]
                self._record(session_id, "read_file", {"path": str(resolved), "truncated": True, "max_chars": _MAX_FILE_CHARS})
                return ReadTextFileResponse(
                    content=content,
                    field_meta={"truncated": True, "path": str(resolved), "max_chars": _MAX_FILE_CHARS},
                )
            self._record(session_id, "read_file", {"path": str(resolved), "truncated": False, "chars": len(content)})
            return ReadTextFileResponse(content=content)
        except Exception as e:
            logger.info("[ACP] read_text_file failed: path=%s err=%s", path, e)
            self._record(session_id, "read_file", {"path": path, "error": str(e)})
            return ReadTextFileResponse(content="", field_meta={"error": str(e), "path": path})

    async def write_text_file(self, content: str, path: str, session_id: str, **kwargs: Any) -> Optional[WriteTextFileResponse]:
        try:
            resolved = _safe_resolve_path(self._root_dir, path)
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(content or "", encoding="utf-8")
            self._record(session_id, "write_file", {"path": str(resolved), "chars": len(content or "")})
            return WriteTextFileResponse()
        except Exception as e:
            logger.info("[ACP] write_text_file failed: path=%s err=%s", path, e)
            self._record(session_id, "write_file", {"path": path, "error": str(e)})
            return WriteTextFileResponse(field_meta={"error": str(e), "path": path})

    # ------------------------------------------------------------------
    # Terminal operations (stub — agent manages its own terminals)
    # ------------------------------------------------------------------
    async def create_terminal(self, command: str, session_id: str, **kwargs: Any) -> CreateTerminalResponse:
        ok, reason = self._sandbox.is_command_safe(command)
        if not ok:
            # Create a virtual terminal that immediately returns the safety error.
            term_id = f"term_{uuid.uuid4().hex[:8]}"
            self._terminals[term_id] = _TerminalRecord(
                output=f"❌ 安全检查未通过: {reason}",
                exit_code=-1,
                truncated=False,
            )
            self._record(session_id, "execute", {"command": command, "blocked": True, "reason": reason})
            return CreateTerminalResponse(terminal_id=term_id, field_meta={"blocked": True, "reason": reason})

        result = self._sandbox.execute(command, cwd=self._root_dir)
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        output_parts: list[str] = []
        if stdout:
            output_parts.append(stdout)
        if stderr:
            output_parts.append(stderr)
        output = "\n".join(output_parts).strip("\n")
        truncated = ("输出被截断" in output) or ("错误输出被截断" in output)

        # Persist a capped copy of output for recovery/debug UI.
        cap = 8000
        output_cap = output if len(output) <= cap else (output[:cap] + "\n... (truncated)")
        self._record(session_id, "execute", {
            "command": command,
            "cwd": self._root_dir,
            "exit_code": result.return_code,
            "truncated": truncated,
            "output": output_cap,
        })

        term_id = f"term_{uuid.uuid4().hex[:8]}"
        self._terminals[term_id] = _TerminalRecord(
            output=output,
            exit_code=result.return_code,
            truncated=truncated,
        )
        return CreateTerminalResponse(terminal_id=term_id)

    async def terminal_output(self, session_id: str, terminal_id: str, **kwargs: Any) -> TerminalOutputResponse:
        rec = self._terminals.get(terminal_id)
        if not rec:
            return TerminalOutputResponse(output="", truncated=False, field_meta={"error": "unknown_terminal"})
        chunk = rec.output[rec.cursor:]
        rec.cursor = len(rec.output)
        return TerminalOutputResponse(
            output=chunk,
            truncated=rec.truncated,
            exit_status=TerminalExitStatus(exit_code=rec.exit_code),
        )

    async def wait_for_terminal_exit(self, session_id: str, terminal_id: str, **kwargs: Any) -> WaitForTerminalExitResponse:
        rec = self._terminals.get(terminal_id)
        if not rec:
            return WaitForTerminalExitResponse(exit_code=None, signal=None, field_meta={"error": "unknown_terminal"})
        return WaitForTerminalExitResponse(exit_code=rec.exit_code, signal=None)

    async def kill_terminal(self, session_id: str, terminal_id: str, **kwargs: Any) -> Optional[KillTerminalCommandResponse]:
        self._terminals.pop(terminal_id, None)
        return KillTerminalCommandResponse()

    async def release_terminal(self, session_id: str, terminal_id: str, **kwargs: Any) -> Optional[ReleaseTerminalResponse]:
        self._terminals.pop(terminal_id, None)
        return ReleaseTerminalResponse()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def on_connect(self, conn: Agent) -> None:
        # NOTE: ACP SDK calls `on_connect()` synchronously.
        # Keep this hook sync to avoid "coroutine was never awaited" warnings.
        return None
