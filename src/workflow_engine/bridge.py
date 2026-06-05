"""RuntimeBridge — manages the Node.js workflow runtime subprocess."""

from __future__ import annotations

import collections
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Optional

from .constants import (
    DEFAULT_MAX_CONCURRENT,
    HARD_MAX_CONCURRENT,
    MAX_NESTING_DEPTH,
    MAX_QUEUE_SIZE,
    NODE_MIN_VERSION,
    RUNTIME_JS_PATH,
    WORKFLOW_TOTAL_TIMEOUT_S,
)
from .errors import ErrorCategory, _strip_internal_details, sanitize_for_reply
from .models import AgentCallParams, AgentCallResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_node_version(version_str: str) -> tuple[int, ...]:
    """Parse a Node.js version string like 'v20.11.0' into (20, 11, 0)."""
    cleaned = version_str.strip().lstrip("v")
    parts = cleaned.split(".")
    return tuple(int(p) for p in parts if p.isdigit())


# ---------------------------------------------------------------------------
# RuntimeBridge
# ---------------------------------------------------------------------------


class RuntimeBridge:
    """Manages a Node.js subprocess running the workflow runtime.

    Communication uses JSON-RPC 2.0 over stdin/stdout (NDJSON — one JSON
    object per line). The bridge spawns the subprocess, dispatches incoming
    requests/notifications, and provides a thread-safe write path.
    """

    def __init__(
        self,
        script_path: str,
        cwd: str,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        budget_total: int = 0,
        on_agent_call: Optional[Callable[[AgentCallParams], AgentCallResult]] = None,
        on_phase: Optional[Callable[[str], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        allowed_tools: Optional[list[str]] = None,
        nesting_depth: int = 0,
        args: Optional[dict[str, Any]] = None,
    ) -> None:
        self._script_path = script_path
        self._cwd = cwd
        self._max_concurrent = min(max_concurrent, HARD_MAX_CONCURRENT)
        self._budget_total = budget_total
        self._on_agent_call = on_agent_call
        self._on_phase = on_phase
        self._on_log = on_log
        self._cancel_event = cancel_event or threading.Event()
        self._allowed_tools = allowed_tools
        self._nesting_depth = nesting_depth
        self._args = args or {}

        # Subprocess handle
        self._process: Optional[subprocess.Popen] = None

        # Thread-safe stdin writes
        self._write_lock = threading.Lock()

        # Read loop thread
        self._reader_thread: Optional[threading.Thread] = None

        # Incoming message queue (for the run() loop)
        self._msg_queue: collections.deque[dict[str, Any]] = collections.deque()
        self._msg_condition = threading.Condition()

        # ThreadPoolExecutor for agent calls
        self._executor: Optional[ThreadPoolExecutor] = None

        # Separate executor for sub-workflow calls (avoids starvation)
        self._workflow_executor: Optional[ThreadPoolExecutor] = None

        # Track active futures for graceful shutdown (set for O(1) discard)
        self._active_futures: set[Future] = set()
        self._futures_lock = threading.Lock()

        # Parent reference and child tracking for cascade cancellation
        self._parent: Optional["RuntimeBridge"] = None
        self._children: list["RuntimeBridge"] = []
        self._children_lock = threading.Lock()

        # Terminal state
        self._done = False
        self._result: Optional[str] = None
        self._error: Optional[str] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @classmethod
    def check_node_available(cls) -> bool:
        """Check that Node.js is installed and meets the minimum version."""
        node_bin = shutil.which("node")
        if not node_bin:
            logger.warning("Node.js not found in PATH")
            return False

        try:
            result = subprocess.run(
                [node_bin, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                logger.warning("node --version returned non-zero: %s", result.stderr)
                return False

            version = _parse_node_version(result.stdout)
            if version < NODE_MIN_VERSION:
                logger.warning(
                    "Node.js version %s is below minimum %s",
                    result.stdout.strip(),
                    ".".join(str(v) for v in NODE_MIN_VERSION),
                )
                return False

            logger.debug("Node.js version OK: %s", result.stdout.strip())
            return True

        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("Failed to check Node.js version: %s", exc)
            return False

    def start(self) -> None:
        """Spawn the Node.js runtime subprocess and wait for 'ready' signal.

        Raises RuntimeError if the process fails to start or doesn't send
        the ready notification within a reasonable time.
        """
        if self._process is not None:
            raise RuntimeError("RuntimeBridge already started")

        # Resolve paths
        runtime_path = os.path.join(self._cwd, RUNTIME_JS_PATH)
        if not os.path.isfile(runtime_path):
            # Try absolute path as fallback
            runtime_path = RUNTIME_JS_PATH

        node_bin = shutil.which("node")
        if not node_bin:
            raise RuntimeError("Node.js not found in PATH")

        cmd = [node_bin, "--experimental-vm-modules", runtime_path, self._script_path]
        logger.info("Starting Node.js runtime: %s", " ".join(cmd))

        try:
            self._process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self._cwd,
                text=True,
                bufsize=1,  # Line-buffered
            )
        except OSError as exc:
            raise RuntimeError(f"Failed to spawn Node.js process: {exc}") from exc

        # Start the reader thread (daemon so it dies with the process)
        self._reader_thread = threading.Thread(
            target=self._read_loop,
            name="RuntimeBridge-reader",
            daemon=True,
        )
        self._reader_thread.start()

        # Start stderr drain thread to prevent pipe buffer deadlock (NFR-3)
        self._stderr_thread = threading.Thread(
            target=self._stderr_reader,
            name="RuntimeBridge-stderr",
            daemon=True,
        )
        self._stderr_thread.start()

        # Wait for the 'ready' notification
        ready = self._wait_for_notification("ready", timeout=30.0)
        if ready is None:
            self._kill_process()
            stderr_content = self._drain_stderr()
            raise RuntimeError(
                f"Node.js runtime did not send 'ready' within 30s. "
                f"stderr: {stderr_content}"
            )

        # Send init with budget and workflow args
        self._send({
            "jsonrpc": "2.0",
            "method": "init",
            "params": {"budget_total": self._budget_total, "args": self._args},
        })

        # Create executor for agent calls
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_concurrent,
            thread_name_prefix="RuntimeBridge-agent",
        )

        # Separate executor for sub-workflow calls (max 2 concurrent sub-workflows)
        self._workflow_executor = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="RuntimeBridge-subwf",
        )

        logger.info("Node.js runtime ready and initialized")

    def run(self) -> str:
        """Main event loop: process messages from the Node.js runtime.

        Blocks until the runtime sends 'done' or 'error', or until the
        total timeout expires. Returns the final result string.

        Raises:
            RuntimeError: If the subprocess dies unexpectedly, times out,
                or sends an error notification.
        """
        if self._process is None:
            raise RuntimeError("RuntimeBridge not started — call start() first")

        start_time = time.monotonic()
        deadline = start_time + WORKFLOW_TOTAL_TIMEOUT_S

        while not self._done:
            # Check cancellation
            if self._cancel_event.is_set():
                logger.info("Cancel event set — stopping runtime")
                self._send_cancel()
                self._kill_process()
                raise RuntimeError("Workflow cancelled")

            # Check timeout
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.error("Workflow total timeout exceeded (%ds)", WORKFLOW_TOTAL_TIMEOUT_S)
                self._kill_process()
                raise RuntimeError(
                    f"Workflow execution exceeded total timeout of "
                    f"{WORKFLOW_TOTAL_TIMEOUT_S}s"
                )

            # Check process health
            if self._process.poll() is not None and not self._done:
                stderr_content = self._drain_stderr()
                sanitized_stderr = _strip_internal_details(stderr_content)
                raise RuntimeError(
                    f"Node.js process exited unexpectedly with code "
                    f"{self._process.returncode}. stderr: {sanitized_stderr}"
                )

            # Wait for next message
            msg = self._pop_message(timeout=min(1.0, remaining))
            if msg is None:
                continue

            self._dispatch_message(msg)

        # Return result or raise error
        if self._error:
            raise RuntimeError(f"Workflow runtime error: {self._error}")

        return self._result or ""

    def stop(self) -> None:
        """Send cancel notification and kill the subprocess.

        Also cascades cancellation to all child sub-workflows.
        """
        # Cascade cancel to children first (under lock to avoid race)
        with self._children_lock:
            for child in self._children:
                try:
                    child._cancel_event.set()
                    child.stop()
                except Exception:
                    logger.exception("Failed to cascade stop to child sub-workflow")
            self._children.clear()

        # Signal our own cancel event
        self._cancel_event.set()

        if self._process is None:
            return

        try:
            self._send_cancel()
        except (OSError, BrokenPipeError):
            pass  # Process may already be dead

        self._kill_process()

        # Shutdown executors
        if self._executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
        if self._workflow_executor:
            self._workflow_executor.shutdown(wait=False, cancel_futures=True)
            self._workflow_executor = None

        logger.info("RuntimeBridge stopped")

    def is_alive(self) -> bool:
        """Check if the Node.js subprocess is still running."""
        if self._process is None:
            return False
        return self._process.poll() is None

    # ------------------------------------------------------------------
    # Internal: Read loop
    # ------------------------------------------------------------------

    def _read_loop(self) -> None:
        """Reader thread — parses NDJSON lines from stdout.

        Runs as a daemon thread until stdout is closed or the process dies.
        """
        assert self._process is not None
        assert self._process.stdout is not None

        try:
            for line in self._process.stdout:
                line = line.strip()
                if not line:
                    continue

                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("Non-JSON output from runtime: %s", line[:200])
                    continue

                # Push to message queue (bounded)
                with self._msg_condition:
                    if len(self._msg_queue) >= MAX_QUEUE_SIZE:
                        logger.error(
                            "Message queue full (%d) — dropping message",
                            MAX_QUEUE_SIZE,
                        )
                        continue
                    self._msg_queue.append(msg)
                    self._msg_condition.notify()

        except (ValueError, OSError):
            # stdout closed — process is shutting down
            pass

        logger.debug("Reader thread exiting")

    # ------------------------------------------------------------------
    # Internal: Message dispatch
    # ------------------------------------------------------------------

    def _dispatch_message(self, msg: dict[str, Any]) -> None:
        """Route an incoming JSON-RPC message by method."""
        method = msg.get("method")
        params = msg.get("params", {})
        msg_id = msg.get("id")

        # Request from JS runtime (has id — expects a response)
        if msg_id is not None and method is not None:
            self._dispatch_request(msg)
            return

        # Notification from JS runtime (no id)
        if method == "ready":
            # Already handled during start()
            pass
        elif method == "phase":
            title = params.get("title", "")
            if self._on_phase:
                try:
                    self._on_phase(title)
                except Exception:
                    logger.exception("on_phase callback error")
        elif method == "log":
            message = params.get("message", "")
            if self._on_log:
                try:
                    self._on_log(message)
                except Exception:
                    logger.exception("on_log callback error")
        elif method == "done":
            result = params.get("result")
            self._result = json.dumps(result) if result is not None else ""
            self._done = True
        elif method == "error":
            error_msg = params.get("message", "Unknown error")
            stack = params.get("stack", "")
            # Store raw detail for logging; only the message (without stack)
            # propagates as the RuntimeError — handler layer does final sanitization.
            if stack:
                logger.debug("JS runtime error stack:\n%s", stack)
            self._error = error_msg
            self._done = True
        else:
            logger.debug("Unhandled notification method: %s", method)

    def _dispatch_request(self, msg: dict[str, Any]) -> None:
        """Handle an incoming JSON-RPC request (has id, expects response)."""
        method = msg.get("method", "")
        params = msg.get("params", {})
        request_id = msg.get("id")

        if method == "agent_call":
            self._handle_agent_call(params, request_id)
        elif method == "workflow_call":
            self._handle_workflow_call(params, request_id)
        else:
            self._send_error_response(
                request_id,
                code=-32601,
                message=f"Unknown method: {method}",
            )

    def _handle_agent_call(self, params: dict[str, Any], request_id: Any) -> None:
        """Submit an agent call to the thread pool and send response on completion."""
        if self._executor is None:
            self._send_error_response(
                request_id,
                code=-32603,
                message="Executor not available",
            )
            return

        if self._on_agent_call is None:
            self._send_error_response(
                request_id,
                code=-32603,
                message="No agent call handler configured",
            )
            return

        # Backpressure: reject if response queue is overwhelmed
        with self._msg_condition:
            queue_depth = len(self._msg_queue)
        if queue_depth >= MAX_QUEUE_SIZE:
            self._send_error_response(
                request_id,
                code=-32000,
                message="Queue backpressure: too many pending messages, retry later",
            )
            return

        def _execute() -> None:
            try:
                # Parse params into model
                agent_params = AgentCallParams.model_validate(params)
                # Execute the callback
                result = self._on_agent_call(agent_params)
                # Build response payload
                response_data: dict[str, Any] = {}
                if result.parsed is not None:
                    response_data["data"] = result.parsed
                elif result.output is not None:
                    response_data["data"] = result.output
                if result.error:
                    response_data["error"] = result.error

                # Include budget metadata for JS-side tracking
                response_data["_budget_used"] = result.token_usage
                response_data["token_usage"] = result.token_usage
                response_data["duration_s"] = result.duration_s

                self._send_response(request_id, response_data)

            except Exception as exc:
                logger.exception("Agent call failed for request %s", request_id)
                self._send_error_response(
                    request_id,
                    code=-32603,
                    message=sanitize_for_reply(str(exc), ErrorCategory.INTERNAL_ERROR),
                )

        future = self._executor.submit(_execute)
        with self._futures_lock:
            self._active_futures.add(future)
        future.add_done_callback(lambda f: self._discard_future(f))

    # ------------------------------------------------------------------
    # Internal: Sub-workflow call
    # ------------------------------------------------------------------

    def _handle_workflow_call(self, params: dict[str, Any], request_id: Any) -> None:
        """Handle a workflow() call from the JS runtime (sub-workflow nesting).

        Max nesting depth is controlled by constants.MAX_NESTING_DEPTH.
        Sub-workflows run as a new RuntimeBridge subprocess with the same
        agent_call handler.
        Accepts either `script_path` (absolute/relative file) or `name` (template name).
        """
        if self._nesting_depth >= MAX_NESTING_DEPTH:
            self._send_error_response(
                request_id,
                code=-32603,
                message=f"Sub-workflow nesting depth exceeded (max={MAX_NESTING_DEPTH})",
            )
            return

        script_path = params.get("script_path", "")

        # Resolve template name to script_path if not provided directly
        if not script_path:
            name = params.get("name", "")
            if name:
                from .templates import resolve_template_path
                script_path = resolve_template_path(self._cwd, name)
            if not script_path:
                self._send_error_response(
                    request_id,
                    code=-32602,
                    message="workflow_call requires 'script_path' or resolvable 'name' parameter",
                )
                return

        # Resolve script path relative to cwd
        import os
        if not os.path.isabs(script_path):
            script_path = os.path.join(self._cwd, script_path)

        # Path traversal protection: ensure resolved path stays within one of
        # three trusted roots: project cwd, global templates, or builtin templates
        cwd_realpath = os.path.realpath(self._cwd)
        script_realpath = os.path.realpath(script_path)

        # 1. Project directory (implicitly trusted)
        path_allowed = (
            script_realpath.startswith(cwd_realpath + os.sep)
            or script_realpath == cwd_realpath
        )

        # 2. Check trusted template roots (global + builtin)
        if not path_allowed:
            from .constants import TRUSTED_TEMPLATE_ROOTS
            from .templates import _BUILTIN_TEMPLATES_DIR

            # Collect all trusted roots: global from constants + builtin from templates
            trusted_roots: list[str] = []
            for root in TRUSTED_TEMPLATE_ROOTS:
                trusted_roots.append(os.path.realpath(os.path.expanduser(root)))
            # Add builtin templates directory
            trusted_roots.append(os.path.realpath(str(_BUILTIN_TEMPLATES_DIR)))

            for root in trusted_roots:
                if script_realpath.startswith(root + os.sep) or script_realpath == root:
                    path_allowed = True
                    break

        if not path_allowed:
            self._send_error_response(
                request_id,
                code=-32602,
                message="Sub-workflow script path escapes the project directory",
            )
            return

        if not os.path.isfile(script_path):
            self._send_error_response(
                request_id,
                code=-32602,
                message=f"Sub-workflow script not found: {script_path}",
            )
            return

        # Extract args to pass to sub-workflow
        sub_args = params.get("args", {})

        # Calculate isolated sub-workflow budget (ratio of parent budget)
        # Import settings lazily to avoid circular imports
        try:
            from src.config import get_settings
            settings = get_settings()
            subflow_ratio = getattr(settings, "workflow_subflow_budget_ratio", 0.2)
        except Exception:
            subflow_ratio = 0.2
        sub_budget = int(self._budget_total * subflow_ratio) if self._budget_total > 0 else 0

        def _execute_sub_workflow() -> None:
            try:
                # Independent cancel_event for sub-workflow isolation
                # Parent will cascade set() on stop()
                sub_cancel_event = threading.Event()

                sub_bridge = RuntimeBridge(
                    script_path=script_path,
                    cwd=self._cwd,
                    max_concurrent=self._max_concurrent,
                    budget_total=sub_budget,
                    on_agent_call=self._on_agent_call,
                    on_phase=self._on_phase,
                    on_log=self._on_log,
                    cancel_event=sub_cancel_event,
                    allowed_tools=self._allowed_tools,
                    nesting_depth=self._nesting_depth + 1,
                    args=sub_args,
                )
                # Link parent-child for cascade cancellation
                sub_bridge._parent = self
                with self._children_lock:
                    self._children.append(sub_bridge)

                try:
                    sub_bridge.start()
                    result = sub_bridge.run()
                    sub_bridge.stop()
                    self._send_response(request_id, {"data": result})
                finally:
                    # Clean up child reference
                    with self._children_lock:
                        if sub_bridge in self._children:
                            self._children.remove(sub_bridge)
            except Exception as exc:
                logger.exception("Sub-workflow call failed for request %s", request_id)
                self._send_error_response(
                    request_id,
                    code=-32603,
                    message=sanitize_for_reply(str(exc), ErrorCategory.INTERNAL_ERROR),
                )

        if self._workflow_executor is None:
            self._send_error_response(
                request_id,
                code=-32603,
                message="Executor not available for sub-workflow",
            )
            return

        future = self._workflow_executor.submit(_execute_sub_workflow)
        with self._futures_lock:
            self._active_futures.add(future)
        future.add_done_callback(lambda f: self._discard_future(f))

    # ------------------------------------------------------------------
    # Internal: Transport (stdin writes — must be thread-safe)
    # ------------------------------------------------------------------

    def _send(self, msg: dict[str, Any]) -> None:
        """Write a JSON-RPC message to the subprocess stdin (thread-safe)."""
        if self._process is None or self._process.stdin is None:
            return

        line = json.dumps(msg, separators=(",", ":")) + "\n"

        with self._write_lock:
            try:
                self._process.stdin.write(line)
                self._process.stdin.flush()
            except (OSError, BrokenPipeError) as exc:
                logger.debug("Failed to write to stdin: %s", exc)

    def _send_response(self, request_id: Any, result: Any) -> None:
        """Send a JSON-RPC success response."""
        self._send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": result,
        })

    def _send_error_response(
        self, request_id: Any, code: int, message: str
    ) -> None:
        """Send a JSON-RPC error response."""
        self._send({
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        })

    def _send_cancel(self) -> None:
        """Send cancel notification to the JS runtime."""
        self._send({
            "jsonrpc": "2.0",
            "method": "cancel",
            "params": {},
        })

    # ------------------------------------------------------------------
    # Internal: Future tracking
    # ------------------------------------------------------------------

    def _discard_future(self, future: Future) -> None:
        """Remove a completed future from the active set (done callback)."""
        with self._futures_lock:
            self._active_futures.discard(future)

    # ------------------------------------------------------------------
    # Internal: Message queue helpers
    # ------------------------------------------------------------------

    def _pop_message(self, timeout: float = 1.0) -> Optional[dict[str, Any]]:
        """Pop the next message from the queue, blocking up to timeout."""
        with self._msg_condition:
            if not self._msg_queue:
                self._msg_condition.wait(timeout=timeout)
            if self._msg_queue:
                return self._msg_queue.popleft()
        return None

    def _wait_for_notification(
        self, method: str, timeout: float = 30.0
    ) -> Optional[dict[str, Any]]:
        """Wait for a specific notification method, with timeout.

        Messages that don't match are re-queued.
        """
        deadline = time.monotonic() + timeout
        stash: list[dict[str, Any]] = []

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            msg = self._pop_message(timeout=min(0.5, remaining))
            if msg is None:
                # Check if process died while waiting
                if self._process and self._process.poll() is not None:
                    break
                continue

            if msg.get("method") == method:
                # Put stashed messages back (prepend to front of deque)
                if stash:
                    with self._msg_condition:
                        self._msg_queue.extendleft(reversed(stash))
                        self._msg_condition.notify()
                return msg

            stash.append(msg)

        # Timed out — put stashed messages back
        if stash:
            with self._msg_condition:
                self._msg_queue.extendleft(reversed(stash))
                self._msg_condition.notify()

        return None

    # ------------------------------------------------------------------
    # Internal: Process management
    # ------------------------------------------------------------------

    def _kill_process(self) -> None:
        """Terminate and clean up the subprocess."""
        if self._process is None:
            return

        try:
            if self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=5)
        except OSError:
            pass

        # Close handles
        for stream in (self._process.stdin, self._process.stdout, self._process.stderr):
            if stream:
                try:
                    stream.close()
                except OSError:
                    pass

        self._process = None

    def _stderr_reader(self) -> None:
        """Drain stderr continuously to prevent pipe buffer deadlock."""
        assert self._process is not None
        assert self._process.stderr is not None
        try:
            while True:
                line = self._process.stderr.readline()
                if not line:
                    break  # EOF — process closed stderr
                logger.debug("[runtime stderr] %s", line.rstrip())
        except (OSError, ValueError):
            pass

    def _drain_stderr(self) -> str:
        """Read remaining stderr content from the subprocess."""
        if self._process is None or self._process.stderr is None:
            return ""
        try:
            # Non-blocking read of whatever is available
            content = self._process.stderr.read()
            return content.strip() if content else ""
        except (OSError, ValueError):
            return ""
