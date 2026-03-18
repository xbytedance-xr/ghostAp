
with open("src/loop_engine/engine.py", "r") as f:
    content = f.read()

imports_add = """from ..spec_engine.retry import should_retry, get_retry_delay, RetryPolicy
"""
if "from ..spec_engine.retry" not in content:
    content = content.replace("from ..config import get_settings", imports_add + "from ..config import get_settings")

send_prompt_find = """                # Track events for this iteration
                iter_tracker = IterationTracker()
                on_event = self._make_on_event(iter_tracker, iteration, callbacks)
                result = self._session.send_prompt(prompt, on_event=on_event, timeout=timeout)"""

send_prompt_repl = """                # Track events for this iteration
                iter_tracker = IterationTracker()
                on_event = self._make_on_event(iter_tracker, iteration, callbacks)
                
                # Retry logic with exponential backoff
                retry_policy = RetryPolicy(max_retries=3, retry_delay=2.0)
                attempt = 0
                result = None
                while attempt <= retry_policy.max_retries:
                    try:
                        result = self._session.send_prompt(prompt, on_event=on_event, timeout=timeout)
                        break
                    except Exception as e:
                        if attempt < retry_policy.max_retries and should_retry(e):
                            delay = get_retry_delay(attempt, retry_policy)
                            logger.warning(f"[Loop] send_prompt 失败: {e}. 正在进行第 {attempt + 1} 次重试，等待 {delay:.1f}s")
                            time.sleep(delay)
                            attempt += 1
                        else:
                            raise"""
content = content.replace(send_prompt_find, send_prompt_repl)

with open("src/loop_engine/engine.py", "w") as f:
    f.write(content)
