import logging
import time
from typing import Optional, Callable
from ..session.base import BaseSession
from ..config import get_settings
from .models import DeepTask, ExecutionResult, DeepTaskStatus

logger = logging.getLogger(__name__)

# CocoSession and ClaudeSession share the same BaseSession interface
AISession = BaseSession


class TaskExecutor:
    def __init__(self, session: AISession, cwd: str, should_stop: Optional[Callable[[], bool]] = None):
        self.session = session
        self.cwd = cwd
        self.settings = get_settings()
        self._should_stop = should_stop

    def execute(
        self,
        task: DeepTask,
        on_chunk: Optional[Callable[[str], None]] = None,
        timeout: Optional[int] = None
    ) -> ExecutionResult:
        start_time = time.time()
        task.start()

        try:
            if on_chunk:
                output = self.session.send_prompt_streaming(
                    prompt=task.prompt,
                    on_chunk=on_chunk,
                    timeout=timeout or self.settings.coco_execution_timeout,
                    cwd=self.cwd,
                    chunk_interval=0.5,
                    should_stop=self._should_stop,
                )
            else:
                output = self.session.send_prompt(
                    prompt=task.prompt,
                    timeout=timeout or self.settings.coco_execution_timeout,
                    cwd=self.cwd,
                )

            duration = time.time() - start_time
            success = self._check_success(output)

            if success:
                task.complete(output)
            else:
                error = self._extract_error(output)
                task.fail(error)

            return ExecutionResult(
                task_id=task.task_id,
                success=success,
                output=output,
                duration=duration,
                error=None if success else self._extract_error(output),
            )

        except Exception as e:
            duration = time.time() - start_time
            error_msg = str(e)
            task.fail(error_msg)

            return ExecutionResult(
                task_id=task.task_id,
                success=False,
                output="",
                duration=duration,
                error=error_msg,
            )

    def _check_success(self, output: str) -> bool:
        # 1. Explicit tokens (highest priority)
        if "DEEP_TASK_SUCCESS" in output:
            return True
        if "DEEP_TASK_FAILURE" in output:
            return False

        lower_output = output.lower()

        # 2. Critical errors that usually mean immediate failure
        critical_errors = [
            "traceback (most recent call last)",
            "command not found",
            "no such file or directory",
            "syntaxerror:",
            "importerror:",
            "modulenotfounderror:",
            "permission denied",
        ]
        
        for err in critical_errors:
            if err in lower_output:
                # If "✅" appears AFTER the error, assume it was fixed/handled
                if "✅" in output:
                    if output.rindex("✅") > lower_output.rindex(err):
                        continue
                return False

        # 3. General error indicators
        error_indicators = [
            "❌",
            "error:",
            "failed:",
            "exception:",
            "执行超时",
            "执行异常",
        ]

        for indicator in error_indicators:
            ind_lower = indicator.lower()
            if ind_lower in lower_output:
                # Check for recovery (success marker after error)
                last_error_idx = lower_output.rindex(ind_lower)
                
                has_recovery = False
                # Strong recovery markers
                recovery_markers = ["✅", "fixed", "resolved", "success"]
                for mark in recovery_markers:
                    if mark in lower_output:
                        if lower_output.rindex(mark) > last_error_idx:
                            has_recovery = True
                            break
                
                if has_recovery:
                    continue
                    
                return False

        # 4. Success indicators
        success_indicators = [
            "✅",
            "完成",
            "成功",
            "done",
            "success",
            "created",
            "updated",
            "installed",
            "completed",
            "finished",
            "saved",
            "generated",
            "verified",
        ]

        # If any success indicator is present OR output is reasonably long (heuristic)
        return any(indicator in lower_output for indicator in success_indicators) or len(output) > 50

    def _extract_error(self, output: str) -> str:
        error_patterns = [
            "Error:",
            "error:",
            "Failed:",
            "Exception:",
            "❌",
        ]

        for pattern in error_patterns:
            if pattern in output:
                start = output.index(pattern)
                end = min(start + 200, len(output))
                return output[start:end].strip()

        return output[:200] if output else "未知错误"

    def execute_with_retry(
        self,
        task: DeepTask,
        on_chunk: Optional[Callable[[str], None]] = None,
        max_retries: int = 2
    ) -> ExecutionResult:
        result = self.execute(task, on_chunk)

        retry_count = 0
        while not result.success and retry_count < max_retries:
            retry_count += 1
            logger.info("任务 %s 执行失败，第 %d 次重试...", task.title, retry_count)

            task.status = DeepTaskStatus.PENDING
            result = self.execute(task, on_chunk)

        return result
