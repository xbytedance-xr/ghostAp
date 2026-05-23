"""Protocol definitions for Slock Engine component interfaces.

Provides structural subtyping contracts (typing.Protocol) to enforce
compile-time type safety between loosely coupled components without
requiring inheritance.
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Protocol, runtime_checkable

from src.acp.models import PromptResult
from .models import AgentIdentity


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
