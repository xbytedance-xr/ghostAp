"""Verify TaskScheduler._reap_completed_states evicts terminal states correctly."""
from __future__ import annotations

import time
import threading

from src.tasking.scheduler import TaskScheduler, TaskSpec, TaskStatus


def _noop(ctx):
    return "ok"


def _make_scheduler(**kw) -> TaskScheduler:
    return TaskScheduler(max_concurrent=2, **kw)


class TestReapCompletedStates:

    def test_evicts_terminal_states_past_max_age(self):
        sched = _make_scheduler()
        try:
            spec = TaskSpec(chat_id="c1", name="t1")
            handle = sched.submit(spec, _noop)
            result = sched.wait(handle.run_id, timeout=5)
            assert result.status == TaskStatus.SUCCEEDED

            # Immediately reap with max_age=0 → should evict
            with sched._cv:
                evicted = sched._reap_completed_states(max_age_seconds=0)
            assert evicted == 1
            assert handle.run_id not in sched._states
        finally:
            sched.stop(shutdown_executor=True)

    def test_does_not_evict_young_terminal_states(self):
        sched = _make_scheduler()
        try:
            spec = TaskSpec(chat_id="c1", name="t2")
            handle = sched.submit(spec, _noop)
            sched.wait(handle.run_id, timeout=5)

            # Reap with large max_age → nothing evicted
            with sched._cv:
                evicted = sched._reap_completed_states(max_age_seconds=9999)
            assert evicted == 0
            assert handle.run_id in sched._states
        finally:
            sched.stop(shutdown_executor=True)

    def test_does_not_evict_running_states(self):
        blocker = threading.Event()
        sched = _make_scheduler()
        try:
            def _blocking(ctx):
                blocker.wait(timeout=10)
                return "done"

            spec = TaskSpec(chat_id="c1", name="t3")
            handle = sched.submit(spec, _blocking)

            # Wait until task is running
            deadline = time.time() + 5
            while time.time() < deadline:
                st = sched.get_state(handle.run_id)
                if st and st.status == TaskStatus.RUNNING:
                    break
                time.sleep(0.05)

            with sched._cv:
                evicted = sched._reap_completed_states(max_age_seconds=0)
            assert evicted == 0
            assert handle.run_id in sched._states
        finally:
            blocker.set()
            sched.stop(shutdown_executor=True)

    def test_cleans_secondary_indexes(self):
        sched = _make_scheduler()
        try:
            spec = TaskSpec(chat_id="c1", name="t4", project_id="proj1", task_id="tid1")
            handle = sched.submit(spec, _noop)
            sched.wait(handle.run_id, timeout=5)

            # Verify indexes exist before reap
            assert handle.run_id in sched._by_task_id.values() or "tid1" in sched._by_task_id

            with sched._cv:
                sched._reap_completed_states(max_age_seconds=0)

            # After reap: indexes should be cleaned
            assert "tid1" not in sched._by_task_id
        finally:
            sched.stop(shutdown_executor=True)
