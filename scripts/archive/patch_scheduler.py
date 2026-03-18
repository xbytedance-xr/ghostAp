
with open("src/tasking/scheduler.py", "r") as f:
    content = f.read()

# Add imports for RateLimiter and CircuitBreaker
imports_str = """
from concurrent.futures import Future, ThreadPoolExecutor

from ..utils.rate_limit import RateLimiter, RateLimitExceededException
from ..utils.circuit_breaker import CircuitBreaker, CircuitState, CircuitBreakerOpenException
"""
content = content.replace("from concurrent.futures import Future, ThreadPoolExecutor", imports_str)

# Add dicts in __init__
init_str = """        self._listeners: list[Callable[[TaskEvent], None]] = []"""
init_repl = """        self._listeners: list[Callable[[TaskEvent], None]] = []
        
        # Rate limiters and circuit breakers by task_type
        self._rate_limiters: dict[str, RateLimiter] = {}
        self._circuit_breakers: dict[str, CircuitBreaker] = {}"""
content = content.replace(init_str, init_repl)

methods_str = """    def add_listener(self, listener: Callable[[TaskEvent], None]):
        with self._lock:
            self._listeners.append(listener)"""
methods_repl = """    def register_policy(
        self,
        task_type: str,
        rate_limiter: Optional[RateLimiter] = None,
        circuit_breaker: Optional[CircuitBreaker] = None
    ):
        with self._lock:
            if rate_limiter:
                self._rate_limiters[task_type] = rate_limiter
            if circuit_breaker:
                self._circuit_breakers[task_type] = circuit_breaker

    def add_listener(self, listener: Callable[[TaskEvent], None]):
        with self._lock:
            self._listeners.append(listener)"""
content = content.replace(methods_str, methods_repl)

submit_str = """    def submit(self, spec: TaskSpec, fn: Callable[[TaskContext], Any]) -> TaskHandle:
        run_id = str(uuid.uuid4())[:10]
        state = TaskRunState(spec=spec, run_id=run_id)

        with self._cv:
            if self._stopped:
                raise RuntimeError("TaskScheduler is stopped")"""
submit_repl = """    def submit(self, spec: TaskSpec, fn: Callable[[TaskContext], Any]) -> TaskHandle:
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
                raise RuntimeError("TaskScheduler is stopped")"""
content = content.replace(submit_str, submit_repl)

run_str = """        try:
            token.raise_if_canceled()
            value = task.fn(ctx)
            token.raise_if_canceled()"""
run_repl = """        try:
            token.raise_if_canceled()
            
            cb = self._circuit_breakers.get(spec.task_type)
            if cb:
                value = cb.call(task.fn, ctx)
            else:
                value = task.fn(ctx)
                
            token.raise_if_canceled()"""
content = content.replace(run_str, run_repl)

with open("src/tasking/scheduler.py", "w") as f:
    f.write(content)
