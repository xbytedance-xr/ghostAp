"""Session backends abstraction.

GhostAP currently supports two different ways to talk to an agent:

1) ACP backend (JSON-RPC 2.0 over stdio) — used by Coco.
2) CLI backend (spawn per prompt)       — used by Claude Code CLI.

The handlers expect an ACP-like streaming callback signature. For CLI backend
we downgrade to text-only ACPEvent(TEXT_CHUNK) events so that existing
rendering and streaming cards can be reused.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from .acp.models import ACPEvent, ACPEventType, PromptResult
from .acp.sync_adapter import SyncACPSession
from .config import get_settings
from .ttadk.models import ModelListResult, ResolvedModelResult
from .ttadk.env_sandbox import build_ttadk_subprocess_env

logger = logging.getLogger(__name__)


TTADKStartupError = None  # legacy alias; do not use


class SyncSession(Protocol):
    """A minimal sync session interface used by handlers."""

    session_id: str
    created_at: float
    last_active: float
    message_count: int
    last_query: str
    is_resumed: bool

    def describe_agent(self) -> str: ...
    def start(self, startup_timeout: float = 60) -> str: ...
    def load_session(self, session_id: str) -> None: ...
    def load_local_history(self, session_id: Optional[str] = None, limit: int = 200) -> list[dict]: ...
    def send_prompt(self, text: str, on_event: Optional[Callable[[ACPEvent], None]] = None, timeout: Optional[int] = None) -> PromptResult: ...
    def cancel(self) -> None: ...
    def close(self) -> None: ...
    def to_snapshot(self) -> dict: ...
    def get_session_info(self) -> str: ...

    def is_server_running(self) -> bool: ...
    def is_server_healthy(self, healthcheck_timeout: float = 2.0) -> bool: ...


@dataclass
class ClaudeCLIConfig:
    """Configuration knobs for Claude Code CLI backend."""

    command: str = "claude"
    add_dir: bool = True
    bypass_permissions: Optional[bool] = None  # None → use config.claude_cli_skip_permissions


class SyncClaudeCLISession:
    """Claude Code CLI backend.

    - Uses `claude -p` (print and exit) per prompt.
    - Uses `--session-id` for the first prompt and `--resume <id>` afterwards.
    - Emits TEXT_CHUNK ACP events only (no plan/tool events).
    """

    def __init__(self, cwd: str, config: Optional[ClaudeCLIConfig] = None):
        self._cwd = cwd
        self._cfg = config or ClaudeCLIConfig()
        self._proc: Optional[subprocess.Popen] = None
        self._cancel_event = threading.Event()

        self.session_id: str = ""
        self.created_at: float = time.time()
        self.last_active: float = time.time()
        self.message_count: int = 0
        self.last_query: str = ""
        self.is_resumed: bool = False

    def describe_agent(self) -> str:
        try:
            return f"cmd={self._cfg.command} cwd={self._cwd} backend=cli"
        except Exception:
            return "agent=claude backend=cli"

    def start(self, startup_timeout: float = 60) -> str:
        # No long-running server here; just validate executable and mint a session id.
        if not shutil.which(self._cfg.command):
            raise RuntimeError(f"未找到 Claude CLI 可执行文件: {self._cfg.command}")
        if not self.session_id:
            self.session_id = str(uuid.uuid4())
        return self.session_id

    def load_session(self, session_id: str) -> None:
        # Claude CLI uses local persistence; we just switch to target session id.
        self.session_id = session_id
        self.is_resumed = True

    def load_local_history(self, session_id: Optional[str] = None, limit: int = 200) -> list[dict]:
        # Claude CLI manages its own history; GhostAP doesn't parse it here.
        return []

    def is_server_running(self) -> bool:
        # Per-prompt spawn — no persistent server to check.
        return True

    def is_server_healthy(self, healthcheck_timeout: float = 2.0) -> bool:
        return True

    def _resolve_bypass_permissions(self) -> bool:
        """Resolve whether to skip Claude permissions (config > explicit)."""
        if self._cfg.bypass_permissions is not None:
            return self._cfg.bypass_permissions
        return get_settings().claude_cli_skip_permissions

    def send_prompt(
        self,
        text: str,
        on_event: Optional[Callable[[ACPEvent], None]] = None,
        timeout: Optional[int] = None,
    ) -> PromptResult:
        if not self.session_id:
            self.start()

        self._cancel_event.clear()
        self.last_active = time.time()
        self.message_count += 1
        self.last_query = text

        def _build_args(resumed: bool) -> list[str]:
            args: list[str] = [self._cfg.command, "-p"]
            if self._cfg.add_dir:
                args += ["--add-dir", self._cwd]
            if self._resolve_bypass_permissions():
                args.append("--dangerously-skip-permissions")

            if resumed:
                args += ["--resume", self.session_id]
            else:
                args += ["--session-id", self.session_id]

            args.append(text)
            return args

        def _run_once(resumed: bool) -> tuple[int, str, str, str]:
            """Run one claude invocation and return (returncode, stdout, stderr, state)."""
            args = _build_args(resumed)
            chunks: list[str] = []
            try:
                # Claude Code CLI refuses to launch inside another Claude Code session.
                # Our process may run under Claude Code / other wrappers, so we must
                # explicitly unset the guard env to avoid nested-session crash.
                env = os.environ.copy()
                env.pop("CLAUDECODE", None)

                self._proc = subprocess.Popen(
                    args,
                    cwd=self._cwd,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )

                deadline = (time.monotonic() + timeout) if timeout else None
                assert self._proc.stdout is not None
                for line in self._proc.stdout:
                    if self._cancel_event.is_set():
                        self._proc.terminate()
                        self._proc.wait(timeout=5)
                        return (1, "".join(chunks), "", "cancelled")
                    if deadline and time.monotonic() > deadline:
                        self._proc.terminate()
                        self._proc.wait(timeout=5)
                        return (1, "".join(chunks), "", "timeout")
                    chunks.append(line)
                    if on_event:
                        on_event(ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text=line))

                self._proc.wait(timeout=30)
                rc = int(self._proc.returncode or 0)
                err = (self._proc.stderr.read() or "").strip("\n") if self._proc.stderr else ""
                return (rc, "".join(chunks).strip("\n"), err, "ok")
            finally:
                self._proc = None

        def _is_missing_conversation(err_text: str, out_text: str) -> bool:
            blob = (err_text or "") + "\n" + (out_text or "")
            return "No conversation found with session ID" in blob

        try:
            # First try: follow the normal resume/session-id flow
            rc, out, err, state = _run_once(resumed=self.is_resumed)

            if state == "cancelled":
                self.is_resumed = True
                return PromptResult(stop_reason="cancelled", text=out)
            if state == "timeout":
                self.is_resumed = True
                return PromptResult(stop_reason="cancelled", text="❌ Claude 执行超时，已取消")

            # If resume failed because local conversation doesn't exist, fall back to a fresh session once.
            if self.is_resumed and rc != 0 and _is_missing_conversation(err, out):
                logger.info("[ClaudeCLI] resume failed (missing conversation), fallback to new session")
                self.session_id = str(uuid.uuid4())
                self.is_resumed = False
                rc, out, err, _ = _run_once(resumed=False)

            output = out
            if rc != 0 and err:
                output = (output + "\n" + err).strip("\n")
                if on_event:
                    on_event(ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="\n" + err))

            self.is_resumed = True
            stop_reason = "end_turn" if rc == 0 else "failed"
            return PromptResult(stop_reason=stop_reason, text=output)

        except Exception as e:
            self.is_resumed = True
            return PromptResult(stop_reason="error", text=f"❌ Claude 执行异常: {e}")

    def cancel(self) -> None:
        """Signal cancellation — the streaming loop will terminate the process."""
        self._cancel_event.set()
        proc = self._proc
        if proc:
            try:
                proc.terminate()
            except Exception:
                pass

    def close(self) -> None:
        # Nothing persistent to close.
        return

    def to_snapshot(self) -> dict:
        return {
            "session_id": self.session_id,
            "agent_type": "claude",
            "cwd": self._cwd,
            "created_at": self.created_at,
            "last_active": self.last_active,
            "message_count": self.message_count,
            "last_query": self.last_query,
            "is_resumed": self.is_resumed,
            "backend": "cli",
        }

    def get_session_info(self) -> str:
        duration = int(time.time() - self.created_at)
        minutes, seconds = divmod(duration, 60)
        resumed_info = " (已恢复)" if self.is_resumed else ""
        return (
            f"📊 Claude 会话信息{resumed_info} (CLI):\n"
            f"- 会话ID: {self.session_id}\n"
            f"- 消息数: {self.message_count}\n"
            f"- 持续时间: {minutes}分{seconds}秒"
        )


class SyncTTADKCLISession:
    """TTADK CLI backend.

    - Uses `ttadk code -t <tool> -m <model>` per prompt.
    - Emits TEXT_CHUNK ACP events only (no plan/tool events).
    - Stateless (no session ID management).
    """

    def __init__(self, agent_type: str, cwd: str, model_name: Optional[str] = None):
        self._agent_type = agent_type
        self._cwd = cwd
        self._model_name = model_name
        self._tool_name = agent_type.replace("ttadk_", "", 1) if agent_type.startswith("ttadk_") else "unknown"
        
        self.session_id: str = ""
        self.created_at: float = time.time()
        self.last_active: float = time.time()
        self.message_count: int = 0
        self.last_query: str = ""
        self.is_resumed: bool = False
        self._cancel_event = threading.Event()
        self._proc: Optional[subprocess.Popen] = None

    def describe_agent(self) -> str:
        return f"tool={self._tool_name} model={self._model_name or '(auto)'} backend=cli cwd={self._cwd}"

    def start(self, startup_timeout: float = 60) -> str:
        if not shutil.which("ttadk"):
            raise RuntimeError("未找到 ttadk 可执行文件")
        if not self.session_id:
            self.session_id = str(uuid.uuid4())
        return self.session_id

    def load_session(self, session_id: str) -> None:
        self.session_id = session_id
        self.is_resumed = True

    def load_local_history(self, session_id: Optional[str] = None, limit: int = 200) -> list[dict]:
        return []

    def is_server_running(self) -> bool:
        return True

    def is_server_healthy(self, healthcheck_timeout: float = 2.0) -> bool:
        return True

    def cancel(self) -> None:
        self._cancel_event.set()
        proc = self._proc
        if proc:
            try:
                proc.terminate()
            except Exception:
                pass

    def close(self) -> None:
        return

    def to_snapshot(self) -> dict:
        return {
            "session_id": self.session_id,
            "agent_type": self._agent_type,
            "cwd": self._cwd,
            "created_at": self.created_at,
            "last_active": self.last_active,
            "message_count": self.message_count,
            "last_query": self.last_query,
            "is_resumed": self.is_resumed,
            "backend": "cli",
            "model_name": self._model_name,
        }

    def get_session_info(self) -> str:
        duration = int(time.time() - self.created_at)
        minutes, seconds = divmod(duration, 60)
        return (
            f"📊 TTADK 会话信息 (CLI):\n"
            f"- 工具: {self._tool_name}\n"
            f"- 模型: {self._model_name or '(auto)'}\n"
            f"- 消息数: {self.message_count}\n"
            f"- 持续时间: {minutes}分{seconds}秒"
        )

    def send_prompt(
        self,
        text: str,
        on_event: Optional[Callable[[ACPEvent], None]] = None,
        timeout: Optional[int] = None,
    ) -> PromptResult:
        if not self.session_id:
            self.start()

        self._cancel_event.clear()
        self.last_active = time.time()
        self.message_count += 1
        self.last_query = text

        cmd = ["ttadk", "code", "-t", self._tool_name]
        if self._model_name:
            cmd.extend(["-m", self._model_name])
        # Append prompt as the last argument
        cmd.append(text)

        chunks: list[str] = []
        stderr_chunks: list[str] = []
        
        # ANSI escape sequence regex
        ansi_escape = _re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

        def _strip_ansi(s: str) -> str:
            return ansi_escape.sub('', s)
        
        def _read_stderr(pipe):
            try:
                for line in pipe:
                    stderr_chunks.append(line)
            except Exception:
                pass

        try:
            # Use unified environment sandbox for consistency and safety
            # This ensures PATH, PYTHONPATH, and other critical vars are set correctly
            # just like in other TTADK components (fetcher, runner).
            env, _ = build_ttadk_subprocess_env(
                cwd=self._cwd, 
                agent_type=self._agent_type, 
                tool_name=self._tool_name
            )
            
            # Force unbuffered output to ensure real-time streaming
            env["PYTHONUNBUFFERED"] = "1"
            # Force no color to simplify output parsing
            env["NO_COLOR"] = "1"
            env["TERM"] = "dumb"
            
            self._proc = subprocess.Popen(
                cmd,
                cwd=self._cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1, # Line buffered
            )
            
            # Start stderr reader thread to prevent deadlock
            stderr_thread = threading.Thread(target=_read_stderr, args=(self._proc.stderr,), daemon=True)
            stderr_thread.start()

            deadline = (time.monotonic() + timeout) if timeout else None
            
            # Read stdout line by line for streaming
            if self._proc.stdout:
                for line in self._proc.stdout:
                    if self._cancel_event.is_set():
                        self._proc.terminate()
                        self._proc.wait(timeout=5)
                        return PromptResult(stop_reason="cancelled", text="".join(chunks))
                    
                    if deadline and time.monotonic() > deadline:
                        self._proc.terminate()
                        self._proc.wait(timeout=5)
                        return PromptResult(stop_reason="timeout", text="".join(chunks) + "\n❌ TTADK 执行超时")

                    clean_line = _strip_ansi(line)
                    chunks.append(clean_line)
                    if on_event:
                        on_event(ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text=clean_line))

            self._proc.wait(timeout=30)
            stderr_thread.join(timeout=1)

            rc = int(self._proc.returncode or 0)
            err = _strip_ansi("".join(stderr_chunks).strip())
            
            output = "".join(chunks).strip()
            
            if rc != 0:
                if err:
                    output = (output + "\n" + err).strip()
                    if on_event:
                        on_event(ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="\n" + err))
                stop_reason = "failed"
            else:
                stop_reason = "end_turn"

            return PromptResult(stop_reason=stop_reason, text=output)

        except Exception as e:
            return PromptResult(stop_reason="error", text=f"❌ TTADK 执行异常: {e}")
        finally:
            self._proc = None


import re as _re

_RATE_LIMIT_PATTERNS = [
    _re.compile(r"rate.?limit", _re.IGNORECASE),
    _re.compile(r"\b429\b"),
    _re.compile(r"too many requests", _re.IGNORECASE),
    _re.compile(r"overloaded", _re.IGNORECASE),
]
_RETRY_AFTER_RE = _re.compile(r"retry[_\- ]?after[:\s=]*(\d+)", _re.IGNORECASE)


# =====================================================================
# Model compaction / loop / failover detection (send_prompt-time)
# =====================================================================
#
# 这些错误通常由底层模型服务返回，表现为：
# - "Model failed: model 'gpt-5.2': receive message: need compaction"
# - "loop detected"
# - "Failing over to: gpt-5.1"
#
# 统一诊断字段（稳定契约，供日志与单测冻结）：
# - fail_phase: str         # model_compaction | model_loop | model_failover | unknown
# - reason: str             # need_compaction | loop_detected | unknown
# - failed_model: str       # 从错误文本中解析的模型名（best-effort）
# - failover_to: str        # 从错误文本中解析的 failover 目标（best-effort）
# - attempt_count: int      # loop 检测用（窗口期内计数），若未知则为 0

_NEED_COMPACTION_RE = _re.compile(r"\bneed\s+compaction\b", _re.IGNORECASE)
_LOOP_DETECTED_RE = _re.compile(r"\bloop\s+detected\b", _re.IGNORECASE)
_FAILED_MODEL_RE = _re.compile(r"\bmodel\s*['\"]([^'\"\s]+)['\"]", _re.IGNORECASE)
_FAILOVER_TO_RE = _re.compile(r"\bfailing\s+over\s+to\s*:\s*([^\s]+)", _re.IGNORECASE)


def _build_generic_error_blob(error: Exception) -> str:
    """将 error 转成可匹配的通用文本 blob（best-effort, never raises）。

    注意：该函数用于 compaction/loop/failover 等“通用模型失败”检测。
    Invalid model 的“可用模型列表提取/诊断上下文构造”必须收敛到 `src.ttadk.models.build_invalid_model_context`
    等 SSOT 入口，避免在上层重复实现/分叉规则。
    """
    parts: list[str] = []
    try:
        parts.append(str(error) or "")
    except Exception:
        parts.append("")
    # 兼容 ACPStartupError/TTADKProbeError 等携带 snippet 字段的异常
    for k in ("stderr_snippet", "stdout_snippet", "stderr", "stdout", "message"):
        try:
            v = getattr(error, k, None)
            if v:
                parts.append(str(v))
        except Exception:
            continue
    return "\n".join([p for p in parts if p])


def _extract_failed_model(blob: str) -> str:
    """从错误文本中提取失败模型名（best-effort）。"""
    try:
        m = _FAILED_MODEL_RE.search(blob or "")
        return (m.group(1) or "").strip() if m else ""
    except Exception:
        return ""


def _extract_failover_to(blob: str) -> str:
    """从错误文本中提取 failover 目标模型名（best-effort）。"""
    try:
        m = _FAILOVER_TO_RE.search(blob or "")
        return (m.group(1) or "").strip() if m else ""
    except Exception:
        return ""


def classify_model_failure(*, error: Exception) -> dict:
    """分类模型失败原因（send_prompt-time）。

    返回字段遵循本文件顶部“统一诊断字段”约定。
    """
    blob = _build_generic_error_blob(error)
    failed_model = _extract_failed_model(blob)
    failover_to = _extract_failover_to(blob)

    reason = "unknown"
    fail_phase = "unknown"
    try:
        if _NEED_COMPACTION_RE.search(blob or ""):
            reason = "need_compaction"
            fail_phase = "model_compaction"
        elif _LOOP_DETECTED_RE.search(blob or ""):
            reason = "loop_detected"
            fail_phase = "model_loop"
    except Exception:
        reason = "unknown"
        fail_phase = "unknown"

    # failover 目标存在时，标记 fail_phase 为 model_failover（不覆盖更具体的 compaction/loop）
    if (fail_phase == "unknown") and bool(failover_to):
        fail_phase = "model_failover"

    # 注意：attempt_count 由上层 loop 检测器回填；这里保持 0。
    return {
        "fail_phase": fail_phase,
        "reason": reason,
        "failed_model": failed_model,
        "failover_to": failover_to,
        "attempt_count": 0,
        "error_blob": blob,
    }


def _extract_model_from_agent_args(args: list[str]) -> str:
    """从 agent_args 中 best-effort 提取当前 model 名称。"""
    try:
        xs = [str(x) for x in (args or [])]
    except Exception:
        return ""

    # coco: -c model.name=xxx
    for i, x in enumerate(xs):
        if not x:
            continue
        if x == "-c" and i + 1 < len(xs):
            y = str(xs[i + 1] or "")
            if y.startswith("model.name="):
                return y.split("=", 1)[1].strip()
        if "model.name=" in x:
            try:
                return x.split("model.name=", 1)[1].strip()
            except Exception:
                continue

    # ttadk wrapper: python3 -m <wrapper_module> ... ttadk code ... -m <model>
    # 注意：args 中可能同时存在两处 "-m"：
    # - python 的 "-m <module>"
    # - ttadk code 的 "-m <model>"（我们需要提取这个）
    try:
        for i in range(len(xs) - 1):
            if xs[i] == "ttadk" and xs[i + 1] == "code":
                for j in range(i + 2, len(xs) - 1):
                    if xs[j] == "-m":
                        return str(xs[j + 1] or "").strip()
                break
    except Exception:
        pass

    # generic: first -m <value>
    for i, x in enumerate(xs):
        if x == "-m" and i + 1 < len(xs):
            return str(xs[i + 1] or "").strip()
    return ""


def _replace_model_in_agent_args(args: list[str], new_model: str) -> tuple[list[str], bool]:
    """在 agent_args 中替换 model 参数（best-effort）。

    返回 (new_args, replaced)。
    """
    new_model = str(new_model or "").strip()
    if not new_model:
        return (list(args or []), False)

    try:
        xs = [str(x) for x in (args or [])]
    except Exception:
        xs = list(args or [])

    out = list(xs)
    replaced = False

    # coco style: -c model.name=xxx
    for i, x in enumerate(out):
        if x == "-c" and i + 1 < len(out):
            y = str(out[i + 1] or "")
            if y.startswith("model.name="):
                out[i + 1] = f"model.name={new_model}"
                replaced = True
                break
    if replaced:
        return (out, True)

    # ttadk wrapper: locate "ttadk code" then replace its "-m <model>"
    try:
        for i in range(len(out) - 1):
            if str(out[i] or "") == "ttadk" and str(out[i + 1] or "") == "code":
                for j in range(i + 2, len(out) - 1):
                    if str(out[j] or "") == "-m":
                        out[j + 1] = new_model
                        return (out, True)
                break
    except Exception:
        pass

    # generic: first -m <value>
    for i, x in enumerate(out):
        if x == "-m" and i + 1 < len(out):
            out[i + 1] = new_model
            return (out, True)

    return (out, False)


def _remove_model_in_agent_args(args: list[str]) -> tuple[list[str], bool]:
    """在 agent_args 中移除 model 参数（best-effort）。

    目前主要用于 TTADK 运行期 Invalid model 自愈的 auto 回退：移除 `-m <model>`。

    返回 (new_args, removed)。
    """
    try:
        xs = [str(x) for x in (args or [])]
    except Exception:
        xs = list(args or [])

    # 优先只移除 ttadk code 的 "-m <model>"，避免误删 python 的 "-m <module>"。
    try:
        for i in range(len(xs) - 1):
            if str(xs[i] or "") == "ttadk" and str(xs[i + 1] or "") == "code":
                out = list(xs)
                for j in range(i + 2, len(out) - 1):
                    if str(out[j] or "") == "-m":
                        # delete "-m" and its value
                        try:
                            del out[j : j + 2]
                        except Exception:
                            return (list(xs), False)
                        return (out, True)
                return (list(xs), False)
    except Exception:
        pass

    # fallback: remove first -m <value>
    out2: list[str] = []
    removed = False
    i = 0
    while i < len(xs):
        x = str(xs[i] or "")
        if x == "-m":
            removed = True
            i += 2
            continue
        out2.append(x)
        i += 1
    return (out2, removed)


def _apply_compaction_once(
    *,
    session: SyncSession,
    session_builder: Optional[Callable[..., SyncSession]] = None,
    startup_timeout_s: Optional[float] = None,
) -> Optional[SyncSession]:
    """对当前 session 执行一次“轻量 compaction”处理（best-effort）。

    设计取舍：
    - 这里不尝试“压缩 LLM 上下文”（ACP 协议当前无该能力），而是通过重建会话来清空上下文。
    - 对于支持 resume 的场景，调用方应使用更高层的恢复逻辑；此处只服务于运行期自动自愈。

    返回新 session（已启动）表示已执行并认为可能有帮助；返回 None 表示无法执行。
    """
    # 如果没有必要的生命周期方法，直接失败（避免 AttributeError 冒泡）。
    if not hasattr(session, "close") or not hasattr(session, "start"):
        return None

    # 尽量保留 agent_type/cwd 以便重建（仅对 ACP Session 有意义）。
    agent_type = str(getattr(session, "_agent_type", "") or "")
    cwd = str(getattr(session, "_cwd", "") or "")
    if not agent_type or not cwd:
        return None

    # 继承 cmd/args（特别是 TTADK wrapper / PTY 等启动参数）
    agent_cmd = str(getattr(session, "_agent_cmd", "") or "")
    agent_args = list(getattr(session, "_agent_args", []) or [])

    if not agent_cmd and not agent_args:
        # 不是 ACP backend 或缺少启动信息
        return None

    # 关闭旧会话
    try:
        session.close()
    except Exception:
        # close 失败也继续尝试重建（best-effort）
        pass

    # 重建新会话（仅 ACP 后端），保持相同 cmd/args（即保持同模型）。
    try:
        timeout_s = float(startup_timeout_s or getattr(get_settings(), "acp_startup_timeout", 20) or 20)
    except Exception:
        timeout_s = 20.0
    timeout_s = max(1.0, timeout_s)

    builder = session_builder
    if builder is None:
        def builder(**kwargs):
            return SyncACPSession(**kwargs)

    try:
        new_sess = builder(agent_type=agent_type, cwd=cwd, agent_cmd=agent_cmd, agent_args=list(agent_args))
        new_sess.start(startup_timeout=timeout_s)
        return new_sess
    except Exception:
        return None


def _default_compaction_action(*, session: SyncSession) -> Optional[SyncSession]:
    """默认 compaction 动作（best-effort，可用于生产调用）。"""
    return _apply_compaction_once(session=session)


def _detect_rate_limit(error: Exception) -> Optional[int]:
    """Detect rate limiting from error.  Returns suggested wait seconds or 0 (detected
    but no explicit wait), or None (not a rate-limit error)."""
    msg = str(error)
    for pat in _RATE_LIMIT_PATTERNS:
        if pat.search(msg):
            m = _RETRY_AFTER_RE.search(msg)
            if m:
                val = int(m.group(1))
                return max(1, min(val, 600))  # clamp to [1, 600]
            return 0  # detected but no explicit wait
    return None


class RateLimitAwareSession:
    """Wraps a SyncSession with rate-limit-aware retry on send_prompt().

    Implements the full SyncSession protocol by explicit delegation (no __getattr__).
    """

    def __init__(
        self,
        inner: SyncSession,
        on_rate_limit: Optional[Callable[[int], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ):
        self._inner = inner
        self._on_rate_limit = on_rate_limit
        self._cancel_event = cancel_event or threading.Event()
        self._settings = get_settings()
        # Rate-limit state visible to status queries
        self.rate_limit_until: Optional[float] = None  # monotonic deadline

    def __getattr__(self, name: str):
        """Best-effort proxy for non-protocol attributes.

        说明：本类以“显式 delegation”实现 SyncSession 协议，避免隐藏行为。
        但仓内部分测试/诊断会读取 `_model_name` / `_agent_args` 等私有字段。
        为保持兼容，这里将未知属性透传给 inner。
        """
        return getattr(self._inner, name)

    # --- Explicit SyncSession protocol delegation ---

    @property
    def session_id(self) -> str:
        return self._inner.session_id

    @session_id.setter
    def session_id(self, value: str):
        self._inner.session_id = value

    @property
    def created_at(self) -> float:
        return self._inner.created_at

    @property
    def last_active(self) -> float:
        return self._inner.last_active

    @last_active.setter
    def last_active(self, value: float):
        self._inner.last_active = value

    @property
    def message_count(self) -> int:
        return self._inner.message_count

    @property
    def last_query(self) -> str:
        return self._inner.last_query

    @property
    def is_resumed(self) -> bool:
        return self._inner.is_resumed

    def describe_agent(self) -> str:
        return self._inner.describe_agent()

    def start(self, startup_timeout: float = 60) -> str:
        return self._inner.start(startup_timeout=startup_timeout)

    def load_session(self, session_id: str) -> None:
        self._inner.load_session(session_id)

    def load_local_history(self, session_id: Optional[str] = None, limit: int = 200) -> list[dict]:
        return self._inner.load_local_history(session_id=session_id, limit=limit)

    def cancel(self) -> None:
        self._cancel_event.set()
        self._inner.cancel()

    def close(self) -> None:
        self._inner.close()

    def to_snapshot(self) -> dict:
        return self._inner.to_snapshot()

    def get_session_info(self) -> str:
        return self._inner.get_session_info()

    def is_server_running(self) -> bool:
        return self._inner.is_server_running()

    def is_server_healthy(self, healthcheck_timeout: float = 2.0) -> bool:
        return self._inner.is_server_healthy(healthcheck_timeout=healthcheck_timeout)

    # --- Rate-limit-aware send_prompt ---

    def send_prompt(
        self,
        text: str,
        on_event: Optional[Callable[[ACPEvent], None]] = None,
        timeout: Optional[int] = None,
    ) -> PromptResult:
        if not self._settings.rate_limit_retry_enabled:
            return self._inner.send_prompt(text, on_event=on_event, timeout=timeout)

        max_retries = self._settings.rate_limit_max_retries
        max_wait = self._settings.rate_limit_max_wait
        base_wait = self._settings.rate_limit_base_wait
        last_error: Optional[Exception] = None

        for attempt in range(max_retries + 1):
            try:
                self.rate_limit_until = None
                return self._inner.send_prompt(text, on_event=on_event, timeout=timeout)
            except Exception as e:
                wait_hint = _detect_rate_limit(e)
                if wait_hint is None or attempt >= max_retries:
                    raise
                last_error = e
                wait_time = min(wait_hint or base_wait, max_wait)
                wait_time = max(wait_time, 1)

                # Notify caller (UI) — swallow callback exceptions
                try:
                    if self._on_rate_limit:
                        self._on_rate_limit(wait_time)
                except Exception:
                    pass

                logger.warning(
                    "[RateLimit] 限速检测，等待 %ds 后重试 (attempt=%d/%d): %s",
                    wait_time, attempt + 1, max_retries, e,
                )

                # Interruptible sleep: check cancel_event every second
                self.rate_limit_until = time.monotonic() + wait_time
                deadline = time.monotonic() + wait_time
                while time.monotonic() < deadline:
                    if self._cancel_event.is_set():
                        self.rate_limit_until = None
                        raise last_error  # re-raise original error on cancel
                    remaining = deadline - time.monotonic()
                    self._cancel_event.wait(timeout=min(remaining, 1.0))
                self.rate_limit_until = None

        # Should not reach here, but just in case
        if last_error:
            raise last_error
        return self._inner.send_prompt(text, on_event=on_event, timeout=timeout)


class ModelFailureAwareSession:
    """在 send_prompt 阶段处理模型侧错误（need compaction / loop / failover）。

    当前阶段（任务 4）：仅处理 need compaction：执行一次 compaction 动作并用同模型重试一次。
    后续任务会在此类中扩展 loop 检测与模型 failover。
    """

    def __init__(
        self,
        inner: SyncSession,
        *,
        compaction_action: Optional[Callable[[SyncSession], Optional[SyncSession]]] = None,
        on_rate_limit: Optional[Callable[[int], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ):
        self._inner = inner
        self._settings = get_settings()
        self._compaction_action = compaction_action
        self._on_rate_limit = on_rate_limit
        self._cancel_event = cancel_event or threading.Event()

        # compaction loop detector (per wrapper instance)
        self._compaction_loop_events: list[float] = []

    def _loop_limits(self) -> tuple[float, int]:
        """读取 loop 检测参数（window_s, max_count）。"""
        try:
            window_s = float(getattr(self._settings, "model_failure_compaction_loop_window_s", 180.0) or 180.0)
        except Exception:
            window_s = 180.0
        try:
            max_count = int(getattr(self._settings, "model_failure_compaction_loop_max", 2) or 2)
        except Exception:
            max_count = 2
        window_s = max(0.0, window_s)
        max_count = max(1, max_count)
        return (window_s, max_count)

    def _record_compaction_event_and_check_loop(self) -> tuple[bool, int]:
        """记录一次 compaction 事件，并判断是否达到 loop 阈值。"""
        now = time.time()
        window_s, max_count = self._loop_limits()
        if window_s <= 0:
            # window<=0: 认为每次都在窗口内
            self._compaction_loop_events.append(now)
        else:
            self._compaction_loop_events = [t for t in self._compaction_loop_events if (now - float(t or 0.0)) <= window_s]
            self._compaction_loop_events.append(now)
        n = len(self._compaction_loop_events)
        return (n >= max_count, n)

    # --- Explicit SyncSession protocol delegation ---

    @property
    def session_id(self) -> str:
        return self._inner.session_id

    @session_id.setter
    def session_id(self, value: str):
        self._inner.session_id = value

    @property
    def created_at(self) -> float:
        return self._inner.created_at

    @property
    def last_active(self) -> float:
        return self._inner.last_active

    @last_active.setter
    def last_active(self, value: float):
        self._inner.last_active = value

    @property
    def message_count(self) -> int:
        return self._inner.message_count

    @property
    def last_query(self) -> str:
        return self._inner.last_query

    @property
    def is_resumed(self) -> bool:
        return self._inner.is_resumed

    def describe_agent(self) -> str:
        return self._inner.describe_agent()

    def start(self, startup_timeout: float = 60) -> str:
        return self._inner.start(startup_timeout=startup_timeout)

    def load_session(self, session_id: str) -> None:
        self._inner.load_session(session_id)

    def load_local_history(self, session_id: Optional[str] = None, limit: int = 200) -> list[dict]:
        return self._inner.load_local_history(session_id=session_id, limit=limit)

    def cancel(self) -> None:
        self._cancel_event.set()
        self._inner.cancel()

    def close(self) -> None:
        self._inner.close()

    def to_snapshot(self) -> dict:
        return self._inner.to_snapshot()

    def get_session_info(self) -> str:
        return self._inner.get_session_info()

    def is_server_running(self) -> bool:
        return self._inner.is_server_running()

    def is_server_healthy(self, healthcheck_timeout: float = 2.0) -> bool:
        return self._inner.is_server_healthy(healthcheck_timeout=healthcheck_timeout)

    # --- Internal helpers ---

    def _unwrap_rate_limit(self) -> tuple[SyncSession, Callable[[SyncSession], SyncSession]]:
        """若 inner 是 RateLimitAwareSession，则解包到其底层 session，并提供 rewrap 函数。"""
        inner = self._inner
        if isinstance(inner, RateLimitAwareSession):
            base = getattr(inner, "_inner", None) or inner

            def _rewrap(new_base: SyncSession) -> SyncSession:
                return RateLimitAwareSession(inner=new_base, on_rate_limit=self._on_rate_limit, cancel_event=self._cancel_event)

            return base, _rewrap

        def _id(new_base: SyncSession) -> SyncSession:
            return new_base

        return inner, _id

    def _do_compaction(self) -> bool:
        """执行一次 compaction，并在成功时替换 self._inner。"""
        action = self._compaction_action
        if action is None:
            # 默认行为：重建同 cmd/args 的 ACP session
            action = lambda s: _default_compaction_action(session=s)

        base, rewrap = self._unwrap_rate_limit()
        try:
            new_base = action(base)
        except Exception:
            new_base = None

        if new_base is None:
            return False
        self._inner = rewrap(new_base)
        return True

    def _parse_failover_map(self) -> dict[str, str]:
        """解析 failover 映射（from:to）。"""
        raw = ""
        try:
            raw = str(getattr(self._settings, "model_failure_failover_map", "") or "")
        except Exception:
            raw = ""
        pairs = []
        for chunk in raw.replace(",", " ").split():
            s = (chunk or "").strip()
            if not s or ":" not in s:
                continue
            a, b = s.split(":", 1)
            a, b = a.strip(), b.strip()
            if a and b:
                pairs.append((a, b))
        out: dict[str, str] = {}
        for a, b in pairs:
            if a not in out:
                out[a] = b
        return out

    def _do_failover(self, *, from_model: str, to_model: str) -> bool:
        """执行一次 failover：切换到 to_model，并重建 session 后替换 self._inner。"""
        from_model = str(from_model or "").strip()
        to_model = str(to_model or "").strip()
        if not to_model:
            return False

        base, rewrap = self._unwrap_rate_limit()
        agent_cmd = str(getattr(base, "_agent_cmd", "") or "")
        agent_args = list(getattr(base, "_agent_args", []) or [])
        agent_type = str(getattr(base, "_agent_type", "") or "")
        cwd = str(getattr(base, "_cwd", "") or "")
        if not agent_cmd and not agent_args:
            return False
        if not agent_type or not cwd:
            return False

        new_args, replaced = _replace_model_in_agent_args(agent_args, to_model)
        if not replaced:
            return False

        # close old
        try:
            base.close()
        except Exception:
            pass

        # rebuild and start
        try:
            timeout_s = float(getattr(self._settings, "acp_startup_timeout", 20) or 20)
        except Exception:
            timeout_s = 20.0
        timeout_s = max(1.0, timeout_s)

        try:
            new_base = SyncACPSession(agent_type=agent_type, cwd=cwd, agent_cmd=agent_cmd, agent_args=list(new_args))
            new_base.start(startup_timeout=timeout_s)
        except Exception:
            return False

        self._inner = rewrap(new_base)
        return True

    def _is_ttadk_agent_type(self, agent_type: str) -> bool:
        try:
            return str(agent_type or "").strip().lower().startswith("ttadk_")
        except Exception:
            return False

    def _extract_ttadk_tool_name(self, *, agent_type: str, agent_args: list[str]) -> str:
        """best-effort 提取 ttadk tool 名称（例如 codex/claude/coco）。"""
        try:
            at = str(agent_type or "").strip().lower()
        except Exception:
            at = ""
        if at.startswith("ttadk_"):
            return at.replace("ttadk_", "", 1)

        # fallback: try parse "-t <tool>" from args
        try:
            xs = [str(x) for x in (agent_args or [])]
        except Exception:
            xs = list(agent_args or [])
        for i, x in enumerate(xs):
            if x in ("-t", "--tool") and i + 1 < len(xs):
                v = str(xs[i + 1] or "").strip().lower()
                if v:
                    return v
        return ""

    def _runtime_invalid_model_settings(self) -> tuple[bool, bool, int, float]:
        """读取运行期 invalid-model 自愈配置（enabled, allow_autoswitch, max_retries, cooldown_s）。"""
        s = self._settings
        try:
            enabled = bool(getattr(s, "ttadk_runtime_retry_enabled", True))
        except Exception:
            enabled = True
        try:
            allow_autoswitch = bool(getattr(s, "ttadk_runtime_retry_allow_autoswitch", True))
        except Exception:
            allow_autoswitch = True
        try:
            max_retries = int(getattr(s, "ttadk_runtime_max_retries", 1) or 1)
        except Exception:
            max_retries = 1
        try:
            cooldown_s = float(getattr(s, "ttadk_runtime_retry_cooldown_s", 120.0) or 120.0)
        except Exception:
            cooldown_s = 120.0
        max_retries = max(0, max_retries)
        cooldown_s = max(0.0, cooldown_s)
        return enabled, allow_autoswitch, max_retries, cooldown_s

    def _pick_best_ttadk_retry_model(self, *, tool_name: str, input_model: str, available_models: list[str], allow_autoswitch: bool) -> str | None:
        """从 available_models 中选择候选真实 model（best-effort）。"""
        try:
            cands = [str(x).strip() for x in (available_models or []) if str(x).strip()]
        except Exception:
            cands = []
        if not cands:
            return None

        tool = str(tool_name or "").strip().lower()
        if tool:
            try:
                tool_cands = [m for m in cands if tool in m.lower()]
            except Exception:
                tool_cands = []
        else:
            tool_cands = []

        pool = tool_cands or cands
        if not allow_autoswitch:
            return pool[0]
        try:
            from .ttadk.models import choose_best_available_model

            best = choose_best_available_model(input_model=str(input_model or ""), available_models=list(pool))
            best = str(best).strip() if best is not None else ""
            return best or pool[0]
        except Exception:
            return pool[0]

    def _do_ttadk_auto(self) -> bool:
        """将当前 TTADK 会话切换到 auto（移除 -m）并重建 session。"""
        base, rewrap = self._unwrap_rate_limit()
        agent_cmd = str(getattr(base, "_agent_cmd", "") or "")
        agent_args = list(getattr(base, "_agent_args", []) or [])
        agent_type = str(getattr(base, "_agent_type", "") or "")
        cwd = str(getattr(base, "_cwd", "") or "")
        if not agent_cmd and not agent_args:
            return False
        if not agent_type or not cwd:
            return False
        if not self._is_ttadk_agent_type(agent_type):
            return False

        new_args, removed = _remove_model_in_agent_args(agent_args)
        if not removed:
            # already auto
            return False

        try:
            base.close()
        except Exception:
            pass

        try:
            timeout_s = float(getattr(self._settings, "acp_startup_timeout", 20) or 20)
        except Exception:
            timeout_s = 20.0
        timeout_s = max(1.0, timeout_s)

        try:
            new_base = SyncACPSession(agent_type=agent_type, cwd=cwd, agent_cmd=agent_cmd, agent_args=list(new_args))
            new_base.start(startup_timeout=timeout_s)
        except Exception:
            return False

        self._inner = rewrap(new_base)
        return True

    def _do_degrade_to_coco(self) -> bool:
        """运行期降级：切换到 coco ACP session 并替换 inner（best-effort）。"""
        base, rewrap = self._unwrap_rate_limit()
        cwd = str(getattr(base, "_cwd", "") or "")
        if not cwd:
            return False

        try:
            from .acp.sync_adapter import start_session_with_retry
            from .coco_model import get_coco_model_manager

            model = get_coco_model_manager().get_current_model()
            timeout_s = float(getattr(self._settings, "acp_startup_timeout", 20) or 20)
            new_base = start_session_with_retry(agent_type="coco", cwd=cwd, startup_timeout=timeout_s, model_name=model)
        except Exception:
            return False

        try:
            # best-effort 标记
            setattr(new_base, "_degraded_to", "coco")
        except Exception:
            pass
        self._inner = rewrap(new_base)
        return True

    # --- Model-failure-aware send_prompt ---

    def send_prompt(
        self,
        text: str,
        on_event: Optional[Callable[[ACPEvent], None]] = None,
        timeout: Optional[int] = None,
    ) -> PromptResult:
        compaction_tried = False
        failover_tried = False
        invalid_real_tried = False
        invalid_auto_tried = False
        degraded_tried = False
        runtime_attempts: list[dict] = []

        def _set_last_attempts():
            # 仅供诊断/单测：best-effort
            try:
                self._last_runtime_invalid_model_attempts = list(runtime_attempts)
            except Exception:
                pass

        def _record_attempt(step: str, *, ok: bool, tool: str = "", input_model: str = "", passthrough_model: str | None = None, error: Exception | None = None, extra: Optional[dict] = None):
            d: dict = {
                "phase": "runtime_invalid_model",
                "step": str(step or ""),
                "ok": bool(ok),
                "tool": str(tool or ""),
                "input_model": str(input_model or ""),
                "passthrough_model": passthrough_model,
            }
            if error is not None:
                d["error_type"] = type(error).__name__
                try:
                    d["error"] = str(error) or "(empty)"
                except Exception:
                    d["error"] = "(empty)"
            if extra:
                try:
                    d.update(dict(extra))
                except Exception:
                    pass
            runtime_attempts.append(d)
            _set_last_attempts()

        while True:
            try:
                return self._inner.send_prompt(text, on_event=on_event, timeout=timeout)
            except Exception as e:
                # ------------------------------------------------------------
                # TTADK runtime invalid-model self-healing (SSOT entry)
                # ------------------------------------------------------------
                base = getattr(self._inner, "_inner", self._inner)
                agent_type = str(getattr(base, "_agent_type", "") or "")
                if self._is_ttadk_agent_type(agent_type):
                    enabled, allow_autoswitch, max_retries, cooldown_s = self._runtime_invalid_model_settings()
                    if enabled and max_retries > 0:
                        try:
                            from .ttadk.models import build_invalid_model_context
                            from .ttadk import get_ttadk_manager

                            ctx = build_invalid_model_context(e, get_settings_fn=get_settings, limit=1600)
                            is_invalid = bool(ctx.get("is_invalid_model"))
                            available_models = list(ctx.get("available_models") or [])
                        except Exception:
                            ctx = {}
                            is_invalid = False
                            available_models = []

                        if is_invalid:
                            try:
                                args0 = list(getattr(base, "_agent_args", []) or [])
                            except Exception:
                                args0 = []
                            tool = self._extract_ttadk_tool_name(agent_type=agent_type, agent_args=args0)
                            input_model = _extract_model_from_agent_args(args0)

                            _record_attempt(
                                "detect",
                                ok=True,
                                tool=tool,
                                input_model=input_model,
                                extra={"available_models_count": len(available_models), "retry_count": int(invalid_real_tried) + int(invalid_auto_tried)},
                            )

                            # cooldown gate
                            try:
                                mgr = get_ttadk_manager()
                                allowed, last_ts = mgr.check_and_mark_runtime_invalid_model_repair(
                                    tool_name=tool,
                                    cooldown_s=float(cooldown_s or 0.0),
                                    now_ts=time.time(),
                                )
                            except Exception:
                                allowed, last_ts = True, 0.0

                            if not allowed:
                                _record_attempt(
                                    "cooldown_skip",
                                    ok=False,
                                    tool=tool,
                                    input_model=input_model,
                                    extra={"cooldown_s": float(cooldown_s or 0.0), "last_ts": float(last_ts or 0.0)},
                                )
                                raise

                            # 1) retry with real model once
                            if (not invalid_real_tried) and max_retries >= 1:
                                candidate = self._pick_best_ttadk_retry_model(
                                    tool_name=tool,
                                    input_model=input_model,
                                    available_models=available_models,
                                    allow_autoswitch=allow_autoswitch,
                                )
                                candidate = str(candidate or "").strip() or None
                                if candidate and candidate != str(input_model or "").strip():
                                    invalid_real_tried = True
                                    ok = self._do_failover(from_model=str(input_model or ""), to_model=str(candidate or ""))
                                    _record_attempt(
                                        "retry_real_model",
                                        ok=bool(ok),
                                        tool=tool,
                                        input_model=input_model,
                                        passthrough_model=candidate,
                                        extra={"retry_count": int(invalid_real_tried) + int(invalid_auto_tried)},
                                    )
                                    logger.warning(
                                        "[TTADK:RuntimeInvalidModel] action=retry_real_model ok=%s tool=%s input_model=%s to_model=%s available_models=%d",
                                        bool(ok),
                                        tool,
                                        input_model,
                                        candidate,
                                        len(available_models),
                                    )
                                    if ok:
                                        continue

                            # 2) retry with auto once
                            if (not invalid_auto_tried) and max_retries >= 1:
                                invalid_auto_tried = True
                                ok = self._do_ttadk_auto()
                                _record_attempt(
                                    "retry_auto",
                                    ok=bool(ok),
                                    tool=tool,
                                    input_model=input_model,
                                    passthrough_model=None,
                                    extra={"retry_count": int(invalid_real_tried) + int(invalid_auto_tried)},
                                )
                                logger.warning(
                                    "[TTADK:RuntimeInvalidModel] action=retry_auto ok=%s tool=%s input_model=%s",
                                    bool(ok),
                                    tool,
                                    input_model,
                                )
                                if ok:
                                    continue

                            # 3) degrade to coco (best-effort)
                            if not degraded_tried:
                                degraded_tried = True
                                ok = self._do_degrade_to_coco()
                                _record_attempt(
                                    "degrade_to_coco",
                                    ok=bool(ok),
                                    tool=tool,
                                    input_model=input_model,
                                    extra={"retry_count": int(invalid_real_tried) + int(invalid_auto_tried), "degraded": bool(ok)},
                                )
                                logger.warning(
                                    "[TTADK:RuntimeInvalidModel] action=degrade_to_coco ok=%s tool=%s input_model=%s",
                                    bool(ok),
                                    tool,
                                    input_model,
                                )
                                if ok:
                                    # 下一轮循环会对 coco 会话执行 send_prompt（达到“自动降级不崩溃”的目标）
                                    continue

                            # give up: raise a diagnosable error (keep original as cause)
                            try:
                                err = RuntimeError("ttadk_runtime_invalid_model_unrecoverable")
                                try:
                                    setattr(err, "tool_name", tool)
                                    setattr(err, "input_model", input_model)
                                    setattr(err, "available_models_count", len(available_models))
                                    setattr(err, "attempts", list(runtime_attempts))
                                except Exception:
                                    pass
                                raise err from e
                            except Exception:
                                raise

                info = classify_model_failure(error=e)

                # 1) loop detected: attempt failover once
                if info.get("reason") == "loop_detected" and not failover_tried:
                    failover_tried = True
                    failed = info.get("failed_model") or _extract_model_from_agent_args(list(getattr(getattr(self._inner, "_inner", self._inner), "_agent_args", []) or []))
                    fmap = self._parse_failover_map()
                    target = fmap.get(str(failed or "").strip()) or fmap.get("gpt-5.2")
                    ok = self._do_failover(from_model=str(failed or ""), to_model=str(target or ""))
                    logger.warning(
                        "[ModelFailure] action=failover reason=loop_detected fail_phase=model_loop failover=%s from_model=%s to_model=%s attempt_count=%d",
                        bool(ok),
                        failed or "",
                        target or "",
                        int(info.get("attempt_count") or 0),
                    )
                    if ok:
                        continue
                    raise

                # 2) need compaction:
                #    - 记录事件用于 loop 检测
                #    - 首次命中：先 compaction，再同模型重试一次
                #    - 若 compaction 后仍命中 need_compaction（或达到 loop 阈值）：触发 failover 一次
                if info.get("reason") == "need_compaction":
                    # loop detection (record every time)
                    is_loop, n = self._record_compaction_event_and_check_loop()
                    try:
                        info["attempt_count"] = int(n)
                    except Exception:
                        pass

                    # feature flag: disabled => no auto-repair
                    try:
                        if not bool(getattr(self._settings, "model_failure_compaction_enabled", True)):
                            raise
                    except Exception:
                        pass

                    # If loop detected: suppress compaction and attempt failover once.
                    if is_loop:
                        try:
                            window_s, max_count = self._loop_limits()
                        except Exception:
                            window_s, max_count = (0.0, 1)
                        logger.warning(
                            "[ModelFailure] action=suppress reason=need_compaction fail_phase=model_loop attempt_count=%d loop_window_s=%.1f loop_max=%d",
                            int(n),
                            float(window_s or 0.0),
                            int(max_count or 1),
                        )
                        if not failover_tried:
                            failover_tried = True
                            failed = info.get("failed_model") or _extract_model_from_agent_args(
                                list(getattr(getattr(self._inner, "_inner", self._inner), "_agent_args", []) or [])
                            )
                            fmap = self._parse_failover_map()
                            target = fmap.get(str(failed or "").strip()) or fmap.get("gpt-5.2")
                            ok = self._do_failover(from_model=str(failed or ""), to_model=str(target or ""))
                            logger.warning(
                                "[ModelFailure] action=failover reason=need_compaction fail_phase=model_loop failover=%s from_model=%s to_model=%s attempt_count=%d",
                                bool(ok),
                                failed or "",
                                target or "",
                                int(n),
                            )
                            if ok:
                                continue
                        raise

                    # Not loop: if compaction already tried once, attempt failover once.
                    if compaction_tried and (not failover_tried):
                        failover_tried = True
                        failed = info.get("failed_model") or _extract_model_from_agent_args(
                            list(getattr(getattr(self._inner, "_inner", self._inner), "_agent_args", []) or [])
                        )
                        fmap = self._parse_failover_map()
                        target = fmap.get(str(failed or "").strip()) or fmap.get("gpt-5.2")
                        ok = self._do_failover(from_model=str(failed or ""), to_model=str(target or ""))
                        logger.warning(
                            "[ModelFailure] action=failover reason=need_compaction fail_phase=model_loop failover=%s from_model=%s to_model=%s attempt_count=%d",
                            bool(ok),
                            failed or "",
                            target or "",
                            int(n),
                        )
                        if ok:
                            continue

                    # First time: do compaction once
                    if not compaction_tried:
                        compaction_tried = True
                        ok = self._do_compaction()
                        logger.warning(
                            "[ModelFailure] action=compaction reason=need_compaction fail_phase=model_compaction compaction=%s model=%s failover_to=%s attempt_count=%d",
                            bool(ok),
                            info.get("failed_model") or "",
                            info.get("failover_to") or "",
                            int(info.get("attempt_count") or 0),
                        )
                        if ok:
                            continue
                raise


def close_session_safely(session: Optional[SyncSession]) -> None:
    """Close an ACP/CLI session, ignoring errors."""
    if session:
        try:
            session.close()
        except Exception as e:
            logger.debug("关闭旧ACP session失败: %s", e)


def resolve_ttadk_engine_startup_model(
    *,
    agent_type: str,
    cwd: str,
    model_intent: Optional[str],
) -> dict:
    """为 Deep/Loop/Spec 引擎统一解析 TTADK 启动模型。

    注意：该函数仅做“启动阶段预校验”，不做执行阶段强校验/纠错。
    统一收敛到 `src.ttadk.startup_common.precheck_ttadk_startup_model()`，避免多处实现漂移。
    """
    from .ttadk.startup_common import precheck_ttadk_startup_model
    from .utils.path import normalize_ttadk_cwd

    raw_cwd = cwd
    norm_cwd = normalize_ttadk_cwd(raw_cwd)
    cwd = norm_cwd or raw_cwd
    try:
        if bool(getattr(get_settings(), "ttadk_cwd_debug_enabled", False)):
            logger.debug("[TTADK:CWD] where=%s raw_cwd=%r normalized_cwd=%r", "agent_session.resolve_ttadk_engine_startup_model", raw_cwd, norm_cwd)
    except Exception:
        pass

    info = precheck_ttadk_startup_model(agent_type=agent_type, cwd=cwd, model_intent=model_intent)
    # 兼容旧调用方字段名：resolved_model
    # 说明：startup_common 已输出 resolved_model；这里仅做 best-effort 兜底，不覆盖其语义。
    if "resolved_model" not in info:
        info["resolved_model"] = info.get("model")
    # 透出诊断（用于引擎日志/排障，不参与逻辑判断）
    if "diagnostics" not in info:
        info["diagnostics"] = {}
    return info


def create_sync_session(agent_type: str, cwd: str, model_name: Optional[str] = None) -> SyncSession:
    """Factory for creating a sync session by backend.

    - coco/default: ACP backend
    - claude: CLI backend
    - ttadk_*: ACP backend (direct agent type)
    """
    from .coco_model import get_coco_model_manager
    from .utils.path import normalize_ttadk_cwd

    agent_type = (agent_type or "").lower()
    raw_cwd = cwd
    norm_cwd = normalize_ttadk_cwd(raw_cwd)
    cwd = norm_cwd or raw_cwd
    try:
        if bool(getattr(get_settings(), "ttadk_cwd_debug_enabled", False)):
            logger.debug("[TTADK:CWD] where=%s raw_cwd=%r normalized_cwd=%r", "agent_session.create_sync_session", raw_cwd, norm_cwd)
    except Exception:
        pass
    if agent_type == "claude":
        return SyncClaudeCLISession(cwd=cwd)

    effective_model = model_name
    if not effective_model and agent_type in ("coco", ""):
        effective_model = get_coco_model_manager().get_current_model()

    if agent_type.startswith("ttadk_"):
        # 该工厂只负责构造 session：启动阶段预校验下沉到统一 helper，validated 才透传 -m。
        try:
            from .ttadk.startup_common import precheck_ttadk_startup_model

            info = precheck_ttadk_startup_model(agent_type=agent_type, cwd=cwd, model_intent=model_name)
            model_name = info.get("model")
            logger.info(
                "[SessionFactory] ttadk precheck(startup): tool=%s input_model=%s model=%s validated=%s source=%s decision=%s fail_phase=%s warnings=%s",
                info.get("tool") or "",
                info.get("input_model") or "",
                (model_name or "(auto)"),
                bool(info.get("validated")),
                info.get("source") or "unknown",
                info.get("decision") or "",
                info.get("fail_phase") or "",
                list(info.get("warnings") or []),
            )
        except Exception:
            model_name = None
        # Switch to CLI backend
        return SyncTTADKCLISession(agent_type=agent_type, cwd=cwd, model_name=model_name)

    return SyncACPSession(agent_type=agent_type or "coco", cwd=cwd, model_name=effective_model)


def create_engine_session(
    agent_type: str,
    cwd: str,
    on_rate_limit: Optional[Callable[[int], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    model_name: Optional[str] = None,
) -> SyncSession:
    """Create and start a session for Deep/Loop/Spec engines.

    - Claude: CLI backend (no ACP retry needed)
    - ttadk_*: CLI backend (no ACP retry needed)
    - Others: ACP backend with retry and progressive timeout

    If rate_limit_retry_enabled is True in settings, the returned session
    is wrapped with RateLimitAwareSession for automatic retry on throttling.
    """
    from .acp.sync_adapter import start_session_with_retry
    from .coco_model import get_coco_model_manager
    from .utils.path import normalize_ttadk_cwd

    settings = get_settings()
    agent_type = (agent_type or "").lower()

    # TTADK/引擎侧 cwd 归一化：避免传入 "." 导致 TTADK 项目级缓存不落盘。
    raw_cwd = cwd
    norm_cwd = normalize_ttadk_cwd(raw_cwd)
    cwd = norm_cwd or raw_cwd
    try:
        if bool(getattr(get_settings(), "ttadk_cwd_debug_enabled", False)):
            logger.debug("[TTADK:CWD] where=%s raw_cwd=%r normalized_cwd=%r", "agent_session.create_engine_session", raw_cwd, norm_cwd)
    except Exception:
        pass

    # 日志语义：
    # - TTADK: 传入的可能是“友好名/意图”，并不等于最终透传 -m 的真实模型名；避免用 `model=` 误导。
    # - 非 TTADK: 依旧输出 `model=` 便于排障。
    if agent_type.startswith("ttadk_"):
        logger.info(
            "[SessionFactory] create_engine_session: agent=%s cwd=%s input_model=%s (ACP mode)",
            agent_type or "coco",
            cwd,
            model_name,
        )
    else:
        logger.info(
            "[SessionFactory] create_engine_session: agent=%s cwd=%s model=%s",
            agent_type or "coco",
            cwd,
            model_name,
        )

    if agent_type == "claude":
        session: SyncSession = SyncClaudeCLISession(cwd=cwd)
        session.start()
    elif agent_type.startswith("ttadk_"):
        # TTADK CLI mode: precheck model then use CLI session
        # Switch to CLI backend as requested, replacing the previous ACP startup coordinator.
        try:
            from .ttadk.startup_common import precheck_ttadk_startup_model

            # 1. Precheck to resolve model name
            info = precheck_ttadk_startup_model(
                agent_type=agent_type,
                cwd=cwd,
                model_intent=model_name
            )

            resolved_model = info.get("model")  # Validated model ID or None (auto)

            logger.info(
                "[SessionFactory] ttadk cli startup: tool=%s input_model=%s model=%s validated=%s source=%s warnings=%s",
                info.get("tool") or "",
                info.get("input_model") or "",
                (resolved_model or "(auto)"),
                bool(info.get("validated")),
                info.get("source") or "unknown",
                list(info.get("warnings") or []),
            )

            # 2. Create CLI session
            session = SyncTTADKCLISession(
                agent_type=agent_type,
                cwd=cwd,
                model_name=resolved_model
            )
            session.start()

        except Exception:
            raise
    else:
        effective_model = model_name
        if not effective_model:
            effective_model = get_coco_model_manager().get_current_model()

        session = start_session_with_retry(
            agent_type=agent_type or "coco",
            cwd=cwd,
            startup_timeout=settings.acp_startup_timeout,
            model_name=effective_model,
        )

    if settings.rate_limit_retry_enabled:
        session = RateLimitAwareSession(
            inner=session,
            on_rate_limit=on_rate_limit,
            cancel_event=cancel_event,
        )

    # Model failure (compaction/loop/failover) auto-repair wrapper.
    # 说明：该 wrapper 只在 send_prompt 阶段生效，不影响启动时 TTADK/ACP 的既有重试逻辑。
    try:
        session = ModelFailureAwareSession(
            inner=session,
            on_rate_limit=on_rate_limit,
            cancel_event=cancel_event,
        )
    except Exception:
        # best-effort: wrapper 失败不应影响正常会话创建
        pass

    return session
