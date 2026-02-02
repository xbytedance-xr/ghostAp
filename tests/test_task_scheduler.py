import time
import threading


from src.tasking import TaskScheduler, TaskSpec, TaskPriority, TaskStatus


def test_scheduler_respects_global_concurrency_limit():
    scheduler = TaskScheduler(max_concurrent=2, per_chat_concurrency=1)

    lock = threading.Lock()
    active = 0
    max_active = 0

    def make_task(i: int):
        def _fn(ctx):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.15)
            with lock:
                active -= 1
            return i
        return _fn

    handles = []
    for i in range(6):
        spec = TaskSpec(chat_id=f"chat_{i}", name=f"t{i}", task_type="test")
        handles.append(scheduler.submit(spec, make_task(i)))

    results = [h.wait(timeout=3) for h in handles]
    assert all(r.status == TaskStatus.SUCCEEDED for r in results)
    assert max_active <= 2


def test_scheduler_serializes_tasks_with_same_queue_key():
    scheduler = TaskScheduler(max_concurrent=4, per_chat_concurrency=1)

    lock = threading.Lock()
    active = 0
    max_active = 0

    def _fn(ctx):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.1)
        with lock:
            active -= 1
        return "ok"

    chat_id = "chat_same"
    handles = [
        scheduler.submit(TaskSpec(chat_id=chat_id, queue_key=chat_id, name=f"s{i}"), _fn)
        for i in range(5)
    ]
    for h in handles:
        r = h.wait(timeout=3)
        assert r.status == TaskStatus.SUCCEEDED
    assert max_active == 1


def test_cancel_queued_task_prevents_execution():
    scheduler = TaskScheduler(max_concurrent=1, per_chat_concurrency=1)
    started = threading.Event()
    release = threading.Event()

    def long_task(ctx):
        started.set()
        release.wait(timeout=2)
        return "long"

    def should_not_run(ctx):
        raise AssertionError("canceled task should not execute")

    h1 = scheduler.submit(TaskSpec(chat_id="a", name="long"), long_task)
    assert started.wait(timeout=1)

    h2 = scheduler.submit(TaskSpec(chat_id="b", name="to_cancel", priority=TaskPriority.NORMAL), should_not_run)
    assert h2.cancel() is True

    release.set()
    r1 = h1.wait(timeout=3)
    assert r1.status == TaskStatus.SUCCEEDED

    r2 = h2.wait(timeout=1)
    assert r2.status == TaskStatus.CANCELED


def test_scheduler_can_update_project_id_and_query_by_project():
    scheduler = TaskScheduler(max_concurrent=2, per_chat_concurrency=1)

    def _fn(ctx):
        # project is resolved inside task body
        assert scheduler.update_project_id(ctx.run_id, "p1") is True
        ctx.progress("working", percent=30)
        return "ok"

    h = scheduler.submit(TaskSpec(chat_id="c1", name="t1", task_type="test"), _fn)
    r = h.wait(timeout=3)
    assert r.status == TaskStatus.SUCCEEDED

    # include_done=True to include terminal tasks
    tasks = scheduler.list_tasks(project_id="p1", include_done=True)
    assert any(st.run_id == h.run_id for st in tasks)

    # intersection filter
    tasks2 = scheduler.list_tasks(chat_id="c1", project_id="p1", include_done=True)
    assert any(st.run_id == h.run_id for st in tasks2)
