"""TTADK CLI session backend and helpers."""

from __future__ import annotations

import json as _json
import logging
import re as _re
import shlex as _shlex
import subprocess
import threading
import time
import uuid
from typing import Callable, Optional

from ..acp.models import ACPEvent, ACPEventType, PromptResult
from ..ttadk.env_sandbox import build_ttadk_subprocess_env, resolve_ttadk_executable
from ..utils.errors import get_error_detail
from ..utils.retry import RetryPolicy, prompt_with_retry

logger = logging.getLogger(__name__)


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
        if not resolve_ttadk_executable():
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
                logger.debug("SyncTTADKCLISession.cancel: terminate failed", exc_info=True)

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
        cmd.extend(["-a", _build_ttadk_passthrough_prompt(self._tool_name, text)])

        raw_chunks: list[str] = []
        visible_chunks: list[str] = []
        json_chunks: list[str] = []
        stderr_chunks: list[str] = []
        json_mode = False
        json_extractor = _JSONTextExtractor()

        # ANSI escape sequence regex
        ansi_escape = _re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

        def _strip_ansi(s: str) -> str:
            return ansi_escape.sub("", s)

        def _read_stderr(pipe):
            try:
                for line in pipe:
                    stderr_chunks.append(line)
            except Exception:
                logger.debug("_read_stderr: pipe read failed", exc_info=True)

        try:
            # Use unified environment sandbox for consistency and safety
            # This ensures PATH, PYTHONPATH, and other critical vars are set correctly
            # just like in other TTADK components (fetcher, runner).
            env, _ = build_ttadk_subprocess_env(cwd=self._cwd, agent_type=self._agent_type, tool_name=self._tool_name)

            # Force unbuffered output to ensure real-time streaming
            env["PYTHONUNBUFFERED"] = "1"
            # Force no color to simplify output parsing
            env["NO_COLOR"] = "1"
            env["TERM"] = "dumb"

            logger.debug("[TTADK:CLI] cmd=%s cwd=%s", cmd, self._cwd)

            self._proc = subprocess.Popen(
                cmd,
                cwd=self._cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # Line buffered
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
                        current = "".join(json_chunks if json_mode else (visible_chunks or raw_chunks))
                        return PromptResult(stop_reason="cancelled", text=current)

                    if deadline and time.monotonic() > deadline:
                        self._proc.terminate()
                        self._proc.wait(timeout=5)
                        current = "".join(json_chunks if json_mode else (visible_chunks or raw_chunks))
                        return PromptResult(stop_reason="timeout", text=(current + "\n❌ TTADK 执行超时").strip())

                    clean_line = _strip_ansi(line)
                    raw_chunks.append(clean_line)

                    fragments = json_extractor.feed(clean_line)
                    if fragments:
                        json_mode = True
                        for frag in fragments:
                            payload = frag if frag.endswith("\n") else (frag + "\n")
                            json_chunks.append(payload)
                            if on_event:
                                on_event(ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text=payload))
                        continue

                    if json_mode:
                        # 已进入 JSON 输出模式后，仅输出可解析 JSON，避免前后缀噪声混入。
                        continue

                    if json_extractor.has_json_candidate():
                        continue

                    if _is_ttadk_preamble_line(clean_line):
                        continue

                    visible_chunks.append(clean_line)
                    if on_event:
                        on_event(ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text=clean_line))

            self._proc.wait(timeout=30)
            stderr_thread.join(timeout=1)

            rc = int(self._proc.returncode or 0)
            err = _strip_ansi("".join(stderr_chunks).strip())

            if json_mode:
                output = "".join(json_chunks).strip()
            else:
                output = "".join(visible_chunks).strip()
                if not output:
                    output = "".join(raw_chunks).strip()

            if rc != 0:
                if err:
                    output = (output + "\n" + err).strip()
                    if on_event:
                        on_event(ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="\n" + err))
                stop_reason = "failed"
            else:
                stop_reason = "end_turn"

            return PromptResult(stop_reason=stop_reason, text=output)

        except (subprocess.SubprocessError, OSError, TimeoutError) as e:
            return PromptResult(stop_reason="error", text=f"❌ TTADK 执行异常: {get_error_detail(e)}")
        finally:
            proc = self._proc
            self._proc = None
            # Ensure subprocess is terminated even on unexpected exceptions
            # (e.g. KeyboardInterrupt, SystemExit) to prevent zombie processes
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    logger.debug("Failed to terminate TTADK subprocess", exc_info=True)
                    try:
                        proc.kill()
                        proc.wait(timeout=3)
                    except Exception:
                        logger.debug("Failed to kill TTADK subprocess", exc_info=True)

    def send_prompt_with_retry(
        self,
        text: str,
        on_event: Optional[Callable[[ACPEvent], None]] = None,
        timeout: Optional[int] = None,
        retry_policy: Optional[RetryPolicy] = None,
        before_retry: Optional[Callable[[int, Exception], None]] = None,
        total_timeout: Optional[float] = None,
    ) -> PromptResult:
        return prompt_with_retry(
            lambda: self.send_prompt(text, on_event=on_event, timeout=timeout),
            self._cancel_event,
            retry_policy=retry_policy,
            before_retry=before_retry,
            total_timeout=total_timeout,
        )


_TTADK_PREAMBLE_PATTERNS = [
    _re.compile(r"^[\s_/\\|.'\"-]{6,}$"),
    _re.compile(r"^TikTok AI-Driven Development Kit$", _re.IGNORECASE),
    _re.compile(r"^Version\s+\d+\.\d+\.\d+", _re.IGNORECASE),
    _re.compile(r"^Team:\s+", _re.IGNORECASE),
    _re.compile(r"^(?:🚀|👋|✔|⚠|❯)\s"),
    _re.compile(r"^\?\s+Select a model", _re.IGNORECASE),
    _re.compile(r"^↑↓\s+navigate", _re.IGNORECASE),
    _re.compile(r"^Continuing with current version", _re.IGNORECASE),
    _re.compile(r"Login successful", _re.IGNORECASE),
    _re.compile(r"^Launching\s+", _re.IGNORECASE),
]


def _is_ttadk_preamble_line(text: str) -> bool:
    s = str(text or "").strip()
    if not s:
        return True
    for p in _TTADK_PREAMBLE_PATTERNS:
        try:
            if p.search(s):
                return True
        except Exception:
            logger.debug("_is_ttadk_preamble_line: pattern match failed", exc_info=True)
            continue
    return False


_TTADK_PRINT_MODE_TOOLS = frozenset({"coco", "claude", "gemini"})


def _build_ttadk_passthrough_prompt(tool_name: str, prompt: str) -> str:
    """Build the ``-a`` value that passes *prompt* through to the downstream tool.

    ``ttadk code -a <value>`` shell-splits *value* before forwarding to the
    downstream tool CLI, so we use :func:`shlex.join` to produce a properly
    quoted fragment.

    * **coco / claude / gemini** — ``-p <prompt>`` activates *print* (headless)
      mode so the tool outputs the answer and exits without requiring a TTY.
    * **codex** and others — the prompt is passed as a bare positional argument.
    """
    tool = (tool_name or "").strip().lower()
    if tool in _TTADK_PRINT_MODE_TOOLS:
        return _shlex.join(["-p", prompt])
    return _shlex.quote(prompt)


class _JSONTextExtractor:
    """Extract complete JSON values from mixed text output."""

    def __init__(self, max_buffer: int = 512_000):
        self._decoder = _json.JSONDecoder()
        self._buffer = ""
        try:
            self._max_buffer = max(4096, int(max_buffer or 0))
        except Exception:
            logger.debug("_JSONTextExtractor: max_buffer conversion failed", exc_info=True)
            self._max_buffer = 512_000

    @staticmethod
    def _find_json_start(text: str, start: int = 0) -> int:
        p_obj = text.find("{", start)
        p_arr = text.find("[", start)
        if p_obj < 0:
            return p_arr
        if p_arr < 0:
            return p_obj
        return min(p_obj, p_arr)

    def feed(self, chunk: str) -> list[str]:
        if chunk:
            self._buffer += str(chunk)
        return self._drain()

    def has_json_candidate(self) -> bool:
        return self._find_json_start(self._buffer, 0) >= 0

    @staticmethod
    def _looks_incomplete_json(text: str) -> bool:
        s = str(text or "").lstrip()
        if not s or s[0] not in "{[":
            return False

        in_string = False
        escaped = False
        curly = 0
        square = 0
        for ch in s:
            if escaped:
                escaped = False
                continue
            if ch == "\\":
                escaped = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                curly += 1
            elif ch == "}":
                curly = max(0, curly - 1)
            elif ch == "[":
                square += 1
            elif ch == "]":
                square = max(0, square - 1)

        if in_string or curly > 0 or square > 0:
            return True

        tail = s.rstrip()
        if not tail:
            return True
        return tail[-1] in (",", ":", "{", "[")

    def _drain(self) -> list[str]:
        out: list[str] = []
        scan_from = 0
        while True:
            start = self._find_json_start(self._buffer, scan_from)
            if start < 0:
                if len(self._buffer) > self._max_buffer:
                    self._buffer = self._buffer[-self._max_buffer :]
                return out

            try:
                _, end = self._decoder.raw_decode(self._buffer, start)
            except _json.JSONDecodeError:
                candidate = self._buffer[start:]
                if self._looks_incomplete_json(candidate):
                    if len(candidate) > self._max_buffer:
                        candidate = candidate[-self._max_buffer :]
                    self._buffer = candidate
                    return out
                scan_from = start + 1
                continue
            except Exception:
                logger.debug("_JSONTextExtractor._drain: unexpected error during JSON parse", exc_info=True)
                return out

            out.append(self._buffer[start:end])
            self._buffer = self._buffer[end:]
            scan_from = 0
