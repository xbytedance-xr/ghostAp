import threading
import time

from src.tasking import TaskPriority, TaskStatus
from tests.control_plane_fixtures import BlockedTask, make_scheduler, make_spec, wait_event


def test_help_command_responds_within_100ms_while_task_running():
    scheduler = make_scheduler(max_concurrent=1, per_key_concurrency=1, system_concurrency=1)

    blocked = BlockedTask()
    h1 = scheduler.submit(
        make_spec(chat_id="chat", name="normal", task_type="work", project_id="p1"),
        blocked.fn(),
    )
    assert wait_event(blocked.started, timeout_s=1.0)

    sys_started = threading.Event()

    def sys_fn(_ctx):
        sys_started.set()
        return "help"

    t0 = time.perf_counter()
    h2 = scheduler.submit(
        make_spec(
            chat_id="chat",
            name="help",
            task_type="system_help",
            project_id="p1",
            is_system_command=True,
            priority=TaskPriority.HIGH,
        ),
        sys_fn,
    )

    assert sys_started.wait(timeout=0.2)
    assert (time.perf_counter() - t0) <= 0.1

    # The normal (programming) task should not be interrupted.
    assert not blocked.finished.is_set()

    blocked.unblock.set()
    assert h2.wait(timeout=2).status == TaskStatus.SUCCEEDED
    assert h1.wait(timeout=2).status == TaskStatus.SUCCEEDED

    scheduler.stop(wait=True, shutdown_executor=True)


def test_exit_command_responds_within_100ms_while_task_running():
    scheduler = make_scheduler(max_concurrent=1, per_key_concurrency=1, system_concurrency=1)

    blocked = BlockedTask()
    h1 = scheduler.submit(
        make_spec(chat_id="chat", name="normal", task_type="work", project_id="p1"),
        blocked.fn(),
    )
    assert wait_event(blocked.started, timeout_s=1.0)

    sys_started = threading.Event()

    def sys_fn(_ctx):
        sys_started.set()
        return "exit"

    t0 = time.perf_counter()
    h2 = scheduler.submit(
        make_spec(
            chat_id="chat",
            name="exit",
            task_type="system_exit",
            project_id="p1",
            is_system_command=True,
            priority=TaskPriority.HIGH,
        ),
        sys_fn,
    )

    assert sys_started.wait(timeout=0.2)
    assert (time.perf_counter() - t0) <= 0.1

    assert not blocked.finished.is_set()
    blocked.unblock.set()
    assert h2.wait(timeout=2).status == TaskStatus.SUCCEEDED
    assert h1.wait(timeout=2).status == TaskStatus.SUCCEEDED

    scheduler.stop(wait=True, shutdown_executor=True)

