"""Protocol definitions for Slock Engine component interfaces.

Provides structural subtyping contracts (typing.Protocol) to enforce
compile-time type safety between loosely coupled components without
requiring inheritance.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Optional, Protocol, runtime_checkable

from src.acp.models import PromptResult

from .models import AgentIdentity, SlockChannel

if TYPE_CHECKING:
    from src.config.settings import Settings


@runtime_checkable
class SlockEngineContext(Protocol):
    """只读协议：暴露引擎共享状态给管理器，替代 lambda 闭包注入。

    管理器依赖该协议而非引擎内部实现，消除循环引用与私有状态泄露。
    """

    @property
    def channel(self) -> Optional[SlockChannel]:
        """当前激活的 SlockChannel（只读）。"""
        ...

    @property
    def chat_id(self) -> str:
        """当前引擎绑定的 chat_id。"""
        ...

    @property
    def dirty(self) -> bool:
        """任务看板是否需要持久化（只读）。"""
        ...

    def set_dirty(self, value: bool) -> None:
        """设置 dirty 标志。"""
        ...

    def execute_agent(
        self,
        agent: AgentIdentity,
        content: str,
        callbacks: Any,
        *,
        freshness_check: bool = True,
    ) -> Optional[str]:
        """执行单个 agent 的响应周期。"""
        ...

    def resolve_agent_for_role(self, role: str, channel_id: str) -> Optional[AgentIdentity]:
        """为指定角色在 channel 中解析最佳可用 agent。"""
        ...

    def execute_task(
        self,
        task_id: str,
        agent_id: str,
        callbacks: Any,
        *,
        request_review: bool = True,
        freshness_check: bool = True,
    ) -> Optional[str]:
        """按任务 ID 与 agent ID 执行任务。"""
        ...


@runtime_checkable
class DiscussionEngineProtocol(Protocol):
    """Minimum interface that DiscussionManager requires from the engine.

    Any object satisfying this structural protocol can be used as the
    engine parameter for DiscussionManager.__init__. This avoids circular
    imports and tight coupling to the concrete SlockEngine class.
    """

    @property
    def settings(self) -> Any:
        """Access to engine settings (slock_discussion_* fields)."""
        ...

    @property
    def registry(self) -> Any:
        """Access to the AgentRegistry for agent lookup."""
        ...

    @property
    def conclusion_card_callback(self) -> Optional[Callable[[dict], None]]:
        """Callback to send conclusion notification cards."""
        ...

    def get_agent(self, agent_id: str) -> Optional[AgentIdentity]:
        """Retrieve an agent by its ID."""
        ...

    def find_agent_by_name(self, name: str, channel_id: str = "") -> Optional[AgentIdentity]:
        """Find an agent by display name (case-insensitive)."""
        ...

    def build_agent_prompt(self, agent: AgentIdentity, task_context: str = "") -> str:
        """Build the system prompt for an agent session."""
        ...

    def run_agent_session_full(
        self,
        agent: AgentIdentity,
        prompt: str,
        *,
        timeout: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Optional[PromptResult]:
        """Run a full agent session and return the PromptResult (including token usage)."""
        ...


@runtime_checkable
class ActivationGuardProtocol(Protocol):
    """Protocol for activation guard implementations.

    Defines the structural contract for components that control passive
    auto-activation permission and rate limiting. Any object implementing
    this protocol can be used as an activation guard.
    """

    def can_auto_activate(
        self,
        sender_id: str,
        chat_id: str,
        settings: Settings,
    ) -> tuple[bool, str]:
        """Check if the sender is allowed to trigger auto-activation.

        Args:
            sender_id: The open_id of the message sender.
            chat_id: The chat_id where activation is requested.
            settings: Application settings instance.

        Returns:
            A tuple of (allowed, reason):
            - allowed: True if activation is permitted, False otherwise.
            - reason: A string indicating the reason (e.g., "allowed", "rate_limit").
        """
        ...
