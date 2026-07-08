"""Regression tests for the "Runtime process closed stdout unexpectedly" bug.

Root cause (two layers):

1. runtime.js called ``sendNotification('done'|'error', ...)`` and then
   ``process.exit()`` synchronously. When stdout is an async pipe with a
   backlog (a real workflow emits many log/phase frames), ``process.exit()``
   truncates the un-flushed tail — including the terminal ``done``/``error``
   frame. The Python bridge then saw a bare stdout EOF and reported the
   generic "closed stdout unexpectedly" message, masking the true result.

   Fix: ``flushAndExit()`` drains stdout/stderr before exiting.

2. bridge.py's reader thread set ``self._error`` to the generic message on
   stdout EOF *even when* a valid terminal frame was still queued (a race made
   more likely once the runtime reliably flushes done-before-EOF). And the
   dying process's stderr was logged at DEBUG only, so the real cause was lost.

   Fix: the EOF fallback is stored separately (``_eof_fallback_error``) and only
   used when no done/error frame was dispatched; a bounded stderr tail feeds the
   diagnostic; and run() drains queued frames before deciding the outcome.

Layer 1 is verified end-to-end against the real Node runtime (skipped when Node
is unavailable). Layer 2 is verified with in-process bridge unit tests.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import unittest
from unittest.mock import MagicMock

from src.workflow_engine.bridge import RuntimeBridge
from src.workflow_engine.models import AgentCallResult

RUNTIME_JS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src",
    "workflow_engine",
    "runtime",
    "runtime.js",
)


# ---------------------------------------------------------------------------
# Layer 1: real-Node end-to-end — flush before exit delivers the final frame
# ---------------------------------------------------------------------------


class TestRuntimeFlushBeforeExit(unittest.TestCase):
    """The real Node runtime must deliver the terminal frame despite a backlog."""

    def _run_script(self, user_script: str, timeout: float = 30.0):
        """Spawn the real runtime.js with *user_script*, answering init.

        Returns (exit_code, terminal_method, terminal_params, frame_count).
        Auto-answers agent_call with a trivial success so scripts that call
        agent()/verify()/parallel() still terminate.
        """
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not available in PATH")
        if not os.path.exists(RUNTIME_JS):
            self.skipTest(f"runtime.js not found at {RUNTIME_JS}")

        import tempfile

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".mjs", delete=False, encoding="utf-8"
        ) as f:
            f.write(user_script)
            script_path = f.name

        proc = subprocess.Popen(
            [node, "--experimental-vm-modules", RUNTIME_JS, script_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        stdin_lock = threading.Lock()
        state: dict = {
            "terminal_method": None,
            "terminal_params": None,
            "frames": 0,
            "init_sent": False,
        }
        done_flag = threading.Event()

        def write(obj: dict) -> None:
            line = json.dumps(obj, separators=(",", ":")) + "\n"
            with stdin_lock:
                try:
                    proc.stdin.write(line)
                    proc.stdin.flush()
                except (BrokenPipeError, ValueError):
                    pass

        def reader() -> None:
            try:
                for raw in iter(proc.stdout.readline, ""):
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    state["frames"] += 1
                    method = msg.get("method")
                    if method == "ready" and not state["init_sent"]:
                        state["init_sent"] = True
                        write(
                            {
                                "jsonrpc": "2.0",
                                "method": "init",
                                "params": {"args": {}, "max_concurrent": 4},
                            }
                        )
                    elif method == "agent_call" and msg.get("id") is not None:
                        write(
                            {
                                "jsonrpc": "2.0",
                                "id": msg["id"],
                                "result": {
                                    "data": {"ok": True},
                                    "token_usage": 0,
                                    "duration_s": 0.0,
                                },
                            }
                        )
                    elif method in ("done", "error"):
                        state["terminal_method"] = method
                        state["terminal_params"] = msg.get("params")
                        break
            finally:
                done_flag.set()

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        try:
            done_flag.wait(timeout=timeout)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            return (
                proc.returncode,
                state["terminal_method"],
                state["terminal_params"],
                state["frames"],
            )
        finally:
            try:
                if proc.poll() is None:
                    proc.kill()
            except OSError:
                pass
            try:
                os.unlink(script_path)
            except OSError:
                pass

    def test_done_frame_survives_large_log_backlog(self):
        """A big log() backlog before returning must NOT drop the done frame."""
        script = """
export const meta = {
  name: 'backlog',
  description: 'emit a large backlog then return a result',
  phases: [{ title: 'work', detail: 'emit logs and return' }],
  tools: [],
};
export default async function run() {
  for (let i = 0; i < 20000; i++) {
    log('backlog ' + i + ' ' + 'x'.repeat(120));
  }
  return { status: 'REAL_RESULT', logs: 20000 };
}
"""
        code, method, params, frames = self._run_script(script)
        self.assertEqual(method, "done", "terminal done frame must be delivered")
        self.assertEqual(code, 0)
        self.assertIsInstance(params, dict)
        self.assertEqual(params.get("result", {}).get("status"), "REAL_RESULT")
        # 20000 logs + ready + done all delivered (frames counts logs+done)
        self.assertGreaterEqual(frames, 20000)

    def test_error_frame_survives_backlog(self):
        """A throwing script after a log backlog must deliver the error frame."""
        script = """
export const meta = {
  name: 'backlog-error',
  description: 'emit a backlog then throw',
  phases: [{ title: 'work', detail: 'emit logs then throw' }],
  tools: [],
};
export default async function run() {
  for (let i = 0; i < 20000; i++) {
    log('backlog ' + i + ' ' + 'x'.repeat(120));
  }
  throw new Error('DELIBERATE_FAILURE_MARKER');
}
"""
        code, method, params, _ = self._run_script(script)
        self.assertEqual(method, "error", "terminal error frame must be delivered")
        self.assertEqual(code, 1)
        self.assertIn("DELIBERATE_FAILURE_MARKER", (params or {}).get("message", ""))


# ---------------------------------------------------------------------------
# Layer 2: bridge EOF-race precedence + stderr diagnostics (in-process)
# ---------------------------------------------------------------------------


def _make_bridge(tmp_path) -> RuntimeBridge:
    bridge = RuntimeBridge(
        script_path="test.js",
        cwd=str(tmp_path),
        on_agent_call=lambda p: AgentCallResult(output="ok"),
    )
    # Minimal live-looking process; poll() returns an exit code (process dead).
    proc = MagicMock()
    proc.poll.return_value = 0
    proc.returncode = 0
    proc.stdin = MagicMock()
    proc.stdout = MagicMock()
    proc.stderr = MagicMock()
    bridge._process = proc
    return bridge


class TestBridgeTerminalFramePrecedence(unittest.TestCase):
    """A queued done/error frame must win over the synthetic EOF fallback."""

    def test_queued_done_wins_over_eof_fallback(self, ):
        """done frame queued at EOF -> run() returns the result, not the error."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            bridge = _make_bridge(td)
            # Simulate: reader queued a done frame, THEN hit EOF and set the
            # fallback + _done (exactly the observed race).
            with bridge._msg_condition:
                bridge._msg_queue.append(
                    {"jsonrpc": "2.0", "method": "done", "params": {"result": {"ok": 1}}}
                )
            bridge._eof_fallback_error = "Runtime process closed stdout unexpectedly"
            bridge._done = True

            result = bridge.run()
            self.assertEqual(json.loads(result), {"ok": 1})

    def test_queued_error_wins_over_eof_fallback(self):
        """A real error frame is surfaced instead of the generic EOF message."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            bridge = _make_bridge(td)
            with bridge._msg_condition:
                bridge._msg_queue.append(
                    {
                        "jsonrpc": "2.0",
                        "method": "error",
                        "params": {"message": "REAL_SCRIPT_ERROR"},
                    }
                )
            bridge._eof_fallback_error = "Runtime process closed stdout unexpectedly"
            bridge._done = True

            with self.assertRaises(RuntimeError) as ctx:
                bridge.run()
            self.assertIn("REAL_SCRIPT_ERROR", str(ctx.exception))
            self.assertNotIn("closed stdout unexpectedly", str(ctx.exception))

    def test_eof_fallback_used_when_no_terminal_frame(self):
        """With no done/error frame, the enriched EOF fallback is raised."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            bridge = _make_bridge(td)
            bridge._eof_fallback_error = (
                "Runtime process closed stdout unexpectedly (exit code 1). "
                "stderr: FATAL ERROR: heap out of memory"
            )
            bridge._done = True

            with self.assertRaises(RuntimeError) as ctx:
                bridge.run()
            msg = str(ctx.exception)
            self.assertIn("closed stdout unexpectedly", msg)
            self.assertIn("heap out of memory", msg)


class TestBridgeStderrTail(unittest.TestCase):
    """The stderr ring buffer must feed the unexpected-exit diagnostic."""

    def test_describe_unexpected_exit_includes_stderr_tail(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            bridge = _make_bridge(td)
            bridge._process.poll.return_value = -9  # SIGKILL
            # Populate the tail as the drain thread would.
            with bridge._stderr_tail_lock:
                bridge._stderr_tail.append("FATAL ERROR: Reached heap limit")
                bridge._stderr_tail.append("1: 0xabc node::Abort()")
            # _drain_stderr reads the (already-empty) pipe.
            bridge._process.stderr.read.return_value = ""

            detail = bridge._describe_unexpected_exit()
            self.assertIn("signal 9", detail)
            self.assertIn("Reached heap limit", detail)

    def test_stderr_reader_records_tail(self):
        """_stderr_reader appends non-empty lines to the bounded tail."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            bridge = _make_bridge(td)
            lines = ["line one\n", "line two\n", "\n", ""]  # last "" = EOF
            bridge._process.stderr.readline.side_effect = lines
            bridge._stderr_reader()
            self.assertEqual(bridge._stderr_tail_text(), "line one\nline two")


if __name__ == "__main__":
    unittest.main()
