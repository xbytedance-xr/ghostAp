import time
import threading


from src.tasking import TaskScheduler, TaskSpec, TaskPriority, TaskStatus


def test_scheduler_respects_global_concurrency_limit():
    scheduler = TaskScheduler(max_concurrent=2, per_key_concurrency=1)

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
    scheduler = TaskScheduler(max_concurrent=4, per_key_concurrency=1)

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
    scheduler = TaskScheduler(max_concurrent=1, per_key_concurrency=1)
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
    scheduler = TaskScheduler(max_concurrent=2, per_key_concurrency=1)

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


def test_system_command_uses_system_queue():
    """System commands should use the SYSTEM queue suffix."""
    spec = TaskSpec(chat_id="chat1", name="help", is_system_command=True)
    assert spec.get_effective_queue_key() == "chat1:SYSTEM"


def test_project_task_uses_project_queue():
    """Project tasks should use the project_id in queue key."""
    spec = TaskSpec(chat_id="chat1", name="task", project_id="proj1")
    assert spec.get_effective_queue_key() == "chat1:proj1"


def test_no_project_task_uses_default_queue():
    """Tasks without project should use DEFAULT queue."""
    spec = TaskSpec(chat_id="chat1", name="task")
    assert spec.get_effective_queue_key() == "chat1:DEFAULT"


def test_explicit_queue_key_takes_precedence():
    """Explicit queue_key should override automatic calculation."""
    spec = TaskSpec(chat_id="chat1", name="task", queue_key="custom:key", project_id="proj1")
    assert spec.get_effective_queue_key() == "custom:key"


def test_system_queue_has_higher_concurrency():
    """System queue should allow higher concurrency than normal queues."""
    scheduler = TaskScheduler(max_concurrent=20, per_key_concurrency=1, system_concurrency=10)

    lock = threading.Lock()
    active_system = 0
    max_active_system = 0
    active_normal = 0
    max_active_normal = 0

    def system_task(ctx):
        nonlocal active_system, max_active_system
        with lock:
            active_system += 1
            max_active_system = max(max_active_system, active_system)
        time.sleep(0.1)
        with lock:
            active_system -= 1
        return "system"

    def normal_task(ctx):
        nonlocal active_normal, max_active_normal
        with lock:
            active_normal += 1
            max_active_normal = max(max_active_normal, active_normal)
        time.sleep(0.1)
        with lock:
            active_normal -= 1
        return "normal"

    handles = []
    for i in range(5):
        handles.append(scheduler.submit(
            TaskSpec(chat_id="chat1", name=f"sys{i}", is_system_command=True),
            system_task
        ))
    for i in range(5):
        handles.append(scheduler.submit(
            TaskSpec(chat_id="chat1", name=f"norm{i}"),
            normal_task
        ))

    for h in handles:
        r = h.wait(timeout=5)
        assert r.status == TaskStatus.SUCCEEDED

    assert max_active_system >= 2
    assert max_active_normal == 1


def test_different_projects_can_run_concurrently():
    """Tasks from different projects should be able to run concurrently."""
    scheduler = TaskScheduler(max_concurrent=10, per_key_concurrency=1)

    lock = threading.Lock()
    active_by_project: dict[str, int] = {"proj1": 0, "proj2": 0}
    max_active_by_project: dict[str, int] = {"proj1": 0, "proj2": 0}
    total_concurrent = 0
    max_total_concurrent = 0

    def make_task(proj_id: str):
        def _fn(ctx):
            nonlocal total_concurrent, max_total_concurrent
            with lock:
                active_by_project[proj_id] += 1
                max_active_by_project[proj_id] = max(max_active_by_project[proj_id], active_by_project[proj_id])
                total_concurrent += 1
                max_total_concurrent = max(max_total_concurrent, total_concurrent)
            time.sleep(0.15)
            with lock:
                active_by_project[proj_id] -= 1
                total_concurrent -= 1
            return proj_id
        return _fn

    handles = []
    for i in range(3):
        handles.append(scheduler.submit(
            TaskSpec(chat_id="chat1", name=f"p1_{i}", project_id="proj1"),
            make_task("proj1")
        ))
        handles.append(scheduler.submit(
            TaskSpec(chat_id="chat1", name=f"p2_{i}", project_id="proj2"),
            make_task("proj2")
        ))

    for h in handles:
        r = h.wait(timeout=5)
        assert r.status == TaskStatus.SUCCEEDED

    assert max_active_by_project["proj1"] == 1
    assert max_active_by_project["proj2"] == 1
    assert max_total_concurrent == 2
