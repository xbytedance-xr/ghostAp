"""AgentExecutor — executes a single agent() call via ACP/CLI session."""

from __future__ import annotations

import concurrent.futures
import json
import logging
import threading
import time
from typing import Any, Callable, Optional

from .constants import (
    AGENT_CALL_TIMEOUT_S,
    DEFAULT_MAX_CONCURRENT,
    HARD_MAX_CONCURRENT,
    MAX_RETRIES,
    RETRY_BACKOFF_BASE_S,
    SCHEMA_RETRY_MAX,
    SESSION_CREATE_TIMEOUT_S,
)
from .errors import is_transient_error
from .models import AgentCallParams, AgentCallResult
from .roles import get_subagent_encouragement_prompt

logger = logging.getLogger(__name__)


class AgentExecutor:
    """Executes individual agent() calls via ACP/CLI sessions.

    Each execute() call creates a short-lived, one-shot session: the session is
    opened, the prompt is sent, the result is collected, and the session is closed.
    Failures are isolated — exceptions never propagate out of execute().
    """

    def __init__(
        self,
        cwd: str,
        cancel_event: threading.Event,
        on_token_usage: Optional[Callable[[int], None]] = None,
        max_workers: int = DEFAULT_MAX_CONCURRENT,
        # Deprecated: kept for backwards compatibility
        budget_total: Optional[int] = None,
        on_budget_exceeded: Optional[Callable[[], None]] = None,
    ) -> None:
        self.cwd = cwd
        self.cancel_event = cancel_event
        self.on_token_usage = on_token_usage
        # Deprecated parameters - kept for backwards compatibility but ignored
        del budget_total, on_budget_exceeded
        # Shared thread pool for session creation — avoids per-call pool overhead.
        # Size is capped by HARD_MAX_CONCURRENT to prevent runaway concurrency even
        # when callers pass an explicit value.
        pool_size = max(1, min(int(max_workers), HARD_MAX_CONCURRENT))
        self._session_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=pool_size, thread_name_prefix="wf_session"
        )
        self._shutdown_done = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute(self, params: AgentCallParams) -> AgentCallResult:
        """Execute a single agent() call end-to-end with retry logic.

        1. Build the full prompt (role preamble + task + encouragement).
        2. Create a session via create_engine_session.
        3. Send prompt with timeout, collecting events.
        4. Validate output against schema if required (with retries).
        5. Retry transient errors with exponential backoff (MAX_RETRIES).
        6. Return AgentCallResult with output, token_usage, duration_s.
        7. On any exception, return AgentCallResult with error field set.

        Retry behavior:
        - Transient errors (network timeout, rate limit, etc.) are retried
          with exponential backoff: delay = RETRY_BACKOFF_BASE_S * 2^attempt
        - Permanent errors (invalid schema, permission denied) are not retried
        - Max retries: MAX_RETRIES (default 3)
        - Cancel event is checked before each retry attempt
        """
        start = time.monotonic()
        last_error: Optional[str] = None
        total_token_usage = 0

        for attempt in range(MAX_RETRIES + 1):
            session = None
            time.monotonic()

            try:
                # Early cancel check
                if self.cancel_event.is_set():
                    return AgentCallResult(
                        error="Cancelled before execution",
                        tool=params.tool,
                        model=params.model,
                        duration_s=time.monotonic() - start,
                    )

                if attempt > 0:
                    logger.info(
                        "[AgentExecutor] Retry attempt %d/%d for tool=%s (previous error: %s)",
                        attempt,
                        MAX_RETRIES,
                        params.tool,
                        last_error,
                    )

                full_prompt = self._build_prompt(params)

                # Create session (with timeout protection via shared pool)
                from src.agent_session.factory import create_engine_session

                future = self._session_pool.submit(
                    create_engine_session,
                    agent_type=params.tool,
                    cwd=self.cwd,
                    model_name=params.model,
                    cancel_event=self.cancel_event,
                )
                try:
                    session = future.result(timeout=SESSION_CREATE_TIMEOUT_S)
                except concurrent.futures.TimeoutError:
                    logger.error(
                        "[AgentExecutor] session creation timeout for tool=%s (>%ds) [RUNTIME_TIMEOUT]",
                        params.tool,
                        SESSION_CREATE_TIMEOUT_S,
                    )
                    # Attempt cleanup if session was created after we timed out
                    if future.done() and not future.exception():
                        try:
                            stale_session = future.result(timeout=0)
                            stale_session.close()
                        except Exception:
                            pass
                    error_msg = f"session creation timeout (>{SESSION_CREATE_TIMEOUT_S}s)"
                    last_error = error_msg
                    # Session creation timeout is transient — retry if attempts remain
                    if attempt < MAX_RETRIES and is_transient_error(error_msg):
                        self._sleep_with_backoff(attempt)
                        continue
                    return AgentCallResult(
                        error=error_msg,
                        tool=params.tool,
                        model=params.model,
                        duration_s=time.monotonic() - start,
                    )

                # Send prompt and collect result
                token_usage = 0
                result = session.send_prompt(
                    full_prompt,
                    on_event=None,
                    timeout=params.timeout or AGENT_CALL_TIMEOUT_S,
                )

                # Extract text output and token usage from PromptResult
                output_text = result.text if result else ""
                token_usage = result.output_tokens or 0 if result else 0

                # Report token usage via callback
                if token_usage > 0 and self.on_token_usage:
                    self.on_token_usage(token_usage)
                total_token_usage += token_usage

                # Cancel check after prompt completion
                if self.cancel_event.is_set():
                    return AgentCallResult(
                        output=output_text,
                        token_usage=total_token_usage,
                        duration_s=time.monotonic() - start,
                        error="Cancelled during execution",
                        tool=params.tool,
                        model=params.model,
                    )

                # Schema validation with retry (separate from general retry)
                parsed: Optional[dict[str, Any]] = None
                if params.output_schema:
                    valid, parsed = self._validate_schema(
                        output_text, params.output_schema
                    )

                    schema_retry_count = 0
                    while not valid and schema_retry_count < SCHEMA_RETRY_MAX:
                        if self.cancel_event.is_set():
                            break

                        schema_retry_count += 1
                        fix_prompt = self._build_schema_fix_prompt(
                            output_text, params.output_schema
                        )
                        logger.info(
                            "[AgentExecutor] Schema validation failed, retry %d/%d for tool=%s",
                            schema_retry_count,
                            SCHEMA_RETRY_MAX,
                            params.tool,
                        )

                        retry_result = session.send_prompt(
                            fix_prompt,
                            on_event=None,
                            timeout=params.timeout or AGENT_CALL_TIMEOUT_S,
                        )

                        retry_text = retry_result.text if retry_result else ""
                        retry_tokens = (
                            retry_result.output_tokens or 0 if retry_result else 0
                        )

                        # Accumulate token usage from retries
                        total_token_usage += retry_tokens
                        if retry_tokens > 0 and self.on_token_usage:
                            self.on_token_usage(retry_tokens)

                        output_text = retry_text
                        valid, parsed = self._validate_schema(
                            output_text, params.output_schema
                        )

                    if not valid:
                        logger.warning(
                            "[AgentExecutor] Schema validation exhausted retries for tool=%s",
                            params.tool,
                        )

                duration_s = time.monotonic() - start
                return AgentCallResult(
                    output=output_text,
                    parsed=parsed,
                    token_usage=total_token_usage,
                    duration_s=duration_s,
                    tool=params.tool,
                    model=params.model,
                )

            except Exception as e:
                error_msg = f"{type(e).__name__}: {e}"
                last_error = error_msg
                logger.error(
                    "[AgentExecutor] execute failed (attempt %d/%d) for tool=%s: %s",
                    attempt + 1,
                    MAX_RETRIES + 1,
                    params.tool,
                    error_msg,
                    exc_info=True,
                )

                # Check if we should retry
                if attempt < MAX_RETRIES and is_transient_error(error_msg):
                    # Check cancel before sleeping
                    if self.cancel_event.is_set():
                        break
                    self._sleep_with_backoff(attempt)
                    continue

                # Permanent error or max retries exceeded — return error
                duration_s = time.monotonic() - start
                return AgentCallResult(
                    error=error_msg,
                    tool=params.tool,
                    model=params.model,
                    duration_s=duration_s,
                )

            finally:
                # Always close the session (short-lived, one-shot)
                if session is not None:
                    try:
                        session.close()
                    except Exception as close_err:
                        logger.debug(
                            "[AgentExecutor] session close failed: %s", repr(close_err)
                        )

        # If we exited the loop due to cancel event
        duration_s = time.monotonic() - start
        return AgentCallResult(
            error="Cancelled during retry",
            tool=params.tool,
            model=params.model,
            duration_s=duration_s,
        )

    def _sleep_with_backoff(self, attempt: int) -> None:
        """Sleep for exponential backoff delay.

        Delay = RETRY_BACKOFF_BASE_S * 2^attempt

        Parameters
        ----------
        attempt:
            The zero-based retry attempt number (0 = first retry).
        """
        delay = RETRY_BACKOFF_BASE_S * (2**attempt)
        logger.debug(
            "[AgentExecutor] Backoff delay: %.2fs before retry attempt %d",
            delay,
            attempt + 1,
        )
        # Sleep in small increments to check cancel event frequently
        sleep_start = time.monotonic()
        while time.monotonic() - sleep_start < delay:
            if self.cancel_event.is_set():
                break
            time.sleep(min(0.1, delay - (time.monotonic() - sleep_start)))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def shutdown(self, wait: bool = True) -> None:
        """Shut down the shared session pool. Safe to call multiple times.

        Parameters
        ----------
        wait:
            If ``True`` (default), block until all pending workers finish.
            If ``False``, shut down asynchronously.
        """
        if self._shutdown_done:
            return
        self._shutdown_done = True
        if self._session_pool is not None:
            try:
                self._session_pool.shutdown(wait=wait, cancel_futures=True)
            except TypeError:
                # Older Python may not accept ``cancel_futures`` keyword.
                self._session_pool.shutdown(wait=wait)
            except Exception as e:
                logger.debug("[AgentExecutor] session pool shutdown failed: %s", repr(e))
            self._session_pool = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_prompt(self, params: AgentCallParams) -> str:
        """Compose the full prompt: role prefix + task + subagent encouragement.

        Structure:
            [Role: {role}\\n\\n]  (if params.role is set)
            {prompt}
            \\n\\n{encouragement}  (if enabled via settings)
        """
        parts: list[str] = []

        # Role preamble
        if params.role:
            parts.append(f"Role: {params.role}\n\n")

        # Core task prompt
        parts.append(params.prompt)

        # Subagent encouragement suffix (may be "" when disabled via settings)
        encouragement = get_subagent_encouragement_prompt()
        if encouragement:
            parts.append(f"\n\n{encouragement}")

        return "".join(parts)

    def _validate_schema(
        self, output: str, schema: dict[str, Any]
    ) -> tuple[bool, Optional[dict[str, Any]]]:
        """Try JSON parse + validate against schema keys.

        Validation strategy:
        - Parse the output as JSON.
        - Check that all top-level keys defined in the schema are present in
          the parsed result.

        Returns:
            (valid, parsed_dict_or_None)
        """
        try:
            parsed = json.loads(output)
        except (json.JSONDecodeError, TypeError):
            # Try to extract JSON from markdown code blocks
            parsed = self._extract_json_from_text(output)
            if parsed is None:
                return False, None

        if not isinstance(parsed, dict):
            return False, None

        # Validate that all required schema keys are present
        required_keys = set(schema.keys())
        present_keys = set(parsed.keys())

        if not required_keys.issubset(present_keys):
            missing = required_keys - present_keys
            logger.debug(
                "[AgentExecutor] Schema validation: missing keys %s", missing
            )
            return False, None

        return True, parsed

    def _extract_json_from_text(self, text: str) -> Optional[dict[str, Any]]:
        """Attempt to extract JSON from text that may contain markdown fences.

        Handles common cases where the agent wraps JSON in ```json ... ``` blocks.
        """
        # Try stripping markdown code fences
        stripped = text.strip()
        if stripped.startswith("```"):
            # Remove opening fence (with optional language tag)
            lines = stripped.split("\n", 1)
            if len(lines) > 1:
                body = lines[1]
                # Remove closing fence
                if body.rstrip().endswith("```"):
                    body = body.rstrip()[: -len("```")].rstrip()
                try:
                    return json.loads(body)
                except (json.JSONDecodeError, TypeError):
                    pass

        # Try finding the first { ... } block
        first_brace = text.find("{")
        last_brace = text.rfind("}")
        if first_brace != -1 and last_brace > first_brace:
            candidate = text[first_brace : last_brace + 1]
            try:
                return json.loads(candidate)
            except (json.JSONDecodeError, TypeError):
                pass

        return None

    def _build_schema_fix_prompt(
        self, failed_output: str, schema: dict[str, Any]
    ) -> str:
        """Build a prompt asking the agent to fix its output to match the schema."""
        schema_desc = json.dumps(schema, indent=2, ensure_ascii=False)
        return (
            "Your previous output did not conform to the required JSON schema.\n\n"
            f"Required schema (all keys must be present):\n```json\n{schema_desc}\n```\n\n"
            f"Your previous output was:\n```\n{failed_output[:2000]}\n```\n\n"
            "Please output ONLY valid JSON matching the schema above. "
            "Do not include any explanation, markdown fences, or extra text — "
            "just the raw JSON object."
        )

