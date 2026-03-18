import contextvars
import threading
import time
import uuid
from collections import defaultdict, deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from enum import Enum, IntEnum
from typing import Any, Callable, Deque, Optional

from ..utils.circuit_breaker import CircuitBreaker, CircuitBreakerOpenException, CircuitState
from ..utils.rate_limit import RateLimiter, RateLimitExceededException


class TaskStatus(str, Enum):
    """任务运行状态（终态：`SUCCEEDED`/`FAILED`/`CANCELED`）。"""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class TaskPriority(IntEnum):
    """任务优先级：数值越小优先级越高。"""

    HIGH = 0
    NORMAL = 10
    LOW = 20


SYSTEM_QUEUE_SUFFIX = ":SYSTEM"
DEFAULT_QUEUE_SUFFIX = ":DEFAULT"


@dataclass(frozen=True)
class TaskSpec:
    """Metadata that influences routing and scheduling."""

    chat_id: str
    name: str
    task_type: str = "generic"
    queue_key: Optional[str] = None
    project_id: Optional[str] = None
    message_id: Optional[str] = None
    origin_message_id: Optional[str] = None
    request_id: Optional[str] = None
    task_id: Optional[str] = None  # Human-readable ID e.g. "myproject_20260227_143025_a1b2"
    priority: TaskPriority = TaskPriority.NORMAL
    is_system_command: bool = False

    def get_effective_queue_key(self) -> str:
        """Calculate the effective queue key for routing.

        Routing rules:
        - System commands: {chat_id}:SYSTEM (high concurrency, bypasses per-key limit)
        - Project tasks: {chat_id}:{project_id} (serial within project)
        - No project: {chat_id}:DEFAULT (serial)
        """
        if self.queue_key:
            return self.queue_key
        if self.is_system_command:
            return f"{self.chat_id}{SYSTEM_QUEUE_SUFFIX}"
        if self.project_id:
            return f"{self.chat_id}:{self.project_id}"
        return f"{self.chat_id}{DEFAULT_QUEUE_SUFFIX}"

    # lowerCamelCase alias (compat / gradual refactor)
    def getEffectiveQueueKey(self) -> str:
        """`get_effective_queue_key()` 的 lowerCamelCase 兼容别名。"""
        return self.get_effective_queue_key()


@dataclass
class TaskResult:
    """任务执行结果（由 `TaskScheduler.wait()` 返回）。"""

    run_id: str
    status: TaskStatus
    value: Any = None
    error: Optional[str] = None
    started_at: Optional[float] = None
    ended_at: Optional[float] = None


@dataclass(frozen=True)
class TaskEvent:
    """任务状态/进度事件（用于 listeners 与可观测性输出）。"""

    run_id: str
    chat_id: str
    status: TaskStatus
    timestamp: float
    name: str
    task_type: str
    project_id: Optional[str] = None
    message_id: Optional[str] = None
    origin_message_id: Optional[str] = None
    request_id: Optional[str] = None
    task_id: Optional[str] = None
    progress_message: Optional[str] = None
    progress_percent: Optional[float] = None
    error: Optional[str] = None


class CancellationToken:
    """任务取消令牌。

    约定：调度器不会强制中断运行中的线程；任务需要主动检查该 token。
    """

    def __init__(self):
        self._evt = threading.Event()

    def cancel(self):
        """标记为已取消（幂等）。"""
        self._evt.set()

    def isCanceled(self) -> bool:
        """lowerCamelCase 兼容别名：`is_canceled`。"""
        return self.is_canceled

    @property
    def is_canceled(self) -> bool:
        return self._evt.is_set()

    def raise_if_canceled(self):
        """若已取消则抛 `TaskCanceledError`。"""
        if self.is_canceled:
            raise TaskCanceledError("task canceled")

    def raiseIfCanceled(self):
        """lowerCamelCase 兼容别名：`raise_if_canceled()`。"""
        return self.raise_if_canceled()


class TaskCanceledError(RuntimeError):
    """任务取消异常（任务内部可用来提前退出）。"""

    pass


@dataclass
class TaskRunState:
    """任务运行态（调度器内部 SSOT）。"""

    spec: TaskSpec
    run_id: str
    status: TaskStatus = TaskStatus.QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    progress_message: Optional[str] = None
    progress_percent: Optional[float] = None
    error: Optional[str] = None
    cancellation: CancellationToken = field(default_factory=CancellationToken)
    future: Optional[Future] = None


class TaskContext:
    """Context passed to the running task function."""

    def __init__(self, scheduler: "TaskScheduler", run_id: str, token: CancellationToken, spec: "TaskSpec"):
        self._scheduler = scheduler
        self.run_id = run_id
        self.cancel_token = token
        self.spec = spec

    def progress(self, message: str, percent: Optional[float] = None):
        """更新任务进度（仅对 RUNNING 有效）。"""
        self._scheduler.update_progress(self.run_id, message=message, percent=percent)

    def updateProgress(self, message: str, percent: Optional[float] = None):
        """lowerCamelCase 兼容别名：`progress()`。"""
        return self.progress(message, percent)

    def check_canceled(self):
        """若任务已取消则抛异常（建议任务函数周期性调用）。"""
        self.cancel_token.raise_if_canceled()

    def checkCanceled(self):
        """lowerCamelCase 兼容别名：`check_canceled()`。"""
        return self.check_canceled()


@dataclass
class _QueuedTask:
    """队列中的任务项（内部数据结构）。"""

    run_id: str
    spec: TaskSpec
    fn: Callable[[TaskContext], Any]
    context: contextvars.Context


class TaskHandle:
    """任务句柄：供调用方取消/等待/查询状态。"""

    def __init__(self, scheduler: "TaskScheduler", run_id: str):
        self._scheduler = scheduler
        self.run_id = run_id

    def cancel(self) -> bool:
        """请求取消任务。"""
        return self._scheduler.cancel(self.run_id)

    def cancelTask(self) -> bool:
        """lowerCamelCase 兼容别名：`cancel()`。"""
        return self.cancel()

    def wait(self, timeout: Optional[float] = None) -> TaskResult:
        """等待任务结束并返回 `TaskResult`。"""
        return self._scheduler.wait(self.run_id, timeout=timeout)

    def waitForResult(self, timeout: Optional[float] = None) -> TaskResult:
        """lowerCamelCase 兼容别名：`wait()`。"""
        return self.wait(timeout=timeout)

    def get_state(self) -> TaskRunState:
        """返回当前 `TaskRunState`（不存在则抛 `KeyError`）。"""
        state = self._scheduler.get_state(self.run_id)
        if not state:
            raise KeyError(f"unknown run_id: {self.run_id}")
        return state

    def getState(self) -> TaskRunState:
        """lowerCamelCase 兼容别名：`get_state()`。"""
        return self.get_state()


class TaskScheduler:
    """一个轻量级、线程安全的任务调度器。

    设计目标（面向服务端长连接 Bot）：
    - **按队列串行**：同一 `queue_key` 默认串行执行，避免同一项目/会话的并发写冲突。
    - **全局并发受控**：通过线程池限制整体并发，避免资源打爆。
    - **系统命令快通道**：系统控制类任务可走 `:SYSTEM` 队列，绕开 per-key 串行限制。
    - **可观测性**：`TaskRunState` + `TaskEvent` 提供状态/进度/错误的统一视图。
    - **背压/熔断**：按 `task_type` 维度接入 `RateLimiter` / `CircuitBreaker`。

    关键语义约定：
    - `submit()` 只负责排队 + 返回 `TaskHandle`，不会阻塞执行。
    - `cancel(run_id)`：
      - 若任务仍在队列中，会从队列移除并进入 `CANCELED`。
      - 若任务正在运行，只会设置 cancellation token，具体中断由任务函数自行检查。
    - `update_progress()` 只在 RUNNING 阶段生效（避免错误的“进度倒灌”）。

    Queue key routing:
    - per-key ordered execution (default per_key_concurrency=1)
    - global concurrency limit (max_concurrent)
    - task status tracking and progress updates
    - system command fast-track (bypasses per-key limit)

    Queue key routing:
    - System commands: {chat_id}:SYSTEM (high concurrency)
    - Project tasks: {chat_id}:{project_id} (serial within project)
    - No project: {chat_id}:DEFAULT (serial)

    This allows different projects to execute concurrently while
    maintaining serial execution within each project.
    """

    def __init__(
        self,
        *,
        max_concurrent: int = 10,
        per_key_concurrency: int = 1,
        system_concurrency: int = 10,
        worker_executor: Optional[ThreadPoolExecutor] = None,
        thread_name_prefix: str = "task_worker",
    ):
        if max_concurrent <= 0:
            raise ValueError("max_concurrent must be > 0")
        if per_key_concurrency <= 0:
            raise ValueError("per_key_concurrency must be > 0")

        self._max_concurrent = max_concurrent
        self._per_key = per_key_concurrency
        self._system_concurrency = system_concurrency

        self._executor = worker_executor or ThreadPoolExecutor(
            max_workers=max_concurrent,
            thread_name_prefix=thread_name_prefix,
        )

        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._queues: dict[str, Deque[_QueuedTask]] = defaultdict(deque)  # queue_key -> queue
        self._running_by_key: dict[str, int] = defaultdict(int)
        self._running_total = 0
        self._states: dict[str, TaskRunState] = {}
        self._listeners: list[Callable[[TaskEvent], None]] = []

        # Rate limiters and circuit breakers by task_type
        self._rate_limiters: dict[str, RateLimiter] = {}
        self._circuit_breakers: dict[str, CircuitBreaker] = {}

        # Lightweight indexes for querying tasks by project/chat.
        # Keep ordering by insertion time (oldest -> newest).
        self._by_chat: dict[str, Deque[str]] = defaultdict(deque)  # chat_id -> run_ids
        self._by_project: dict[str, Deque[str]] = defaultdict(deque)  # project_id -> run_ids
        self._by_task_id: dict[str, str] = {}  # task_id -> run_id
        self._stopped = False
        self._dispatcher = threading.Thread(
            target=self._dispatch_loop,
            name="task_scheduler_dispatcher",
            daemon=True,
        )
        self._dispatcher.start()

    def register_policy(
        self,
        task_type: str,
        rate_limiter: Optional[RateLimiter] = None,
        circuit_breaker: Optional[CircuitBreaker] = None,
    ):
        """为指定 `task_type` 注册背压/熔断策略。"""
        with self._lock:
            if rate_limiter:
                self._rate_limiters[task_type] = rate_limiter
            if circuit_breaker:
                self._circuit_breakers[task_type] = circuit_breaker

    def registerPolicy(
        self,
        taskType: str,
        rateLimiter: Optional[RateLimiter] = None,
        circuitBreaker: Optional[CircuitBreaker] = None,
    ):
        """lowerCamelCase 兼容别名：`register_policy()`。"""
        return self.register_policy(taskType, rate_limiter=rateLimiter, circuit_breaker=circuitBreaker)

    def add_listener(self, listener: Callable[[TaskEvent], None]):
        """添加任务事件监听器（best-effort，监听器异常会被吞掉）。"""
        with self._lock:
            self._listeners.append(listener)

    def addListener(self, listener: Callable[[TaskEvent], None]):
        """lowerCamelCase 兼容别名：`add_listener()`。"""
        return self.add_listener(listener)

    def submit(self, spec: TaskSpec, fn: Callable[[TaskContext], Any]) -> TaskHandle:
        """提交任务（入队）并返回 `TaskHandle`。

        该方法会先执行 *task_type 级别* 的背压检查：
        - CircuitBreaker OPEN -> 直接拒绝
        - RateLimiter 获取失败 -> 直接拒绝

        通过后才会生成 `run_id`、落地 `TaskRunState`，并推入对应的队列。
        """
        with self._lock:
            rl = self._rate_limiters.get(spec.task_type)
            cb = self._circuit_breakers.get(spec.task_type)

            if cb and cb.state == CircuitState.OPEN:
                raise CircuitBreakerOpenException(f"Circuit breaker OPEN for task type {spec.task_type}")

            if rl and not rl.acquire(1, blocking=False):
                raise RateLimitExceededException(f"Rate limit exceeded for task type {spec.task_type}")

        run_id = str(uuid.uuid4())[:10]
        state = TaskRunState(spec=spec, run_id=run_id)

        with self._cv:
            if self._stopped:
                raise RuntimeError("TaskScheduler is stopped")
            self._states[run_id] = state
            if spec.task_id:
                self._by_task_id[spec.task_id] = run_id
            self._by_chat[spec.chat_id].append(run_id)
            if spec.project_id:
                self._by_project[str(spec.project_id)].append(run_id)
            key = spec.get_effective_queue_key()
            q = self._queues[key]

            # Capture current context
            ctx = contextvars.copy_context()
            item = _QueuedTask(run_id=run_id, spec=spec, fn=fn, context=ctx)

            if spec.priority == TaskPriority.HIGH:
                q.appendleft(item)
            else:
                q.append(item)

            self._emit(run_id, TaskStatus.QUEUED)
            self._cv.notify_all()

        return TaskHandle(self, run_id)

    def submitTask(self, spec: TaskSpec, fn: Callable[[TaskContext], Any]) -> TaskHandle:
        """lowerCamelCase 兼容别名：`submit()`。"""
        return self.submit(spec, fn)

    def update_project_id(self, run_id: str, project_id: Optional[str]) -> bool:
        """Best-effort update of project_id for an existing task.

        Useful when the project is resolved inside the task body (e.g. by
        reply-chain mapping) rather than at submit time.
        """
        if not project_id:
            return False

        with self._lock:
            state = self._states.get(run_id)
            if not state:
                return False
            old_project = state.spec.project_id
            if old_project == project_id:
                return True

            # replace frozen TaskSpec
            state.spec = replace(state.spec, project_id=str(project_id))

            # update indexes
            if old_project:
                self._remove_from_index_unlocked(self._by_project, str(old_project), run_id)
            self._by_project[str(project_id)].append(run_id)

            # emit updated state to listeners (as RUNNING with progress info if running,
            # otherwise as current status). This is for observability only.
            self._emit(
                run_id, state.status, progress_message=state.progress_message, progress_percent=state.progress_percent
            )
            return True

    def updateProjectId(self, runId: str, projectId: Optional[str]) -> bool:
        """lowerCamelCase 兼容别名：`update_project_id()`。"""
        return self.update_project_id(runId, projectId)

    def cancel(self, run_id: str) -> bool:
        """取消任务。

        - 若任务仍在队列中：移除并标记为 `CANCELED`。
        - 若任务已在运行：仅设置取消令牌，任务需主动检查。
        """
        with self._cv:
            state = self._states.get(run_id)
            if not state:
                return False
            if state.status in (TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELED):
                return False

            state.cancellation.cancel()

            if state.status == TaskStatus.QUEUED:
                self._remove_from_queue_unlocked(run_id, state.spec.get_effective_queue_key())
                state.status = TaskStatus.CANCELED
                state.ended_at = time.time()
                self._emit(run_id, TaskStatus.CANCELED)
                self._cv.notify_all()
                return True

            self._cv.notify_all()
            return True

    def cancelRun(self, runId: str) -> bool:
        """lowerCamelCase 兼容别名：`cancel()`。"""
        return self.cancel(runId)

    def update_progress(self, run_id: str, *, message: str, percent: Optional[float] = None):
        """更新任务进度（仅 RUNNING 状态有效）。"""
        with self._lock:
            state = self._states.get(run_id)
            if not state:
                return
            if state.status != TaskStatus.RUNNING:
                return
            state.progress_message = message
            if percent is not None:
                # clamp into [0, 100]
                state.progress_percent = max(0.0, min(100.0, float(percent)))
            self._emit(run_id, state.status, progress_message=message, progress_percent=state.progress_percent)

    def updateProgress(self, runId: str, *, message: str, percent: Optional[float] = None):
        """lowerCamelCase 兼容别名：`update_progress()`。"""
        return self.update_progress(runId, message=message, percent=percent)

    def get_state(self, run_id: str) -> Optional[TaskRunState]:
        """获取任务运行态（不存在返回 None）。"""
        with self._lock:
            return self._states.get(run_id)

    def getState(self, runId: str) -> Optional[TaskRunState]:
        """lowerCamelCase 兼容别名：`get_state()`。"""
        return self.get_state(runId)

    def get_state_by_task_id(self, task_id: str) -> Optional[TaskRunState]:
        """Look up a task by its human-readable task_id.

        Supports exact match and partial suffix match (last 6+ chars).
        """
        with self._lock:
            # Exact match
            run_id = self._by_task_id.get(task_id)
            if run_id:
                return self._states.get(run_id)
            # Partial suffix match (for short-id queries like "a1b2" or "143025_a1b2")
            if len(task_id) >= 4:
                matches = [
                    (tid, rid) for tid, rid in self._by_task_id.items() if tid.endswith(task_id) or task_id in tid
                ]
                if len(matches) == 1:
                    return self._states.get(matches[0][1])
            return None

    def getStateByTaskId(self, taskId: str) -> Optional[TaskRunState]:
        """lowerCamelCase 兼容别名：`get_state_by_task_id()`。"""
        return self.get_state_by_task_id(taskId)

    def list_tasks(
        self,
        *,
        chat_id: Optional[str] = None,
        project_id: Optional[str] = None,
        include_done: bool = False,
        limit: int = 50,
    ) -> list[TaskRunState]:
        """Query tasks by chat/project.

        - If both chat_id and project_id are provided, it returns the intersection.
        - By default, it returns non-terminal tasks only.
        """
        if limit <= 0:
            return []

        with self._lock:
            run_ids: list[str]

            if chat_id is not None and project_id is not None:
                chat_ids = list(self._by_chat.get(chat_id, []))
                proj_ids = set(self._by_project.get(str(project_id), []))
                run_ids = [rid for rid in chat_ids if rid in proj_ids]
            elif chat_id is not None:
                run_ids = list(self._by_chat.get(chat_id, []))
            elif project_id is not None:
                run_ids = list(self._by_project.get(str(project_id), []))
            else:
                run_ids = list(self._states.keys())

            # newest first
            run_ids = run_ids[-limit:][::-1]

            out: list[TaskRunState] = []
            for rid in run_ids:
                st = self._states.get(rid)
                if not st:
                    continue
                if not include_done and st.status in (TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELED):
                    continue
                out.append(st)

            return out

    def listTasks(
        self,
        *,
        chatId: Optional[str] = None,
        projectId: Optional[str] = None,
        includeDone: bool = False,
        limit: int = 50,
    ) -> list[TaskRunState]:
        """lowerCamelCase 兼容别名：`list_tasks()`。"""
        return self.list_tasks(chat_id=chatId, project_id=projectId, include_done=includeDone, limit=limit)

    def wait(self, run_id: str, timeout: Optional[float] = None) -> TaskResult:
        """等待任务进入终态并返回 `TaskResult`（超时抛 `TimeoutError`）。"""
        deadline = None if timeout is None else (time.time() + timeout)

        with self._cv:
            st = self._states.get(run_id)
            if not st:
                raise KeyError(f"unknown run_id: {run_id}")

            while st.status not in (TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELED):
                remaining = None if deadline is None else max(0.0, deadline - time.time())
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("wait timeout")
                self._cv.wait(timeout=remaining)
                st = self._states.get(run_id)
                if not st:
                    raise KeyError(f"unknown run_id: {run_id}")

            # terminal
            value = None
            if st.future and st.status == TaskStatus.SUCCEEDED:
                try:
                    # future should already be done; keep it best-effort
                    value = st.future.result(timeout=0)
                except Exception:
                    value = None

            return TaskResult(
                run_id=run_id,
                status=st.status,
                value=value,
                error=st.error,
                started_at=st.started_at,
                ended_at=st.ended_at,
            )

    def waitForRun(self, runId: str, timeout: Optional[float] = None) -> TaskResult:
        """lowerCamelCase 兼容别名：`wait()`。"""
        return self.wait(runId, timeout=timeout)

    def stop(self, *, wait: bool = False, shutdown_executor: bool = False):
        """停止调度器（best-effort）。

        - `wait=True`：等待 dispatcher thread 退出（短超时）。
        - `shutdown_executor=True`：关闭线程池（不会强杀运行中的任务）。
        """
        with self._cv:
            self._stopped = True
            self._cv.notify_all()
        if wait:
            self._dispatcher.join(timeout=2)
        if shutdown_executor:
            try:
                # Best-effort: cancel queued futures to speed up shutdown.
                # Running futures cannot be forcibly stopped by ThreadPoolExecutor.
                self._executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass

    # ------------------------ internal ------------------------

    def _dispatch_loop(self):
        """调度循环：不断从队列中挑选可运行任务并投递到线程池。

        该循环运行在一个 daemon thread 中：
        - 等待条件：有任务可跑 / 有任务结束释放并发额度 / stop() 通知
        - 选择策略：优先 SYSTEM 队列（高并发），否则按队列公平选择（尽量避免饥饿）
        """
        while True:
            with self._cv:
                if self._stopped:
                    return

                task = self._pick_next_task_unlocked()
                if not task:
                    self._cv.wait(timeout=0.2)
                    continue

                self._running_total += 1
                key = task.spec.get_effective_queue_key()
                self._running_by_key[key] += 1
                state = self._states.get(task.run_id)
                if state:
                    state.status = TaskStatus.RUNNING
                    state.started_at = time.time()
                    self._emit(task.run_id, TaskStatus.RUNNING)
                self._cv.notify_all()

                fut = self._executor.submit(self._run_wrapper, task)
                if state:
                    state.future = fut

    def _is_system_queue(self, key: str) -> bool:
        """判断队列 key 是否为系统快通道。"""
        return key.endswith(SYSTEM_QUEUE_SUFFIX)

    def _get_key_concurrency_limit(self, key: str) -> int:
        """返回指定 queue_key 的并发上限。"""
        if self._is_system_queue(key):
            return self._system_concurrency
        return self._per_key

    def _pick_next_task_unlocked(self) -> Optional[_QueuedTask]:
        """挑选一个当前可运行的任务（调用方需持有 `_cv` 锁）。"""
        if self._running_total >= self._max_concurrent:
            return None

        for key, q in list(self._queues.items()):
            if not q:
                continue
            limit = self._get_key_concurrency_limit(key)
            if self._running_by_key.get(key, 0) >= limit:
                continue

            while q:
                item = q[0]
                st = self._states.get(item.run_id)
                if st and st.cancellation.is_canceled:
                    q.popleft()
                    st.status = TaskStatus.CANCELED
                    st.ended_at = time.time()
                    self._emit(item.run_id, TaskStatus.CANCELED)
                    continue
                break

            if not q:
                continue
            return q.popleft()

        return None

    def _run_wrapper(self, task: _QueuedTask):
        """Wrapper to run the task in its captured context."""
        return task.context.run(self._do_run, task)

    def _do_run(self, task: _QueuedTask):
        """执行任务并维护状态（运行在 worker thread 中）。"""
        run_id = task.run_id
        spec = task.spec
        state = self.get_state(run_id)
        token = state.cancellation if state else CancellationToken()
        ctx = TaskContext(self, run_id=run_id, token=token, spec=spec)

        try:
            token.raise_if_canceled()

            cb = self._circuit_breakers.get(spec.task_type)
            if cb:
                value = cb.call(task.fn, ctx)
            else:
                value = task.fn(ctx)

            token.raise_if_canceled()

            with self._cv:
                st = self._states.get(run_id)
                if st:
                    st.status = TaskStatus.SUCCEEDED
                    st.ended_at = time.time()
                    self._emit(run_id, TaskStatus.SUCCEEDED)
                self._cv.notify_all()
                return value

        except TaskCanceledError:
            with self._cv:
                st = self._states.get(run_id)
                if st:
                    st.status = TaskStatus.CANCELED
                    st.ended_at = time.time()
                    self._emit(run_id, TaskStatus.CANCELED)
                self._cv.notify_all()
            return None

        except Exception as e:
            with self._cv:
                st = self._states.get(run_id)
                if st:
                    st.status = TaskStatus.FAILED
                    st.error = str(e)
                    st.ended_at = time.time()
                    self._emit(run_id, TaskStatus.FAILED, error=str(e))
                self._cv.notify_all()
            raise

        finally:
            with self._cv:
                self._running_total = max(0, self._running_total - 1)
                key = spec.get_effective_queue_key()
                self._running_by_key[key] = max(0, self._running_by_key[key] - 1)
                self._cv.notify_all()

    def _emit(
        self,
        run_id: str,
        status: TaskStatus,
        *,
        progress_message: Optional[str] = None,
        progress_percent: Optional[float] = None,
        error: Optional[str] = None,
    ):
        """向所有监听器广播一次任务事件（best-effort）。"""
        st = self._states.get(run_id)
        if not st:
            return
        ev = TaskEvent(
            run_id=run_id,
            chat_id=st.spec.chat_id,
            status=status,
            timestamp=time.time(),
            name=st.spec.name,
            task_type=st.spec.task_type,
            project_id=st.spec.project_id,
            message_id=st.spec.message_id,
            origin_message_id=st.spec.origin_message_id,
            request_id=st.spec.request_id,
            task_id=st.spec.task_id,
            progress_message=progress_message,
            progress_percent=progress_percent,
            error=error,
        )
        for listener in list(self._listeners):
            try:
                listener(ev)
            except Exception:
                # listeners must never break scheduler
                pass

    def _remove_from_queue_unlocked(self, run_id: str, key: str):
        """从某个队列中移除指定 run_id（调用方需持锁）。"""
        q = self._queues.get(key)
        if not q:
            return
        new_q = deque(item for item in q if item.run_id != run_id)
        self._queues[key] = new_q

    def _remove_from_index_unlocked(self, index: dict[str, Deque[str]], key: str, run_id: str):
        """从索引（by_chat/by_project）中移除 run_id（调用方需持锁）。"""
        q = index.get(key)
        if not q:
            return
        # rebuild to remove run_id
        index[key] = deque(rid for rid in q if rid != run_id)
