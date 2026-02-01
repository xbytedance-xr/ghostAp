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
    def __init__(self, session: AISession, cwd: str):
        self.session = session
        self.cwd = cwd
        self.settings = get_settings()

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
        error_indicators = [
            "❌",
            "Error:",
            "error:",
            "Failed:",
            "failed:",
            "Exception:",
            "Traceback",
            "未找到",
            "执行超时",
            "执行异常",
        ]

        for indicator in error_indicators:
            if indicator in output:
                if "✅" in output and output.index("✅") > output.index(indicator):
                    continue
                return False

        success_indicators = [
            "✅",
            "完成",
            "成功",
            "Done",
            "Success",
            "Created",
            "Updated",
            "Installed",
        ]

        return any(indicator in output for indicator in success_indicators) or len(output) > 50

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
