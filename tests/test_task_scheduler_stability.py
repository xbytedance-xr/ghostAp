import time
import threading

from src.tasking import TaskScheduler, TaskSpec, TaskPriority, TaskStatus


def test_scheduler_isolates_queue_keys_within_same_chat():
    """同一个 chat_id 下，不同 queue_key 的任务可以并行执行（用于 deep 不阻塞消息）。"""

    scheduler = TaskScheduler(max_concurrent=2, per_key_concurrency=1)

    lock = threading.Lock()
    active_total = 0
    max_active_total = 0
    active_by_key: dict[str, int] = {"msg": 0, "deep": 0}
    max_active_by_key: dict[str, int] = {"msg": 0, "deep": 0}

    def make_task(key: str):
        def _fn(ctx):
            nonlocal active_total, max_active_total
            with lock:
                active_total += 1
                active_by_key[key] += 1
                max_active_total = max(max_active_total, active_total)
                max_active_by_key[key] = max(max_active_by_key[key], active_by_key[key])
            time.sleep(0.2)
            with lock:
                active_total -= 1
                active_by_key[key] -= 1
            return key

        return _fn

    chat_id = "chat_1"
    h1 = scheduler.submit(
        TaskSpec(chat_id=chat_id, queue_key=f"{chat_id}:msg", name="m1", task_type="msg"),
        make_task("msg"),
    )
    h2 = scheduler.submit(
        TaskSpec(chat_id=chat_id, queue_key=f"{chat_id}:deep", name="d1", task_type="deep"),
        make_task("deep"),
    )

    r1 = h1.wait(timeout=3)
    r2 = h2.wait(timeout=3)
    assert r1.status == TaskStatus.SUCCEEDED
    assert r2.status == TaskStatus.SUCCEEDED
    assert max_active_total == 2
    assert max_active_by_key["msg"] == 1
    assert max_active_by_key["deep"] == 1


def test_scheduler_emits_valid_event_sequence_with_progress_updates():
    scheduler = TaskScheduler(max_concurrent=2, per_key_concurrency=1)
    events: list[tuple[str, str]] = []  # (run_id, status)
    lock = threading.Lock()

    def listener(ev):
        with lock:
            events.append((ev.run_id, ev.status))

    scheduler.add_listener(listener)

    def _fn(ctx):
        ctx.progress("p1", percent=10)
        time.sleep(0.05)
        ctx.progress("p2", percent=50)
        time.sleep(0.05)
        return "ok"

    h = scheduler.submit(TaskSpec(chat_id="c", name="t", task_type="test"), _fn)
    r = h.wait(timeout=3)
    assert r.status == TaskStatus.SUCCEEDED

    with lock:
        run_events = [st for rid, st in events if rid == h.run_id]

    # 至少包含 QUEUED -> RUNNING -> (RUNNING progress...)-> SUCCEEDED
    assert run_events[0] == TaskStatus.QUEUED
    assert TaskStatus.RUNNING in run_events
    assert run_events[-1] == TaskStatus.SUCCEEDED
    # 终态只出现一次
    assert run_events.count(TaskStatus.SUCCEEDED) == 1
    assert run_events.count(TaskStatus.FAILED) == 0
    assert run_events.count(TaskStatus.CANCELED) == 0


def test_scheduler_priority_high_runs_before_normal_in_same_queue():
    scheduler = TaskScheduler(max_concurrent=1, per_key_concurrency=1)

    order: list[str] = []
    lock = threading.Lock()
    started = threading.Event()
    release = threading.Event()

    def blocking(ctx):
        started.set()
        release.wait(timeout=2)
        with lock:
            order.append("blocking")
        return "blocking"

    def record(name: str):
        def _fn(ctx):
            with lock:
                order.append(name)
            return name

        return _fn

    chat_id = "c"
    hb = scheduler.submit(TaskSpec(chat_id=chat_id, name="blocking"), blocking)
    assert started.wait(timeout=1)

    # 在同一 queue_key 下提交 NORMAL 与 HIGH，释放后应先执行 HIGH
    hn = scheduler.submit(TaskSpec(chat_id=chat_id, name="normal", priority=TaskPriority.NORMAL), record("normal"))
    hh = scheduler.submit(TaskSpec(chat_id=chat_id, name="high", priority=TaskPriority.HIGH), record("high"))

    release.set()
    assert hb.wait(timeout=3).status == TaskStatus.SUCCEEDED
    assert hh.wait(timeout=3).status == TaskStatus.SUCCEEDED
    assert hn.wait(timeout=3).status == TaskStatus.SUCCEEDED

    with lock:
        # blocking 完成后，high 应该先于 normal
        assert order.index("high") < order.index("normal")


def test_scheduler_cancel_running_task_cooperatively():
    scheduler = TaskScheduler(max_concurrent=1, per_key_concurrency=1)
    started = threading.Event()

    def long_running(ctx):
        started.set()
        # 模拟长任务，定期检查取消
        for _ in range(50):
            ctx.check_canceled()
            time.sleep(0.01)
        return "done"

    h = scheduler.submit(TaskSpec(chat_id="c", name="long"), long_running)
    assert started.wait(timeout=1)
    assert h.cancel() is True

    r = h.wait(timeout=3)
    assert r.status == TaskStatus.CANCELED


def test_scheduler_stress_no_deadlock_or_conflict():
    """轻量压力测试：大量任务混合不同 chat/queue_key，验证不会死锁且满足并发语义。"""

    scheduler = TaskScheduler(max_concurrent=8, per_key_concurrency=1)
    lock = threading.Lock()
    active_total = 0
    max_active_total = 0
    active_by_key: dict[str, int] = {}
    max_active_by_key: dict[str, int] = {}

    def _task(key: str):
        def _fn(ctx):
            nonlocal active_total, max_active_total
            with lock:
                active_total += 1
                max_active_total = max(max_active_total, active_total)
                active_by_key[key] = active_by_key.get(key, 0) + 1
                max_active_by_key[key] = max(max_active_by_key.get(key, 0), active_by_key[key])

            # 短暂停留以放大并发窗口
            time.sleep(0.005)

            with lock:
                active_total -= 1
                active_by_key[key] -= 1
            return "ok"

        return _fn

    handles = []
    for i in range(200):
        chat_id = f"chat_{i % 10}"
        key = f"{chat_id}:{'deep' if (i % 5 == 0) else 'msg'}"
        handles.append(
            scheduler.submit(
                TaskSpec(chat_id=chat_id, queue_key=key, name=f"t{i}", task_type="stress"),
                _task(key),
            )
        )

    results = [h.wait(timeout=5) for h in handles]
    assert all(r.status == TaskStatus.SUCCEEDED for r in results)
    assert max_active_total <= 8

    # 每个 key 内必须串行（per_key_concurrency=1 实际按 queue_key 生效）
    assert all(v <= 1 for v in max_active_by_key.values())

