"""Slock Engine handler — multi-Agent mouthpiece collaboration engine."""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import threading
import time
from typing import TYPE_CHECKING, Optional

from ...acp.helper import fetch_acp_models
from ...card import CardBuilder
from ...card.actions import dispatch as action_ids
from ...model_selection import is_default_model_option
from ...slock_engine.exceptions import ExecutorQueueFullError, TaskQueueFullError
from ...slock_engine.models import SlockChannel
from ...slock_engine.slash_commands import (
    SlockCommandAction,
    is_slock_command,
    parse_slock_command,
)
from ...utils.errors import safe_error_message
from ...utils.redact import redact_sensitive
from ..emoji import EmojiReaction
from ..user_cache import resolve_display_name
from .base import CardActionContext
from .engine_base import BaseEngineHandler

if TYPE_CHECKING:
    from ...project import ProjectContext
    from ..handler_context import HandlerContext

logger = logging.getLogger(__name__)


def _get_nli_loop():
    """Return the shared loop used for async NLI classification."""
    from src.slock_engine.engine import _get_shared_loop

    return _get_shared_loop()


class SlockHandler(BaseEngineHandler):
    """Manages the full lifecycle of Slock Engine (multi-Agent mouthpiece) tasks."""

    def __init__(self, ctx: "HandlerContext") -> None:
        super().__init__(ctx)
        # Rate-limit tracker: key = "chat_id:sender_id" → list of timestamps
        self._rate_limit_tracker: dict[str, list[float]] = {}
        # Singleton IntentRouter (AC-10: only instantiated once per handler lifecycle)
        from src.slock_engine.intent_router import IntentRouter
        self._intent_router = IntentRouter(
            confidence_threshold=getattr(ctx.settings, "slock_nli_confidence_threshold", 0.7),
            timeout=getattr(ctx.settings, "slock_nli_timeout", 5),
        )
        # Shared executor for NLI classification (avoids per-message ThreadPoolExecutor)
        from concurrent.futures import ThreadPoolExecutor
        self._nli_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="slock_nli")

    def cleanup(self) -> None:
        """Release resources held by this handler instance.

        Called by the dispatcher when the handler is being rebuilt or shut down.
        Shuts down the NLI executor to prevent thread leakage.
        """
        try:
            self._nli_executor.shutdown(wait=False)
            logger.debug("SlockHandler cleanup: _nli_executor shut down")
        except Exception as exc:
            logger.warning("SlockHandler cleanup error: %s", str(exc))

    # ------------------------------------------------------------------
    # Reply mode override: Slock uses slock_reply_mode (default "direct")
    # ------------------------------------------------------------------

    def reply_text(self, message_id: str, text: str, *, reply_in_thread: Optional[bool] = None) -> Optional[str]:
        """Override: default to slock_reply_mode instead of default_reply_mode."""
        if reply_in_thread is None:
            reply_in_thread = self.ctx.settings.slock_reply_mode == "thread"
        return super().reply_text(message_id, text, reply_in_thread=reply_in_thread)

    def reply_card(self, message_id: str, card_content: str, *, reply_in_thread: Optional[bool] = None) -> Optional[str]:
        """Override: default to slock_reply_mode instead of default_reply_mode."""
        if reply_in_thread is None:
            reply_in_thread = self.ctx.settings.slock_reply_mode == "thread"
        return super().reply_card(message_id, card_content, reply_in_thread=reply_in_thread)

    # ------------------------------------------------------------------
    # BaseEngineHandler abstract method implementations
    # ------------------------------------------------------------------

    def _get_engine_manager(self):
        return self.ctx.slock_engine_manager

    def _get_engine_name_prefix(self) -> str:
        return "Slock"

    def _get_task_type(self) -> str:
        return "slock_engine"

    def _show_status(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        self.show_slock_status(message_id, chat_id, project)

    def _create_callbacks(
        self, message_id: str, chat_id: str, project: Optional["ProjectContext"], engine_name: str, root_path: str
    ):
        from ...slock_engine.card_channel import SlockCardChannel
        from ...slock_engine.engine import SlockEngineCallbacks

        # Unified card channel — routes through CardDelivery for payload guard + retry
        _slock_thread = self.ctx.settings.slock_reply_mode == "thread"
        channel = SlockCardChannel(self.get_card_delivery(), chat_id, reply_in_thread=_slock_thread)

        def on_agent_wake(agent):
            logger.debug("Slock agent waking: %s in chat %s", agent.name, chat_id)

        def on_agent_running(agent, msg):
            logger.debug("Slock agent running: %s task=%s", agent.name, msg[:80])

        def on_agent_done(agent, result):
            logger.debug("Slock agent done: %s result_len=%d", agent.name, len(result))
            # Broadcast agent result as identity card when broadcast mode enabled
            if getattr(self.ctx.settings, 'slock_discussion_broadcast_rounds', True) and result.strip():
                from ...slock_engine.mouthpiece import Mouthpiece
                mouthpiece = Mouthpiece()
                try:
                    card = mouthpiece.format_card(agent, result, channel_id=chat_id)
                    channel.send_card(card)
                except Exception as bcast_exc:
                    logger.debug("on_agent_done broadcast failed: %s", bcast_exc)

        def on_error(err_msg):
            logger.error("Slock engine error in chat %s: %s", chat_id, err_msg)

        def on_escalation(esc):
            """Send escalation card to chat and write back message_id."""
            manager = self._get_engine_manager()
            engine = manager.get_active_engine(chat_id)
            if not engine and hasattr(manager, "get_activated_engine"):
                engine = manager.get_activated_engine(chat_id)
            if not engine:
                logger.warning("on_escalation: engine not found for chat %s", chat_id)
                return
            card = engine.get_escalation_card(esc)
            if not card:
                logger.warning("on_escalation: failed to build card for esc %s", esc.escalation_id)
                return
            sent_msg_id = channel.send_card(card, reply_to=message_id)
            if sent_msg_id:
                esc.card_message_id = sent_msg_id
            else:
                logger.warning("on_escalation: send_card_to_chat returned None for esc %s", esc.escalation_id)

        def on_card_send(card):
            """Send a card via unified channel (payload guard + retry)."""
            return channel.send_card(card, reply_to=message_id)

        def on_card_update(msg_id, card):
            """Update an existing card via unified channel."""
            channel.update_card(msg_id, card)

        return SlockEngineCallbacks(
            on_agent_wake=on_agent_wake,
            on_agent_running=on_agent_running,
            on_agent_done=on_agent_done,
            on_escalation=on_escalation,
            on_error=on_error,
            on_card_send=on_card_send,
            on_card_update=on_card_update,
        )

    # ------------------------------------------------------------------
    # Command router
    # ------------------------------------------------------------------

    def handle_slock_command(
        self, message_id: str, chat_id: str, text: str, project: Optional["ProjectContext"] = None
    ):
        """Route slock-related commands to the appropriate handler method."""
        cmd = parse_slock_command(text)

        dispatch: dict[SlockCommandAction, callable] = {
            SlockCommandAction.ACTIVATE: lambda: self.activate_slock(message_id, chat_id, cmd.args, project),
            SlockCommandAction.STATUS: lambda: self.show_slock_status(message_id, chat_id, project),
            SlockCommandAction.STOP: lambda: self.stop_slock_engine(message_id, chat_id, project),
            SlockCommandAction.HELP: lambda: self.show_slock_help(message_id),
            SlockCommandAction.NEW_TEAM: lambda: self.create_team(message_id, chat_id, cmd.args, project),
            SlockCommandAction.TEAM_LIST: lambda: self.list_teams(message_id, chat_id, project),
            SlockCommandAction.TEAM_STATUS: lambda: self.show_team_status(message_id, chat_id, cmd.target, project),
            SlockCommandAction.TEAM_DISSOLVE: lambda: self.dissolve_team(message_id, chat_id, cmd.target, project),
            SlockCommandAction.NEW_ROLE: lambda: self.create_role(message_id, chat_id, cmd.args, project),
            SlockCommandAction.ROLE_LIST: lambda: self.list_roles(message_id, chat_id, project),
            SlockCommandAction.ROLE_REMOVE: lambda: self.remove_role(message_id, chat_id, cmd.target, project),
            SlockCommandAction.ROLE_MOVE: lambda: self.move_role(message_id, chat_id, cmd.target, cmd.args, project),
            SlockCommandAction.ROLE_INFO: lambda: self.show_role_info(message_id, chat_id, cmd.target, project),
            SlockCommandAction.TASK_LIST: lambda: self.list_tasks(message_id, chat_id, project),
            SlockCommandAction.TASK_STATUS: lambda: self.show_task_status(message_id, chat_id, project),
            SlockCommandAction.TASK_ASSIGN: lambda: self.assign_task(message_id, chat_id, cmd.args, cmd.target, project),
            SlockCommandAction.DISCUSSION: lambda: self._trigger_nli_discussion(message_id, chat_id, cmd.args, {}, project),
            SlockCommandAction.STOP_DISCUSSION: lambda: self.stop_discussion(message_id, chat_id, project),
            SlockCommandAction.DISCUSSION_HISTORY: lambda: self.show_discussion_history(message_id, chat_id, cmd.target, project),
            SlockCommandAction.DISCUSSION_LIST: lambda: self.list_discussions(message_id, chat_id, project),
            SlockCommandAction.COUNCIL: lambda: self.run_council(message_id, chat_id, cmd.args, project),
            SlockCommandAction.MEMORY: lambda: self.show_agent_memory(message_id, chat_id, cmd.target, project),
            SlockCommandAction.MEMORY_LIST: lambda: self.show_memory_list(message_id, chat_id, project),
            SlockCommandAction.MEMORY_GROUP: lambda: self.show_memory_group(message_id, chat_id, project),
            SlockCommandAction.PLAN_LIST: lambda: self.list_plans(message_id, chat_id, project),
            SlockCommandAction.PLAN_DETAIL: lambda: self.show_plan_detail(message_id, chat_id, cmd.target, project),
        }

        handler = dispatch.get(cmd.action)
        if handler:
            handler()
        else:
            self.show_slock_help(message_id)

    # ------------------------------------------------------------------
    # Async execution helper (shared by handle_message & _submit_task_execution)
    # ------------------------------------------------------------------

    def _execute_async(
        self,
        *,
        engine,
        execute_fn,
        placeholder_card_json: str,
        result_card_fn,
        error_card_fn,
        empty_card_fn,
        busy_card_fn,
        message_id: str,
        chat_id: str,
    ) -> None:
        """Submit async execution with placeholder→update card pattern.

        This method encapsulates the common pattern:
          1. Send placeholder card immediately
          2. Submit execute_fn to BoundedExecutor
          3. On success: update card via result_card_fn(result, duration)
          4. On empty result: update via empty_card_fn()
          5. On exception: update via error_card_fn(exception)
          6. On queue full: update via busy_card_fn()
          7. On queue wait timeout: update with timeout card

        Callbacks:
          execute_fn() -> Optional[str] — the actual work
          result_card_fn(result: str, duration: float) -> str — JSON card for success
          error_card_fn(exc: Exception) -> str — JSON card for error
          empty_card_fn() -> str — JSON card for empty result
          busy_card_fn() -> str — JSON card for busy/full
        """
        card_message_id = self.send_card_to_chat(
            chat_id, placeholder_card_json, origin_message_id=message_id
        )

        executor = engine._get_executor()

        def _async_work():
            import time as _time

            start_time = _time.time()

            # Check queue wait timeout
            if self._check_queue_wait_timeout(future, start_time, card_message_id, chat_id):
                return

            try:
                result = execute_fn()
            except Exception as e:
                from src.utils.errors import redact_sensitive
                logger.error("Slock _execute_async error: %s", redact_sensitive(repr(e)), exc_info=True)
                error_card = error_card_fn(e)
                if card_message_id:
                    self.update_card(card_message_id, error_card)
                return

            if result is None:
                empty_card = empty_card_fn()
                if card_message_id:
                    self.update_card(card_message_id, empty_card)
                return

            duration = _time.time() - start_time

            if not result:
                # Empty string — agent ran successfully but produced no output
                result = "✅ 处理完成"

            success_card = result_card_fn(result, duration)
            if card_message_id:
                self.update_card(card_message_id, success_card)
            else:
                self.send_card_to_chat(chat_id, success_card, origin_message_id=message_id)

        future = None  # Assigned by executor.submit(); closure reads after thread start
        try:
            future = executor.submit(_async_work)
        except (ExecutorQueueFullError, RuntimeError) as e:
            logger.warning("Slock executor submit rejected for chat %s: %s", chat_id, repr(e))
            busy_card = busy_card_fn()
            if card_message_id:
                self.update_card(card_message_id, busy_card)

    # ------------------------------------------------------------------
    # Message routing (non-command messages in slock-active chats)
    # ------------------------------------------------------------------

    def handle_message(
        self, message_id: str, chat_id: str, text: str, project: Optional["ProjectContext"] = None
    ):
        """Route a non-command message in a slock-active chat.

        Routing priority (FS-01):
        1. /task keyword → redirect to handle_slock_command
        2. @AgentName → precise route to named agent
        3. NLI intent detection → if high confidence, execute as command
        4. Normal text → engine.execute() smart routing

        NLI fallback: UNKNOWN or low confidence falls through to smart routing (FS-02).
        Execution is submitted asynchronously to the engine's thread pool.
        """
        import re

        # Priority 1: /task keyword → redirect to command handler
        if text and text.strip().lower().startswith("/task"):
            self.handle_slock_command(message_id, chat_id, text, project)
            return

        # Priority 2: @AgentName precise routing (must be before NLI per AC-01)
        at_match = re.search(r"@([\w\-]+)", text or "")
        target_agent = None

        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)

        if at_match and engine:
            agent_name = at_match.group(1)
            target_agent = engine.registry.find_by_name(agent_name, channel_id=chat_id)
            if target_agent:
                # Direct route to mentioned agent — skip NLI entirely
                self._execute_routed_message(
                    engine, message_id, chat_id, text, project, target_agent
                )
                return

        # Priority 3: NLI Intent Detection (via dedicated event loop thread)
        # Gate: use TaskClassifier confidence to decide if NLI LLM fallback is needed
        from src.slock_engine.task_classifier import TaskClassifier

        is_chitchat, confidence = TaskClassifier.classify(text or "")

        intent_result = None
        coro = None
        future = None
        scheduled = False
        try:
            # First try synchronous fast path (no LLM, no async)
            fast_result = self._intent_router.fast_classify(text or "")
            if fast_result is not None:
                intent_result = fast_result
            elif not is_chitchat or confidence < 0.7:
                # Ambiguous or likely-task — invoke LLM NLI for better classification
                nli_loop = _get_nli_loop()
                coro = self._classify_with_timeout(text or "")
                future = asyncio.run_coroutine_threadsafe(coro, nli_loop)
                from concurrent.futures import Future
                scheduled = isinstance(future, Future)
                intent_result = future.result(timeout=self.ctx.settings.slock_nli_timeout + 0.2)
                if coro is not None and not scheduled:
                    coro.close()
                    coro = None
        except Exception as nli_exc:
            if coro is not None and not scheduled:
                coro.close()
            if future is not None and scheduled:
                future.cancel()
            logger.debug("NLI classification skipped (timeout/error): %s", nli_exc)
            # Fall through to smart routing on any NLI failure

        # Handle activate intent even when engine is not active (FS-03, AC-03)
        if intent_result and intent_result.action == SlockCommandAction.ACTIVATE:
            if not engine:
                self.activate_slock(message_id, chat_id, text or "", project)
                return
            # Engine already active — dispatch activate as status
            self._dispatch_nli_intent(message_id, chat_id, text, project, intent_result)
            return

        if intent_result and intent_result.action == SlockCommandAction.CHITCHAT:
            logger.debug("Skipping smart routing: high-confidence chitchat")
            return

        # If engine not activated and no activate intent → send hint (FS-03)
        if not engine:
            if intent_result and intent_result.action != SlockCommandAction.UNKNOWN:
                # User has an intent but engine isn't active
                self._send_no_engine_hint(message_id, chat_id)
            return

        # NLI dispatch: only for known actions above threshold (FS-02: UNKNOWN falls through)
        settings = self.ctx.settings
        if (
            intent_result
            and intent_result.action != SlockCommandAction.UNKNOWN
            and intent_result.confidence >= settings.slock_nli_confidence_threshold
        ):
            if intent_result.confidence >= 0.85:
                # High confidence: execute directly
                self._dispatch_nli_intent(message_id, chat_id, text, project, intent_result)
                return
            else:
                # Medium confidence: show confirmation card
                from ...slock_engine.card_templates import build_nli_feedback_card

                action_value = intent_result.action.value if hasattr(intent_result.action, "value") else str(intent_result.action)
                description = self._NLI_ACTION_DESCRIPTIONS.get(action_value, action_value)
                intent_params = {
                    "action": action_value,
                    "params": intent_result.params,
                    "original_text": text,
                }
                feedback_card = build_nli_feedback_card(
                    intent_description=description,
                    channel_id=chat_id,
                    intent_params=intent_params,
                )
                self.reply_card(message_id, json.dumps(feedback_card, ensure_ascii=False))
                return

        # Priority 3.5: If text looks like a failed command, show error suggestion
        if text and text.strip().startswith("/") and engine:
            from ...slock_engine.card_templates import build_error_suggestion_card

            suggestions = [
                "`/role` — 角色管理",
                "`/task` — 任务管理",
                "`/team` — 团队管理",
                "`/council` — 发起评审",
                "`/slock help` — 查看帮助",
            ]
            error_card = build_error_suggestion_card(text, suggestions, channel_id=chat_id)
            self.reply_card(message_id, json.dumps(error_card, ensure_ascii=False))
            return

        if self._try_start_autonomous_collaboration_task(
            message_id=message_id,
            chat_id=chat_id,
            text=text,
            project=project,
            engine=engine,
            target_agent=target_agent,
        ):
            return

        # Priority 4: Smart routing — engine.execute() (fallback for UNKNOWN/low confidence)
        self._execute_routed_message(engine, message_id, chat_id, text, project, target_agent=None)

    def _try_start_autonomous_collaboration_task(
        self,
        *,
        message_id: str,
        chat_id: str,
        text: str,
        project: Optional["ProjectContext"],
        engine,
        target_agent,
    ) -> bool:
        """Turn ordinary task messages into planned multi-role work when enabled."""
        if not self._autonomous_task_planning_enabled():
            return False
        if target_agent is not None:
            return False
        if not engine or not text or not text.strip():
            return False
        if self._looks_like_shell_text(text):
            return False

        from src.slock_engine.task_classifier import TaskClassifier

        classification, _confidence = TaskClassifier.classify_with_uncertainty(
            text,
            managed_chat=True,
        )
        if classification != "task":
            return False

        self.assign_task(message_id, chat_id, text.strip(), "", project)
        return True

    def _autonomous_task_planning_enabled(self) -> bool:
        """Return True only for an explicit boolean True setting."""
        return getattr(self.ctx.settings, "slock_autonomous_task_planning_enabled", False) is True

    @staticmethod
    def _looks_like_shell_text(text: str) -> bool:
        """Mirror shell-like fast path so commands are not stolen by task planning."""
        if not text:
            return False
        text_lower = text.lower().strip()
        if not text_lower:
            return False
        first_word = text_lower.split()[0]
        try:
            from src.agent.intent_recognizer import IntentRecognizer

            if first_word == "cd":
                return True
            if first_word in IntentRecognizer.SHELL_COMMANDS:
                return True
            if first_word in IntentRecognizer.COMMON_WORDS:
                return False
            return IntentRecognizer._looks_like_shell_token(first_word, text_lower)
        except Exception:
            logger.debug("Shell-like check failed for Slock autonomous task planning", exc_info=True)
            return False

    async def _classify_with_timeout(self, text: str):
        """Run NLI classification with timeout protection.

        Runs inside the shared slock event loop (src/slock_engine/engine.py).
        Uses safe_wait_for for timeout; TimeoutError propagates to the caller.
        """
        from src.utils.async_helpers import safe_wait_for

        return await safe_wait_for(
            self._intent_router.classify_intent(text),
            timeout=self.ctx.settings.slock_nli_timeout,
            action="NLI 意图分类",
        )

    def _execute_routed_message(
        self,
        engine,
        message_id: str,
        chat_id: str,
        text: str,
        project: Optional["ProjectContext"],
        target_agent,
    ):
        """Execute a message routed to a specific agent or via smart routing.

        Implements fallback chain:
        - If no IDLE agent: wait up to 60s for one to become available
        - If execution fails: retry with an alternative agent once
        """
        from ...slock_engine.task_router import RoutingStatus

        agent_used = None

        def _execute():
            nonlocal agent_used
            callbacks = self._create_callbacks(message_id, chat_id, project, engine.engine_name, engine.root_path)
            if target_agent:
                agent_used = target_agent
                return engine._execute_agent(target_agent, text, callbacks)
            channel_id = engine.channel.channel_id if engine.channel else chat_id
            agents = engine.registry.list_agents(channel_id=channel_id)

            # Use fallback-aware routing
            routing_result = engine.router.route_message_with_fallback(text, agents)

            if routing_result.status == RoutingStatus.ASSIGNED:
                agent_used = routing_result.agent
                if callbacks and callbacks.on_message_routed:
                    callbacks.on_message_routed(text, agent_used)
                return engine._execute_agent(agent_used, text, callbacks)

            if routing_result.status == RoutingStatus.QUEUE_WAIT:
                # Non-blocking enqueue: hand off to dispatch loop
                from ...slock_engine.task_queue import QueuedTask
                logger.info(
                    "All %d agents busy, enqueuing message %s for dispatch loop",
                    routing_result.busy_count, message_id,
                )
                callbacks = self._create_callbacks(
                    message_id, chat_id, project, engine.engine_name, engine.root_path
                )

                # Create final result delivery callback
                def _deliver_final_result(task_id: str, result: str, card_msg_id: Optional[str]) -> None:
                    """Deliver final result to user via reply or card update."""
                    if not result:
                        return
                    try:
                        if card_msg_id:
                            # Update existing card
                            from ...slock_engine.card_templates import build_result_card
                            result_card = build_result_card(
                                task_preview=text[:100],
                                result=result,
                            )
                            self.update_card(card_msg_id, json.dumps(result_card, ensure_ascii=False))
                        else:
                            # Reply to original message
                            self.reply_text(message_id, result)
                    except Exception as e:
                        logger.warning("Failed to deliver final result for task %s: %s", task_id, str(e))

                queued = QueuedTask(
                    task_id=f"msg:{message_id}",
                    text=text,
                    chat_id=chat_id,
                    message_id=message_id,
                    callbacks=callbacks,
                    engine=engine,
                    project=project,
                    handler=self,
                    origin_message_id=message_id,
                    final_result_callback=_deliver_final_result,
                )
                try:
                    position = engine.enqueue_task(queued)
                except TaskQueueFullError:
                    logger.warning("Task queue full, rejecting message %s", message_id)
                    return None

                # Send queue position feedback card and track card_message_id
                try:
                    from ...slock_engine.card_templates import build_queue_wait_card
                    queue_card = build_queue_wait_card(
                        position=position,
                        busy_count=routing_result.busy_count,
                        message_preview=text,
                    )
                    card_msg_id = self.send_card_to_chat(chat_id, json.dumps(queue_card, ensure_ascii=False))
                    queued.card_message_id = card_msg_id
                except Exception:
                    pass  # Non-critical: don't fail execution for card issues

                # Return immediately — dispatch loop will consume when agent is idle
                return "queued"

            # Compatibility: older/mocked routers may not implement the fallback
            # result object. Fall back to the direct route_message contract.
            if routing_result.status not in (
                RoutingStatus.ASSIGNED,
                RoutingStatus.QUEUE_WAIT,
                RoutingStatus.NO_MATCH,
            ):
                direct_agent = engine.router.route_message(text, agents)
                if direct_agent:
                    agent_used = direct_agent
                    if callbacks and callbacks.on_message_routed:
                        callbacks.on_message_routed(text, agent_used)
                    return engine._execute_agent(agent_used, text, callbacks)

            # NO_MATCH — no agents at all
            return None

        def _execute_with_retry():
            """Wrap _execute with one retry using an alternative agent on failure.

            Treats both exceptions AND None return (agent failed silently) as
            retryable failures — tries an alternative agent once before giving up.
            """
            nonlocal agent_used
            try:
                result = _execute()
            except Exception as first_err:
                logger.warning(
                    "Primary agent execution failed for message %s: %s, attempting retry",
                    message_id, first_err,
                )
                result = None
                _first_err = first_err
            else:
                if result is not None:
                    return result
                _first_err = None
                logger.warning(
                    "Primary agent returned None for message %s (agent=%s), attempting retry",
                    message_id, agent_used.name if agent_used else "?",
                )

            # Retry: try a different agent
            _failed_agent_name = agent_used.name if agent_used else "unknown"
            try:
                channel_id = engine.channel.channel_id if engine.channel else chat_id
                agents = engine.registry.list_agents(channel_id=channel_id)
                fallback_agents = [a for a in agents if a != agent_used] if agent_used else agents

                # Filter out agents whose agent_type is known to be ACP-incompatible
                # (avoids wasting ~100s retrying claude when it can't serve ACP)
                if fallback_agents:
                    try:
                        from ...acp.sync_adapter import resolve_agent_spec
                        _acp_compatible = []
                        for a in fallback_agents:
                            try:
                                resolve_agent_spec(a.agent_type, model_name=a.model_name or None)
                                _acp_compatible.append(a)
                            except (RuntimeError, Exception):
                                logger.debug(
                                    "Retry skip agent %s (%s): ACP incompatible",
                                    a.name, a.agent_type,
                                )
                        if _acp_compatible:
                            fallback_agents = _acp_compatible
                    except ImportError:
                        pass

                if fallback_agents:
                    alt_agent = engine.router.route_message(text, fallback_agents)
                    if alt_agent:
                        # Send retry swap feedback card
                        try:
                            from ...slock_engine.card_templates import build_retry_swap_card
                            swap_card = build_retry_swap_card(
                                failed_agent_name=_failed_agent_name,
                                new_agent_name=alt_agent.name,
                                error_hint=str(_first_err)[:120] if _first_err else "",
                            )
                            self.send_card_to_chat(chat_id, json.dumps(swap_card, ensure_ascii=False))
                        except Exception:
                            pass
                        agent_used = alt_agent
                        callbacks = self._create_callbacks(
                            message_id, chat_id, project, engine.engine_name, engine.root_path
                        )
                        return engine._execute_agent(alt_agent, text, callbacks)
            except Exception as retry_err:
                logger.error(
                    "Retry also failed for message %s: %s",
                    message_id, retry_err,
                )
            # Re-raise original error if retry didn't help
            if _first_err:
                raise _first_err
            return None

        def _result_card(result: str, duration: float) -> str:
            if agent_used:
                try:
                    engine.memory.write_agent_reasoning_snapshot(
                        agent_used.agent_id,
                        f"message:{message_id}",
                        prompt_summary=text[:1000],
                        result_summary=result[:2000],
                        tool_name=agent_used.agent_type,
                        model_name=agent_used.model_name,
                    )
                except Exception:
                    logger.warning(
                        "Failed to persist Slock reasoning snapshot for message %s agent %s",
                        message_id,
                        agent_used.agent_id,
                        exc_info=True,
                    )
                card_data = engine._mouthpiece.format_card(
                    agent_used,
                    result,
                    model_info=agent_used.agent_type,
                    duration_s=duration,
                    channel_id=chat_id,
                    task_id=f"message:{message_id}",
                )
                # Append confirmation buttons for result feedback
                try:
                    from ...slock_engine.card_templates.queue_feedback import build_result_confirmation_buttons
                    confirm_elements = build_result_confirmation_buttons(
                        channel_id=chat_id,
                        message_id=message_id,
                        task_id=f"message:{message_id}",
                    )
                    if isinstance(card_data, dict):
                        body = card_data.get("body", card_data.get("card", {}).get("body", {}))
                        elements = body.get("elements", [])
                        elements.extend(confirm_elements)
                except Exception:
                    pass  # Non-critical: don't break result delivery for button rendering
                return json.dumps(card_data, ensure_ascii=False)
            from ...slock_engine.card_templates.common import build_card_wrapper  # noqa: F811
            return json.dumps(build_card_wrapper(
                header_title="💬 Agent 回复",
                header_template="blue",
                elements=[{"tag": "markdown", "content": result}],
                mobile_optimize=True,
            ), ensure_ascii=False)

        def _error_card(exc: Exception) -> str:
            from ...slock_engine.card_templates.common import build_card_wrapper
            return json.dumps(build_card_wrapper(
                header_title="❌ 执行出错",
                header_template="red",
                elements=[{"tag": "markdown", "content": f"Agent 执行出错: {safe_error_message(exc)}"}],
                mobile_optimize=True,
            ), ensure_ascii=False)

        def _empty_card() -> str:
            from ...slock_engine.card_templates.common import build_card_wrapper
            return json.dumps(build_card_wrapper(
                header_title="⚠️ 执行无结果",
                header_template="orange",
                elements=[{"tag": "markdown", "content": (
                    "Agent 执行完成但未产出结果。可能原因：\n"
                    "- 所有角色正在忙碌\n"
                    "- 执行超时被自动中止\n"
                    "- 任务内容不明确\n\n"
                    "请稍后重试或补充更多描述。"
                )}],
                mobile_optimize=True,
            ), ensure_ascii=False)

        def _busy_card() -> str:
            from ...slock_engine.card_templates.common import build_card_wrapper
            return json.dumps(build_card_wrapper(
                header_title="⚠️ 团队繁忙",
                header_template="orange",
                elements=[{"tag": "markdown", "content": "当前所有角色均在忙碌中，请稍后重试。"}],
                mobile_optimize=True,
            ), ensure_ascii=False)

        from ...slock_engine.card_templates.queue_feedback import build_task_ack_card
        ack_card = build_task_ack_card(
            message_preview=text or "",
            status="received",
        )
        placeholder_card = json.dumps(ack_card, ensure_ascii=False)

        self._execute_async(
            engine=engine,
            execute_fn=_execute_with_retry,
            placeholder_card_json=placeholder_card,
            result_card_fn=_result_card,
            error_card_fn=_error_card,
            empty_card_fn=_empty_card,
            busy_card_fn=_busy_card,
            message_id=message_id,
            chat_id=chat_id,
        )

    # ------------------------------------------------------------------
    # NLI and routing helpers
    # ------------------------------------------------------------------

    _NLI_ACTION_DESCRIPTIONS: dict[str, str] = {
        "status": "查看团队状态",
        "stop": "停止 Slock 引擎",
        "help": "查看帮助信息",
        "new_role": "创建新角色",
        "role_list": "查看角色列表",
        "task_list": "查看任务列表",
        "task_assign": "分配任务",
        "task_status": "查看任务状态",
        "activate": "启动 Slock",
        "discussion": "发起讨论",
    }

    def _send_no_engine_hint(self, message_id: str, chat_id: str) -> None:
        """Send a friendly hint card when slock engine is not activated."""
        hint_card = json.dumps({
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "💡 Slock 未激活"},
                "template": "wathet",
            },
            "body": {"elements": [
                {"tag": "markdown", "content": (
                    "当前群聊尚未启用 Slock 协作模式。\n\n"
                    "**直接在群里发任务即可，Agent 自动处理。** 无需任何前置命令。\n\n"
                    "---\n"
                    "📎 *也可用命令*: `/slock`、`/new-team 团队名`、`/slock help`"
                )},
            ]},
        }, ensure_ascii=False)
        self.reply_card(message_id, hint_card)

    def _dispatch_nli_intent(
        self,
        message_id: str,
        chat_id: str,
        text: str,
        project: Optional["ProjectContext"],
        intent_result,
    ) -> None:
        """Dispatch a high-confidence NLI intent by calling handler methods directly.

        Avoids command string assembly — passes structured params to internal
        handler methods, eliminating quoting/escaping issues.
        """
        from src.slock_engine.slash_commands import SlockCommandAction

        action = intent_result.action

        # Param whitelist filtering: only allow known keys per action to prevent injection
        _ALLOWED_PARAM_KEYS: dict[SlockCommandAction, set[str]] = {
            SlockCommandAction.NEW_ROLE: {"name", "tool", "role"},
            SlockCommandAction.DISCUSSION: {"participants", "topic"},
            SlockCommandAction.COUNCIL: {"topic"},
            SlockCommandAction.NEW_TEAM: {"name"},
            SlockCommandAction.ROLE_REMOVE: {"target"},
            SlockCommandAction.ROLE_INFO: {"target"},
            SlockCommandAction.ACTIVATE: {"requirement"},
        }
        allowed_keys = _ALLOWED_PARAM_KEYS.get(action, set())
        params = {k: v for k, v in intent_result.params.items() if k in allowed_keys} if allowed_keys else {}

        # --- Simple no-param actions: direct method calls ---
        if action == SlockCommandAction.STOP:
            self.stop_slock_engine(message_id, chat_id, project)
        elif action == SlockCommandAction.STATUS:
            self.show_slock_status(message_id, chat_id, project)
        elif action == SlockCommandAction.HELP:
            self.show_slock_help(message_id)
        elif action == SlockCommandAction.TASK_LIST:
            self.list_tasks(message_id, chat_id, project)
        elif action == SlockCommandAction.TASK_STATUS:
            self.show_task_status(message_id, chat_id, project)
        elif action == SlockCommandAction.ROLE_LIST:
            self.list_roles(message_id, chat_id, project)
        elif action == SlockCommandAction.TEAM_LIST:
            self.list_teams(message_id, chat_id, project)
        elif action == SlockCommandAction.ACTIVATE:
            self.activate_slock(message_id, chat_id, params.get("requirement", ""), project)

        # --- Parameterized actions: structured params ---
        elif action == SlockCommandAction.NEW_ROLE:
            name = params.get("name", "Agent")
            # Build args string for create_role (it handles shlex parsing internally)
            args_parts = [name]
            if params.get("tool"):
                args_parts.append(f"--tool {params['tool']}")
            if params.get("role"):
                args_parts.append(f"--role {params['role']}")
            self.create_role(message_id, chat_id, " ".join(args_parts), project)
        elif action == SlockCommandAction.NEW_TEAM:
            team_name = params.get("name", "Team")
            self.create_team(message_id, chat_id, team_name, project)
        elif action == SlockCommandAction.ROLE_REMOVE:
            target = params.get("target", "")
            self.remove_role(message_id, chat_id, target, project)
        elif action == SlockCommandAction.ROLE_INFO:
            target = params.get("target", "")
            self.show_role_info(message_id, chat_id, target, project)

        # --- Discussion action: trigger inter-agent discussion ---
        elif action == SlockCommandAction.DISCUSSION:
            self._trigger_nli_discussion(message_id, chat_id, text, params, project)
        elif action == SlockCommandAction.COUNCIL:
            self.run_council(message_id, chat_id, params.get("topic") or text, project)

        else:
            # Unhandled intent — fallback to reply
            self.reply_text(message_id, f"🤔 理解为：{action.value}，但暂不支持自然语言执行此操作。请使用对应的 / 命令。")

    def _trigger_nli_discussion(
        self,
        message_id: str,
        chat_id: str,
        text: str,
        params: dict,
        project: Optional["ProjectContext"],
    ) -> None:
        """Handle DISCUSSION intent: resolve participant names and trigger engine discussion.

        Extracts participant agent names from NLI params (or action_hint fallback),
        resolves them to agent IDs via the engine registry, and triggers the
        discussion flow through the engine's executor (bounded concurrency + watchdog).

        Permission: admin/owner can always trigger; other members are rate-limited
        to 3 discussion triggers per 5 minutes.
        """
        import json as _json

        from src.slock_engine.models import AgentStatus, DiscussionStatus, DiscussionThread

        # Permission gate: check discussion trigger rate-limit for non-admin users
        if not self._check_discussion_permission(message_id, chat_id):
            return

        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            self._send_no_engine_hint(message_id, chat_id)
            return

        # Extract participant names from params
        participants_raw: list[str] = []
        if "participants" in params:
            participants_raw = params["participants"]
        elif "participant_a" in params and "participant_b" in params:
            participants_raw = [params["participant_a"], params["participant_b"]]

        if len(participants_raw) < 2:
            self.reply_text(
                message_id,
                "💬 发起讨论需要至少两个参与者。\n\n示例：「让 coder 和 reviewer 讨论一下代码方案」",
            )
            return

        # Resolve names to agent IDs
        participant_ids: list[str] = []
        unresolved: list[str] = []
        for name in participants_raw:
            agent = engine.find_agent_by_name(name) if hasattr(engine, "find_agent_by_name") else None
            if agent is None:
                # Fallback: try get_agent directly (if name is already an ID)
                agent = engine.get_agent(name)
            if agent:
                participant_ids.append(agent.agent_id)
            else:
                unresolved.append(name)

        if unresolved:
            self.reply_text(
                message_id,
                f"❌ 未找到以下角色：{'、'.join(unresolved)}\n\n请确认角色名称后重试，或使用 `/role list` 查看可用角色。",
            )
            return

        if len(participant_ids) < 2:
            self.reply_text(message_id, "💬 需要至少两个有效参与者才能发起讨论。")
            return

        # Build discussion thread with config from settings
        topic = params.get("topic", text)
        config = engine.build_discussion_config_from_settings()
        thread = DiscussionThread(
            channel_id=chat_id,
            participants=participant_ids,
            trigger_reason=f"NLI discussion: {topic[:100]}",
            config=config,
        )

        # Check capacity and add
        if not engine._add_discussion(chat_id, thread):
            self.reply_text(
                message_id,
                "⏳ 当前频道讨论数已达上限，请等待现有讨论结束后重试。",
            )
            return

        # Send starting card
        from src.slock_engine.card_templates import (
            build_discussion_card_from_thread,
            build_discussion_summary_card_from_thread,
        )

        try:
            card = build_discussion_card_from_thread(thread)
            card_json = _json.dumps(card, ensure_ascii=False)
            discussion_card_msg_id = self.send_card_to_chat(
                chat_id, card_json, origin_message_id=message_id
            )
        except Exception:
            discussion_card_msg_id = None

        # Run discussion via engine executor (bounded concurrency) with watchdog
        settings = self.ctx.settings
        watchdog_timeout = settings.slock_discussion_timeout

        def _run():
            from src.slock_engine.discussion_manager import DiscussionManager

            # Watchdog timer: force-terminate if discussion exceeds timeout
            watchdog = threading.Timer(watchdog_timeout, lambda: _watchdog_trigger())
            watchdog_fired = threading.Event()

            def _watchdog_trigger():
                watchdog_fired.set()
                thread.status = DiscussionStatus.TIMEOUT
                logger.warning("Discussion watchdog fired: %s (timeout=%ds)", thread.thread_id, watchdog_timeout)

            watchdog.daemon = True
            watchdog.start()

            try:
                dm = DiscussionManager(engine=engine, memory_manager=engine._memory, config=config)

                def on_round_complete(updated_thread):
                    if watchdog_fired.is_set():
                        return
                    if discussion_card_msg_id:
                        try:
                            updated_card = build_discussion_card_from_thread(updated_thread)
                            self.update_card(discussion_card_msg_id, _json.dumps(updated_card, ensure_ascii=False))
                        except Exception:
                            pass

                completed = dm.run_discussion(thread, topic, on_round_complete=on_round_complete)

                # Send summary card
                try:
                    summary_card = build_discussion_summary_card_from_thread(completed)
                    self.send_card_to_chat(chat_id, _json.dumps(summary_card, ensure_ascii=False))
                except Exception:
                    pass

            except Exception as exc:
                from src.utils.errors import redact_sensitive, safe_error_message
                logger.error("NLI discussion failed: %s", redact_sensitive(str(exc)), exc_info=True)
                self.reply_text(message_id, f"❌ 讨论执行失败：{safe_error_message(exc)}")
            finally:
                watchdog.cancel()
                engine._remove_discussion(chat_id, thread.thread_id)
                # Reset participant agent states to IDLE
                for pid in participant_ids:
                    try:
                        engine.set_agent_status(pid, AgentStatus.IDLE)
                    except Exception:
                        pass

        # Submit to engine executor for bounded concurrency
        try:
            executor = engine._get_executor()
            executor.submit(_run)
        except Exception as exc:
            from src.utils.errors import redact_sensitive
            logger.warning("Failed to submit discussion to executor: %s", redact_sensitive(str(exc)))
            engine._remove_discussion(chat_id, thread.thread_id)
            self.reply_text(message_id, "⏳ 执行队列已满，请稍后再试。")

    def run_council(
        self,
        message_id: str,
        chat_id: str,
        question: str = "",
        project: Optional["ProjectContext"] = None,
    ) -> None:
        """Run a Slock Council: independent opinions, anonymous review, final synthesis."""
        if not question.strip():
            self.reply_text(message_id, "请提供 Council 议题\n\n用法: `/council <要评审的问题或方案>`")
            return

        if not self._check_discussion_permission(message_id, chat_id):
            return

        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            self._send_no_engine_hint(message_id, chat_id)
            return

        from src.slock_engine.card_templates import build_council_card
        from src.slock_engine.models import AgentStatus, CouncilRun, CouncilStatus

        channel_id = engine.channel.channel_id if engine.channel else chat_id
        agents = list(engine.registry.list_agents(channel_id=channel_id))
        idle_agents = [
            agent for agent in agents
            if engine.get_agent_status(agent.agent_id) == AgentStatus.IDLE
        ]
        if len(idle_agents) < 2:
            self.reply_text(message_id, "Council 至少需要两个空闲角色。可用 `/role list` 查看角色状态。")
            return

        rank_agents = getattr(engine.router, "rank_agents_for_claim", None)
        ranked_agents = rank_agents(question, idle_agents) if callable(rank_agents) else idle_agents
        if not isinstance(ranked_agents, list) or len(ranked_agents) < 2:
            ranked_agents = idle_agents

        max_agents = max(2, int(getattr(self.ctx.settings, "slock_max_parallel_agents", 4)))
        participants = ranked_agents[:max_agents]
        chairman = self._select_council_chairman(idle_agents, participants)

        initial_run = CouncilRun(
            channel_id=channel_id,
            question=question,
            participant_ids=[agent.agent_id for agent in participants],
            chairman_agent_id=chairman.agent_id if chairman else "",
            status=CouncilStatus.STARTING,
        )
        card_message_id = self.send_card_to_chat(
            chat_id,
            json.dumps(build_council_card(initial_run, channel_id=channel_id), ensure_ascii=False),
            origin_message_id=message_id,
        )

        def _update_stage(run) -> None:
            if not card_message_id:
                return
            try:
                self.update_card(
                    card_message_id,
                    json.dumps(build_council_card(run, channel_id=channel_id), ensure_ascii=False),
                )
            except Exception as exc:
                logger.debug("Council card update failed: %s", str(exc))

        def _run() -> None:
            try:
                run = engine.run_council(
                    question,
                    participants=participants,
                    chairman=chairman,
                    on_stage=_update_stage,
                    timeout=float(getattr(self.ctx.settings, "slock_discussion_timeout", 300)),
                )
                final_card = json.dumps(build_council_card(run, channel_id=channel_id), ensure_ascii=False)
                if card_message_id:
                    self.update_card(card_message_id, final_card)
                else:
                    self.send_card_to_chat(chat_id, final_card, origin_message_id=message_id)
            except Exception as exc:
                logger.error("Slock Council failed: %s", exc, exc_info=True)
                failed = CouncilRun(
                    channel_id=channel_id,
                    question=question,
                    participant_ids=[agent.agent_id for agent in participants],
                    chairman_agent_id=chairman.agent_id if chairman else "",
                    status=CouncilStatus.FAILED,
                    error=safe_error_message(exc),
                )
                failed_card = json.dumps(build_council_card(failed, channel_id=channel_id), ensure_ascii=False)
                if card_message_id:
                    self.update_card(card_message_id, failed_card)
                else:
                    self.send_card_to_chat(chat_id, failed_card, origin_message_id=message_id)

        try:
            engine._get_executor().submit(_run)
        except (ExecutorQueueFullError, RuntimeError) as exc:
            logger.warning("Failed to submit Slock Council: %s", repr(exc))
            busy = CouncilRun(
                channel_id=channel_id,
                question=question,
                participant_ids=[agent.agent_id for agent in participants],
                chairman_agent_id=chairman.agent_id if chairman else "",
                status=CouncilStatus.FAILED,
                error="执行队列已满，请稍后重试。",
            )
            if card_message_id:
                self.update_card(
                    card_message_id,
                    json.dumps(build_council_card(busy, channel_id=channel_id), ensure_ascii=False),
                )
            else:
                self.reply_text(message_id, "⏳ 执行队列已满，请稍后重试。")

    @staticmethod
    def _select_council_chairman(agents: list, participants: list):
        """Prefer a chair/architect/planner/reviewer, otherwise reuse the first participant."""
        preferred_roles = ("chair", "architect", "planner", "reviewer")
        for role in preferred_roles:
            for agent in agents:
                if getattr(agent, "role", "") == role:
                    return agent
        return participants[0] if participants else None

    # ------------------------------------------------------------------
    # Slock activation
    # ------------------------------------------------------------------

    def activate_slock(
        self,
        message_id: str,
        chat_id: str,
        requirement: str = "",
        project: Optional["ProjectContext"] = None,
        *,
        skip_guard_check: bool = False,
    ) -> bool:
        """Activate slock mode for the current chat.

        Args:
            skip_guard_check: If True, skip the ActivationGuard permission/rate-limit
                check. Used when the guard has already been checked by the caller
                (e.g., ws_client._auto_activate_slock) to avoid double consumption
                of rate-limit budget.

        Returns:
            True if activation succeeded (or was already active), False otherwise.
        """
        project = self._ensure_project(message_id, chat_id, project)
        if not project:
            return False

        # ActivationGuard permission check — deny early if sender lacks permission
        if not skip_guard_check:
            from ...slock_engine.activation_guard import get_activation_guard
            from ...thread.manager import get_current_sender_id

            guard = get_activation_guard()
            sender_id = get_current_sender_id() or ""
            allowed, reason = guard.can_auto_activate(sender_id, chat_id, self.ctx.settings)
            if not allowed:
                from ...slock_engine.card_templates.queue_feedback import build_activation_denied_card

                card = build_activation_denied_card(
                    reason=reason,
                    hint="当前用户无权激活 Slock 团队，请联系管理员配置白名单或使用 allow_all 策略。",
                )
                self.send_card_to_chat(chat_id, json.dumps(card, ensure_ascii=False))
                return False

        root_path = project.root_path if project else self.get_working_dir(chat_id)
        manager = self._get_engine_manager()

        # Check if already active — show interactive command panel
        existing = manager.get_activated_engine(chat_id)
        if existing:
            from ...slock_engine.card_templates import build_command_panel_card

            panel_card = build_command_panel_card(channel_id=chat_id)
            self.reply_card(message_id, json.dumps(panel_card, ensure_ascii=False))
            return True

        from ...thread.manager import get_current_sender_id

        sender_open_id = get_current_sender_id() or ""

        engine_name = self.get_engine_name(chat_id, project_id=(project.project_id if project else None))
        engine = manager.get_or_create(
            chat_id, root_path, engine_name=engine_name,
        )

        # Create and activate channel
        channel = SlockChannel(
            channel_id=chat_id,
            name=project.project_name if project else "slock",
            team_name=project.project_name if project else "Team",
            owner_id=sender_open_id,
        )
        engine.activate_channel(channel)

        # Bootstrap default roles asynchronously (non-blocking)
        self._bootstrap_default_roles_if_configured(engine, channel.channel_id, chat_id)

        manager.register_managed_chat(chat_id)

        # Wire UI callbacks for escalation timeout notifications
        engine.set_escalation_ui_callbacks(
            update_card_fn=self.update_card,
            send_text_fn=self.send_text_to_chat,
        )

        # Wire card delivery callbacks for progress tracking
        def _send_card(card: dict) -> "Optional[str]":
            card_json = json.dumps(card, ensure_ascii=False)
            return self.send_card_to_chat(chat_id, card_json)

        def _update_card(msg_id: str, card: dict) -> bool:
            card_json = json.dumps(card, ensure_ascii=False)
            return self.update_card(msg_id, card_json)

        engine.set_card_callbacks(send_fn=_send_card, update_fn=_update_card)

        self.add_reaction(message_id, EmojiReaction.on_multi_task_start())

        # Use unified welcome card with team/channel info prepended
        from ...slock_engine.card_templates import build_welcome_card

        welcome_card = build_welcome_card(team_name=channel.team_name)
        # Prepend team/channel metadata to the welcome card body
        team_info_element = {
            "tag": "markdown",
            "content": (
                f"**团队**: {channel.team_name}\n"
                f"**频道**: {channel.name}"
            ),
        }
        welcome_card["body"]["elements"].insert(0, team_info_element)
        welcome_card["header"]["title"]["content"] = "🎭 Slock 协作模式已激活"

        # Task 13: detect synthetic message_id → use send_card_to_chat instead of reply
        if message_id.startswith("passive-activate-"):
            card_json = json.dumps(welcome_card, ensure_ascii=False)
            self.send_card_to_chat(chat_id, card_json)
        else:
            session = self.create_static_card_session(chat_id, reply_to=message_id)
            session.send(welcome_card)
            session.close()

        # Task 12: Enqueue first message atomically if requirement is provided
        if requirement:
            import uuid as _uuid

            from ...slock_engine.task_queue import QueuedTask

            # Create final result delivery callback for first message
            def _deliver_first_result(task_id: str, result: str, card_msg_id: Optional[str]) -> None:
                """Deliver final result for the first message."""
                if not result:
                    return
                try:
                    if card_msg_id:
                        from ...slock_engine.card_templates import build_result_card
                        result_card = build_result_card(
                            task_preview=requirement[:100],
                            result=result,
                        )
                        self.update_card(card_msg_id, json.dumps(result_card, ensure_ascii=False))
                    else:
                        if message_id.startswith("passive-activate-"):
                            self.send_text_to_chat(chat_id, result)
                        else:
                            self.reply_text(message_id, result)
                except Exception as e:
                    logger.warning("Failed to deliver first message result: %s", str(e))

            task = QueuedTask(
                task_id=f"first-msg-{_uuid.uuid4().hex[:8]}",
                text=requirement,
                chat_id=chat_id,
                message_id=message_id,
                bootstrap_pending=True,
                origin_message_id=message_id,
                final_result_callback=_deliver_first_result,
            )
            try:
                engine._task_queue.enqueue(task)
                logger.info("First message enqueued for chat=%s: %s", chat_id, requirement[:60])
            except Exception as enqueue_err:
                from ...slock_engine.card_templates.queue_feedback import build_queue_full_card
                from ...slock_engine.exceptions import TaskQueueFullError

                is_queue_full = isinstance(enqueue_err, TaskQueueFullError)
                logger.warning(
                    "Failed to enqueue first message for chat=%s (queue_full=%s): %s",
                    chat_id, is_queue_full, enqueue_err,
                )
                if is_queue_full:
                    try:
                        card = build_queue_full_card(
                            message_preview=requirement,
                            max_size=getattr(engine._task_queue, "_max_size", 8),
                        )
                        if message_id.startswith("passive-activate-"):
                            self.send_card_to_chat(chat_id, json.dumps(card, ensure_ascii=False))
                        else:
                            self.reply_card(message_id, json.dumps(card, ensure_ascii=False))
                    except Exception as card_err:
                        logger.warning("Failed to send queue-full card: %s", card_err)

        return True

    def _bootstrap_default_roles_if_configured(
        self,
        engine: "object",
        channel_id: str,
        chat_id: str,
    ) -> None:
        """Bootstrap default roles with exception safety and async execution.

        - Never blocks the caller (uses asyncio.create_task if a loop is running,
          otherwise falls back to threading).
        - Never raises — bootstrap failure is logged and a degradation notice is
          sent to the chat so the user knows some roles may be missing.
        """
        _default_roles_cfg = getattr(self.ctx.settings, "slock_default_roles", "")
        if not _default_roles_cfg:
            # No roles to bootstrap — leave bootstrap-ready state as-is (default: ready)
            return

        # Signal dispatch loop to wait for bootstrap via public API
        engine.prepare_bootstrap()

        def _do_bootstrap() -> None:
            try:
                from ...slock_engine.role_bootstrap import bootstrap_default_roles
                created = bootstrap_default_roles(engine, channel_id, _default_roles_cfg)
                if created:
                    logger.info(
                        "_bootstrap_default_roles_if_configured: created %d role(s) in %s",
                        len(created), channel_id,
                    )
            except Exception as e:
                logger.error(
                    "_bootstrap_default_roles_if_configured: bootstrap failed for channel %s: %s",
                    channel_id, e, exc_info=True,
                )
                # Send degradation notice to the chat
                try:
                    self.send_text_to_chat(
                        chat_id,
                        "⚠️ 部分预置角色创建失败，可使用 /role list 检查",
                    )
                except Exception:
                    logger.debug("Failed to send bootstrap degradation notice")
            finally:
                # Always signal bootstrap complete via public API so dispatch loop can proceed
                engine.finish_bootstrap()

        # Execute asynchronously to avoid blocking the response path
        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, _do_bootstrap)
        except RuntimeError:
            # No running event loop — fall back to thread
            threading.Thread(
                target=_do_bootstrap,
                name="slock-bootstrap",
                daemon=True,
            ).start()

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def show_slock_status(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        """Show slock engine status with refresh button."""
        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        engine_name = self.get_engine_name(chat_id, project_id=(project.project_id if project else None))

        if not engine:
            _msg_type, card_content = CardBuilder.build_info_card(
                project=project,
                title="🎭 Slock 状态",
                content="当前没有活跃的 Slock 协作团队\n\n发送 `/slock` 激活协作模式",
                engine_name=engine_name,
                show_buttons=False,
            )
            self.reply_card(message_id, card_content)
            return

        # Build status panel card with clear sections:
        # 1) Team overview (name + member count)
        # 2) Role status table (emoji | name | status | current task)
        # 3) Action buttons (refresh, stop all, per-agent stop)
        from ...slock_engine.card_templates import build_status_panel_card
        from ...slock_engine.models import AgentStatus as AgentStatusEnum
        from ...slock_engine.models import TaskStatus

        channel_id = engine.channel.channel_id if engine.channel else chat_id
        team_name = engine.channel.team_name if engine.channel else ""

        agents: list[tuple] = []
        current_tasks: dict = {}
        skill_profiles: dict = {}
        for agent in engine.registry.list_agents(channel_id=channel_id):
            status = engine.get_agent_status(agent.agent_id) or AgentStatusEnum.IDLE
            agents.append((agent, status))
            # Find active task claimed by this agent
            for task in engine.tasks:
                if task.claimed_by == agent.agent_id and task.status == TaskStatus.IN_PROGRESS:
                    current_tasks[agent.agent_id] = task
                    break
            # Collect skill profiles
            profiles = engine.memory.read_skill_profiles(agent.agent_id)
            if profiles:
                skill_profiles[agent.agent_id] = [
                    {"tag": p.tag, "success_rate": p.success_rate, "total_tasks": p.total_tasks}
                    for p in profiles
                ]

        status_card = build_status_panel_card(
            agents=agents,
            team_name=team_name,
            channel_id=channel_id,
            current_tasks=current_tasks,
            skill_profiles=skill_profiles,
            tasks_summary={
                "total": len(engine.tasks),
                "todo": sum(1 for t in engine.tasks if t.status == TaskStatus.TODO),
                "in_progress": sum(1 for t in engine.tasks if t.status == TaskStatus.IN_PROGRESS),
                "in_review": sum(1 for t in engine.tasks if t.status == TaskStatus.IN_REVIEW),
                "done": sum(1 for t in engine.tasks if t.status == TaskStatus.DONE),
            },
        )
        self.reply_card(message_id, json.dumps(status_card, ensure_ascii=False))

    # ------------------------------------------------------------------
    # Help
    # ------------------------------------------------------------------

    def show_slock_help(self, message_id: str):
        """Show slock help information using the command panel card."""
        from ...slock_engine.card_templates import build_command_panel_card

        panel_card = build_command_panel_card()
        self.reply_card(message_id, json.dumps(panel_card, ensure_ascii=False))

    # ------------------------------------------------------------------
    # Team management
    # ------------------------------------------------------------------

    def create_team(
        self, message_id: str, chat_id: str, name: str = "", project: Optional["ProjectContext"] = None
    ):
        """Create a new Feishu group and activate slock runtime inside it.

        Flow: validate → create group → promote sender → init engine →
              activate channel (workspace) → register managed chat →
              send welcome in new group → send jump card in original group.
        On failure: rollback by deleting the created group.
        """
        if not name:
            self.reply_text(message_id, "请提供团队名称\n\n用法: `/new-team <团队名称>`")
            return

        from ...project_chat.lark_chat_client import LarkChatClient
        from ...thread.manager import get_current_sender_id

        sender_open_id = get_current_sender_id() or ""
        if not sender_open_id:
            self.reply_text(message_id, "❌ 无法获取发送者信息，请重试")
            return

        settings = self.ctx.settings
        group_name = self._format_slock_group_name(name, getattr(settings, "slock_team_name_suffix", "[Slock]"))

        # Step 1: Create Feishu group
        lark_client = LarkChatClient(api_client_factory=self.ctx.api_client_factory)
        try:
            result = lark_client.create_chat(
                name=group_name,
                description=f"Slock 协作团队: {name}",
                user_id_list=[sender_open_id],
            )
        except Exception as e:
            from src.utils.errors import redact_sensitive
            logger.error("create_team: 建群失败 name=%s err=%s", name, redact_sensitive(str(e)))
            self.reply_text(message_id, f"❌ 创建团队群失败: {safe_error_message(e)}")
            return

        new_chat_id = result.chat_id

        try:
            # Step 2: Promote sender to group manager
            lark_client.add_managers(new_chat_id, [sender_open_id])

            # Step 3: Initialize slock engine for the new group
            root_path = project.root_path if project else self.get_working_dir(chat_id)
            manager = self._get_engine_manager()
            engine_name = self.get_engine_name(
                new_chat_id, project_id=(project.project_id if project else None)
            )
            engine = manager.get_or_create(new_chat_id, root_path, engine_name=engine_name)

            # Step 4: Activate channel (creates workspace directory + marker)
            channel = SlockChannel(
                channel_id=new_chat_id,
                name=group_name,
                team_name=name,
                owner_id=sender_open_id,
            )
            engine.activate_channel(channel)

            # Bootstrap default roles asynchronously (non-blocking)
            self._bootstrap_default_roles_if_configured(engine, channel.channel_id, new_chat_id)

            # Wire UI callbacks for escalation timeout notifications
            engine.set_escalation_ui_callbacks(
                update_card_fn=self.update_card,
                send_text_fn=self.send_text_to_chat,
            )

            # Step 5: Register managed chat for event routing
            manager.register_managed_chat(new_chat_id)

            # Step 6: Send welcome card in the new group
            from ...slock_engine.card_templates import build_welcome_card

            welcome_card = build_welcome_card(team_name=name)
            self.send_card_to_chat(new_chat_id, json.dumps(welcome_card, ensure_ascii=False))

            # Step 7: Send confirmation with jump link in original group
            from ...slock_engine.card_templates import build_team_created_card

            card = build_team_created_card(
                team_name=name,
                group_name=group_name,
                channel_id=new_chat_id,
            )
            self.reply_card(message_id, json.dumps(card, ensure_ascii=False))

        except Exception as e:
            # Rollback: delete the created group on any activation failure
            from src.utils.errors import redact_sensitive
            logger.error("create_team: 激活失败, 回滚建群 chat=%s err=%s", new_chat_id, redact_sensitive(str(e)))
            lark_client.delete_chat(new_chat_id)
            self.reply_text(message_id, f"❌ 团队激活失败已回滚: {safe_error_message(e)}")

    @staticmethod
    def _format_slock_group_name(name: str, suffix: str = "[Slock]") -> str:
        """Format Slock team group names with a visible suffix marker."""
        clean_name = (name or "").strip()
        clean_suffix = (suffix or "").strip()
        if not clean_suffix:
            return clean_name
        if clean_name.casefold().endswith(clean_suffix.casefold()):
            return clean_name
        separator = "" if suffix.startswith((" ", "-", "_", "/", "|", "｜", ":", "：")) else " "
        return f"{clean_name}{separator}{suffix}"

    def list_teams(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        """List all active Slock teams."""
        manager = self._get_engine_manager()
        engines = manager.list_activated_engines()

        if not engines:
            self.reply_text(message_id, "当前没有活跃的团队\n\n发送 `/slock` 激活协作模式")
            return

        teams = []
        for engine in sorted(engines, key=lambda item: (item.channel.team_name if item.channel else "")):
            channel = engine.channel
            if not channel:
                continue
            agents = engine.registry.list_agents(channel_id=channel.channel_id)
            agent_count = len(agents)
            task_count = len(engine.tasks)
            teams.append(
                {
                    "team_name": channel.team_name or channel.name or channel.channel_id,
                    "name": channel.name,
                    "channel_id": channel.channel_id,
                    "agent_count": agent_count,
                    "task_count": task_count,
                }
            )

        from ...slock_engine.card_templates import build_team_list_card

        self.reply_card(message_id, json.dumps(build_team_list_card(teams), ensure_ascii=False))

    def show_team_status(
        self, message_id: str, chat_id: str, name: str = "", project: Optional["ProjectContext"] = None
    ):
        """Show status of a specific team."""
        manager = self._get_engine_manager()
        engine = manager.find_team(name) if name else manager.get_activated_engine(chat_id)
        if not engine or not engine.channel:
            self.reply_text(message_id, f"未找到团队: **{redact_sensitive(name)}**" if name else "当前没有活跃的团队")
            return

        team_name = engine.channel.team_name if engine.channel else ""
        status_card = engine.get_status_card(team_name=team_name)
        sent_msg_id = self.reply_card(message_id, json.dumps(status_card, ensure_ascii=False))
        # Task 30: Register status card message_id for auto-refresh
        if sent_msg_id and engine.channel:
            engine._status_card_msg_ids[engine.channel.channel_id] = sent_msg_id

    def dissolve_team(
        self, message_id: str, chat_id: str, name: str = "", project: Optional["ProjectContext"] = None
    ):
        """Dissolve (stop) a team."""
        manager = self._get_engine_manager()
        engine = manager.find_team(name) if name else manager.get_activated_engine(chat_id)
        if not engine or not engine.channel:
            self.reply_text(message_id, f"未找到团队: **{redact_sensitive(name)}**" if name else "当前没有活跃的团队")
            return

        if not self._check_slock_permission(engine, message_id, chat_id):
            return

        target_chat_id = engine.channel.channel_id
        team_name = engine.channel.team_name or engine.channel.name or target_chat_id
        engine.deactivate()
        manager.unregister_managed_chat(target_chat_id)
        manager.remove(target_chat_id, engine.root_path)
        from ...project_chat.lark_chat_client import LarkChatClient

        lark_client = LarkChatClient(api_client_factory=self.ctx.api_client_factory)
        try:
            lark_client.delete_chat(target_chat_id)
        except Exception as e:
            logger.error("dissolve_team: 解散飞书群失败 chat=%s err=%s", target_chat_id, redact_sensitive(str(e)))
            self.reply_text(
                message_id,
                f"⚠️ 团队 **{team_name}** 本地运行时已停止，但解散飞书群失败: {safe_error_message(e)}",
            )
            return

        self.reply_text(message_id, f"✅ 团队 **{team_name}** 已解散并归档本地状态")

    # ------------------------------------------------------------------
    # Role / Agent management
    # ------------------------------------------------------------------

    # tool_type → default role inference mapping
    TOOL_TYPE_ROLE_MAP: dict[str, str] = {
        "codex": "coder",
        "claude": "reviewer",
        "coco": "writer",
        "aiden": "coder",
        "gemini": "coder",
        "ttadk": "custom",
    }

    # role → default personality traits (used when --traits not specified)
    DEFAULT_PERSONALITY_TRAITS: dict[str, list[str]] = {
        "coder": ["严谨", "注重细节"],
        "reviewer": ["批判性思维", "追求质量"],
        "tester": ["细致", "追求覆盖"],
        "planner": ["全局视角", "有条理"],
        "architect": ["抽象思维", "系统设计"],
        "writer": ["表达清晰", "注重结构"],
        "custom": [],
    }
    TOOL_SELECT_OPTIONS: tuple[dict[str, str], ...] = (
        {"name": "coco", "label": "Coco", "emoji": "🥥", "description": "默认协作工具"},
        {"name": "codex", "label": "Codex", "emoji": "🧠", "description": "代码实现"},
        {"name": "aiden", "label": "Aiden", "emoji": "🎯", "description": "AI 编程助手"},
        {"name": "claude", "label": "Claude", "emoji": "📝", "description": "评审与长文"},
        {"name": "gemini", "label": "Gemini", "emoji": "💎", "description": "多模态与代码"},
        {"name": "ttadk", "label": "TTADK", "emoji": "🧩", "description": "CLI 桥接"},
    )

    def create_role(
        self, message_id: str, chat_id: str, name: str = "", project: Optional["ProjectContext"] = None
    ):
        """Create a new virtual agent role.

        Supports parameter syntax:
            /new-role <name> [--tool <type>] [--model <model>] [--emoji <e>] [--role <role>] [--prompt <text>]
        """
        if not name:
            self.reply_text(
                message_id,
                "请提供角色名称\n\n用法: `/new-role <角色名称>` [--tool codex] [--model o3-pro] "
                "[--emoji 🔧] [--role coder] [--prompt <text>]",
            )
            return

        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            self.reply_text(message_id, "请先激活 Slock 模式: `/slock`")
            return

        # Permission gate: only admin or channel owner may create roles.
        if not self._check_slock_permission(engine, message_id, chat_id):
            return

        # Parse optional arguments from the name/args string
        try:
            tokens = shlex.split(name)
        except ValueError:
            tokens = name.split()

        role_name = tokens[0] if tokens else name
        if len(tokens) == 1:
            self.show_new_role_tool_selection(message_id, role_name, project)
            return

        tool_type = "coco"
        model_name = ""
        emoji = "🤖"
        system_prompt = ""
        explicit_role: str | None = None
        template_name = ""
        fork_name = ""
        explicit_traits: str | None = None
        tool_explicit = False
        model_explicit = False
        emoji_explicit = False
        prompt_explicit = False

        i = 1
        while i < len(tokens):
            tok = tokens[i]
            if tok == "--tool" and i + 1 < len(tokens):
                tool_type = tokens[i + 1]
                tool_explicit = True
                i += 2
            elif tok == "--model" and i + 1 < len(tokens):
                model_name = tokens[i + 1]
                model_explicit = True
                i += 2
            elif tok == "--emoji" and i + 1 < len(tokens):
                emoji = tokens[i + 1]
                emoji_explicit = True
                i += 2
            elif tok == "--role" and i + 1 < len(tokens):
                explicit_role = tokens[i + 1]
                i += 2
            elif tok == "--prompt" and i + 1 < len(tokens):
                system_prompt = tokens[i + 1]
                prompt_explicit = True
                i += 2
            elif tok == "--template" and i + 1 < len(tokens):
                template_name = tokens[i + 1]
                i += 2
            elif tok == "--fork" and i + 1 < len(tokens):
                fork_name = tokens[i + 1]
                i += 2
            elif tok == "--traits" and i + 1 < len(tokens):
                explicit_traits = tokens[i + 1]
                i += 2
            else:
                i += 1

        # Validate tool_type against whitelist
        from ...slock_engine.models import AGENT_ROLE_COLORS, AgentIdentity, SlockMemory

        template_data: dict = {}
        if template_name:
            raw_template = engine.memory.read_agent_template(template_name)
            template_data = raw_template if isinstance(raw_template, dict) else {}
            if not template_data:
                self.reply_text(message_id, f"❌ 未找到 Agent 模板: `{template_name}`")
                return
            if not tool_explicit:
                tool_type = template_data.get("tool_type", tool_type)
            if not model_explicit:
                model_name = template_data.get("model_name", model_name)
            if not emoji_explicit:
                emoji = template_data.get("emoji", emoji)
            if explicit_role is None:
                explicit_role = template_data.get("role") or explicit_role
            if not prompt_explicit:
                system_prompt = template_data.get("system_prompt", system_prompt)

        fork_source: AgentIdentity | None = None
        if fork_name:
            fork_source = engine.registry.find_by_name(fork_name, channel_id=chat_id) or engine.registry.find_by_name(fork_name)
            if not isinstance(fork_source, AgentIdentity):
                self.reply_text(message_id, f"❌ 未找到可 fork 的角色: `{fork_name}`")
                return
            if not tool_explicit:
                tool_type = fork_source.agent_type
            if not model_explicit:
                model_name = fork_source.model_name
            if not emoji_explicit:
                emoji = fork_source.emoji
            if explicit_role is None:
                explicit_role = fork_source.role
            if not prompt_explicit:
                system_prompt = fork_source.system_prompt

        VALID_TOOLS = set(self.TOOL_TYPE_ROLE_MAP.keys())
        if tool_type not in VALID_TOOLS:
            self.reply_text(
                message_id,
                f"❌ 无效的 tool_type: `{tool_type}`\n"
                f"合法值: {', '.join(sorted(VALID_TOOLS))}",
            )
            return

        # Validate explicit role against whitelist
        VALID_ROLES = set(AGENT_ROLE_COLORS.keys())
        if explicit_role and explicit_role not in VALID_ROLES:
            self.reply_text(
                message_id,
                f"❌ 无效的 role: `{explicit_role}`\n"
                f"合法值: {', '.join(sorted(VALID_ROLES))}",
            )
            return

        # Determine role: explicit --role takes priority, otherwise infer from tool_type
        if explicit_role:
            role = explicit_role
        else:
            role = self.TOOL_TYPE_ROLE_MAP.get(tool_type, "custom")

        # Assign unique emoji if not explicitly provided
        if not emoji_explicit:
            from ...slock_engine.role_bootstrap import pick_unique_emoji
            used_emojis = {a.emoji for a in engine.registry.list_agents() if hasattr(a, 'emoji')}
            emoji = pick_unique_emoji(role, used_emojis)

        runtime_agent_type = self._resolve_slock_runtime_agent_type(tool_type)
        agent_id = f"{runtime_agent_type}:{model_name or 'default'}:{role_name}"
        existing_raw = engine.registry.get(agent_id)
        existing_agent = existing_raw if isinstance(existing_raw, AgentIdentity) else None
        if existing_agent and not system_prompt:
            system_prompt = existing_agent.system_prompt
        if not system_prompt:
            system_prompt = self._build_default_directive(
                role_name=role_name,
                role=role,
                tool_type=tool_type,
                model_name=model_name,
                team_name=(engine.channel.team_name if engine.channel else chat_id),
            )

        # Determine personality traits: explicit --traits overrides default role mapping
        if explicit_traits:
            personality_traits = [t.strip() for t in explicit_traits.replace("，", ",").split(",") if t.strip()]
        else:
            personality_traits = list(self.DEFAULT_PERSONALITY_TRAITS.get(role, []))

        agent = AgentIdentity(
            agent_id=agent_id,
            name=role_name,
            emoji=emoji,
            agent_type=runtime_agent_type,
            model_name=model_name,
            system_prompt=system_prompt,
            role=role,
            owner_group=chat_id,
            member_groups=[chat_id],
            personality_traits=personality_traits,
        )
        workspace_paths = engine.memory.initialize_agent_workspace(agent.agent_id)
        agent.memory_path = workspace_paths.get("memory_path") or engine.memory.agent_memory_path(agent.agent_id)
        agent.notes_path = workspace_paths.get("notes_path") or engine.memory.agent_notes_path(agent.agent_id)
        agent.workspace_path = workspace_paths.get("workspace_path") or engine.memory.agent_workspace_path(agent.agent_id)
        if not existing_agent:
            if fork_source is not None:
                source_memory = engine.memory.read_agent_memory(fork_source.agent_id)
                active_context = source_memory.active_context
                fork_entry = f"Forked from {fork_source.agent_id} into Slock team {chat_id}."
                active_context = f"{active_context}\n{fork_entry}".strip() if active_context else fork_entry
                engine.memory.write_agent_memory(
                    agent.agent_id,
                    SlockMemory(
                        role=source_memory.role or system_prompt,
                        key_knowledge=source_memory.key_knowledge,
                        active_context=active_context,
                    ),
                )
                source_profiles = engine.memory.read_skill_profiles(fork_source.agent_id)
                engine.memory.write_skill_profiles(agent.agent_id, source_profiles)
            else:
                key_knowledge = template_data.get("key_knowledge") or (
                    f"tool_type={tool_type}\nruntime_agent_type={runtime_agent_type}\n"
                    f"model={model_name or 'default'}\nrole={role}"
                )
                engine.memory.write_agent_memory(
                    agent.agent_id,
                    SlockMemory(
                        role=system_prompt,
                        key_knowledge=key_knowledge,
                        active_context=f"Created in Slock team {chat_id}.",
                    ),
                )
        else:
            engine.memory.update_agent_context(agent.agent_id, f"Joined Slock team {chat_id}.")
        engine.registry.register(agent)

        self.reply_text(
            message_id,
            f"✅ 角色 **{agent.emoji} {agent.name}** 已创建 (ID: `{agent.agent_id[:8]}`)\n"
            f"   工具: `{tool_type}` | 运行时: `{runtime_agent_type}` | 模型: `{model_name or '默认'}` | "
            f"角色: `{role}` | Emoji: {emoji}",
        )

    @staticmethod
    def _resolve_slock_runtime_agent_type(tool_type: str) -> str:
        """Map role creation tool choice to an executable agent session backend."""
        if tool_type == "ttadk":
            return "ttadk_coco"
        return tool_type

    def show_new_role_tool_selection(
        self,
        message_id: str,
        role_name: str,
        project: Optional["ProjectContext"] = None,
    ) -> None:
        """Show the first `/new-role` interactive step: choose a backing tool."""

        _, card_content = CardBuilder.build_slock_role_tool_select_card(
            role_name,
            list(self.TOOL_SELECT_OPTIONS),
            project_id=(project.project_id if project else None),
        )
        self.reply_card(message_id, card_content)

    def handle_new_role_select_tool(
        self,
        message_id: str,
        chat_id: str,
        value: dict,
        project: Optional["ProjectContext"] = None,
    ) -> None:
        """Handle `/new-role` tool selection and show the ACP model picker."""

        role_name = str(value.get("role_name") or "").strip()
        tool_name = str(value.get("tool_name") or "").strip().lower()
        if not role_name or not tool_name:
            self.reply_text(message_id, "请选择有效的 Slock 角色工具")
            return

        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            self.reply_text(message_id, "请先激活 Slock 模式: `/slock`")
            return

        if tool_name == "ttadk":
            self.create_role(message_id, chat_id, f"{shlex.quote(role_name)} --tool ttadk", project)
            return

        cwd = getattr(project, "root_path", None) or getattr(engine, "root_path", None) or self.get_working_dir(chat_id)
        models = fetch_acp_models(tool_name, cwd=cwd, current_model=None)
        _, card_content = CardBuilder.build_acp_model_select_card(
            models,
            tool_name,
            project_id=(project.project_id if project else value.get("project_id")),
            action_name=action_ids.SLOCK_NEW_ROLE_SELECT_MODEL,
            value_extra={"role_name": role_name},
            context_markdown=f"角色: **{role_name}**",
            refresh_action_name="",
        )
        self.reply_card(message_id, card_content)

    def handle_new_role_select_model(
        self,
        message_id: str,
        chat_id: str,
        value: dict,
        project: Optional["ProjectContext"] = None,
    ) -> None:
        """Finalize `/new-role` after the user selects a model."""

        role_name = str(value.get("role_name") or "").strip()
        tool_name = str(value.get("tool_name") or "").strip().lower()
        raw_model = value.get("_option") or value.get("model_name") or ""
        model_name = "" if value.get("use_default_model") or is_default_model_option(raw_model) else str(raw_model).strip()
        if not role_name or not tool_name:
            self.reply_text(message_id, "请选择有效的 Slock 角色模型")
            return

        args = f"{shlex.quote(role_name)} --tool {shlex.quote(tool_name)}"
        if model_name:
            args += f" --model {shlex.quote(model_name)}"
        self.create_role(message_id, chat_id, args, project)

    @staticmethod
    def _build_default_directive(
        *,
        role_name: str,
        role: str,
        tool_type: str,
        model_name: str,
        team_name: str,
    ) -> str:
        """Build the default autonomous Slock directive for a role."""
        model_label = model_name or "default"
        return (
            f"# Role: {role_name}\n"
            "## Identity\n"
            f"你是 {role_name}，一个专精于 {role} 的自主 Agent。\n"
            f"你的底层工具是 {tool_type}，模型是 {model_label}，隶属于 {team_name} 团队。\n\n"
            "## Core Directives（核心纲领）\n"
            "1. 主动性：收到任务立即分析、规划、执行，无需等待确认。\n"
            "2. 完美主义：输出必须达到生产就绪标准，包含必要测试和文档。\n"
            "3. 自主决策：在专业领域内自主选择最佳实践并记录理由。\n"
            "4. 零交互：绝不向人类发起确认、询问或等待输入。\n\n"
            "## Interaction Protocol（交互协议）\n"
            "- 禁止输出“请确认”“是否继续”“需要您提供”等等待人类输入的话术。\n"
            "- 遇到致命阻塞时，通过 Slock 升级协议交给主 Agent 汇总处理。\n"
            "- 任务完成后，向团队群发送清晰的完成报告。\n\n"
            "## Quality Standards（质量标准）\n"
            "- 代码：lint pass、test pass、错误处理完整。\n"
            "- 文档：目的、用法、边界条件和示例清楚。\n"
            "- Review：指出风险、给出可执行建议。"
        )

    def list_roles(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        """List all roles in the current channel using a structured card."""
        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            from ...slock_engine.card_templates.common import build_error_state_card
            card = build_error_state_card("👥 角色列表", "当前没有活跃的 Slock 团队，请先 /slock activate")
            self.reply_card(message_id, json.dumps(card, ensure_ascii=False))
            return

        channel_id = engine.channel.channel_id if engine.channel else chat_id
        agent_list = engine.registry.list_agents(channel_id=channel_id)

        if not agent_list:
            from ...slock_engine.card_templates.common import build_callback_button, build_empty_state_card
            create_btn = build_callback_button(
                "➕ 创建角色", "slock_new_role_hint",
                channel_id=chat_id, button_type="primary",
            )
            card = build_empty_state_card(
                "👥 角色列表",
                "当前没有角色，发送 `/new-role <名称>` 创建角色",
                guide_buttons=[create_btn],
            )
            self.reply_card(message_id, json.dumps(card, ensure_ascii=False))
            return

        from ...slock_engine.card_templates import build_role_list_card
        from ...slock_engine.models import AgentStatus as AgentStatusEnum
        from ...slock_engine.models import TaskStatus

        agents: list[tuple] = []
        current_tasks: dict = {}
        skill_profiles: dict = {}
        for agent in agent_list:
            status = engine.get_agent_status(agent.agent_id) or AgentStatusEnum.IDLE
            agents.append((agent, status))
            for task in engine.tasks:
                if task.claimed_by == agent.agent_id and task.status == TaskStatus.IN_PROGRESS:
                    current_tasks[agent.agent_id] = task
                    break
            profiles = engine.memory.read_skill_profiles(agent.agent_id)
            if profiles:
                skill_profiles[agent.agent_id] = [
                    {"tag": p.tag, "success_rate": p.success_rate, "total_tasks": p.total_tasks}
                    for p in profiles
                ]

        team_name = engine.channel.team_name if engine.channel else ""
        card = build_role_list_card(
            agents=agents,
            team_name=team_name,
            channel_id=channel_id,
            current_tasks=current_tasks,
            skill_profiles=skill_profiles,
        )
        self.reply_card(message_id, json.dumps(card, ensure_ascii=False))

    def remove_role(
        self, message_id: str, chat_id: str, name: str = "", project: Optional["ProjectContext"] = None
    ):
        """Remove a virtual agent role."""
        if not name:
            self.reply_text(message_id, "请提供角色名称\n\n用法: `/role remove <名称>`")
            return

        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            from ...slock_engine.card_templates.common import build_error_state_card
            card = build_error_state_card("👥 角色管理", "当前没有活跃的 Slock 团队，请先 /slock activate")
            self.reply_card(message_id, json.dumps(card, ensure_ascii=False))
            return

        if not self._check_slock_permission(engine, message_id, chat_id):
            return

        agent = engine.registry.find_by_name(name, channel_id=chat_id)
        if not agent:
            self.reply_text(message_id, f"未找到角色: **{redact_sensitive(name)}**")
            return

        engine.registry.remove(agent.agent_id)
        self.reply_text(message_id, f"✅ 角色 **{agent.emoji} {agent.name}** 已移除")

    def move_role(
        self,
        message_id: str,
        chat_id: str,
        name: str = "",
        target_team_name: str = "",
        project: Optional["ProjectContext"] = None,
    ):
        """Move an agent from the current team to a target team.

        Flow: validate → permission(source+target) → try_lock_for_move (atomic
        IDLE→MOVING) → registry.move_agent → unlock → refresh target cache →
        notify target (best-effort) → append L1 context → reply confirm card.

        Design: 'move first, notify second' — registry persistence is the
        authoritative operation; notification card is best-effort UI.  If the
        notification fails, the move still stands and source group receives a
        degraded warning instead of a rollback.
        """
        import time as _time

        from ...slock_engine.card_templates import (
            build_agent_move_confirm_card,
            build_agent_move_departure_card,
            build_agent_move_notification_card,
        )
        from ...thread.manager import get_current_sender_id

        if not name or not target_team_name:
            self.reply_text(
                message_id,
                "请提供角色名称和目标团队\n\n用法: `/role move <角色名> <目标团队名>`",
            )
            return

        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            from ...slock_engine.card_templates.common import build_error_state_card
            card = build_error_state_card("👥 角色管理", "当前没有活跃的 Slock 团队，请先 /slock activate")
            self.reply_card(message_id, json.dumps(card, ensure_ascii=False))
            return

        # Permission check: source group
        if not self._check_slock_permission(engine, message_id, chat_id):
            return

        agent = engine.registry.find_by_name(name, channel_id=chat_id)
        if not agent:
            self.reply_text(message_id, f"未找到角色: **{redact_sensitive(name)}**")
            return

        # Find target team
        target_engine = manager.find_team(target_team_name)
        if not target_engine or not target_engine.channel:
            self.reply_text(message_id, f"未找到目标团队: **{redact_sensitive(target_team_name)}**")
            return

        target_channel_id = target_engine.channel.channel_id
        if target_channel_id == chat_id:
            self.reply_text(message_id, "⚠️ 目标团队与当前团队相同，无需移动")
            return

        # Permission check: target group (operator must be global admin or target owner)
        if not self._check_slock_permission(target_engine, message_id, chat_id):
            return

        # Atomic: check IDLE and lock agent in MOVING state
        if not engine.try_lock_for_move(agent.agent_id):
            status = engine.get_agent_status(agent.agent_id)
            self.reply_text(
                message_id,
                f"⚠️ 角色 **{agent.name}** 当前状态为 {status.value}，仅 IDLE 状态可移动",
            )
            return

        source_team_display = (engine.channel.team_name if engine.channel else chat_id) or chat_id
        target_team_display = target_engine.channel.team_name or target_team_name
        operator_id = get_current_sender_id() or ""

        try:
            # Step 1: Perform atomic move in registry (authoritative operation)
            outcome = engine.registry.move_agent(agent.agent_id, chat_id, target_channel_id)
            if not outcome.success:
                from ...slock_engine.agent_registry import MoveResult

                if outcome.status == MoveResult.NOT_FOUND:
                    err_msg = f"❌ 移动失败：角色 **{redact_sensitive(name)}** 未找到"
                elif outcome.status == MoveResult.NOT_IN_SOURCE:
                    err_msg = "❌ 移动失败，请确认角色属于当前团队"
                elif outcome.status == MoveResult.PERSIST_FAILED:
                    err_msg = "❌ 迁移失败：数据持久化异常，角色仍留在原团队，请稍后重试"
                else:
                    err_msg = "❌ 移动失败，请确认角色属于当前团队"
                logger.warning(
                    "slock move_role: registry move_agent failed | agent=%s source=%s target=%s operator=%s status=%s",
                    agent.agent_id, chat_id, target_channel_id, operator_id, outcome.status.value,
                )
                self.reply_text(message_id, err_msg)
                return

            # Step 1.5: Redact active_context to prevent source-group history leakage
            import hashlib as _hashlib

            try:
                _pre_memory = engine.memory.read_agent_memory(agent.agent_id)
                _ctx_len = len(_pre_memory.active_context) if _pre_memory.active_context else 0
                _ctx_md5 = _hashlib.md5(_pre_memory.active_context.encode()).hexdigest() if _pre_memory.active_context else ""
                logger.info(
                    "slock move_role: pre-redact audit | agent=%s context_chars=%d md5=%s",
                    agent.agent_id, _ctx_len, _ctx_md5,
                )
                engine.memory.redact_active_context_for_move(agent.agent_id, chat_id, target_channel_id)
            except Exception as exc:
                logger.warning(
                    "slock move_role: redact_active_context failed (non-fatal) | agent=%s error=%s",
                    agent.agent_id, exc,
                )
        finally:
            engine.unlock_after_move(agent.agent_id)

        # Refresh target engine's registry cache so it can discover the agent
        target_engine.registry.refresh_agent(agent.agent_id)

        # Verify L1 memory integrity after move (user-facing degradation notice)
        _l1_memory_degraded = False
        try:
            _moved_memory = target_engine.memory.read_agent_memory(agent.agent_id)
            if agent.system_prompt and not _moved_memory.role and not _moved_memory.key_knowledge:
                logger.error(
                    "slock move_role: L1 memory empty after move — persona may be inconsistent | agent=%s",
                    agent.agent_id,
                )
                _l1_memory_degraded = True
        except Exception as exc:
            logger.error(
                "slock move_role: L1 memory read failed after move | agent=%s error=%s",
                agent.agent_id, exc,
            )
            _l1_memory_degraded = True

        # Resolve operator display name for notification card
        operator_display = resolve_display_name(operator_id, self.ctx.api_client_factory) if operator_id else ""

        # Step 2: Send notification card to target group (best-effort)
        notification_card = build_agent_move_notification_card(
            agent=agent,
            source_team=source_team_display,
            target_team=target_team_display,
            operator_display=operator_display,
        )
        sent_msg_id = self.send_card_to_chat(
            target_channel_id, json.dumps(notification_card, ensure_ascii=False)
        )
        if not sent_msg_id:
            logger.warning(
                "slock move_role: notification card send failed (non-fatal) | agent=%s source=%s target=%s operator=%s",
                agent.agent_id, chat_id, target_channel_id, operator_id,
            )
            self.reply_text(
                message_id,
                "✅ 移动成功，但目标群通知发送失败。目标群可通过 /role list 查看新成员。",
            )
            # Do NOT return — continue to append context and send confirm card

        # Step 2.5: Send departure notification card to source group (best-effort)
        try:
            departure_card = build_agent_move_departure_card(
                agent=agent,
                target_team=target_team_display,
            )
            self.send_card_to_chat(chat_id, json.dumps(departure_card, ensure_ascii=False))
        except Exception as exc:
            logger.warning(
                "slock move_role: departure card send failed (non-fatal) | agent=%s source=%s error=%s",
                agent.agent_id, chat_id, exc,
            )

        # Step 3: Append migration record to L1 active context
        context_record = (
            f"[{_time.strftime('%Y-%m-%d %H:%M')}] "
            f"Moved from {source_team_display} to {target_team_display}"
        )
        try:
            engine.memory.update_agent_context(agent.agent_id, context_record)
        except Exception as exc:
            logger.warning(
                "slock move_role: update_agent_context failed (non-fatal) | agent=%s error=%s",
                agent.agent_id, exc,
            )

        # Step 4: Reply confirm card to source group with jump button
        confirm_card = build_agent_move_confirm_card(
            agent=agent,
            source_team=source_team_display,
            target_team=target_team_display,
            target_channel_id=target_channel_id,
        )
        self.reply_card(message_id, json.dumps(confirm_card, ensure_ascii=False))

        if _l1_memory_degraded:
            self.reply_text(
                message_id,
                "⚠️ 注意：角色记忆加载异常，人格一致性可能受影响。请在目标群执行 /role info 确认。",
            )

        logger.info(
            "slock move_role: success | agent=%s source=%s target=%s operator=%s",
            agent.agent_id, chat_id, target_channel_id, operator_id,
        )

    def show_role_info(
        self, message_id: str, chat_id: str, name: str = "", project: Optional["ProjectContext"] = None
    ):
        """Show detailed info about a role using structured card.

        Permission-aware: admin/owner sees full details including Active Context
        and permissions; regular members see only non-sensitive identity info.
        """
        if not name:
            from ...slock_engine.card_templates.common import build_usage_hint_card
            card = build_usage_hint_card(
                "/role info <名称>",
                ["/role info coder", "/role info reviewer", "/r info tester"],
            )
            self.reply_card(message_id, json.dumps(card, ensure_ascii=False))
            return

        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            from ...slock_engine.card_templates.common import build_error_state_card
            card = build_error_state_card("👤 角色详情", "当前没有活跃的 Slock 团队，请先 /slock activate")
            self.reply_card(message_id, json.dumps(card, ensure_ascii=False))
            return

        agent = engine.registry.find_by_name(name, channel_id=chat_id)
        if not agent:
            self.reply_text(message_id, f"未找到角色: **{redact_sensitive(name)}**")
            return

        from ...slock_engine.card_templates import build_role_info_card
        from ...slock_engine.models import AgentStatus as AgentStatusEnum
        from ...slock_engine.models import TaskStatus

        channel_id = engine.channel.channel_id if engine.channel else chat_id
        status = engine.get_agent_status(agent.agent_id) or AgentStatusEnum.IDLE
        memory = engine.memory.read_agent_memory(agent.agent_id)
        profiles = engine.memory.read_skill_profiles(agent.agent_id)
        skill_profiles = [
            {"tag": p.tag, "success_rate": p.success_rate, "total_tasks": p.total_tasks}
            for p in profiles
        ]

        # Current and recent tasks
        current_task = None
        recent_tasks: list = []
        for task in engine.tasks:
            if task.claimed_by == agent.agent_id:
                if task.status == TaskStatus.IN_PROGRESS:
                    current_task = task
                elif task.status == TaskStatus.DONE:
                    recent_tasks.append(task)
        recent_tasks = recent_tasks[-3:]  # Last 3 completed

        card = build_role_info_card(
            agent,
            status=status,
            memory=memory,
            skill_profiles=skill_profiles,
            current_task=current_task,
            recent_tasks=recent_tasks,
            channel_id=channel_id,
        )
        self.reply_card(message_id, json.dumps(card, ensure_ascii=False))

    # ------------------------------------------------------------------
    # Task management
    # ------------------------------------------------------------------

    def list_tasks(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        """List all tasks in the current slock session."""
        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            from ...slock_engine.card_templates.common import build_error_state_card
            card = build_error_state_card("📋 任务列表", "当前没有活跃的 Slock 团队，请先 /slock activate")
            self.reply_card(message_id, json.dumps(card, ensure_ascii=False))
            return

        tasks = engine.tasks
        if not tasks:
            from ...slock_engine.card_templates.common import build_callback_button, build_empty_state_card
            guide_btn = build_callback_button(
                "➕ 分配任务", "slock_assign_task_hint",
                channel_id=chat_id, button_type="primary",
            )
            card = build_empty_state_card(
                "📋 任务列表",
                "当前没有任务。\n\n发送 `/task assign <任务描述>` 或点击下方按钮分配任务。",
                guide_buttons=[guide_btn],
            )
            self.reply_card(message_id, json.dumps(card, ensure_ascii=False))
            return

        from ...slock_engine.card_templates import build_task_board_card

        channel_id = engine.channel.channel_id if engine.channel else chat_id
        agents = engine.registry.list_agents(channel_id=channel_id)
        team_name = engine.channel.team_name if engine.channel else ""
        card = build_task_board_card(engine.tasks, agents, team_name=team_name, channel_id=channel_id)
        self.reply_card(message_id, json.dumps(card, ensure_ascii=False))

    def assign_task(
        self,
        message_id: str,
        chat_id: str,
        content: str = "",
        role_name: str = "",
        project: Optional["ProjectContext"] = None,
    ):
        """Assign a task to a specific role and execute it.

        If role_name is provided: claim → execute → complete/rollback.
        If no role_name: create task only (unassigned).
        """
        if not content:
            self.reply_text(message_id, "请提供任务内容\n\n用法: `/task assign <任务内容> <角色名称>`")
            return

        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            self.reply_text(message_id, "请先激活 Slock 模式: `/slock`")
            return

        # Rate-limit check for non-admin/non-owner users
        if not self._check_assign_rate_limit(engine, message_id, chat_id):
            return

        task = engine.add_task(content)
        if task is None:
            self.reply_text(
                message_id,
                "❌ 任务创建失败（任务队列已满或内部错误），请稍后重试",
            )
            return

        if role_name:
            channel_id = engine.channel.channel_id if engine.channel else chat_id
            agent = engine.registry.find_by_name(role_name, channel_id=channel_id)
            if not agent:
                self.reply_text(
                    message_id,
                    f"⚠️ 任务已创建但未找到角色 **{redact_sensitive(role_name)}**\n"
                    f"• 任务 ID: `{task.task_id[:8]}`\n"
                    f"• 发送 `/role list` 查看可用角色",
                )
                return

            # Claim the task
            if not engine.claim_task(task.task_id, agent.agent_id):
                self.reply_text(message_id, f"❌ 任务 claim 失败，{agent.name} 可能正在执行其他任务")
                return

            # Submit async execution — send placeholder, update on completion
            self._submit_task_execution(
                engine, task, agent, message_id, chat_id, content, project
            )
            return

        # No role specified — use skill-based automatic assignment.
        channel_id = engine.channel.channel_id if engine.channel else chat_id

        # Auto-detect multi-role collaboration: if a chain template matches,
        # create a collaboration plan instead of single-agent execution.
        chain = engine._chain_manager.find_chain_for_task(content)
        if chain and len(chain.roles) > 1:
            plan = engine._collaboration_orchestrator.create_plan(task, channel_id)
            if plan:
                from ...slock_engine.card_templates.progress import build_collaboration_plan_card
                agents = list(engine.registry.list_agents(channel_id=channel_id))
                card = build_collaboration_plan_card(plan, agents, channel_id=channel_id)
                card_msg_id = self.reply_card(message_id, json.dumps(card, ensure_ascii=False))
                # Register overview message_id for in-place card updates
                if card_msg_id and hasattr(engine, '_progress_tracker'):
                    engine._progress_tracker.set_overview_message_id(plan.plan_id, card_msg_id)
                # Confirm delivery — reset auto-start timer from this point
                engine._collaboration_orchestrator.confirm_plan_delivery(plan.plan_id)
                return

        agents = list(engine.registry.list_agents(channel_id=channel_id))
        if not agents:
            self.reply_text(
                message_id,
                f"✅ 任务已创建（等待分配）\n"
                f"• ID: `{task.task_id[:8]}`\n"
                f"• 内容: {redact_sensitive(content[:80])}\n"
                f"• 当前没有可用角色，发送 `/new-role <名称>` 创建角色",
            )
            return

        rank_agents = getattr(engine.router, "rank_agents_for_claim", None)
        ranked_agents = rank_agents(content, agents) if callable(rank_agents) else []
        if not isinstance(ranked_agents, list):
            selected = engine.router.route_message(content, agents)
            ranked_agents = [selected] if selected else []
        if not ranked_agents:
            self.reply_text(
                message_id,
                f"✅ 任务已创建（等待分配）\n"
                f"• ID: `{task.task_id[:8]}`\n"
                f"• 内容: {redact_sensitive(content[:80])}\n"
                f"• 暂无匹配角色，发送 `/role list` 查看可用角色",
            )
            return

        claimed_agent = None
        for candidate in ranked_agents:
            if engine.claim_task(task.task_id, candidate.agent_id):
                claimed_agent = candidate
                break

        if claimed_agent is None:
            self.reply_text(message_id, "❌ 任务 claim 失败，所有匹配角色可能都在执行其他任务")
            return

        self._submit_task_execution(
            engine, task, claimed_agent, message_id, chat_id, content, project, auto_routed=True
        )

    def _submit_task_execution(
        self,
        engine,
        task,
        agent,
        message_id: str,
        chat_id: str,
        content: str,
        project: Optional["ProjectContext"],
        *,
        auto_routed: bool = False,
    ) -> None:
        """Submit task execution to the engine's thread pool asynchronously.

        Sends a placeholder card immediately, then updates it with the result
        or error when execution completes.
        """
        prefix = "自动分配给" if auto_routed else "已分配给"

        def _execute():
            callbacks = self._create_callbacks(message_id, chat_id, project, engine.engine_name, engine.root_path)
            return engine.execute_task(task.task_id, agent.agent_id, callbacks)

        def _result_card(result: str, duration: float) -> str:
            try:
                engine.memory.write_agent_reasoning_snapshot(
                    agent.agent_id,
                    task.task_id,
                    prompt_summary=content[:1000],
                    result_summary=result[:2000],
                    tool_name=agent.agent_type,
                    model_name=agent.model_name,
                )
            except Exception:
                logger.warning(
                    "Failed to persist Slock reasoning snapshot for task %s agent %s",
                    task.task_id,
                    agent.agent_id,
                    exc_info=True,
                )
            card_data = engine._mouthpiece.format_card(
                agent,
                result,
                model_info=agent.agent_type,
                duration_s=duration,
                channel_id=chat_id,
                task_id=task.task_id,
            )
            return json.dumps(card_data, ensure_ascii=False)

        def _error_card(exc: Exception) -> str:
            return json.dumps({
                "schema": "2.0",
                "config": {"wide_screen_mode": True},
                "header": {"title": {"tag": "plain_text", "content": "❌ 任务执行失败"}, "template": "red"},
                "body": {"elements": [{"tag": "markdown", "content": (
                    f"Agent: {agent.emoji} {agent.name}\n"
                    f"任务: {content[:60]}\n"
                    f"错误: {safe_error_message(exc)}\n\n"
                    "任务已回退为 TODO，可重新分配"
                )}]},
            }, ensure_ascii=False)

        def _empty_card() -> str:
            return json.dumps({
                "schema": "2.0",
                "config": {"wide_screen_mode": True},
                "header": {"title": {"tag": "plain_text", "content": "⚠️ 任务完成无输出"}, "template": "orange"},
                "body": {"elements": [{"tag": "markdown", "content": (
                    f"Agent: {agent.emoji} {agent.name}\n"
                    f"任务: {content[:60]}\n\n"
                    "任务已回退为 TODO，可重新分配"
                )}]},
            }, ensure_ascii=False)

        def _busy_card() -> str:
            return json.dumps({
                "schema": "2.0",
                "config": {"wide_screen_mode": True},
                "header": {"title": {"tag": "plain_text", "content": "⚠️ 团队繁忙"}, "template": "orange"},
                "body": {"elements": [{"tag": "markdown", "content": "当前所有角色均在忙碌中，请稍后重试。"}]},
            }, ensure_ascii=False)

        placeholder_card = json.dumps({
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"⏳ 任务{prefix} {agent.emoji} {agent.name}"},
                "template": "indigo",
            },
            "body": {"elements": [{"tag": "markdown", "content": (
                f"**任务**: {content[:80]}\n"
                f"**ID**: `{task.task_id[:8]}`\n\n"
                "Agent 正在执行中..."
            )}]},
        }, ensure_ascii=False)

        self._execute_async(
            engine=engine,
            execute_fn=_execute,
            placeholder_card_json=placeholder_card,
            result_card_fn=_result_card,
            error_card_fn=_error_card,
            empty_card_fn=_empty_card,
            busy_card_fn=_busy_card,
            message_id=message_id,
            chat_id=chat_id,
        )

    def show_task_status(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        """Show task board with status summary."""
        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            from ...slock_engine.card_templates.common import build_error_state_card
            card = build_error_state_card("📊 任务状态", "当前没有活跃的 Slock 团队，请先 /slock activate")
            self.reply_card(message_id, json.dumps(card, ensure_ascii=False))
            return

        from ...slock_engine.card_templates import build_task_board_card

        channel_id = engine.channel.channel_id if engine.channel else chat_id
        agents = engine.registry.list_agents(channel_id=channel_id)
        team_name = engine.channel.team_name if engine.channel else ""
        card = build_task_board_card(engine.tasks, agents, team_name=team_name, channel_id=channel_id)
        self.reply_card(message_id, json.dumps(card, ensure_ascii=False))

    # ------------------------------------------------------------------
    # Memory management
    # ------------------------------------------------------------------

    def show_memory_group(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        """Show L2 shared group memory for the current slock team."""
        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            from ...slock_engine.card_templates.common import build_error_state_card
            card = build_error_state_card("🧠 群组记忆", "当前没有活跃的 Slock 团队，请先 /slock activate")
            self.reply_card(message_id, json.dumps(card, ensure_ascii=False))
            return

        channel_id = engine.channel.channel_id if engine.channel else chat_id
        group_memory = engine.memory.read_group_memory(channel_id)
        if not group_memory:
            from ...slock_engine.card_templates.common import build_empty_state_card
            card = build_empty_state_card(
                "🧠 群组共享记忆",
                "当前群组暂无共享记忆。\n\nAgent 协作过程中会自动积累群组知识。",
            )
            self.reply_card(message_id, json.dumps(card, ensure_ascii=False))
            return

        from ...slock_engine.card_templates.memory import build_memory_group_card

        # Parse group memory text into structured memory_items
        lines = [line.strip() for line in group_memory.strip().split("\n") if line.strip()]
        memory_items: list[dict] = []
        current_category = "context"
        for line in lines[:50]:  # NFR-6: limit to 50 items per read
            if line.startswith("## ") or line.startswith("# "):
                current_category = line.lstrip("#").strip().lower() or "context"
            else:
                memory_items.append({
                    "category": current_category,
                    "content": line[:200],
                    "timestamp": None,
                })

        total_lines = len(lines)
        team_name = engine.channel.team_name if engine.channel else "群组"

        card = build_memory_group_card(
            agent_name=f"{team_name} (共享)",
            agent_emoji="🧠",
            memory_items=memory_items,
            channel_id=channel_id,
        )

        # Inject meta-info row at the top of elements (after header)
        truncated = total_lines > 50
        meta_text = f"📊 共 {total_lines} 条记录"
        if truncated:
            meta_text += " | ⚠️ 内容已截断，仅显示前 50 条"
        if card.get("body", {}).get("elements"):
            card["body"]["elements"].insert(0, {"tag": "markdown", "content": meta_text})

        self.reply_card(message_id, json.dumps(card, ensure_ascii=False))

    # ------------------------------------------------------------------
    # Agent Memory (per-agent L1 memory)
    # ------------------------------------------------------------------

    def show_agent_memory(self, message_id: str, chat_id: str, agent_name: str = "", project: Optional["ProjectContext"] = None):
        """Show L1 memory for a specific agent."""
        if not agent_name:
            from ...slock_engine.card_templates.common import build_usage_hint_card
            card = build_usage_hint_card(
                "/memory <agent_name>",
                ["/memory coder", "/memory list"],
            )
            self.reply_card(message_id, json.dumps(card, ensure_ascii=False))
            return

        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            from ...slock_engine.card_templates.common import build_error_state_card
            card = build_error_state_card("🧠 角色记忆", "当前没有活跃的 Slock 团队，请先 /slock activate")
            self.reply_card(message_id, json.dumps(card, ensure_ascii=False))
            return

        channel_id = engine.channel.channel_id if engine.channel else chat_id
        agent = engine.registry.find_by_name(agent_name, channel_id=channel_id)
        if not agent:
            from ...slock_engine.card_templates.common import build_empty_state_card
            card = build_empty_state_card("🧠 角色记忆", f"未找到角色 `{agent_name}`")
            self.reply_card(message_id, json.dumps(card, ensure_ascii=False))
            return

        from ...slock_engine.card_templates.memory import build_memory_group_card

        memory = engine.memory.read_agent_memory(agent.agent_id)
        memory_items: list[dict] = []

        # Add key_knowledge entries
        if memory.key_knowledge:
            for line in memory.key_knowledge.strip().split("\n"):
                line = line.strip()
                if line:
                    memory_items.append({"category": "key_knowledge", "content": line[:200], "timestamp": None})

        # Add active_context entries
        if memory.active_context:
            for line in memory.active_context.strip().split("\n"):
                line = line.strip()
                if line:
                    memory_items.append({"category": "context", "content": line[:200], "timestamp": None})

        # Add role definition
        if memory.role:
            memory_items.append({"category": "role", "content": memory.role[:200], "timestamp": None})

        if not memory_items:
            from ...slock_engine.card_templates.common import build_empty_state_card
            card = build_empty_state_card("🧠 角色记忆", f"{agent.display_name} 暂无记忆数据")
            self.reply_card(message_id, json.dumps(card, ensure_ascii=False))
            return

        card = build_memory_group_card(
            agent_name=agent.name,
            agent_emoji=agent.emoji,
            memory_items=memory_items,
            channel_id=channel_id,
            agent_id=agent.agent_id,
        )
        self.reply_card(message_id, json.dumps(card, ensure_ascii=False))

    def show_memory_list(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        """Show memory summary for all agents in the channel."""
        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            from ...slock_engine.card_templates.common import build_error_state_card
            card = build_error_state_card("🧠 记忆列表", "当前没有活跃的 Slock 团队，请先 /slock activate")
            self.reply_card(message_id, json.dumps(card, ensure_ascii=False))
            return

        channel_id = engine.channel.channel_id if engine.channel else chat_id
        agents = engine.registry.list_agents(channel_id=channel_id)

        if not agents:
            from ...slock_engine.card_templates.common import build_empty_state_card
            card = build_empty_state_card("🧠 记忆列表", "当前团队暂无角色")
            self.reply_card(message_id, json.dumps(card, ensure_ascii=False))
            return

        from ...slock_engine.card_templates.common import build_card_wrapper

        rows: list[str] = []
        for agent in agents:
            memory = engine.memory.read_agent_memory(agent.agent_id)
            item_count = 0
            if memory.key_knowledge:
                item_count += len([line for line in memory.key_knowledge.split("\n") if line.strip()])
            if memory.active_context:
                item_count += len([line for line in memory.active_context.split("\n") if line.strip()])
            rows.append(f"| {agent.emoji} | {agent.name} | {item_count} 条 |")

        table_md = "| 头像 | 角色 | 记忆条数 |\n| --- | --- | --- |\n" + "\n".join(rows)
        elements: list[dict] = [{"tag": "markdown", "content": table_md}]

        card = build_card_wrapper(
            header_title="🧠 团队记忆概览",
            header_template="turquoise",
            elements=elements,
        )
        self.reply_card(message_id, json.dumps(card, ensure_ascii=False))

    # ------------------------------------------------------------------
    # Discussion management (stop/history/list)
    # ------------------------------------------------------------------

    def stop_discussion(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        """Stop the active discussion in the current channel."""
        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            from ...slock_engine.card_templates.common import build_error_state_card
            card = build_error_state_card("💬 停止讨论", "当前没有活跃的 Slock 团队，请先 /slock activate")
            self.reply_card(message_id, json.dumps(card, ensure_ascii=False))
            return

        channel_id = engine.channel.channel_id if engine.channel else chat_id

        # Find active discussion threads
        active_thread = None
        with engine._discussions_lock:
            threads = engine._active_discussions.get(channel_id, [])
            if threads:
                active_thread = threads[0]

        if not active_thread:
            self.reply_text(message_id, "💬 当前无进行中的讨论")
            return

        # Stop the discussion via DiscussionManager

        if engine._discussion_manager is not None:
            stopped_thread = engine._discussion_manager.stop_discussion(active_thread)
            engine._remove_discussion(channel_id, stopped_thread.thread_id)

            from ...slock_engine.card_templates.discussion import build_discussion_conclusion_card
            card = build_discussion_conclusion_card(
                thread_id=stopped_thread.thread_id,
                participants=getattr(stopped_thread, "participants", []),
                conclusion=getattr(stopped_thread, "conclusion", "") or "讨论已手动停止",
                total_rounds=getattr(stopped_thread, "current_round", 0),
                total_tokens=getattr(stopped_thread, "total_tokens", 0),
                status="manually_stopped",
                channel_id=channel_id,
            )
            self.reply_card(message_id, json.dumps(card, ensure_ascii=False))
        else:
            self.reply_text(message_id, "💬 讨论管理器未初始化，无法停止讨论")

    def show_discussion_history(self, message_id: str, chat_id: str, thread_id: str = "", project: Optional["ProjectContext"] = None):
        """Show discussion history for a specific thread."""
        if not thread_id:
            from ...slock_engine.card_templates.common import build_usage_hint_card
            card = build_usage_hint_card(
                "/discussion history <thread_id>",
                ["/discussion list", "/discussion history abc123"],
            )
            self.reply_card(message_id, json.dumps(card, ensure_ascii=False))
            return

        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            from ...slock_engine.card_templates.common import build_error_state_card
            card = build_error_state_card("💬 讨论历史", "当前没有活跃的 Slock 团队，请先 /slock activate")
            self.reply_card(message_id, json.dumps(card, ensure_ascii=False))
            return

        channel_id = engine.channel.channel_id if engine.channel else chat_id

        # Try to find in active discussions first
        thread = engine.find_active_discussion(channel_id, thread_id)

        # Try loading from persisted discussions if not active
        if not thread and engine._discussion_manager is not None:
            persisted = engine._discussion_manager.load_discussions(channel_id)
            for t in persisted:
                if getattr(t, "thread_id", "") == thread_id:
                    thread = t
                    break

        if not thread:
            from ...slock_engine.card_templates.common import build_empty_state_card
            card = build_empty_state_card("💬 讨论历史", f"未找到讨论 `{thread_id[:12]}`")
            self.reply_card(message_id, json.dumps(card, ensure_ascii=False))
            return

        from ...slock_engine.card_templates import build_discussion_history_card

        # Convert thread to history format expected by the card builder
        messages = getattr(thread, "messages", [])
        history_entry = {
            "topic_hash": getattr(thread, "thread_id", "")[:8],
            "title": getattr(thread, "topic", "") or "讨论",
            "participants": getattr(thread, "participants", []),
            "time": getattr(thread, "created_at", 0),
            "conclusion": getattr(thread, "conclusion", "") or "",
            "messages": [
                {"speaker": getattr(m, "speaker", ""), "content": getattr(m, "content", "")[:200]}
                for m in messages[-20:]
            ],
        }
        card = build_discussion_history_card(history=[history_entry], channel_id=channel_id)
        self.reply_card(message_id, json.dumps(card, ensure_ascii=False))

    def list_discussions(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        """List all discussions (active + recent persisted) in the channel."""
        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            from ...slock_engine.card_templates.common import build_error_state_card
            card = build_error_state_card("💬 讨论列表", "当前没有活跃的 Slock 团队，请先 /slock activate")
            self.reply_card(message_id, json.dumps(card, ensure_ascii=False))
            return

        channel_id = engine.channel.channel_id if engine.channel else chat_id

        # Collect active threads
        all_threads = []
        with engine._discussions_lock:
            active = engine._active_discussions.get(channel_id, [])
            all_threads.extend(active)

        # Load persisted threads
        if engine._discussion_manager is not None:
            persisted = engine._discussion_manager.load_discussions(channel_id)
            existing_ids = {getattr(t, "thread_id", "") for t in all_threads}
            for t in persisted:
                if getattr(t, "thread_id", "") not in existing_ids:
                    all_threads.append(t)

        if not all_threads:
            from ...slock_engine.card_templates.common import build_empty_state_card
            card = build_empty_state_card("💬 讨论列表", "当前暂无讨论记录")
            self.reply_card(message_id, json.dumps(card, ensure_ascii=False))
            return

        import time as _time

        from ...slock_engine.card_templates.common import build_card_wrapper

        rows: list[str] = []
        for thread in all_threads[:20]:
            tid = getattr(thread, "thread_id", "?")[:8]
            status = getattr(thread, "status", "unknown")
            status_label = status.value if hasattr(status, "value") else str(status)
            participants = getattr(thread, "participants", [])
            p_names = ", ".join(participants[:3]) if participants else "-"
            created = getattr(thread, "created_at", 0)
            ts = _time.strftime("%m-%d %H:%M", _time.localtime(created)) if created else "-"
            rows.append(f"| `{tid}` | {status_label} | {p_names} | {ts} |")

        table_md = "| ID | 状态 | 参与者 | 创建时间 |\n| --- | --- | --- | --- |\n" + "\n".join(rows)
        elements: list[dict] = [{"tag": "markdown", "content": table_md}]

        card = build_card_wrapper(
            header_title="💬 讨论列表",
            header_template="blue",
            elements=elements,
        )
        self.reply_card(message_id, json.dumps(card, ensure_ascii=False))

    # ------------------------------------------------------------------
    # Plan management
    # ------------------------------------------------------------------

    def list_plans(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        """List all collaboration plans in the current slock session."""
        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            from ...slock_engine.card_templates.common import build_error_state_card
            card = build_error_state_card("📋 协作计划", "当前没有活跃的 Slock 团队，请先 /slock activate")
            self.reply_card(message_id, json.dumps(card, ensure_ascii=False))
            return

        from ...slock_engine.card_templates.progress import build_progress_overview_card

        channel_id = engine.channel.channel_id if engine.channel else chat_id
        plans = engine.collaboration_orchestrator.list_active_plans(channel_id=channel_id)
        agents = engine.registry.list_agents(channel_id=channel_id)
        if not plans:
            from ...slock_engine.card_templates.common import build_empty_state_card
            card = build_empty_state_card(
                "📋 协作计划",
                "当前没有协作计划。\n\n任务创建后会自动触发协作规划。",
            )
            self.reply_card(message_id, json.dumps(card, ensure_ascii=False))
            return

        card = build_progress_overview_card(
            plans=plans,
            agents=agents,
            team_name=engine.channel.team_name if engine.channel else "",
            channel_id=channel_id,
        )
        self.reply_card(message_id, json.dumps(card, ensure_ascii=False))

    def show_plan_detail(self, message_id: str, chat_id: str, plan_id: str = "", project: Optional["ProjectContext"] = None):
        """Show detailed view of a specific collaboration plan."""
        if not plan_id:
            from ...slock_engine.card_templates.common import build_usage_hint_card
            card = build_usage_hint_card(
                "/plan <plan_id>",
                ["/plan list", "/plan abc123"],
            )
            self.reply_card(message_id, json.dumps(card, ensure_ascii=False))
            return

        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            from ...slock_engine.card_templates.common import build_error_state_card
            card = build_error_state_card("📋 协作计划", "当前没有活跃的 Slock 团队，请先 /slock activate")
            self.reply_card(message_id, json.dumps(card, ensure_ascii=False))
            return

        from ...slock_engine.card_templates.progress import build_task_overview_card
        from ...slock_engine.models import AgentStatus as AgentStatusEnum

        channel_id = engine.channel.channel_id if engine.channel else chat_id
        plan = engine.collaboration_orchestrator.get_plan(plan_id)
        if not plan:
            from ...slock_engine.card_templates.common import build_empty_state_card
            card = build_empty_state_card("📋 协作计划", f"未找到计划 `{plan_id[:12]}`，可能已结束或不存在")
            self.reply_card(message_id, json.dumps(card, ensure_ascii=False))
            return

        # Build agents with status for the task overview card
        agents: list[tuple] = []
        for agent in engine.registry.list_agents(channel_id=channel_id):
            status = engine.get_agent_status(agent.agent_id) or AgentStatusEnum.IDLE
            agents.append((agent, status))

        # Extract latest output from the last completed step
        latest_output_summary = ""
        if plan.steps:
            completed_steps = [s for s in plan.steps if s.get("status") == "DONE"]
            if completed_steps:
                last_step = completed_steps[-1]
                latest_output_summary = last_step.get("output_summary", "")[:200]

        # Gather discussion entries (last 5) from active discussions
        discussion_entries: list[dict] = []
        try:
            with engine._discussions_lock:
                active_threads = engine._active_discussions.get(channel_id, [])
            for thread in active_threads[:5]:
                for msg in getattr(thread, "messages", [])[-5:]:
                    discussion_entries.append({
                        "speaker": getattr(msg, "speaker", ""),
                        "content": getattr(msg, "content", "")[:100],
                        "timestamp": getattr(msg, "timestamp", 0),
                    })
        except Exception:
            pass
        discussion_entries = discussion_entries[-5:]

        # Gather timeline events (last 10) from plan step history
        timeline_events: list[dict] = []
        if plan.steps:
            for step in plan.steps:
                if step.get("started_at"):
                    timeline_events.append({
                        "event_type": "step_started",
                        "agent_id": step.get("agent_id", ""),
                        "timestamp": step.get("started_at", 0),
                        "detail": step.get("name", ""),
                    })
                if step.get("completed_at"):
                    timeline_events.append({
                        "event_type": "step_completed",
                        "agent_id": step.get("agent_id", ""),
                        "timestamp": step.get("completed_at", 0),
                        "detail": step.get("name", ""),
                    })
        timeline_events.sort(key=lambda e: e.get("timestamp", 0), reverse=True)
        timeline_events = timeline_events[:10]

        card = build_task_overview_card(
            plan=plan,
            agents=agents,
            channel_id=channel_id,
            latest_output_summary=latest_output_summary,
            discussion_entries=discussion_entries or None,
            timeline_events=timeline_events or None,
        )
        self.reply_card(message_id, json.dumps(card, ensure_ascii=False))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def stop_slock_engine(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        """Stop the slock engine, unregister managed chat, and clean up."""
        manager = self._get_engine_manager()

        # Deactivate the engine if it exists
        engine = manager.get_activated_engine(chat_id)
        if engine:
            if not self._check_slock_permission(engine, message_id, chat_id):
                return
            engine.deactivate()

        # Unregister managed chat so dispatcher stops routing to this engine
        manager.unregister_managed_chat(chat_id)

        self._safe_lifecycle_action(
            lambda: self._stop_engine_generic(message_id, chat_id, project),
            "stop", chat_id, message_id, project,
        )

    def _stop_single_agent(self, message_id: str, chat_id: str, value: dict):
        """Stop a single agent by agent_id from card action value.

        Falls back to full engine stop if agent_id is not provided (backwards compat).
        """
        agent_id = value.get("agent_id", "")

        if not agent_id:
            # Fallback: no agent_id means legacy card — stop the whole engine
            self.stop_slock_engine(message_id, chat_id)
            return

        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            self.send_text_to_chat(chat_id, "⚠️ 当前群组未激活 Slock 模式。")
            return

        if not self._check_slock_permission(engine, message_id, chat_id):
            return

        stopped = engine.stop_agent(agent_id)
        if stopped:
            self.send_text_to_chat(chat_id, "⏹ Agent 已停止，状态已重置为 IDLE。")
        else:
            self.send_text_to_chat(chat_id, "⚠️ 未找到该 Agent 或其已处于空闲状态。")

    def _expand_discussion(self, chat_id: str, value: dict):
        """Show full discussion thread content in response to expand button."""
        thread_id = value.get("thread_id", "")
        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            self.send_text_to_chat(chat_id, "⚠️ 当前群组未激活 Slock 模式。")
            return

        thread = SlockHandler._find_discussion_thread(engine, chat_id, thread_id)
        if not thread:
            self.send_text_to_chat(chat_id, "ℹ️ 讨论线程已结束或不存在。")
            return

        # Format all messages for display
        lines: list[str] = [f"💬 **讨论详情** (thread: `{thread_id[:12]}...`)\n"]
        for msg in thread.messages:
            lines.append(f"**{msg.sender_agent_id}** (R{msg.round_num}):\n{msg.content}\n")
        self.send_text_to_chat(chat_id, "\n".join(lines)[:4000])

    def _stop_discussion(self, chat_id: str, value: dict):
        """Manually stop an active discussion thread."""
        thread_id = value.get("thread_id", "")
        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            self.send_text_to_chat(chat_id, "⚠️ 当前群组未激活 Slock 模式。")
            return

        dm = getattr(engine, "_discussion_manager", None)
        if dm and thread_id:
            thread_obj = SlockHandler._find_discussion_thread(engine, chat_id, thread_id)
            if thread_obj:
                dm.stop_discussion(thread_obj)
                self.send_text_to_chat(chat_id, "⏹ 讨论已手动终止。")
                return
        self.send_text_to_chat(chat_id, "ℹ️ 讨论线程已结束或不存在。")

    @staticmethod
    def _find_discussion_thread(engine, chat_id: str, thread_id: str):
        """Resolve an active discussion thread across real engine and old test-double shapes."""
        finder = getattr(engine, "find_active_discussion", None)
        if callable(finder):
            try:
                candidate = finder(chat_id, thread_id)
            except Exception:
                candidate = None
            if getattr(candidate, "thread_id", None) == thread_id:
                return candidate

        active_discussions = getattr(engine, "_active_discussions", {})
        candidates = active_discussions.get(chat_id, [])
        if not isinstance(candidates, list):
            candidates = [candidates]
        for candidate in candidates:
            if getattr(candidate, "thread_id", None) == thread_id:
                return candidate
        return None

    # ------------------------------------------------------------------
    # Card action handler
    # ------------------------------------------------------------------

    def _refresh_status_card(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        """Rebuild and update the status panel card in-place."""
        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            self.send_text_to_chat(chat_id, "⚠️ 当前群组未激活 Slock 模式，无法刷新状态。")
            return
        team_name = engine.channel.team_name if engine.channel else ""
        status_card = engine.get_status_card(team_name=team_name)
        card_content = json.dumps(status_card, ensure_ascii=False)
        self.update_card(message_id, card_content)

    def _refresh_task_board_card(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        """Rebuild and update the task board card in-place."""
        from ...slock_engine.card_templates import build_task_board_card

        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            self.send_text_to_chat(chat_id, "⚠️ 当前群组未激活 Slock 模式，无法刷新任务看板。")
            return
        channel_id = engine.channel.channel_id if engine.channel else chat_id
        agents = engine.registry.list_agents(channel_id=channel_id)
        team_name = engine.channel.team_name if engine.channel else ""
        card = build_task_board_card(engine.tasks, agents, team_name=team_name, channel_id=channel_id)
        self.update_card(message_id, json.dumps(card, ensure_ascii=False))

    def _has_slock_permission(self, engine) -> bool:
        """Pure boolean check: is current operator admin or channel owner? No side effects.

        When ADMIN_USER_IDS is empty (bootstrap state), all users are permitted —
        consistent with the one-way /setadmin bootstrap contract.
        """
        from ...config import get_settings
        from ...thread.manager import get_current_sender_id

        operator_id = get_current_sender_id() or ""
        settings = get_settings()
        admin_ids = settings.admin_user_ids if hasattr(settings, "admin_user_ids") else frozenset()

        # Bootstrap state: no admin configured → permissive
        if not admin_ids:
            return True

        channel_owner_id = ""
        if engine.channel:
            channel_owner_id = getattr(engine.channel, "owner_id", "") or ""

        return (
            (operator_id and operator_id in admin_ids)
            or (operator_id and channel_owner_id and operator_id == channel_owner_id)
        )

    def _check_slock_permission(self, engine, message_id: str, chat_id: str) -> bool:
        """Check if current operator is admin or channel owner. Returns True if authorized."""
        if not self._has_slock_permission(engine):
            perm_msg = "⚠️ 权限不足，仅管理员或团队创建者可执行此操作。"
            if not self.reply_text(message_id, perm_msg):
                self.send_text_to_chat(chat_id, perm_msg)
            return False
        return True

    def _require_slock_permission(self, engine, action_type: str, *, allow_assignee: bool = False, task_id: str = "") -> dict | None:
        """Check permission and return rejection card if denied, None if allowed.

        Authorized: admin_user_ids | channel_owner | (task claimed_by if allow_assignee).
        When ADMIN_USER_IDS is empty (bootstrap state), all users are permitted.
        """
        from ...config import get_settings
        from ...thread.manager import get_current_sender_id

        settings = get_settings()
        sender_id = get_current_sender_id() or ""

        # Check admin
        admin_ids = settings.admin_user_ids if hasattr(settings, "admin_user_ids") else frozenset()

        # Bootstrap state: no admin configured → permissive
        if not admin_ids:
            return None  # allowed

        if sender_id and sender_id in admin_ids:
            return None  # allowed

        # Check channel owner
        if engine and engine.channel:
            if sender_id and sender_id == (getattr(engine.channel, "owner_id", "") or ""):
                return None  # allowed

        # Check task assignee
        if allow_assignee and task_id and engine:
            for task in engine.tasks:
                if task.task_id == task_id and task.claimed_by == sender_id:
                    return None  # allowed

        # Denied - return rejection toast
        return {"toast": {"type": "error", "content": "\u26d4 权限不足：仅管理员、群主或任务负责人可执行此操作"}}

    def _check_assign_rate_limit(self, engine, message_id: str, chat_id: str) -> bool:
        """Check rate-limit for task assignment. Admin/owner bypass. Returns True if allowed."""
        import time as _time

        from ...config import get_settings
        from ...thread.manager import get_current_sender_id

        operator_id = get_current_sender_id() or ""
        settings = get_settings()
        admin_ids = settings.admin_user_ids if hasattr(settings, "admin_user_ids") else frozenset()
        channel_owner_id = ""
        if engine.channel:
            channel_owner_id = getattr(engine.channel, "owner_id", "") or ""

        # Admin and owner bypass rate-limit
        is_privileged = (
            (operator_id and operator_id in admin_ids)
            or (operator_id and channel_owner_id and operator_id == channel_owner_id)
        )
        if is_privileged:
            return True

        # Rate-limit for regular users: sliding window of 60s
        rate_limit = settings.slock_assign_rate_limit
        tracker_key = f"{chat_id}:{operator_id}"
        now = _time.time()
        window = 60.0

        self._prune_assign_rate_limit_tracker(now, window)
        timestamps = self._rate_limit_tracker.get(tracker_key, [])

        if len(timestamps) >= rate_limit:
            self.reply_text(
                message_id,
                f"⚠️ 任务提交频率超限（每分钟最多 {rate_limit} 次），请稍后重试。",
            )
            self._rate_limit_tracker[tracker_key] = timestamps
            return False

        timestamps.append(now)
        self._rate_limit_tracker[tracker_key] = timestamps
        return True

    def _prune_assign_rate_limit_tracker(self, now: float, window: float) -> None:
        """Remove expired assign-rate-limit entries for inactive chat/sender pairs."""
        _GLOBAL_TTL = 3600.0  # Remove keys inactive for > 1 hour
        for key, timestamps in list(self._rate_limit_tracker.items()):
            if not timestamps or now - max(timestamps) > _GLOBAL_TTL:
                # Global TTL: no activity for over 1 hour, remove entirely
                self._rate_limit_tracker.pop(key, None)
                continue
            active = [ts for ts in timestamps if now - ts < window]
            if active:
                self._rate_limit_tracker[key] = active
            else:
                self._rate_limit_tracker.pop(key, None)

    def _check_discussion_permission(self, message_id: str, chat_id: str) -> bool:
        """Check permission for triggering a discussion.

        Admin/owner: always allowed (no rate-limit).
        Regular members: rate-limited to 3 discussion triggers per 5 minutes.
        Returns True if allowed, False if denied (with user notification).
        """
        import time as _time

        from ...config import get_settings
        from ...thread.manager import get_current_sender_id

        operator_id = get_current_sender_id() or ""
        settings = get_settings()
        admin_ids = settings.admin_user_ids if hasattr(settings, "admin_user_ids") else frozenset()

        # Try to get channel owner from engine
        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        channel_owner_id = ""
        if engine and engine.channel:
            channel_owner_id = getattr(engine.channel, "owner_id", "") or ""

        # Admin and owner bypass rate-limit
        is_privileged = (
            (operator_id and operator_id in admin_ids)
            or (operator_id and channel_owner_id and operator_id == channel_owner_id)
        )
        if is_privileged:
            return True

        # Rate-limit for regular users: sliding window of 5 minutes, max 3 triggers
        DISCUSSION_WINDOW = 300.0  # 5 minutes
        DISCUSSION_MAX = 3
        tracker_key = f"disc:{chat_id}:{operator_id}"
        now = _time.time()

        timestamps = self._rate_limit_tracker.get(tracker_key, [])
        timestamps = [t for t in timestamps if now - t < DISCUSSION_WINDOW]

        if len(timestamps) >= DISCUSSION_MAX:
            self.reply_text(
                message_id,
                f"⚠️ 讨论触发频率超限（每 5 分钟最多 {DISCUSSION_MAX} 次），请稍后重试。",
            )
            self._rate_limit_tracker[tracker_key] = timestamps
            return False

        timestamps.append(now)
        self._rate_limit_tracker[tracker_key] = timestamps
        return True

    def _check_queue_wait_timeout(self, future, start_time: float, card_message_id: str, chat_id: str) -> bool:
        """Check if task waited too long in queue. Returns True if timed out (caller should abort).

        If future is None (e.g. synchronous executor in tests where the work runs
        before submit() returns), skip timeout detection entirely — the task is
        already executing, so queue-wait is irrelevant.
        """
        import json as _json
        import time

        from ...config import get_settings as _get_settings

        if future is None:
            return False

        _settings = _get_settings()
        _enqueue_time = getattr(future, "enqueue_time", None)
        if _enqueue_time is None:
            _enqueue_time = start_time
        enqueue_elapsed = time.time() - _enqueue_time
        if enqueue_elapsed > _settings.slock_queue_wait_timeout:
            logger.warning(
                "Slock queue wait timeout (%.1fs > %ds) for chat %s",
                enqueue_elapsed, _settings.slock_queue_wait_timeout, chat_id,
            )
            timeout_card = _json.dumps({
                "schema": "2.0",
                "config": {"wide_screen_mode": True},
                "header": {"title": {"tag": "plain_text", "content": "⏱️ 排队超时"}, "template": "orange"},
                "body": {"elements": [{"tag": "markdown", "content": "消息在队列中等待过久，已自动取消。请稍后重试。"}]},
            }, ensure_ascii=False)
            if card_message_id:
                self.update_card(card_message_id, timeout_card)
            return True
        return False

    def _resolve_escalation(self, message_id: str, chat_id: str, value: dict):
        """Handle admin clicking Retry/Skip/Abort on an escalation card.

        Enhanced flow:
        (a) Get operator via thread-local
        (b) Permission check via _check_slock_permission
        (c) Resolution whitelist validation
        (d) Resolve via engine
        (e) Update card to resolved state
        (f) Fallback to text if card update fails
        (g) Handle duplicate clicks gracefully
        """
        from ...slock_engine.card_templates import build_resolved_escalation_card
        from ...thread.manager import get_current_sender_id

        escalation_id = value.get("escalation_id", "")
        resolution = value.get("resolution", "")

        if not escalation_id or not resolution:
            logger.warning("Escalation resolve missing params: escalation_id=%s resolution=%s", escalation_id, resolution)
            self.send_text_to_chat(chat_id, "⚠️ 升级请求处理参数缺失，请重试或联系管理员。")
            return

        # (a) Get operator identity
        operator_id = get_current_sender_id() or ""
        operator_display = resolve_display_name(operator_id, self.ctx.api_client_factory) if operator_id else ""

        # (b) Permission check — admin or team owner (unified)
        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            logger.warning("No active engine for escalation resolve in chat %s", chat_id)
            self.send_text_to_chat(chat_id, "⚠️ 当前群组未激活 Slock 模式，无法处理升级请求。")
            return

        if not self._check_slock_permission(engine, message_id, chat_id):
            return

        # (c) Resolution whitelist validation
        escalation = engine.get_escalation(escalation_id)
        if not escalation:
            self.send_text_to_chat(chat_id, f"⚠️ Escalation `{escalation_id}` 不存在。")
            return

        # Handle duplicate clicks — already resolved
        if escalation.resolved:
            self.send_text_to_chat(chat_id, f"ℹ️ 此升级请求已处理（{escalation.resolution}），无需重复操作。")
            return

        allowed_options = escalation.options or ["重试", "跳过", "中止"]
        resolution_stripped = resolution.strip()
        if resolution_stripped not in allowed_options:
            logger.warning(
                "Escalation resolve invalid resolution: '%s' not in %s",
                resolution, allowed_options,
            )
            self.send_text_to_chat(
                chat_id,
                f"⚠️ 无效的解决选项 `{resolution}`，允许的选项: {', '.join(allowed_options)}",
            )
            return

        # (d) Execute resolve
        resolved = engine.resolve_escalation(escalation_id, resolution_stripped)
        if not resolved:
            # Race condition: resolved between our check and this call
            self.send_text_to_chat(chat_id, "ℹ️ 此升级请求已处理，无需重复操作。")
            return

        # (e) Build resolved card and update in-place
        resolved_card = build_resolved_escalation_card(
            escalation,
            resolved_by=operator_display or operator_id,
            resolution=resolution_stripped,
            resolved_at=resolved.resolved_at,
            channel_id=chat_id,
        )
        card_json = json.dumps(resolved_card, ensure_ascii=False)
        card_updated = self.update_card(message_id, card_json)

        # (f) Fallback to text confirmation if card update fails
        if not card_updated:
            logger.error(
                "Failed to update escalation card: message_id=%s chat_id=%s",
                message_id, chat_id,
            )
            confirm_text = (
                f"✅ Escalation resolved: **{resolved.agent_name}** — {resolution_stripped}\n"
                f"Reason: {redact_sensitive(resolved.reason)}"
            )
            self.send_text_to_chat(chat_id, confirm_text)

        # (g) Trigger agent recovery based on resolution
        engine.resume_after_escalation(resolved)

    def _handle_clarify_confirm(self, open_message_id: str, open_chat_id: str, value: dict) -> None:
        """Handle "是，这是任务" button click from clarification card.

        Triggers task enqueue logic similar to auto-activate path.
        Validates that the action performer is the original message sender.
        """
        import json as _json

        from ...slock_engine.card_templates.queue_feedback import build_clarification_confirmed_card
        from ...thread.manager import get_current_sender_id

        message_preview = str(value.get("message_preview") or "")
        original_message_id = str(value.get("message_id") or "")
        original_sender_id = str(value.get("sender_id") or "")
        current_sender_id = get_current_sender_id() or ""

        # Sender verification: only original sender can confirm
        if original_sender_id and current_sender_id and original_sender_id != current_sender_id:
            logger.warning(
                "Clarify confirm rejected: sender mismatch (original=%s, current=%s)",
                original_sender_id, current_sender_id,
            )
            self.reply_text(open_message_id, "⚠️ 仅消息发送者可确认此操作。")
            return

        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(open_chat_id)

        # Build confirmation card first (will be shown regardless of path)
        confirm_card = build_clarification_confirmed_card(message_preview=message_preview)
        confirm_card_json = _json.dumps(confirm_card, ensure_ascii=False)

        if engine:
            # Engine already active — route the message to the engine
            logger.info(
                "Clarify confirm: engine already active, routing message chat=%s preview=%s",
                open_chat_id, message_preview[:50],
            )
            # Update card first to show confirmation
            self.update_card(open_message_id, confirm_card_json)
            # Then route the message to the engine
            project_id = value.get("project_id", "")
            project = self.project_manager.get_project_for_chat(project_id, open_chat_id) if project_id else None
            # Use original message_id if available, otherwise use the card message_id
            target_message_id = original_message_id or open_message_id
            self.handle_message(target_message_id, open_chat_id, message_preview, project)
        else:
            # Engine not active — trigger auto-activate path
            logger.info(
                "Clarify confirm: engine not active, triggering auto-activate chat=%s preview=%s",
                open_chat_id, message_preview[:50],
            )
            # Activate slock with the message as requirement
            project_id = value.get("project_id", "")
            project = self.project_manager.get_project_for_chat(project_id, open_chat_id) if project_id else None
            success = self.activate_slock(
                message_id=open_message_id,
                chat_id=open_chat_id,
                requirement=message_preview,
                project=project,
            )
            if not success:
                # If activation failed, at least show the confirmation card
                self.update_card(open_message_id, confirm_card_json)

    def _handle_clarify_ignore(self, open_message_id: str, open_chat_id: str, value: dict) -> None:
        """Handle "不是，只是聊天" button click from clarification card.

        Updates the card to "已忽略" state without creating any task.
        Validates that the action performer is the original message sender.
        """
        import json as _json

        from ...slock_engine.card_templates.queue_feedback import build_clarification_ignored_card
        from ...thread.manager import get_current_sender_id

        message_preview = str(value.get("message_preview") or "")
        original_sender_id = str(value.get("sender_id") or "")
        current_sender_id = get_current_sender_id() or ""

        # Sender verification: only original sender can ignore
        if original_sender_id and current_sender_id and original_sender_id != current_sender_id:
            logger.warning(
                "Clarify ignore rejected: sender mismatch (original=%s, current=%s)",
                original_sender_id, current_sender_id,
            )
            self.reply_text(open_message_id, "⚠️ 仅消息发送者可忽略此操作。")
            return

        logger.info(
            "Clarify ignore: user marked message as chat chat=%s preview=%s",
            open_chat_id, message_preview[:50],
        )

        # Build and send ignored card
        ignored_card = build_clarification_ignored_card(message_preview=message_preview)
        ignored_card_json = _json.dumps(ignored_card, ensure_ascii=False)
        self.update_card(open_message_id, ignored_card_json)

    def handle_card_action(self, open_message_id: str, open_chat_id: str, action_type: str, value: dict):
        """Handle slock_* card actions."""
        # Escalation resolve needs extra params from value — handle before standard dispatch
        if action_type == "slock_escalation_resolve":
            self._resolve_escalation(open_message_id, open_chat_id, value)
            return

        # Per-agent stop needs agent_id from value — handle before standard dispatch
        if action_type == "slock_stop_agent":
            self._stop_single_agent(open_message_id, open_chat_id, value)
            return

        if action_type == "slock_agent_follow_up":
            agent_name = value.get("agent_name") or value.get("agent_id") or "Agent"
            self.send_text_to_chat(open_chat_id, f"可以直接在群里 @ {agent_name} 继续追问。")
            return

        if action_type == "slock_agent_show_reasoning":
            manager = self._get_engine_manager()
            engine = manager.get_activated_engine(open_chat_id) or manager.get_active_engine(open_chat_id)
            agent_id = str(value.get("agent_id") or "")
            task_id = str(value.get("task_id") or "")
            rejection = self._require_slock_permission(engine, action_type, allow_assignee=True, task_id=task_id)
            if rejection:
                return rejection
            snapshot = engine.memory.read_agent_reasoning_snapshot(agent_id, task_id) if engine and agent_id and task_id else {}
            if snapshot:
                tool_label = snapshot.get("tool_name") or "unknown"
                model_label = snapshot.get("model_name") or "default"
                prompt_summary = redact_sensitive(str(snapshot.get("prompt_summary") or ""))[:1200]
                result_summary = redact_sensitive(str(snapshot.get("result_summary") or ""))[:1800]
                self.send_text_to_chat(
                    open_chat_id,
                    "🧠 **执行摘要**\n\n"
                    f"• Agent: `{agent_id[:8]}`\n"
                    f"• 工具/模型: `{tool_label}` / `{model_label}`\n\n"
                    f"**输入摘要**\n{prompt_summary or '(空)'}\n\n"
                    f"**输出摘要**\n{result_summary or '(空)'}",
                )
                return
            self.send_text_to_chat(open_chat_id, "当前 Agent 回复未保存可展示的执行摘要；可查看任务执行卡和消息归档。")
            return

        if action_type == "slock_agent_mark_done":
            task_id = value.get("task_id", "")
            manager = self._get_engine_manager()
            engine = manager.get_activated_engine(open_chat_id)
            rejection = self._require_slock_permission(engine, action_type, allow_assignee=True, task_id=task_id)
            if rejection:
                if isinstance(rejection, dict):
                    content = rejection.get("toast", {}).get("content", "权限不足")
                    self.send_text_to_chat(open_chat_id, content)
                    return rejection
                if not self._has_slock_permission(engine):
                    self.send_text_to_chat(open_chat_id, "⚠️ 权限不足，仅管理员或团队创建者可执行此操作。")
                    return rejection
            if not self._has_slock_permission(engine) and isinstance(rejection, dict):
                return rejection
            if task_id and engine:
                engine._force_complete_task(task_id)
            self.send_text_to_chat(open_chat_id, "✅ 已标记完成。")
            return

        # --- Task 20: Show agent memory (L1 snapshot) ---
        if action_type == "slock_agent_show_memory":
            manager = self._get_engine_manager()
            engine = manager.get_activated_engine(open_chat_id)
            agent_id = str(value.get("agent_id") or "")
            rejection = self._require_slock_permission(engine, action_type)
            if rejection:
                return rejection
            if engine and agent_id:
                memory = engine.memory.read_agent_memory(agent_id)
                if memory:
                    import json as _json

                    from src.slock_engine.card_templates import build_memory_display_card
                    agent = engine.registry.get(agent_id)
                    agent_name = agent.display_name if agent else agent_id[:8]
                    card = build_memory_display_card(memory, agent_name=agent_name)
                    self.send_card_to_chat(open_chat_id, _json.dumps(card, ensure_ascii=False))
                    return
            self.send_text_to_chat(open_chat_id, "该 Agent 暂无记忆记录。")
            return

        # --- Task 21: Show role switch card ---
        if action_type == "slock_agent_switch_role":
            manager = self._get_engine_manager()
            engine = manager.get_activated_engine(open_chat_id)
            agent_id = str(value.get("agent_id") or "")
            rejection = self._require_slock_permission(engine, action_type)
            if rejection:
                if isinstance(rejection, dict):
                    content = rejection.get("toast", {}).get("content", "权限不足")
                    self.send_text_to_chat(open_chat_id, content)
                    return rejection
                if not self._has_slock_permission(engine):
                    self.send_text_to_chat(open_chat_id, "⚠️ 权限不足，仅管理员或团队创建者可执行此操作。")
                    return rejection
            if not self._has_slock_permission(engine) and isinstance(rejection, dict):
                return rejection
            if engine and agent_id:
                channel_id = engine.channel.channel_id if engine.channel else ""
                agents = engine.registry.list_agents(channel_id=channel_id)
                available_roles = sorted({a.role for a in agents if a.role})
                # Add standard roles if not already present
                for std_role in ("coder", "reviewer", "writer", "tester", "planner", "architect"):
                    if std_role not in available_roles:
                        available_roles.append(std_role)
                import json as _json

                from src.slock_engine.card_templates import build_role_switch_card
                project_id = value.get("project_id", "")
                card = build_role_switch_card(
                    roles=available_roles,
                    agent_id=agent_id,
                    channel_id=channel_id,
                    project_id=project_id,
                )
                self.send_card_to_chat(open_chat_id, _json.dumps(card, ensure_ascii=False))
            else:
                self.send_text_to_chat(open_chat_id, "⚠️ 未找到活跃引擎或 Agent。")
            return

        # --- Task 22: Confirm role switch ---
        if action_type == "slock_confirm_switch_role":
            manager = self._get_engine_manager()
            engine = manager.get_activated_engine(open_chat_id)
            agent_id = str(value.get("agent_id") or "")
            target_role = str(value.get("target_role") or "")
            rejection = self._require_slock_permission(engine, action_type)
            if rejection:
                return rejection
            if engine and agent_id and target_role:
                agent = engine.registry.get(agent_id)
                if agent:
                    old_role = agent.role
                    agent.role = target_role
                    engine.registry.update(agent)
                    # Update memory role field
                    memory = engine.memory.read_agent_memory(agent_id)
                    if memory:
                        memory.role = f"{target_role}: {agent.system_prompt[:200]}" if agent.system_prompt else target_role
                        engine.memory.write_agent_memory(agent_id, memory)
                    self.send_text_to_chat(
                        open_chat_id,
                        f"🎭 **{agent.display_name}** 角色已切换: `{old_role}` → `{target_role}`",
                    )
                    return
            self.send_text_to_chat(open_chat_id, "⚠️ 角色切换失败，请重试。")
            return

        if action_type == "slock_confirm_discussion":
            thread_id = str(value.get("thread_id") or "")
            trust_type = str(value.get("trust_type") or "")
            manager = self._get_engine_manager()
            engine = manager.get_activated_engine(open_chat_id)
            if not engine:
                self.send_text_to_chat(open_chat_id, "⚠️ 未找到活跃 Slock 引擎。")
                return
            engine.confirm_discussion(thread_id, trust_type=trust_type)
            self.send_text_to_chat(open_chat_id, "✅ 讨论已启动。")
            return

        if action_type == "slock_cancel_discussion":
            thread_id = str(value.get("thread_id") or "")
            manager = self._get_engine_manager()
            engine = manager.get_activated_engine(open_chat_id)
            if not engine:
                self.send_text_to_chat(open_chat_id, "⚠️ 未找到活跃 Slock 引擎。")
                return
            engine.cancel_discussion(thread_id)
            self.send_text_to_chat(open_chat_id, "✅ 已取消讨论。")
            return

        # --- Tasks 26-28: Dissolve confirmation & undo ---
        if action_type == "slock_confirm_dissolve":
            team_name = str(value.get("team_name") or "")
            manager = self._get_engine_manager()
            engine = manager.find_team(team_name) if team_name else manager.get_activated_engine(open_chat_id)
            if engine and engine.channel:
                if not self._check_slock_permission(engine, open_message_id, open_chat_id):
                    return
                # Save snapshot for undo (30s TTL)
                from src.slock_engine.models import TeamSnapshot
                snapshot = TeamSnapshot(
                    channel_id=engine.channel.channel_id,
                    team_name=engine.channel.team_name or engine.channel.name or "",
                    owner_id=engine.channel.owner_id or "",
                    channel=engine.channel,
                    agent_ids=[a.agent_id for a in engine.registry.list_agents(channel_id=engine.channel.channel_id)],
                )
                if not hasattr(self, "_dissolve_snapshots"):
                    self._dissolve_snapshots: dict[str, TeamSnapshot] = {}
                self._dissolve_snapshots[snapshot.channel_id] = snapshot

                target_chat_id = engine.channel.channel_id
                engine.deactivate()
                manager.unregister_managed_chat(target_chat_id)
                manager.remove(target_chat_id, engine.root_path)
                self.send_text_to_chat(
                    open_chat_id,
                    f"✅ 团队 **{snapshot.team_name}** 已解散。30 秒内可点击撤销恢复。",
                )
            else:
                self.send_text_to_chat(open_chat_id, "⚠️ 未找到目标团队。")
            return

        if action_type == "slock_undo_dissolve":
            channel_id = str(value.get("channel_id") or "")
            snapshots = getattr(self, "_dissolve_snapshots", {})
            snapshot = snapshots.pop(channel_id, None) if channel_id else None
            if snapshot and (time.time() - snapshot.created_at) <= 30:
                self.send_text_to_chat(
                    open_chat_id,
                    f"↩️ 团队 **{snapshot.team_name}** 解散已撤销（本地状态恢复）。如飞书群已删除需手动重建。",
                )
            else:
                self.send_text_to_chat(open_chat_id, "⚠️ 撤销已过期或快照不存在。")
            return

        # --- Task 24: Form submissions from command panel ---
        if action_type == "slock_form_new_team":
            team_name = str(value.get("team_name") or "").strip()
            if not team_name:
                self.send_text_to_chat(open_chat_id, "⚠️ 请输入团队名称。")
                return
            project_id = value.get("project_id", "")
            project = self.project_manager.get_project_for_chat(project_id, open_chat_id) if project_id else None
            self.create_team(open_message_id, open_chat_id, team_name, project)
            return

        if action_type == "slock_form_new_role":
            role_name = str(value.get("role_name") or "").strip()
            role_type = str(value.get("role_type") or "coder").strip()
            agent_type = str(value.get("agent_type") or "coco").strip()
            if not role_name:
                self.send_text_to_chat(open_chat_id, "⚠️ 请输入角色名称。")
                return
            project_id = value.get("project_id", "")
            project = self.project_manager.get_project_for_chat(project_id, open_chat_id) if project_id else None
            # Build params string for create_role
            params_str = f"{role_name} --type={role_type} --agent={agent_type}"
            self.create_role(open_message_id, open_chat_id, params_str, project)
            return

        if action_type == "slock_form_council":
            topic = str(value.get("topic") or "").strip()
            if not topic:
                self.send_text_to_chat(open_chat_id, "⚠️ 请输入评审议题。")
                return
            project_id = value.get("project_id", "")
            project = self.project_manager.get_project_for_chat(project_id, open_chat_id) if project_id else None
            self.run_council(open_message_id, open_chat_id, topic, project)
            return

        if action_type == "slock_form_discuss":
            topic = str(value.get("topic") or "").strip()
            if not topic:
                self.send_text_to_chat(open_chat_id, "⚠️ 请输入讨论主题。")
                return
            project_id = value.get("project_id", "")
            project = self.project_manager.get_project_for_chat(project_id, open_chat_id) if project_id else None
            self._trigger_nli_discussion(open_message_id, open_chat_id, topic, {}, project)
            return

        if action_type == "slock_discussion_expand":
            self._expand_discussion(open_chat_id, value)
            return

        if action_type == "slock_discussion_stop":
            self._stop_discussion(open_chat_id, value)
            return

        # --- Tasks 19+20: Collaboration plan actions & user intervention ---
        if action_type == "slock_plan_approve":
            plan_id = value.get("plan_id", "")
            manager = self._get_engine_manager()
            engine = manager.get_activated_engine(open_chat_id)
            if engine and plan_id:
                if not self._check_slock_permission(engine, open_message_id, open_chat_id):
                    return
                success = engine.collaboration_orchestrator.approve_plan(plan_id)
                if success:
                    import json as _json

                    from src.slock_engine.card_templates.progress import build_collaboration_plan_card
                    plan = engine.collaboration_orchestrator.get_plan(plan_id)
                    if plan:
                        channel_id = engine.channel.channel_id if engine.channel else ""
                        agents = engine.registry.list_agents(channel_id=channel_id)
                        card = build_collaboration_plan_card(plan, agents, channel_id=channel_id)
                        self.update_card(open_message_id, _json.dumps(card, ensure_ascii=False))
            return

        if action_type == "slock_plan_cancel":
            plan_id = value.get("plan_id", "")
            manager = self._get_engine_manager()
            engine = manager.get_activated_engine(open_chat_id)
            if engine and plan_id:
                if not self._check_slock_permission(engine, open_message_id, open_chat_id):
                    return
                engine.collaboration_orchestrator.cancel_plan(plan_id)
            return

        if action_type == "slock_progress_refresh":
            manager = self._get_engine_manager()
            engine = manager.get_activated_engine(open_chat_id)
            if engine:
                import json as _json

                from src.slock_engine.card_templates.progress import build_progress_overview_card
                channel_id = engine.channel.channel_id if engine.channel else ""
                plans = engine.collaboration_orchestrator.list_active_plans(channel_id)
                agents = engine.registry.list_agents(channel_id=channel_id)
                card = build_progress_overview_card(plans, agents, channel_id=channel_id)
                self.send_card_to_chat(open_chat_id, _json.dumps(card, ensure_ascii=False))
            return

        if action_type == "slock_user_intervention":
            plan_id = value.get("plan_id", "")
            message = value.get("message", "") or value.get("supplement_content", "")
            manager = self._get_engine_manager()
            engine = manager.get_activated_engine(open_chat_id)
            if not engine or not plan_id:
                self.send_text_to_chat(open_chat_id, "⚠️ 引擎未激活或缺少 plan_id。")
                return
            if not self._check_slock_permission(engine, open_message_id, open_chat_id):
                return

            plan = engine.collaboration_orchestrator.get_plan(plan_id)
            if not plan:
                self.send_text_to_chat(open_chat_id, "⚠️ 未找到该计划。")
                return

            # If no message provided, pause the plan and prompt user for input
            if not message:
                engine.collaboration_orchestrator.pause_plan(plan_id)
                self.send_text_to_chat(
                    open_chat_id,
                    "⏸ 计划已暂停。请在讨论面板中输入补充信息后点击发送，或使用 ▶️ 恢复 按钮继续。",
                )
                return

            # Inject intervention message into current step's agent context
            current = plan.current_step
            if current and current.agent_id:
                engine.memory.update_agent_context(
                    current.agent_id,
                    f"[用户干预] {message[:200]}",
                )
                self.send_text_to_chat(
                    open_chat_id,
                    f"✅ 已将干预信息注入 Agent `{current.agent_id[:8]}` 上下文。",
                )
                # Resume if paused
                from ...slock_engine.models import CollaborationPlanStatus
                if plan.status == CollaborationPlanStatus.PAUSED:
                    engine.collaboration_orchestrator.resume_plan(plan_id)
                    self.send_text_to_chat(open_chat_id, "▶️ 计划已恢复执行。")
            else:
                self.send_text_to_chat(open_chat_id, "⚠️ 当前步骤无执行中的 Agent，无法注入干预信息。")
            return

        if action_type == "slock_plan_supplement":
            plan_id = value.get("plan_id", "")
            # 兼容读取: 优先 _form_value 子字典，回退顶层
            form_value = value.get("_form_value", {})
            content = (form_value.get("supplement_content") or value.get("supplement_content", "")).strip()
            manager = self._get_engine_manager()
            engine = manager.get_activated_engine(open_chat_id)
            if not engine or not plan_id:
                return
            if not content:
                return {"toast": {"type": "error", "content": "请输入补充信息内容"}}
            # Permission check
            rejection = self._require_slock_permission(engine, action_type)
            if rejection:
                return rejection
            # Text length check
            if len(content) > 2000:
                return {"toast": {"type": "error", "content": "补充内容过长，请限制在 2000 字符以内"}}
            # Sensitive content filter
            import re as _re
            if _re.search(r'(token|key|secret|password|credential)[=:]\s*\S{8,}', content, _re.I):
                return {"toast": {"type": "error", "content": "检测到疑似敏感凭据信息，请移除后重试"}}
            plan = engine.collaboration_orchestrator.get_plan(plan_id)
            if not plan:
                return
            # Inject to all collaborating agents in this plan
            agent_ids = {step.agent_id for step in plan.steps if step.agent_id}
            if agent_ids:
                supplement_text = f"[用户补充] {content[:500]}"
                for agent_id in agent_ids:
                    engine.memory.update_agent_context(agent_id, supplement_text)
                self.send_text_to_chat(
                    open_chat_id,
                    f"✅ 补充信息已注入 {len(agent_ids)} 个协作 Agent 上下文。",
                )
            else:
                self.send_text_to_chat(open_chat_id, "⚠️ 计划中暂无已分配的 Agent。")
            return

        # --- Collaboration plan detail / pause / resume ---
        if action_type == "slock_show_plan_detail":
            plan_id = value.get("plan_id", "")
            manager = self._get_engine_manager()
            engine = manager.get_activated_engine(open_chat_id)
            if engine and plan_id:
                from ...slock_engine.card_templates.progress import build_collaboration_plan_card
                plan = engine.collaboration_orchestrator.get_plan(plan_id)
                if plan:
                    channel_id = engine.channel.channel_id if engine.channel else ""
                    agents = engine.registry.list_agents(channel_id=channel_id)
                    card = build_collaboration_plan_card(plan, agents, channel_id=channel_id)
                    self.send_card_to_chat(open_chat_id, json.dumps(card, ensure_ascii=False))
                    return
            self.send_text_to_chat(open_chat_id, "⚠️ 未找到该计划。")
            return

        if action_type == "slock_pause_plan":
            plan_id = value.get("plan_id", "")
            manager = self._get_engine_manager()
            engine = manager.get_activated_engine(open_chat_id)
            if engine and plan_id:
                if not self._check_slock_permission(engine, open_message_id, open_chat_id):
                    return
                success = engine.collaboration_orchestrator.pause_plan(plan_id)
                if success:
                    self.send_text_to_chat(open_chat_id, f"⏸ 计划 `{plan_id[:8]}` 已暂停。")
                    return
            self.send_text_to_chat(open_chat_id, "⚠️ 暂停失败，计划不存在或状态不允许。")
            return

        if action_type == "slock_resume_plan":
            plan_id = value.get("plan_id", "")
            manager = self._get_engine_manager()
            engine = manager.get_activated_engine(open_chat_id)
            if engine and plan_id:
                if not self._check_slock_permission(engine, open_message_id, open_chat_id):
                    return
                success = engine.collaboration_orchestrator.resume_plan(plan_id)
                if success:
                    self.send_text_to_chat(open_chat_id, f"▶️ 计划 `{plan_id[:8]}` 已恢复执行。")
                    return
            self.send_text_to_chat(open_chat_id, "⚠️ 恢复失败，计划不存在或状态不允许。")
            return

        # --- Role info from card button ---
        if action_type == "slock_role_info":
            agent_id = value.get("agent_id", "")
            manager = self._get_engine_manager()
            engine = manager.get_activated_engine(open_chat_id)
            if engine and agent_id:
                agent = engine.registry.get(agent_id)
                if agent:
                    self.show_role_info(open_message_id, open_chat_id, agent.name)
                    return
            self.send_text_to_chat(open_chat_id, "⚠️ 未找到该角色。")
            return

        # --- Task board actions ---
        if action_type == "slock_new_task":
            content = value.get("content", "")
            if not content:
                self.send_text_to_chat(open_chat_id, "⚠️ 请输入任务内容。")
                return
            manager = self._get_engine_manager()
            engine = manager.get_activated_engine(open_chat_id)
            if engine:
                self.assign_task(open_message_id, open_chat_id, content)
            return

        if action_type == "slock_dispatch_tasks":
            manager = self._get_engine_manager()
            engine = manager.get_activated_engine(open_chat_id)
            if engine:
                engine.dispatch_pending_tasks()
                self.send_text_to_chat(open_chat_id, "✅ 已派发待处理任务。")
            return

        if action_type == "slock_show_task_board":
            manager = self._get_engine_manager()
            engine = manager.get_activated_engine(open_chat_id)
            if engine:
                from ...slock_engine.card_templates import build_task_board_card
                channel_id = engine.channel.channel_id if engine.channel else ""
                agents = engine.registry.list_agents(channel_id=channel_id)
                card = build_task_board_card(
                    tasks=engine.tasks,
                    agents=agents,
                    channel_id=channel_id,
                )
                self.send_card_to_chat(open_chat_id, json.dumps(card, ensure_ascii=False))
            return

        if action_type == "slock_assign_task_to_agent":
            task_id = value.get("task_id", "")
            agent_id = value.get("agent_id", "")
            task_content = value.get("task_content", "")
            manager = self._get_engine_manager()
            engine = manager.get_activated_engine(open_chat_id)
            if not engine or not agent_id:
                self.send_text_to_chat(open_chat_id, "⚠️ 引擎未激活或缺少 agent_id。")
                return

            # Case 1: Form submission with task_content → create + assign
            if task_content and not task_id:
                new_task = engine.create_and_assign_task(task_content.strip(), agent_id)
                if new_task:
                    self.send_text_to_chat(
                        open_chat_id,
                        f"✅ 任务已创建并分配给 `{agent_id[:8]}`。",
                    )
                else:
                    self.send_text_to_chat(open_chat_id, "⚠️ 任务创建失败。")
                return

            # Case 2: Existing task_id → reassign
            if task_id:
                success = engine.assign_task_to_agent(task_id, agent_id)
                if success:
                    self.send_text_to_chat(open_chat_id, f"✅ 任务已分配给 `{agent_id[:8]}`。")
                else:
                    self.send_text_to_chat(open_chat_id, "⚠️ 任务分配失败。")
                return

            self.send_text_to_chat(open_chat_id, "⚠️ 缺少任务内容或任务 ID。")
            return

        # --- Task 9: Clarification card button handlers ---
        if action_type == "slock_clarify_confirm":
            self._handle_clarify_confirm(open_message_id, open_chat_id, value)
            return

        if action_type == "slock_clarify_ignore":
            self._handle_clarify_ignore(open_message_id, open_chat_id, value)
            return

        # --- Hub card button routing (slock_hub_cmd) ---
        if action_type == "slock_hub_cmd":
            cmd_text = str(value.get("cmd") or "").strip()
            if not cmd_text:
                self.send_text_to_chat(open_chat_id, "⚠️ 无效的命令。")
                return
            project_id = value.get("project_id", "")
            project = self.project_manager.get_project_for_chat(project_id, open_chat_id) if project_id else None
            self.handle_slock_command(open_message_id, open_chat_id, cmd_text, project)
            return

        # --- Command panel button routing (slock_cmd_* prefix) ---
        if action_type.startswith("slock_cmd_"):
            self._dispatch_cmd_panel_action(open_message_id, open_chat_id, action_type, value)
            return

        project_id = value.get("project_id", "")
        target_project = (
            self.project_manager.get_project_for_chat(project_id, open_chat_id) if project_id else None
        )

        slock_actions = {
            "slock_stop": self.stop_slock_engine,
            "slock_refresh_status": self._refresh_status_card,
            "slock_refresh_task_board": self._refresh_task_board_card,
        }

        if action_type not in slock_actions:
            logger.warning("Unhandled slock card action: %s", action_type)

        self._dispatch_standard_card_action(CardActionContext(
            open_message_id=open_message_id,
            open_chat_id=open_chat_id,
            action_type=action_type,
            value=value,
            prefix="slock",
            action_map=slock_actions,
            toggle_log_method=self._toggle_log,
            switch_mode_method=self._switch_card_mode,
            toggle_ac_method=self._toggle_ac,
            project=target_project,
        ))

    # ------------------------------------------------------------------
    # Command panel button dispatch
    # ------------------------------------------------------------------

    def _dispatch_cmd_panel_action(
        self, message_id: str, chat_id: str, action_type: str, value: dict
    ) -> None:
        """Route slock_cmd_* button actions to existing handler methods."""
        project = None
        project_id = value.get("project_id", "")
        if project_id:
            project = self.project_manager.get_project_for_chat(project_id, chat_id)

        # --- Task 25: Permission check for destructive actions ---
        _PERM_REQUIRED = {"slock_cmd_dissolve_team", "slock_cmd_stop"}
        if action_type in _PERM_REQUIRED:
            manager = self._get_engine_manager()
            engine = manager.get_activated_engine(chat_id)
            if engine and not self._check_slock_permission(engine, message_id, chat_id):
                return

        # --- Task 26: Dissolve with confirmation card ---
        if action_type == "slock_cmd_dissolve_team":
            manager = self._get_engine_manager()
            engine = manager.get_activated_engine(chat_id)
            if not engine or not engine.channel:
                self.send_text_to_chat(chat_id, "⚠️ 当前没有活跃团队可解散。")
                return
            team_name = engine.channel.team_name or engine.channel.name or "当前团队"
            import json as _json
            confirm_card = {
                "schema": "2.0",
                "config": {"wide_screen_mode": True},
                "header": {"title": {"tag": "plain_text", "content": "⚠️ 确认解散团队"}, "template": "red"},
                "body": {"elements": [
                    {"tag": "markdown", "content": f"即将解散团队 **{team_name}**，此操作将：\n- 停止所有 Agent\n- 删除飞书群\n- 清除运行时状态\n\n确认继续？"},
                    {"tag": "action", "actions": [
                        {"tag": "button", "text": {"tag": "plain_text", "content": "确认解散"},
                         "type": "danger",
                         "value": {"action": "slock_confirm_dissolve", "team_name": team_name, "project_id": project_id},
                         "action_type": "slock_confirm_dissolve"},
                        {"tag": "button", "text": {"tag": "plain_text", "content": "取消"},
                         "type": "default",
                         "value": {"action": "noop"},
                         "action_type": "slock_noop"},
                    ]},
                ]},
            }
            self.send_card_to_chat(chat_id, _json.dumps(confirm_card, ensure_ascii=False))
            return

        # --- Task 23: Discuss routing ---
        if action_type == "slock_cmd_discuss":
            topic = str(value.get("topic") or "").strip()
            if not topic:
                # Task 29: send hint for empty params
                self.send_text_to_chat(chat_id, "💡 请输入讨论主题，例如: `/slock discuss 方案对比`")
                return
            self._trigger_nli_discussion(message_id, chat_id, topic, {}, project)
            return

        # --- Task 23: Council routing (empty topic → hint) ---
        if action_type == "slock_cmd_council":
            topic = str(value.get("topic") or "").strip()
            if not topic:
                self.send_text_to_chat(chat_id, "💡 请输入评审议题，例如: `/council 方案是否可行`")
                return
            self.run_council(message_id, chat_id, topic, project)
            return

        # --- Task 22: Extended panel routing ---
        if action_type == "slock_cmd_panel_extended":
            import json as _json

            from ...slock_engine.card_templates import build_command_panel_extended_card
            extended_card = build_command_panel_extended_card(channel_id=chat_id, project_id=project_id)
            self.send_card_to_chat(chat_id, _json.dumps(extended_card, ensure_ascii=False))
            return

        # --- Task 21: Memory routing (empty target → hint) ---
        if action_type == "slock_cmd_memory":
            target = str(value.get("target") or "").strip()
            if not target:
                self.send_text_to_chat(chat_id, "💡 请指定角色，例如: `/memory @coder` 或 `/memory coder`")
                return
            # Delegate to handle_slock_command with /memory syntax
            self.handle_slock_command(message_id, chat_id, f"/memory {target}", project)
            return

        # --- Task 20: Parameter-required actions with hints ---
        if action_type == "slock_cmd_task_assign":
            self.send_text_to_chat(chat_id, "💡 请使用命令分配任务，例如: `/task assign 修复登录bug @coder`")
            return
        if action_type == "slock_cmd_role_info":
            self.send_text_to_chat(chat_id, "💡 请指定角色，例如: `/role info coder`")
            return
        if action_type == "slock_cmd_role_remove":
            self.send_text_to_chat(chat_id, "💡 请指定要移除的角色，例如: `/role remove coder`")
            return
        if action_type == "slock_cmd_team_status":
            self.send_text_to_chat(chat_id, "💡 请指定团队，例如: `/team status 前端团队` 或直接使用 `/slock status`")
            return

        # --- Command fix suggestion: execute the suggested command ---
        if action_type == "slock_cmd_fix":
            fix_command = str(value.get("fix_command") or "").strip()
            if fix_command:
                self.handle_slock_command(message_id, chat_id, fix_command, project)
            else:
                self.send_text_to_chat(chat_id, "⚠️ 修正命令为空，请手动输入命令")
            return

        _CMD_DISPATCH: dict[str, callable] = {
            "slock_cmd_team_list": lambda: self.list_teams(message_id, chat_id, project),
            "slock_cmd_new_team": lambda: self.create_team(message_id, chat_id, "", project),
            "slock_cmd_role_list": lambda: self.list_roles(message_id, chat_id, project),
            "slock_cmd_new_role": lambda: self.create_role(message_id, chat_id, "", project),
            "slock_cmd_task_list": lambda: self.list_tasks(message_id, chat_id, project),
            "slock_cmd_task_status": lambda: self.show_task_status(message_id, chat_id, project),
            "slock_cmd_status": lambda: self._refresh_status_card(message_id, chat_id, value),
        }

        handler = _CMD_DISPATCH.get(action_type)
        if handler:
            handler()
        else:
            self.send_text_to_chat(chat_id, f"未知的面板操作: {action_type}")

    # ------------------------------------------------------------------
    # Static command detection
    # ------------------------------------------------------------------

    @staticmethod
    def is_slock_command(text: str, chat_id: str | None = None, manager=None) -> bool:
        """Check if text is any slock-related command."""
        return is_slock_command(text, chat_id=chat_id, manager=manager)
