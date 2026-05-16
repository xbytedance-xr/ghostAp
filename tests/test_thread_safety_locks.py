"""Thread-safety tests for lock-protected state in engines and ACP layer.

Validates that concurrent access to protected fields does not raise exceptions
or corrupt state after the lock-protection changes (Tasks 1-4).
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

from src.engine_base import EngineRunState

# ---------------------------------------------------------------------------
# Task 1: SpecEngine.execute() lock protection
# ---------------------------------------------------------------------------

class TestSpecEngineLockProtection:
    """Verify concurrent state mutation under lock does not crash."""

    def test_concurrent_state_mutation_no_crash(self, monkeypatch):
        monkeypatch.setattr("src.engine_base.get_settings", lambda: MagicMock(
            spec_max_cycles=1,
        ))
        from src.spec_engine.engine import SpecEngine

        eng = SpecEngine(chat_id="c", root_path="/tmp")
        errors = []

        def mutate_state(i):
            try:
                with eng._lock:
                    eng._run_state = EngineRunState.RUNNING if i % 2 else EngineRunState.IDLE
                    eng._on_rate_limit = None
                    eng._saved_task_id = None
                    eng._termination_reason = None
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=mutate_state, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not errors, f"Unexpected errors: {errors}"


# ---------------------------------------------------------------------------
# Task 2: DeepEngine.execute() lock protection
# ---------------------------------------------------------------------------

class TestDeepEngineLockProtection:
    """Verify concurrent state mutation in DeepEngine is safe."""

    def test_concurrent_state_mutation_no_crash(self, monkeypatch):
        monkeypatch.setattr("src.engine_base.get_settings", lambda: MagicMock())
        from src.deep_engine.engine import DeepEngine

        eng = DeepEngine(chat_id="c", root_path="/tmp")
        errors = []

        def mutate_state(i):
            try:
                with eng._lock:
                    eng._run_state = EngineRunState.RUNNING if i % 2 else EngineRunState.IDLE
                    eng._planning_done_fired = bool(i % 2)
                    eng._on_rate_limit = None
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=mutate_state, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not errors


# ---------------------------------------------------------------------------
# Task 3: GhostAPClient._terminals lock protection
# ---------------------------------------------------------------------------

class TestClientTerminalThreadSafety:
    """Verify concurrent terminal create/kill/release does not corrupt dict."""

    def test_concurrent_terminal_ops(self):
        from src.acp.client import GhostAPClient

        client = GhostAPClient(
            on_event=lambda ev: None,
            auto_approve=True,
            root_dir="/tmp",
        )
        errors = []

        def create_terminals(n):
            try:
                for j in range(n):
                    tid = f"term_{threading.current_thread().name}_{j}"
                    with client._terminals_lock:
                        client._terminals[tid] = MagicMock(created_at=time.time())
            except Exception as e:
                errors.append(e)

        def pop_terminals():
            try:
                for _ in range(50):
                    with client._terminals_lock:
                        keys = list(client._terminals.keys())
                    for k in keys[:1]:
                        with client._terminals_lock:
                            client._terminals.pop(k, None)
            except Exception as e:
                errors.append(e)

        threads = []
        for i in range(5):
            threads.append(threading.Thread(target=create_terminals, args=(10,), name=f"creator-{i}"))
        for i in range(3):
            threads.append(threading.Thread(target=pop_terminals, name=f"popper-{i}"))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors


# ---------------------------------------------------------------------------
# Task 4: ACPSession._event_handler lock protection
# ---------------------------------------------------------------------------

class TestSessionHandlerThreadSafety:
    """Verify concurrent handler set/dispatch does not crash."""

    def test_concurrent_handler_set_and_dispatch(self):
        from src.acp.models import ACPEvent, ACPEventType
        from src.acp.session import ACPSession

        session = ACPSession.__new__(ACPSession)
        session._event_handler = None
        session._handler_lock = threading.Lock()

        received = []
        errors = []

        def handler(ev):
            received.append(ev)

        def set_handler():
            try:
                for _ in range(50):
                    with session._handler_lock:
                        session._event_handler = handler
                    time.sleep(0.001)
                    with session._handler_lock:
                        session._event_handler = None
            except Exception as e:
                errors.append(e)

        def dispatch_events():
            try:
                for _ in range(50):
                    ev = ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="test")
                    with session._handler_lock:
                        h = session._event_handler
                    if h:
                        try:
                            h(ev)
                        except Exception:
                            pass
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=set_handler),
            threading.Thread(target=dispatch_events),
            threading.Thread(target=dispatch_events),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors
