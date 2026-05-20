"""Mouthpiece — formats agent messages for delivery through single Bot identity.

Handles the "mouthpiece" pattern: one Bot speaks on behalf of multiple virtual agents,
using emoji prefixes, named identifiers, and Interactive Cards for visual identity.
"""

from __future__ import annotations

import json
from typing import Optional

from src.card.render.payload_truncator import check_and_truncate_payload

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
        channel_id: str = "",
        task_id: str = "",
    ) -> dict:
        """Format an Interactive Card for an agent's message.

        Returns Feishu-compatible card JSON with:
        - Colored header matching agent role
        - Agent emoji + name as title
        - Markdown content body
        - Footer with agent_type, model, duration
        """
        card = build_agent_message_card(
            agent=agent,
            content=content,
            model_info=model_info,
            duration_s=duration_s,
            channel_id=channel_id,
            task_id=task_id,
        )
        return self._guard_feishu_payload(card)

    def format_thinking(self, agent: AgentIdentity) -> str:
        """Format a 'thinking' indicator message."""
        return f"[{agent.emoji} {agent.name}] 💭 thinking..."

    def format_escalation(self, agent: AgentIdentity, reason: str) -> dict:
        """Format an escalation request card for admin attention."""
        content = f"**⚠️ Escalation Request**\n\n{reason}\n\n*This agent needs human input to proceed.*"
        card = build_agent_message_card(
            agent=agent,
            content=content,
            model_info="escalation",
        )
        return self._guard_feishu_payload(card)

    @staticmethod
    def _guard_feishu_payload(card: dict) -> dict:
        """Apply the shared Feishu card guard to direct Slock mouthpiece cards."""
        raw = json.dumps(card, ensure_ascii=False)
        guarded = check_and_truncate_payload(raw, engine_type="slock")
        if guarded == raw:
            return card
        return json.loads(guarded)
