"""Slock Engine handler — multi-Agent mouthpiece collaboration engine."""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import threading
from typing import TYPE_CHECKING, Optional

from ...acp.helper import fetch_acp_models
from ...card import CardBuilder
from ...card.actions import dispatch as action_ids
from ...model_selection import is_default_model_option
from ...slock_engine.bounded_executor import QueueFullError
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


# ---------------------------------------------------------------------------
# Module-level singleton NLI event loop (daemon thread)
# ---------------------------------------------------------------------------

_NLI_LOOP: asyncio.AbstractEventLoop | None = None
_NLI_LOOP_LOCK = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock


def _get_nli_loop() -> asyncio.AbstractEventLoop:
    """Get or create the singleton NLI event loop running in a daemon thread.

    Thread-safe: uses a module-level lock to ensure only one loop is created.
    If the loop has stopped (e.g., due to an unhandled exception), it will be
    recreated automatically.
    """
    global _NLI_LOOP
    with _NLI_LOOP_LOCK:
        if _NLI_LOOP is not None and _NLI_LOOP.is_running():
            return _NLI_LOOP

        # Create a new event loop in a daemon thread
        loop = asyncio.new_event_loop()

        def _run_loop():
            asyncio.set_event_loop(loop)
            loop.run_forever()

        t = threading.Thread(target=_run_loop, name="slock_nli_loop", daemon=True)
        t.start()
        _NLI_LOOP = loop
        return _NLI_LOOP


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
        from ...slock_engine.engine import SlockEngineCallbacks

        def on_agent_wake(agent):
            logger.debug("Slock agent waking: %s in chat %s", agent.name, chat_id)

        def on_agent_running(agent, msg):
            logger.debug("Slock agent running: %s task=%s", agent.name, msg[:80])

        def on_agent_done(agent, result):
            logger.debug("Slock agent done: %s result_len=%d", agent.name, len(result))

        def on_error(err_msg):
            logger.error("Slock engine error in chat %s: %s", chat_id, err_msg)

        def on_escalation(esc):
            """Send escalation card to chat and write back message_id."""
            manager = self._get_engine_manager()
            engine = manager.get_active_engine(chat_id)
            if not engine:
                logger.warning("on_escalation: engine not found for chat %s", chat_id)
                return
            card = engine.get_escalation_card(esc)
            if not card:
                logger.warning("on_escalation: failed to build card for esc %s", esc.escalation_id)
                return
            import json as _json
            card_json = _json.dumps(card) if isinstance(card, dict) else card
            sent_msg_id = self.send_card_to_chat(chat_id, card_json)
            if sent_msg_id:
                esc.card_message_id = sent_msg_id
            else:
                logger.warning("on_escalation: send_card_to_chat returned None for esc %s", esc.escalation_id)

        def on_card_send(card):
            """Send a card to the chat and return the message_id."""
            import json as _json
            card_json = _json.dumps(card) if isinstance(card, dict) else card
            return self.send_card_to_chat(chat_id, card_json)

        def on_card_update(msg_id, card):
            """Update an existing card by message_id."""
            import json as _json
            card_json = _json.dumps(card) if isinstance(card, dict) else card
            return self.update_card(msg_id, card_json)

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
            SlockCommandAction.TASK_ASSIGN: lambda: self.assign_task(message_id, chat_id, cmd.args, cmd.target, project),
            SlockCommandAction.TASK_STATUS: lambda: self.show_task_status(message_id, chat_id, project),
            SlockCommandAction.DISCUSSION: lambda: self._trigger_nli_discussion(message_id, chat_id, cmd.args, {}, project),
            SlockCommandAction.COUNCIL: lambda: self.run_council(message_id, chat_id, cmd.args, project),
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
                logger.error("Slock _execute_async error: %s", repr(e), exc_info=True)
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
        except (QueueFullError, RuntimeError) as e:
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
        intent_result = None
        try:
            # First try synchronous fast path (no LLM, no async)
            fast_result = self._intent_router.fast_classify(text or "")
            if fast_result is not None:
                intent_result = fast_result
            else:
                # Fall back to async LLM classification via the singleton NLI loop
                nli_loop = _get_nli_loop()
                coro = self._classify_with_timeout(text or "")
                future = asyncio.run_coroutine_threadsafe(coro, nli_loop)
                intent_result = future.result(timeout=self.ctx.settings.slock_nli_timeout + 0.2)
        except Exception as nli_exc:
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

        # Priority 4: Smart routing — engine.execute() (fallback for UNKNOWN/low confidence)
        self._execute_routed_message(engine, message_id, chat_id, text, project, target_agent=None)

    async def _classify_with_timeout(self, text: str):
        """Run NLI classification with timeout protection.

        Runs inside the dedicated _NLI_LOOP event loop thread.
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
        """Execute a message routed to a specific agent or via smart routing."""
        agent_used = None

        def _execute():
            nonlocal agent_used
            callbacks = self._create_callbacks(message_id, chat_id, project, engine.engine_name, engine.root_path)
            if target_agent:
                agent_used = target_agent
                return engine._execute_agent(target_agent, text, callbacks)
            channel_id = engine.channel.channel_id if engine.channel else chat_id
            agents = engine.registry.list_agents(channel_id=channel_id)
            selected_agent = engine.router.route_message(text, agents)
            if not selected_agent:
                return None
            agent_used = selected_agent
            if callbacks and callbacks.on_message_routed:
                callbacks.on_message_routed(text, selected_agent)
            return engine._execute_agent(selected_agent, text, callbacks)

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
                return json.dumps(card_data, ensure_ascii=False)
            return json.dumps({
                "schema": "2.0",
                "config": {"wide_screen_mode": True},
                "header": {"title": {"tag": "plain_text", "content": "💬 Agent 回复"}, "template": "blue"},
                "body": {"elements": [{"tag": "markdown", "content": result}]},
            }, ensure_ascii=False)

        def _error_card(exc: Exception) -> str:
            return json.dumps({
                "schema": "2.0",
                "config": {"wide_screen_mode": True},
                "header": {"title": {"tag": "plain_text", "content": "❌ 执行出错"}, "template": "red"},
                "body": {"elements": [{"tag": "markdown", "content": f"Agent 执行出错: {safe_error_message(exc)}"}]},
            }, ensure_ascii=False)

        def _empty_card() -> str:
            return json.dumps({
                "schema": "2.0",
                "config": {"wide_screen_mode": True},
                "header": {"title": {"tag": "plain_text", "content": "⚠️ 角色忙碌"}, "template": "orange"},
                "body": {"elements": [{"tag": "markdown", "content": "所有角色正在忙碌中，请稍后再试。"}]},
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
            "header": {"title": {"tag": "plain_text", "content": "⏳ 正在处理..."}, "template": "indigo"},
            "body": {"elements": [{"tag": "markdown", "content": "Agent 正在思考中，请稍候..."}]},
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
                    "💬 说「**启动协作**」或「**开始干活**」即可激活\n\n"
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
            SlockCommandAction.TASK_ASSIGN: {"task", "target"},
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
        elif action == SlockCommandAction.TASK_ASSIGN:
            # Rate-limit check: consistent with /task assign path
            if not self._check_assign_rate_limit(message_id, chat_id):
                return
            task_content = params.get("task", text)
            target = params.get("target", "")
            self.assign_task(message_id, chat_id, task_content, target, project)
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
                logger.error("NLI discussion failed: %s", exc, exc_info=True)
                self.reply_text(message_id, f"❌ 讨论执行失败：{str(exc)[:100]}")
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
            logger.warning("Failed to submit discussion to executor: %s", str(exc))
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
        except (QueueFullError, RuntimeError) as exc:
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
        self, message_id: str, chat_id: str, requirement: str = "", project: Optional["ProjectContext"] = None
    ):
        """Activate slock mode for the current chat."""
        project = self._ensure_project(message_id, chat_id, project)
        if not project:
            return

        root_path = project.root_path if project else self.get_working_dir(chat_id)
        manager = self._get_engine_manager()

        # Check if already active
        existing = manager.get_activated_engine(chat_id)
        if existing:
            self.reply_text(
                message_id,
                "⚠️ 当前已有 Slock 协作团队在运行\n\n"
                "发送 `/slock status` 查看状态\n"
                "发送 `/slock stop` 停止",
            )
            return

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
        manager.register_managed_chat(chat_id)

        # Wire UI callbacks for escalation timeout notifications
        engine.set_escalation_ui_callbacks(
            update_card_fn=self.update_card,
            send_text_fn=self.send_text_to_chat,
        )

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

        session = self.create_static_card_session(chat_id, reply_to=message_id)
        session.send(welcome_card)
        session.close()

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def show_slock_status(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        """Show slock engine status."""
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

        team_name = engine.channel.team_name if engine.channel else ""
        status_card = engine.get_status_card(team_name=team_name)
        card_content = json.dumps(status_card, ensure_ascii=False)
        self.reply_card(message_id, card_content)

    # ------------------------------------------------------------------
    # Help
    # ------------------------------------------------------------------

    def show_slock_help(self, message_id: str):
        """Show slock help information."""
        help_text = (
            "🎭 **Slock 协作模式 — 命令帮助**\n\n"
            "**激活 & 状态**\n"
            "• `/slock` — 激活协作模式\n"
            "• `/slock status` — 查看团队状态\n"
            "• `/slock list` / `/slocks` — 在主对话查询所有 Slock 群并跳转\n"
            "• `/slock stop` — 停止协作\n\n"
            "**团队管理**\n"
            "• `/new-team <名称>` — 创建带 `[Slock]` 后缀的协作团队群\n"
            "• `/team list` — 查看团队列表\n"
            "• `/team status <名称>` — 查看团队详情\n"
            "• `/team dissolve <名称>` — 解散团队\n\n"
            "**角色管理**\n"
            "• `/new-role <名称>` — 打开工具选择卡片，再选择模型创建虚拟 Agent\n"
            "• `/new-role <名称> --tool codex --model <模型> --role coder` — 命令式指定工具/模型/角色类型\n"
            "• `/new-role <名称> --template coder` — 从内置模板创建 Agent\n"
            "• `/new-role <名称> --fork <已有角色>` — 复制角色的指令、记忆和技能画像\n"
            "• `/role list` — 查看所有角色\n"
            "• `/role info <名称>` — 查看角色记忆、任务统计和技能画像\n"
            "• `/role remove <名称>` — 移除角色\n"
            "• `/role move <名称> <目标团队>` — 将角色迁移到另一个 Slock 团队\n\n"
            "**任务管理**\n"
            "• `/task list` — 查看任务列表\n"
            "• `/task assign <任务> [角色]` — 分配任务；省略角色时按技能画像自动选择\n"
            "• `/task assign \"多词任务\" \"角色名\"` — 支持引号包裹多词任务和角色\n"
            "• `/task status` — 查看 Kanban 任务进度\n\n"
            "**Council 评议**\n"
            "• `/council <议题>` — 多角色独立作答、匿名互评并由主席综合\n"
            "• `/slock council <议题>` — 同上"
        )
        self.reply_text(message_id, help_text)

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
            logger.error("create_team: 建群失败 name=%s err=%s", name, str(e))
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
            logger.error("create_team: 激活失败, 回滚建群 chat=%s err=%s", new_chat_id, str(e))
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
            self.reply_text(message_id, f"未找到团队: **{name}**" if name else "当前没有活跃的团队")
            return

        team_name = engine.channel.team_name if engine.channel else ""
        status_card = engine.get_status_card(team_name=team_name)
        self.reply_card(message_id, json.dumps(status_card, ensure_ascii=False))

    def dissolve_team(
        self, message_id: str, chat_id: str, name: str = "", project: Optional["ProjectContext"] = None
    ):
        """Dissolve (stop) a team."""
        manager = self._get_engine_manager()
        engine = manager.find_team(name) if name else manager.get_activated_engine(chat_id)
        if not engine or not engine.channel:
            self.reply_text(message_id, f"未找到团队: **{name}**" if name else "当前没有活跃的团队")
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
            logger.error("dissolve_team: 解散飞书群失败 chat=%s err=%s", target_chat_id, str(e))
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
        """List all roles in the current channel."""
        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            self.reply_text(message_id, "当前没有活跃的 Slock 团队")
            return

        channel_id = engine.channel.channel_id if engine.channel else chat_id
        agents = engine.registry.list_agents(channel_id=channel_id)

        if not agents:
            self.reply_text(message_id, "当前没有角色\n\n发送 `/new-role <名称>` 创建角色")
            return

        lines = ["📋 **角色列表**\n"]
        for a in agents:
            status = engine.get_agent_status(a.agent_id)
            lines.append(f"• {a.emoji} **{a.name}** — `{status.value}` · ID: `{a.agent_id[:8]}`")

        self.reply_text(message_id, "\n".join(lines))

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
            self.reply_text(message_id, "当前没有活跃的 Slock 团队")
            return

        if not self._check_slock_permission(engine, message_id, chat_id):
            return

        agent = engine.registry.find_by_name(name, channel_id=chat_id)
        if not agent:
            self.reply_text(message_id, f"未找到角色: **{name}**")
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
            self.reply_text(message_id, "当前没有活跃的 Slock 团队")
            return

        # Permission check: source group
        if not self._check_slock_permission(engine, message_id, chat_id):
            return

        agent = engine.registry.find_by_name(name, channel_id=chat_id)
        if not agent:
            self.reply_text(message_id, f"未找到角色: **{name}**")
            return

        # Find target team
        target_engine = manager.find_team(target_team_name)
        if not target_engine or not target_engine.channel:
            self.reply_text(message_id, f"未找到目标团队: **{target_team_name}**")
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
                    err_msg = f"❌ 移动失败：角色 **{name}** 未找到"
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
        """Show detailed info about a role.

        Permission-aware: admin/owner sees full details including Active Context
        and permissions; regular members see only non-sensitive identity info.
        """
        if not name:
            self.reply_text(message_id, "请提供角色名称\n\n用法: `/role info <名称>`")
            return

        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            self.reply_text(message_id, "当前没有活跃的 Slock 团队")
            return

        agent = engine.registry.find_by_name(name, channel_id=chat_id)
        if not agent:
            self.reply_text(message_id, f"未找到角色: **{name}**")
            return

        # Permission check: admin/owner sees full info, members see limited info
        is_privileged = self._has_slock_permission(engine)

        status = engine.get_agent_status(agent.agent_id)
        status_value = status.value if hasattr(status, "value") else str(status)
        memory = engine.memory.read_agent_memory(agent.agent_id)
        profiles = engine.memory.read_skill_profiles(agent.agent_id)
        assigned_tasks = [task for task in engine.tasks if task.claimed_by == agent.agent_id]
        done_count = sum(1 for task in assigned_tasks if getattr(task.status, "value", task.status) == "done")
        active_count = sum(
            1
            for task in assigned_tasks
            if getattr(task.status, "value", task.status) in {"in_progress", "in_review"}
        )
        memory_lines: list[str] = []
        if memory.role:
            memory_lines.append(f"• Role: {memory.role[:160]}")
        if memory.key_knowledge:
            memory_lines.append(f"• Key Knowledge: {memory.key_knowledge[:160]}")
        # Detect migration redaction and show status indicator
        _is_migrated = memory.active_context and "Context redacted on move:" in memory.active_context
        if _is_migrated:
            # Extract the migration info (everything after the timestamp prefix)
            _migration_info = memory.active_context.strip()
            memory_lines.append(f"• 🔄 迁移记录: {_migration_info[:120]}")
            memory_lines.append("• ℹ️ Active Context 已脱敏（跨群隐私策略）")
        elif is_privileged and memory.active_context:
            memory_lines.append(f"• Active Context: {memory.active_context[-160:]}")
        profile_lines = [
            f"• {profile.tag}: 成功率 {profile.success_rate:.0f}% · {profile.total_tasks} 次"
            for profile in profiles[:6]
        ]

        # Build info string — permissions line only for privileged users
        info_parts = [
            f"{agent.emoji} **{agent.name}**\n",
            f"• ID: `{agent.agent_id[:8]}`\n"
            f"• 类型: `{agent.agent_type}`\n"
            f"• 模型: `{agent.model_name or 'default'}`\n"
            f"• 角色: {agent.role or '(未设置)'}\n"
            f"• 状态: `{status_value}`\n",
        ]
        if is_privileged:
            info_parts.append(
                f"• 权限: `{', '.join(agent.permissions) if agent.permissions else '默认'}`\n"
            )
        info_parts.append(
            "\n**记忆摘要**\n"
            f"{chr(10).join(memory_lines) if memory_lines else '• 暂无记忆'}\n\n"
            "**历史任务**\n"
            f"• 总数: {len(assigned_tasks)} · 已完成: {done_count} · 进行中: {active_count}\n\n"
            "**技能画像**\n"
            f"{chr(10).join(profile_lines) if profile_lines else '• 暂无技能画像'}"
        )
        self.reply_text(message_id, "".join(info_parts))

    # ------------------------------------------------------------------
    # Task management
    # ------------------------------------------------------------------

    def list_tasks(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        """List all tasks in the current slock session."""
        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            self.reply_text(message_id, "当前没有活跃的 Slock 团队")
            return

        tasks = engine.tasks
        if not tasks:
            self.reply_text(message_id, "当前没有任务\n\n发送 `/task assign <任务> <角色>` 分配任务")
            return

        lines = ["📋 **任务列表**\n"]
        for t in tasks:
            assignee = t.claimed_by[:8] if t.claimed_by else "未分配"
            lines.append(f"• `{t.task_id[:8]}` — {t.content[:60]} · `{t.status.value}` · 🧑 {assignee}")

        self.reply_text(message_id, "\n".join(lines))

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
                    f"⚠️ 任务已创建但未找到角色 **{role_name}**\n"
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
        agents = list(engine.registry.list_agents(channel_id=channel_id))
        if not agents:
            self.reply_text(
                message_id,
                f"✅ 任务已创建（等待分配）\n"
                f"• ID: `{task.task_id[:8]}`\n"
                f"• 内容: {content[:80]}\n"
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
                f"• 内容: {content[:80]}\n"
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
            self.reply_text(message_id, "当前没有活跃的 Slock 团队")
            return

        from ...slock_engine.card_templates import build_task_board_card

        channel_id = engine.channel.channel_id if engine.channel else chat_id
        agents = engine.registry.list_agents(channel_id=channel_id)
        team_name = engine.channel.team_name if engine.channel else ""
        card = build_task_board_card(engine.tasks, agents, team_name=team_name, channel_id=channel_id)
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
        """Pure boolean check: is current operator admin or channel owner? No side effects."""
        from ...config import get_settings
        from ...thread.manager import get_current_sender_id

        operator_id = get_current_sender_id() or ""
        settings = get_settings()
        admin_ids = settings.admin_user_ids if hasattr(settings, "admin_user_ids") else frozenset()
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
        for key, timestamps in list(self._rate_limit_tracker.items()):
            active = [timestamp for timestamp in timestamps if now - timestamp < window]
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
            if task_id and engine:
                engine._force_complete_task(task_id)
            self.send_text_to_chat(open_chat_id, "✅ 已标记完成。")
            return

        if action_type == "slock_discussion_expand":
            self._expand_discussion(open_chat_id, value)
            return

        if action_type == "slock_discussion_stop":
            self._stop_discussion(open_chat_id, value)
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
    # Static command detection
    # ------------------------------------------------------------------

    @staticmethod
    def is_slock_command(text: str, chat_id: str | None = None, manager=None) -> bool:
        """Check if text is any slock-related command."""
        return is_slock_command(text, chat_id=chat_id, manager=manager)
