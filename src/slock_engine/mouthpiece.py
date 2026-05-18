"""Mouthpiece — formats agent messages for delivery through single Bot identity.

Handles the "mouthpiece" pattern: one Bot speaks on behalf of multiple virtual agents,
using emoji prefixes, named identifiers, and Interactive Cards for visual identity.
"""

from __future__ import annotations

import time
from typing import Optional

from .card_templates import build_agent_message_card
from .models import AgentIdentity


class Mouthpiece:
    """Formats agent output for delivery as the single ghostAp Bot.

    Two output modes:
    - Plain text: "[emoji Name] message content"
    - Card: Full Interactive Card with colored header and metadata
    """

    def format_text(self, agent: AgentIdentity, message: str) -> str:
        """Format a plain-text mouthpiece message.

        Output: "[🔧 Coder-A] message content"
        """
        return f"[{agent.emoji} {agent.name}] {message}"

    def format_card(
        self,
        agent: AgentIdentity,
        content: str,
        *,
        model_info: str = "",
        duration_s: Optional[float] = None,
    ) -> dict:
        """Format an Interactive Card for an agent's message.

        Returns Feishu-compatible card JSON with:
        - Colored header matching agent role
        - Agent emoji + name as title
        - Markdown content body
        - Footer with agent_type, model, duration
        """
        return build_agent_message_card(
            agent=agent,
            content=content,
            model_info=model_info,
            duration_s=duration_s,
        )

    def format_thinking(self, agent: AgentIdentity) -> str:
        """Format a 'thinking' indicator message."""
        return f"[{agent.emoji} {agent.name}] 💭 thinking..."

    def format_escalation(self, agent: AgentIdentity, reason: str) -> dict:
        """Format an escalation request card for admin attention."""
        content = f"**⚠️ Escalation Request**\n\n{reason}\n\n*This agent needs human input to proceed.*"
        return build_agent_message_card(
            agent=agent,
            content=content,
            model_info="escalation",
        )
