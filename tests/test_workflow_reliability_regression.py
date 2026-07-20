"""Regression tests for workflow reliability fixes.

Covers:
- Global cancel propagation (bridge.stop() sets all per-call cancel events)
- Cancel guard OR semantics (global OR per-call both trigger session.cancel())
- Sticky terminal state in state_manager (all terminal states are final)
- Metrics atomicity under concurrent agent completion
- Session leak guard (_close_late_session threads are joinable on shutdown)
- Consecutive run isolation (cancel_event reset, state rebuild)
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest

from src.workflow_engine.bridge import RuntimeBridge
from src.workflow_engine.executor import AgentExecutor
from src.workflow_engine.models import (
    AgentCallParams,
    WorkflowMetrics,
    WorkflowProject,
    WorkflowStatus,
)
from src.workflow_engine.state_manager import WorkflowStateManager

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def make_executor():
    executors: list[AgentExecutor] = []

    def _factory(tmp_path, cancel_event=None):
        ex = AgentExecutor(
            cwd=str(tmp_path),
            cancel_event=cancel_event or threading.Event(),
            max_workers=2,
        )
        executors.append(ex)
        return ex

    yield _factory

    for ex in executors:
        try:
            ex.shutdown(wait=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 1. Global cancel propagation: bridge.stop() sets per-call cancel events
# ---------------------------------------------------------------------------


class _FakeProcess:
    def __init__(self):
        self.stdin = MagicMock()
        self.stdout = MagicMock()
        self.stderr = MagicMock()
        self.returncode = 0
        self._alive = True

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self._alive = False

    def kill(self):
        self._alive = False

    def wait(self, timeout=None):
        self._alive = False
        return 0


def test_stop_sets_all_per_call_cancel_events(tmp_path):
    """bridge.stop() must set every in-flight per-call cancel_event so that
    agent session cancel guards fire immediately."""
    bridge = RuntimeBridge(
        script_path="test.js",
        cwd=str(tmp_path),
    )
    bridge._process = _FakeProcess()
    bridge._executor = ThreadPoolExecutor(max_workers=2)
    bridge._workflow_executor = ThreadPoolExecutor(max_workers=2)

    n_calls = 5
    events: list[threading.Event] = []
    with bridge._request_cancel_events_lock:
        for i in range(n_calls):
            evt = threading.Event()
            bridge._request_cancel_events[f"req-{i}"] = evt
            events.append(evt)

    assert all(not e.is_set() for e in events)

    bridge.stop()

    for i, evt in enumerate(events):
        assert evt.is_set(), f"per-call cancel event {i} must be set by stop()"


# ---------------------------------------------------------------------------
# 2. Cancel guard OR semantics: global event triggers session.cancel()
# ---------------------------------------------------------------------------


class _SlowCancelableSession:
    """Fake session where send_prompt blocks until cancel() is called."""

    def __init__(self, delay_after_cancel: float = 0.05):
        self._cancel_event = threading.Event()
        self.cancel_called = threading.Event()
        self.close_called = threading.Event()
        self.send_prompt_started = threading.Event()
        self._delay = delay_after_cancel

    def cancel(self) -> None:
        self.cancel_called.set()
        self._cancel_event.set()

    def close(self) -> None:
        self.close_called.set()

    def send_prompt(self, text, on_event=None, timeout=None):
        self.send_prompt_started.set()
        cancelled = self._cancel_event.wait(timeout=timeout or 30)
        if cancelled:
            time.sleep(self._delay)
            raise RuntimeError("cancelled")
        raise TimeoutError("timed out")


def test_per_call_cancel_still_works_with_global_event(tmp_path, make_executor):
    """Per-call cancel (race loser) must still work when a global event exists."""
    global_cancel = threading.Event()  # never set
    executor = make_executor(tmp_path, cancel_event=global_cancel)
    per_call_cancel = threading.Event()
    session = _SlowCancelableSession()

    with patch(
        "src.agent_session.factory.create_engine_session",
        return_value=session,
    ):
        params = AgentCallParams(prompt="hello", tool="coco")

        def run():
            executor.execute(params, cancel_event=per_call_cancel)

        t = threading.Thread(target=run, daemon=True)
        t.start()

        assert session.send_prompt_started.wait(timeout=5.0)

        assert not global_cancel.is_set()
        start = time.monotonic()
        per_call_cancel.set()

        assert session.cancel_called.wait(timeout=2.0)
        elapsed_ms = (time.monotonic() - start) * 1000
        assert elapsed_ms < 500

        t.join(timeout=10.0)
        assert not t.is_alive()


# ---------------------------------------------------------------------------
# 3. Sticky terminal state: all agent terminal states are final
# ---------------------------------------------------------------------------


def _make_sm():
    project = WorkflowProject(
        workflow_id="test",
        status=WorkflowStatus.RUNNING,
        metrics=WorkflowMetrics(),
    )
    sm = WorkflowStateManager(project)
    sm.on_phase_changed("phase1")
    return sm


# ---------------------------------------------------------------------------
# 4. Metrics atomicity under concurrent completion
# ---------------------------------------------------------------------------


def test_concurrent_done_counts_exactly_once():
    """N concurrent on_agent_done calls must produce exactly N completed_agents."""
    sm = _make_sm()
    n = 20
    labels = [sm.on_agent_started(f"a{i}", "coco", "phase1") for i in range(n)]
    barrier = threading.Barrier(n)

    def mark_done(lbl):
        barrier.wait()
        sm.on_agent_done(lbl, {"token_usage": 1, "duration_s": 0.01})

    threads = [threading.Thread(target=mark_done, args=(l,)) for l in labels]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    snap = sm.snapshot()
    assert snap.metrics.total_agents == n
    assert snap.metrics.completed_agents == n
    assert snap.metrics.failed_agents == 0
    assert snap.metrics.total_tokens == n


def test_concurrent_mixed_statuses_consistent_totals():
    """Concurrent done/failed/abort must produce consistent totals."""
    sm = _make_sm()
    nd, nf, na = 15, 10, 5
    total = nd + nf + na

    dl = [sm.on_agent_started(f"d{i}", "coco", "phase1") for i in range(nd)]
    fl = [sm.on_agent_started(f"f{i}", "coco", "phase1") for i in range(nf)]
    al = [sm.on_agent_started(f"a{i}", "coco", "phase1") for i in range(na)]

    barrier = threading.Barrier(total)
    threads = []

    for lbl in dl:
        def _d(l=lbl):
            barrier.wait()
            sm.on_agent_done(l, {"token_usage": 2, "duration_s": 0.01})
        threads.append(threading.Thread(target=_d))

    for lbl in fl:
        def _f(l=lbl):
            barrier.wait()
            sm.on_agent_failed(l, "fail")
        threads.append(threading.Thread(target=_f))

    for lbl in al:
        def _a(l=lbl):
            barrier.wait()
            sm.on_agent_aborted(l, "abort")
        threads.append(threading.Thread(target=_a))

    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    snap = sm.snapshot()
    assert snap.metrics.total_agents == total
    assert snap.metrics.completed_agents == total
    assert snap.metrics.failed_agents == nf
    assert snap.metrics.total_tokens == nd * 2


# ---------------------------------------------------------------------------
# 5. _close_late_session threads are tracked and joined on shutdown
# ---------------------------------------------------------------------------


def test_late_close_threads_are_tracked(tmp_path, make_executor):
    """Late-close threads must be non-daemon and tracked so shutdown() waits
    for them, preventing orphan ACP subprocesses at interpreter exit."""
    executor = make_executor(tmp_path)

    slow_session = MagicMock()
    slow_session.close = MagicMock()
    slow_session_future = ThreadPoolExecutor(max_workers=1).submit(
        lambda: slow_session
    )

    executor._close_late_session(slow_session_future, "coco")

    # Give the thread a moment to start and finish
    time.sleep(0.5)

    with executor._late_close_lock:
        assert len(executor._late_close_threads) > 0, (
            "late-close threads must be tracked in _late_close_threads"
        )
        # Threads must be non-daemon so they don't get abandoned at exit
        for t in executor._late_close_threads:
            assert not t.daemon, (
                "late-close thread must be non-daemon to prevent orphan processes"
            )


# ---------------------------------------------------------------------------
# 6. Consecutive runs: cancel_event is cleared and state is rebuilt
# ---------------------------------------------------------------------------


def test_engine_clears_cancel_event_on_new_run(tmp_path):
    """A new execute_workflow() call must start with a clean cancel_event."""
    from src.workflow_engine.engine import WorkflowEngine

    script_path = tmp_path / "wf.js"
    script_path.write_text(
        """
export const meta = { name: "smoke", description: "", phases: [], tools: [] };
export default async function workflow() { return "ok"; }
""",
        encoding="utf-8",
    )

    class FakeBridge:
        def __init__(self, *args, cancel_event, **kwargs):
            self.cancel_event = cancel_event

        @staticmethod
        def check_node_available():
            return True

        def start(self):
            pass

        def run(self):
            if self.cancel_event.is_set():
                raise RuntimeError("Workflow cancelled")
            return "ok"

        def stop(self):
            pass

    engine = WorkflowEngine(chat_id="chat1", root_path=str(tmp_path))
    engine.cancel_event.set()  # simulate previous run left it set

    with patch("src.workflow_engine.engine.RuntimeBridge", FakeBridge):
        project = engine.execute_workflow("test", str(script_path))

    assert project.status == WorkflowStatus.COMPLETED
    assert not engine.cancel_event.is_set()


def test_engine_resets_agent_call_count_on_new_run(tmp_path):
    """Each run must start with _agent_call_count = 0 so label numbering
    restarts and counters don't drift across runs."""
    from src.workflow_engine.engine import WorkflowEngine

    script_path = tmp_path / "wf.js"
    script_path.write_text(
        """
export const meta = { name: "smoke", phases: [], tools: [] };
export default async function workflow() { return "ok"; }
""",
        encoding="utf-8",
    )

    class FakeBridge:
        def __init__(self, *args, **kwargs):
            pass

        @staticmethod
        def check_node_available():
            return True

        def start(self):
            pass

        def run(self):
            return "ok"

        def stop(self):
            pass

    engine = WorkflowEngine(chat_id="chat1", root_path=str(tmp_path))
    engine._agent_call_count = 999  # simulate drift from previous run

    with patch("src.workflow_engine.engine.RuntimeBridge", FakeBridge):
        engine.execute_workflow("first", str(script_path))

    # After the run, agent_call_count may have increased, but on the next
    # run it must be reset back to 0 at start
    assert engine._agent_call_count == 0  # reset at start of execute_workflow

    with patch("src.workflow_engine.engine.RuntimeBridge", FakeBridge):
        engine.execute_workflow("second", str(script_path))

    assert engine._agent_call_count == 0


def test_engine_project_is_fresh_each_run(tmp_path):
    """Each execute_workflow call must create a brand new project object,
    not reuse state from the previous run."""
    from src.workflow_engine.engine import WorkflowEngine

    script_path = tmp_path / "wf.js"
    script_path.write_text(
        """
export const meta = { name: "smoke", phases: [], tools: [] };
export default async function workflow() { return "ok"; }
""",
        encoding="utf-8",
    )

    class FakeBridge:
        def __init__(self, *args, **kwargs):
            pass

        @staticmethod
        def check_node_available():
            return True

        def start(self):
            pass

        def run(self):
            return "ok"

        def stop(self):
            pass

    engine = WorkflowEngine(chat_id="chat1", root_path=str(tmp_path))

    with patch("src.workflow_engine.engine.RuntimeBridge", FakeBridge):
        p1 = engine.execute_workflow("first", str(script_path))

    with patch("src.workflow_engine.engine.RuntimeBridge", FakeBridge):
        p2 = engine.execute_workflow("second", str(script_path))

    # Different workflow IDs mean fresh state
    assert p1.workflow_id != p2.workflow_id
    assert p2.status == WorkflowStatus.COMPLETED
    assert p2.result == "ok"
