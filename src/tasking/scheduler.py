import time
import uuid
import threading
from collections import defaultdict, deque
from dataclasses import replace
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any, Callable, Deque, Optional

from concurrent.futures import Future, ThreadPoolExecutor


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class TaskPriority(IntEnum):
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


@dataclass
class TaskResult:
    run_id: str
    status: TaskStatus
    value: Any = None
    error: Optional[str] = None
    started_at: Optional[float] = None
    ended_at: Optional[float] = None


@dataclass(frozen=True)
class TaskEvent:
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
    def __init__(self):
        self._evt = threading.Event()

    def cancel(self):
        self._evt.set()

    @property
    def is_canceled(self) -> bool:
        return self._evt.is_set()

    def raise_if_canceled(self):
        if self.is_canceled:
            raise TaskCanceledError("task canceled")


class TaskCanceledError(RuntimeError):
    pass


@dataclass
class TaskRunState:
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
        self._scheduler.update_progress(self.run_id, message=message, percent=percent)

    def check_canceled(self):
        self.cancel_token.raise_if_canceled()


@dataclass
class _QueuedTask:
    run_id: str
    spec: TaskSpec
    fn: Callable[[TaskContext], Any]


class TaskHandle:
    def __init__(self, scheduler: "TaskScheduler", run_id: str):
        self._scheduler = scheduler
        self.run_id = run_id

    def cancel(self) -> bool:
        return self._scheduler.cancel(self.run_id)

    def wait(self, timeout: Optional[float] = None) -> TaskResult:
        return self._scheduler.wait(self.run_id, timeout=timeout)

    def get_state(self) -> TaskRunState:
        state = self._scheduler.get_state(self.run_id)
        if not state:
            raise KeyError(f"unknown run_id: {self.run_id}")
        return state


class TaskScheduler:
    """A lightweight scheduler that provides:
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

        # Lightweight indexes for querying tasks by project/chat.
        # Keep ordering by insertion time (oldest -> newest).
        self._by_chat: dict[str, Deque[str]] = defaultdict(deque)      # chat_id -> run_ids
        self._by_project: dict[str, Deque[str]] = defaultdict(deque)   # project_id -> run_ids
        self._by_task_id: dict[str, str] = {}                          # task_id -> run_id
        self._stopped = False
        self._dispatcher = threading.Thread(
            target=self._dispatch_loop,
            name="task_scheduler_dispatcher",
            daemon=True,
        )
        self._dispatcher.start()

    def add_listener(self, listener: Callable[[TaskEvent], None]):
        with self._lock:
            self._listeners.append(listener)

    def submit(self, spec: TaskSpec, fn: Callable[[TaskContext], Any]) -> TaskHandle:
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
            item = _QueuedTask(run_id=run_id, spec=spec, fn=fn)

            if spec.priority == TaskPriority.HIGH:
                q.appendleft(item)
            else:
                q.append(item)

            self._emit(run_id, TaskStatus.QUEUED)
            self._cv.notify_all()

        return TaskHandle(self, run_id)

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
            self._emit(run_id, state.status, progress_message=state.progress_message, progress_percent=state.progress_percent)
            return True

    def cancel(self, run_id: str) -> bool:
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

    def update_progress(self, run_id: str, *, message: str, percent: Optional[float] = None):
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

    def get_state(self, run_id: str) -> Optional[TaskRunState]:
        with self._lock:
            return self._states.get(run_id)

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
                    (tid, rid)
                    for tid, rid in self._by_task_id.items()
                    if tid.endswith(task_id) or task_id in tid
                ]
                if len(matches) == 1:
                    return self._states.get(matches[0][1])
            return None

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

    def wait(self, run_id: str, timeout: Optional[float] = None) -> TaskResult:
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

    def stop(self, *, wait: bool = False, shutdown_executor: bool = False):
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
        return key.endswith(SYSTEM_QUEUE_SUFFIX)

    def _get_key_concurrency_limit(self, key: str) -> int:
        if self._is_system_queue(key):
            return self._system_concurrency
        return self._per_key

    def _pick_next_task_unlocked(self) -> Optional[_QueuedTask]:
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
        run_id = task.run_id
        spec = task.spec
        state = self.get_state(run_id)
        token = state.cancellation if state else CancellationToken()
        ctx = TaskContext(self, run_id=run_id, token=token, spec=spec)

        try:
            token.raise_if_canceled()
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
        q = self._queues.get(key)
        if not q:
            return
        new_q = deque(item for item in q if item.run_id != run_id)
        self._queues[key] = new_q

    def _remove_from_index_unlocked(self, index: dict[str, Deque[str]], key: str, run_id: str):
        q = index.get(key)
        if not q:
            return
        # rebuild to remove run_id
        index[key] = deque(rid for rid in q if rid != run_id)
