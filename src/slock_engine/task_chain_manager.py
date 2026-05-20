"""TaskChainManager — orchestrates role-to-role task collaboration chains.

Parses chain templates (e.g. "coder->reviewer->tester") and manages
automatic task creation when a predecessor task completes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChainStep:
    """A single step in a task chain."""
    role: str
    order: int


@dataclass
class ChainTemplate:
    """A parsed chain template with ordered steps."""
    name: str
    steps: list[ChainStep] = field(default_factory=list)

    def successor(self, current_role: str) -> Optional[str]:
        """Get the next role in the chain after current_role, or None if terminal."""
        for i, step in enumerate(self.steps):
            if step.role == current_role and i + 1 < len(self.steps):
                return self.steps[i + 1].role
        return None

    def predecessor(self, current_role: str) -> Optional[str]:
        """Get the previous role in the chain before current_role, or None if first."""
        for i, step in enumerate(self.steps):
            if step.role == current_role and i > 0:
                return self.steps[i - 1].role
        return None

    @property
    def first_role(self) -> str:
        return self.steps[0].role if self.steps else ""

    @property
    def last_role(self) -> str:
        return self.steps[-1].role if self.steps else ""


@dataclass
class ChainInstance:
    """A live chain instance tracking progress for a specific task lineage."""
    template_name: str
    origin_task_id: str  # The original task that started the chain
    current_role: str
    completed_roles: list[str] = field(default_factory=list)
    task_ids: dict[str, str] = field(default_factory=dict)  # role -> task_id mapping


class TaskChainManager:
    """Manages task collaboration chains based on configured templates.

    Parses chain templates from settings and provides:
    - Template matching: find applicable chain for a role
    - Successor resolution: determine next role after completion
    - Chain instance tracking: track multi-step progress
    """

    def __init__(self, chain_config: str = "") -> None:
        """Initialize with chain template configuration string.

        Args:
            chain_config: Chain templates in "role->role->role" format,
                         comma-separated for multiple chains.
                         If empty, loads from settings.
        """
        if not chain_config:
            try:
                from ..config import get_settings
                chain_config = get_settings().slock_chain_templates
            except Exception:
                chain_config = "coder->reviewer->tester"

        self._templates: list[ChainTemplate] = self._parse_templates(chain_config)
        self._active_chains: dict[str, ChainInstance] = {}  # origin_task_id -> instance
        logger.info("TaskChainManager initialized with %d templates", len(self._templates))

    @property
    def templates(self) -> list[ChainTemplate]:
        """Return configured chain templates."""
        return list(self._templates)

    def get_successor_role(self, current_role: str) -> Optional[str]:
        """Find the next role in any matching chain template.

        Returns the successor role or None if current_role is terminal
        or not found in any template.
        """
        for template in self._templates:
            successor = template.successor(current_role)
            if successor:
                return successor
        return None

    def get_predecessor_role(self, current_role: str) -> Optional[str]:
        """Find the previous role in any matching chain template."""
        for template in self._templates:
            predecessor = template.predecessor(current_role)
            if predecessor:
                return predecessor
        return None

    def should_chain(self, completed_role: str) -> bool:
        """Check if a completed role has a successor that should be triggered."""
        return self.get_successor_role(completed_role) is not None

    def start_chain(self, origin_task_id: str, starting_role: str) -> Optional[ChainInstance]:
        """Start tracking a new chain instance from the given role.

        Returns the ChainInstance if the role belongs to a chain, else None.
        """
        for template in self._templates:
            for step in template.steps:
                if step.role == starting_role:
                    instance = ChainInstance(
                        template_name=template.name,
                        origin_task_id=origin_task_id,
                        current_role=starting_role,
                        task_ids={starting_role: origin_task_id},
                    )
                    self._active_chains[origin_task_id] = instance
                    logger.debug(
                        "Chain started: template=%s origin=%s role=%s",
                        template.name, origin_task_id, starting_role,
                    )
                    return instance
        return None

    def advance_chain(
        self, origin_task_id: str, completed_role: str, new_task_id: str
    ) -> Optional[str]:
        """Advance a chain after a role completes its task.

        Records completion, moves to next role, returns the next role name
        or None if chain is complete.
        """
        instance = self._active_chains.get(origin_task_id)
        if instance is None:
            return None

        instance.completed_roles.append(completed_role)
        successor = self.get_successor_role(completed_role)
        if successor:
            instance.current_role = successor
            instance.task_ids[successor] = new_task_id
            logger.info(
                "Chain advanced: origin=%s %s -> %s (new_task=%s)",
                origin_task_id, completed_role, successor, new_task_id,
            )
            return successor

        # Chain complete
        self._active_chains.pop(origin_task_id, None)
        logger.info("Chain completed: origin=%s final_role=%s", origin_task_id, completed_role)
        return None

    def get_chain_status(self, origin_task_id: str) -> Optional[ChainInstance]:
        """Get the current status of an active chain."""
        return self._active_chains.get(origin_task_id)

    def is_chain_active(self, origin_task_id: str) -> bool:
        """Check if a chain is still active (not completed)."""
        return origin_task_id in self._active_chains

    @staticmethod
    def _parse_templates(config: str) -> list[ChainTemplate]:
        """Parse chain template configuration string.

        Format: "role1->role2->role3, roleA->roleB"
        Each comma-separated segment is one template.
        """
        templates: list[ChainTemplate] = []
        for segment in config.split(","):
            segment = segment.strip()
            if not segment:
                continue
            roles = [r.strip() for r in segment.split("->") if r.strip()]
            if len(roles) < 2:
                logger.warning("Ignoring invalid chain template (need >=2 roles): %s", segment)
                continue
            steps = [ChainStep(role=role, order=i) for i, role in enumerate(roles)]
            template = ChainTemplate(name="->".join(roles), steps=steps)
            templates.append(template)
        return templates
