
with open("src/feishu/ws_client.py", "r") as f:
    content = f.read()

# Add rate limit imports if needed
imports_add = """from ..utils.rate_limit import RateLimiter, RateLimitExceededException
from ..utils.circuit_breaker import CircuitBreaker, CircuitBreakerOpenException"""
if "RateLimiter" not in content:
    content = content.replace("from ..tasking import TaskPriority, TaskScheduler, TaskSpec", imports_add + "\nfrom ..tasking import TaskPriority, TaskScheduler, TaskSpec")

# Initialize rate limiters in __init__
init_find = """        self._scheduler = TaskScheduler(
            max_concurrent=self.settings.task_scheduler_max_concurrent,
            per_key_concurrency=self.settings.task_scheduler_per_key_concurrency,
            system_concurrency=10,
            thread_name_prefix="ghost_worker",
        )"""
init_repl = """        self._scheduler = TaskScheduler(
            max_concurrent=self.settings.task_scheduler_max_concurrent,
            per_key_concurrency=self.settings.task_scheduler_per_key_concurrency,
            system_concurrency=10,
            thread_name_prefix="ghost_worker",
        )
        # Spec Engine limits: e.g. 50 calls per second, max 100 capacity
        self._scheduler.register_policy(
            "spec_command",
            rate_limiter=RateLimiter(capacity=100, fill_rate=50.0),
            circuit_breaker=CircuitBreaker(failure_threshold=10, recovery_timeout=5.0)
        )"""
content = content.replace(init_find, init_repl)

# Modify _handle_message to detect spec command early
handle_msg_find = """        is_system = self._is_system_command_message(data)
        is_shell_fast = False if is_system else self._is_likely_shell_command_message(data)"""
handle_msg_repl = """        is_system = self._is_system_command_message(data)
        is_shell_fast = False if is_system else self._is_likely_shell_command_message(data)
        
        is_spec = False
        try:
            content_str = data.event.message.content
            if content_str:
                import json
                content_dict = json.loads(content_str)
                text = content_dict.get("text", "").strip()
                is_spec = self._is_spec_command(text)
        except Exception:
            pass"""
content = content.replace(handle_msg_find, handle_msg_repl)

task_spec_find = """            spec = TaskSpec(
                chat_id=chat_id,
                name="process_message",
                task_type="feishu_message","""
task_spec_repl = """            spec = TaskSpec(
                chat_id=chat_id,
                name="process_message",
                task_type="spec_command" if is_spec else "feishu_message","""
content = content.replace(task_spec_find, task_spec_repl)

# Handle backpressure exception in submit
submit_find = """            handle = self._scheduler.submit(spec, lambda ctx, _sf=is_shell_fast: self._process_message_async(data, task_ctx=ctx, shell_fast_tracked=_sf))"""
submit_repl = """            try:
                handle = self._scheduler.submit(spec, lambda ctx, _sf=is_shell_fast: self._process_message_async(data, task_ctx=ctx, shell_fast_tracked=_sf))
            except (RateLimitExceededException, CircuitBreakerOpenException) as e:
                logger.warning(f"Backpressure applied: {e}")
                self._send_text_reply(message_id, f"⚠️ 系统繁忙 (Spec 模式)，请稍后再试: {e}")
                return"""
content = content.replace(submit_find, submit_repl)

with open("src/feishu/ws_client.py", "w") as f:
    f.write(content)
