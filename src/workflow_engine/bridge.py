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
        on_agent_call: Optional[Callable[..., AgentCallResult]] = None,
        on_agent_aborted: Optional[Callable[..., None]] = None,  # (label, reason, request_id=None)
        on_phase: Optional[Callable[[str], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        allowed_tools: Optional[list[str]] = None,
        nesting_depth: int = 0,
        args: Optional[dict[str, Any]] = None,
        initiator_user_id: Optional[str] = None,
    ) -> None:
        self._script_path = script_path
        self._cwd = cwd
        self._max_concurrent = min(max_concurrent, HARD_MAX_CONCURRENT)
        self._on_agent_call = on_agent_call
        self._on_agent_aborted = on_agent_aborted
        self._on_phase = on_phase
        self._on_log = on_log
        self._cancel_event = cancel_event or threading.Event()
        self._allowed_tools = allowed_tools
        self._nesting_depth = nesting_depth
        self._args = args or {}
        # Scoping identity used when resolving workflow_call templates.  Sub
        # workflows inherit this value from their parent so user-scoped
        # templates remain consistent across the tree.  May be None when the
        # bridge is constructed without a known initiator.
        self._initiator_user_id: Optional[str] = initiator_user_id

        # Subprocess handle
        self._process: Optional[subprocess.Popen] = None

        # Thread-safe stdin writes
        self._write_lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock

        # Read loop thread
        self._reader_thread: Optional[threading.Thread] = None

        # Incoming message queue (for the run() loop)
        self._msg_queue: collections.deque[dict[str, Any]] = collections.deque()
        self._msg_condition = threading.Condition()

        # ThreadPoolExecutor for agent calls
        self._executor: Optional[ThreadPoolExecutor] = None

        # Separate executor for sub-workflow calls (avoids starvation)
        self._workflow_executor: Optional[ThreadPoolExecutor] = None

        # Track active futures for graceful shutdown (set for O(1) discard).
        # Once a future is submitted via ``executor.submit`` it is added to
        # ``_active_futures``; the done-callback ``_discard_future`` removes
        # it. ``in_flight_count`` reports the size of that set.
        self._active_futures: set[Future] = set()
        self._futures_lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock

        # Map JSON-RPC request_id → Future for abort propagation. When the JS
        # runtime sends abort_request for a specific in-flight agent call, we
        # cancel the future here.
        self._request_futures: dict[Any, Future] = {}
        self._request_futures_lock = threading.Lock()  # leaf lock

        # Map JSON-RPC request_id → per-call cancel_event. When abort_request
        # arrives, we set this event to interrupt the in-flight ACP session,
        # enabling race() loser agents to stop within seconds rather than
        # running to completion.
        self._request_cancel_events: dict[Any, threading.Event] = {}
        self._request_cancel_events_lock = threading.Lock()  # leaf lock

        # Parent reference and child tracking for cascade cancellation
        self._parent: Optional["RuntimeBridge"] = None
        self._children: list["RuntimeBridge"] = []
        self._children_lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock

        # Shutdown flag to make stop() / cleanup() idempotent
        self._shutdown_done = False

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
            logger.warning("Failed to check Node.js version: %s", repr(exc))
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
            # Minimal environment for Node.js runtime — excludes secrets and
            # API keys from the parent process to enforce sandbox isolation.
            safe_env = {
                "PATH": os.environ.get("PATH", ""),
                "HOME": os.environ.get("HOME", ""),
                "NODE_PATH": os.environ.get("NODE_PATH", ""),
                "LANG": os.environ.get("LANG", "en_US.UTF-8"),
                "TERM": os.environ.get("TERM", "xterm"),
            }
            # Allow explicit NODE_OPTIONS if set (for debugging/flags)
            if os.environ.get("NODE_OPTIONS"):
                safe_env["NODE_OPTIONS"] = os.environ["NODE_OPTIONS"]

            self._process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self._cwd,
                text=True,
                bufsize=1,  # Line-buffered
                env=safe_env,
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

        # Send init with workflow args and max_concurrent so the
        # JS-side parallel() primitive can bound concurrency to match the
        # Python ThreadPoolExecutor size.
        self._send({
            "jsonrpc": "2.0",
            "method": "init",
            "params": {
                "args": self._args,
                "max_concurrent": self._max_concurrent,
            },
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

        Also cascades cancellation to all child sub-workflows and shuts down
        the ThreadPoolExecutors. Idempotent — calling it more than once is
        safe and does nothing after the first successful call.
        """
        if self._shutdown_done:
            return

        # Signal our own cancel event first
        self._cancel_event.set()

        # Cascade cancel to children first (under lock to avoid race)
        with self._children_lock:
            for child in self._children:
                try:
                    child._cancel_event.set()
                    child.stop()
                except Exception:
                    logger.exception("Failed to cascade stop to child sub-workflow")
            self._children.clear()

        # Signal all in-flight agent calls to cancel before killing the
        # process.  Each per-call cancel_event interrupts the ACP session's
        # send_prompt via the cancel-guard thread, so in-flight agents stop
        # within the guard poll interval (~200ms) rather than running to
        # their full 300s timeout.
        with self._request_cancel_events_lock:
            cancel_events_snapshot = list(self._request_cancel_events.values())
        for evt in cancel_events_snapshot:
            try:
                evt.set()
            except Exception:
                logger.debug("Failed to set per-call cancel event during stop()")

        # Kill the Node.js process FIRST before waiting on executor shutdown.
        # This ensures in-flight agent calls get stdin broken and fail-fast
        # rather than blocking stop() for up to 300s per agent.
        if self._process is not None:
            try:
                self._send_cancel()
            except (OSError, BrokenPipeError):
                pass  # Process may already be dead
            self._kill_process()

        # Wait briefly for reader/stderr threads to finish
        if self._reader_thread is not None and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=2.0)
        if hasattr(self, "_stderr_thread") and self._stderr_thread is not None and self._stderr_thread.is_alive():
            self._stderr_thread.join(timeout=2.0)

        # Cancel all pending futures and shut down executors without waiting
        # (process is already dead, in-flight calls will fail with BrokenPipe)
        if self._executor is not None:
            try:
                # Snapshot active futures under the lock, then cancel outside
                # to avoid deadlock: cancel() invokes done callbacks which
                # call _discard_future, which itself acquires _futures_lock.
                with self._futures_lock:
                    futures_to_cancel = list(self._active_futures)
                for future in futures_to_cancel:
                    future.cancel()
                self._executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                self._executor.shutdown(wait=False)
            except Exception:
                logger.debug("RuntimeBridge executor shutdown failed")
            self._executor = None
        if self._workflow_executor is not None:
            try:
                self._workflow_executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                self._workflow_executor.shutdown(wait=False)
            except Exception:
                logger.debug("RuntimeBridge subwf executor shutdown failed")
            self._workflow_executor = None

        # Clear request futures map
        with self._request_futures_lock:
            self._request_futures.clear()
        with self._request_cancel_events_lock:
            self._request_cancel_events.clear()

        self._shutdown_done = True
        logger.info("RuntimeBridge stopped")

    def cleanup(self) -> None:
        """Alias for stop(); ensures all resources including thread pools
        are released. Safe to call multiple times.
        """
        self.stop()

    # Context manager protocol

    def __enter__(self) -> "RuntimeBridge":
        """Enter context manager — returns self for use with `with` statement."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> bool:
        """Exit context manager — stops the bridge.

        Does not suppress exceptions (returns False).
        """
        self.stop()
        return False

    def __del__(self) -> None:
        """Destructor fallback — best-effort cleanup if stop() was never called.

        Defensive: guards against missing attributes and swallows exceptions
        to avoid errors during interpreter shutdown.
        """
        try:
            if hasattr(self, "_shutdown_done") and not self._shutdown_done:
                logger.warning(
                    "RuntimeBridge was not properly stopped; "
                    "call stop() or use as context manager"
                )
                try:
                    self.stop()
                except Exception:
                    logger.debug(
                        "RuntimeBridge __del__ stop() failed (ignored)",
                        exc_info=True,
                    )
        except Exception:
            # Never let __del__ raise — interpreter shutdown is fragile
            pass

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

        consecutive_non_json = 0
        NON_JSON_WARN_THRESHOLD = 10

        try:
            for line in self._process.stdout:
                line = line.strip()
                if not line:
                    continue

                try:
                    msg = json.loads(line)
                    consecutive_non_json = 0
                except json.JSONDecodeError:
                    consecutive_non_json += 1
                    if consecutive_non_json >= NON_JSON_WARN_THRESHOLD:
                        logger.warning(
                            "Non-JSON output from runtime (%d consecutive): %s",
                            consecutive_non_json,
                            line[:200],
                        )
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

        # stdout EOF: signal run() loop to exit (process likely died)
        if not self._done:
            logger.debug("Reader thread: stdout closed, signalling done")
            self._done = True
            if not self._error:
                self._error = "Runtime process closed stdout unexpectedly"

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
            self._result = json.dumps(result, ensure_ascii=False) if result is not None else ""
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
        elif method == "abort_request":
            # JS runtime is asking us to abort a specific in-flight agent request
            request_id = params.get("request_id")
            self._handle_abort_request(request_id)
        elif method == "agent_aborted":
            # JS runtime notifies that an agent was aborted (e.g. race loser).
            # Used to update the progress card so aborted agents no longer
            # show as '执行中'.
            label = params.get("label", "")
            reason = params.get("reason", "Aborted")
            request_id = params.get("request_id")
            if self._on_agent_aborted:
                try:
                    self._on_agent_aborted(label, reason, request_id=request_id)
                except TypeError:
                    # Backward compat: callback only accepts (label, reason)
                    self._on_agent_aborted(label, reason)
                except Exception:
                    logger.exception("on_agent_aborted callback error")
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

        # Backpressure layer 1: reject if inbound JS→Python queue is full.
        # This preserves the historical MAX_QUEUE_SIZE ceiling used by
        # regression tests and prevents a runaway JS parallel() from
        # saturating the bridge loop.
        with self._msg_condition:
            inbound_depth = len(self._msg_queue)
        if inbound_depth >= MAX_QUEUE_SIZE:
            logger.warning(
                "[RuntimeBridge] backpressure rejecting agent_call: "
                "inbound queue full (%d >= %d)",
                inbound_depth,
                MAX_QUEUE_SIZE,
            )
            self._send_error_response(
                request_id,
                code=-32000,
                message=(
                    "Queue backpressure: too many pending messages, "
                    "retry later"
                ),
            )
            return

        # Backpressure layer 2: reject if the executor pool is overwhelmed.
        # ``_active_futures`` tracks submitted-but-in-flight futures. The pool's
        # own queue length is bounded by ``_max_concurrent * 2`` so transient bursts
        # still succeed while sustained floods are throttled.
        with self._futures_lock:
            active_count = len(self._active_futures)
        pending_response_pressure = active_count
        # Cap at 2x the pool size so a transient burst does not reject
        # valid work, but a sustained flood is throttled.
        pressure_cap = max(2, self._max_concurrent * 2)
        if pending_response_pressure >= pressure_cap:
            logger.warning(
                "[RuntimeBridge] backpressure rejecting agent_call "
                "(active=%d, cap=%d)",
                active_count,
                pressure_cap,
            )
            self._send_error_response(
                request_id,
                code=-32000,
                message=(
                    "Queue backpressure: too many pending agent calls, "
                    "retry later"
                ),
            )
            return

        def _execute() -> None:
            try:
                # Parse params into model
                agent_params = AgentCallParams.model_validate(params)
                # Resolve empty tool to the first allowed tool
                if not agent_params.tool and self._allowed_tools:
                    agent_params.tool = self._allowed_tools[0]
                # Get the per-call cancel event (set by _handle_abort_request)
                with self._request_cancel_events_lock:
                    call_cancel_event = self._request_cancel_events.get(request_id)
                # Execute the callback — pass per-call cancel_event if supported
                try:
                    result = self._on_agent_call(
                        agent_params,
                        cancel_event=call_cancel_event,
                        request_id=request_id,
                    )
                except TypeError:
                    # Backward compat: callback only accepts (params, cancel_event) or just params
                    try:
                        result = self._on_agent_call(
                            agent_params,
                            cancel_event=call_cancel_event,
                        )
                    except TypeError:
                        result = self._on_agent_call(agent_params)
                # Build response payload
                response_data: dict[str, Any] = {}
                if result.parsed is not None:
                    response_data["data"] = result.parsed
                elif result.output is not None:
                    response_data["data"] = result.output
                if result.error:
                    response_data["error"] = result.error

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
            finally:
                # Unregister request future and cancel_event mappings
                with self._request_futures_lock:
                    self._request_futures.pop(request_id, None)
                with self._request_cancel_events_lock:
                    self._request_cancel_events.pop(request_id, None)

        # Create per-call cancel event (child of global cancel_event)
        # Setting the global event should also trigger per-call checks, but we
        # use a dedicated event for precise single-call cancellation (e.g. race).
        per_call_cancel = threading.Event()
        with self._request_cancel_events_lock:
            self._request_cancel_events[request_id] = per_call_cancel

        future = self._executor.submit(_execute)
        with self._futures_lock:
            self._active_futures.add(future)
        with self._request_futures_lock:
            self._request_futures[request_id] = future
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

        # SECURITY: Reject raw script_path from the runtime entirely. All
        # sub-workflow resolution MUST go through validate_template_name +
        # resolve_template_path so that user, project, global (allowlisted) global,
        # and built-in scopes are enforced uniformly. Accepting script_path would
        # bypasses those checks and could allow path traversal / scope bypasses.
        if params.get("script_path"):
            self._send_error_response(
                request_id,
                code=-32602,
                message=(
                    "Sub-workflow invocation by raw script_path is forbidden. "
                    "Use workflow('<template_name') instead."
                ),
            )
            return

        name = params.get("name", "")
        if not name or not isinstance(name, str) or not name.strip():
            self._send_error_response(
                request_id,
                code=-32602,
                message="workflow() requires a 'name' argument",
            )
            return

        from .templates import (
            resolve_template_path,
            validate_template_name,
        )

        # 名称合法性校验（拒绝 '/' '\\' '..' 与绝对路径形式)
        name = name.strip()
        ok, err = validate_template_name(name)
        if not ok:
            self._send_error_response(
                request_id,
                code=-32602,
                message=f"Invalid sub-workflow name: {err}",
            )
            return

        # 统一解析：user > project > (allowlisted) global > builtin
        # resolve_template_path 在 global 查找时内部会校验 WORKFLOW_GLOBAL_TEMPLATE_ALLOWLIST。
        resolved = resolve_template_path(
            self._cwd,
            name,
            user_id=self._initiator_user_id,
        )
        if not resolved:
            self._send_error_response(
                request_id,
                code=-32602,
                message=f"Sub-workflow template not found: {name}",
            )
            return

        script_path = resolved

        # --- Sub-workflow tool-allowlist preflight --------------------------
        # Read the template's meta block and ensure every tool it declares is
        # present in the parent's allowed_tools. Failing here (before any
        # sub-process is spawned) keeps the failure cheap and gives handlers a
        # structured payload to surface in the confirmation card.
        sub_meta_tools: list[str] = []
        sub_description: str | None = None
        try:
            with open(script_path, "r", encoding="utf-8") as f:
                sub_content = f.read()
        except OSError as exc:
            self._send_error_response(
                request_id,
                code=-32003,
                message=f"Failed to read sub-workflow template '{name}': {exc}",
            )
            return

        try:
            from .script_gen import extract_meta_from_script

            parsed = extract_meta_from_script(sub_content) or {}
            sub_meta_tools = list(parsed.get("tools", []) or [])
            sub_description = parsed.get("description")
        except Exception as exc:  # noqa: BLE001  — fail-safe, not a security issue
            logger.warning(
                "[RuntimeBridge] Failed to parse sub-workflow meta for %s: %s",
                name,
                exc,
            )
            parsed = {}
            sub_meta_tools = []

        missing_tools = sorted(
            {t for t in sub_meta_tools if t not in set(self._allowed_tools or [])}
        )
        if missing_tools:
            self._send_error_response(
                request_id,
                code=-32004,
                message=(
                    f"Sub-workflow '{name}' requires tools not present in "
                    f"the parent allowlist: missing={missing_tools}; "
                    f"allowed={sorted(self._allowed_tools or [])}"
                ),
            structured={
                "kind": "missing_tools",
                "name": name,
                "description": sub_description,
                "missing_tools": missing_tools,
                "allowed_tools": sorted(self._allowed_tools or []),
            },
            )
            return

        # Extract args to pass to sub-workflow
        sub_args = params.get("args", {})

        def _execute_sub_workflow() -> None:
            try:
                # Independent cancel_event for sub-workflow isolation
                # Parent will cascade set() on stop()
                sub_cancel_event = threading.Event()

                sub_bridge = RuntimeBridge(
                    script_path=script_path,
                    cwd=self._cwd,
                    max_concurrent=self._max_concurrent,
                    on_agent_call=self._on_agent_call,
                    on_agent_aborted=self._on_agent_aborted,
                    on_phase=self._on_phase,
                    on_log=self._on_log,
                    cancel_event=sub_cancel_event,
                    allowed_tools=self._allowed_tools,
                    nesting_depth=self._nesting_depth + 1,
                    args=sub_args,
                    initiator_user_id=self._initiator_user_id,
                )
                # Link parent-child for cascade cancellation
                sub_bridge._parent = self
                with self._children_lock:
                    self._children.append(sub_bridge)

                try:
                    sub_bridge.start()
                    result = sub_bridge.run()
                    sub_bridge.stop()

                    response_data: dict[str, Any] = {"data": result}
                    self._send_response(request_id, response_data)
                finally:
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
            except (OSError, BrokenPipeError, ValueError) as exc:
                logger.debug("Failed to write to stdin: %s", repr(exc))
                # Process died or pipe broken — signal run() loop to exit
                if not self._done:
                    self._error = f"Runtime connection lost: {exc}"
                    self._done = True

    def _send_response(self, request_id: Any, result: Any) -> None:
        """Send a JSON-RPC success response."""
        self._send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": result,
        })

    def _send_error_response(
        self,
        request_id: Any,
        code: int,
        message: str,
        structured: dict[str, Any] | None = None,
    ) -> None:
        """Send a JSON-RPC error response.

        Args:
            request_id: JSON-RPC request id.
            code: Numeric error code.
            message: Human-readable message.
            structured: Optional machine-readable payload surfaced in the
                ``data`` field so callers (handlers, tests) can branch on
                specific failure kinds (e.g. missing tools in sub-workflows).
        """
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }
        if structured:
            payload["error"]["data"] = structured
        self._send(payload)

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

    def _handle_abort_request(self, request_id: Any) -> None:
        """Handle an abort_request notification from the JS runtime.

        Sets the per-call cancel_event to interrupt the in-flight ACP session
        (e.g. for race() loser cancellation). Also cancels the future if not
        yet started. The per-call cancel_event ensures the session's
        send_prompt loop exits within its poll interval (typically ~1s),
        allowing the session to close cleanly and quickly.

        Uses find-and-set for the cancel_event (not pop) so the worker
        thread can still retrieve it from ``_request_cancel_events``.
        This eliminates a race where an early abort would pop the event
        before the worker retrieved it, causing the worker to fall back
        to the global cancel_event and ignore the per-call abort.
        """
        # Set the per-call cancel event first — this is the fast path for
        # interrupting in-flight sessions that poll cancel_event.
        # Use get (not pop) — the worker thread may not have retrieved it yet.
        with self._request_cancel_events_lock:
            call_cancel = self._request_cancel_events.get(request_id)
        if call_cancel is not None:
            call_cancel.set()

        with self._request_futures_lock:
            future = self._request_futures.pop(request_id, None)
        if future is None:
            return
        if not future.cancel():
            # Future is already running — the per-call cancel_event above
            # should interrupt the session's send_prompt loop within its
            # poll interval. The session close in the finally block will
            # clean up resources once the call returns.
            logger.debug("Abort request %s: future already running, per-call cancel_event set", request_id)
        else:
            # Future was cancelled before it started running. The _execute
            # function will never execute, so its finally block won't clean
            # up the cancel_event entry. Do it here to prevent leaks.
            with self._request_cancel_events_lock:
                self._request_cancel_events.pop(request_id, None)

    @property
    def in_flight_count(self) -> int:
        """Count of submitted-but-not-yet-completed futures (thread-safe)."""
        with self._futures_lock:
            return len(self._active_futures)

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
