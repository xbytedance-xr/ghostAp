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

    @property
    def roles(self) -> list[str]:
        """Return list of role names in order (compatibility with handler usage)."""
        return [s.role for s in self.steps]


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

    def find_chain_for_task(
        self, task_content: str, starting_role: str = ""
    ) -> Optional[ChainTemplate]:
        """Select the appropriate chain template based on task context.

        Selection logic:
        1. If starting_role is provided, find the longest chain that starts
           with or contains that role.
        2. Otherwise, use keyword matching on task_content:
           - "plan"/"设计"/"architect" → prefer chain with "planner" as first role
           - "review"/"审查"/"检查" → prefer chain that includes "reviewer"
           - "test"/"测试" → prefer chain that includes "tester"
        3. Default: return the longest (most comprehensive) chain template
           to ensure thorough multi-role collaboration.
        4. Return None if no templates configured.

        Args:
            task_content: The task description text for keyword matching.
            starting_role: Optional role name to match against chain membership.

        Returns:
            The best-matching ChainTemplate, or None if no templates exist.
        """
        if not self._templates:
            return None

        # Strategy 1: Match by starting_role
        if starting_role:
            candidates: list[ChainTemplate] = []
            for template in self._templates:
                roles = template.roles
                if starting_role in roles:
                    candidates.append(template)
            if candidates:
                # Return the longest chain that contains the role
                return max(candidates, key=lambda t: len(t.steps))
            # If no match found by role, fall through to keyword matching

        # Strategy 2: Keyword matching on task_content
        content_lower = task_content.lower()

        # Check for "plan"/"设计"/"architect" → prefer chain with "planner" first
        if any(kw in content_lower for kw in ("plan", "设计", "architect")):
            for template in self._templates:
                if template.first_role == "planner":
                    return template

        # Check for "review"/"审查"/"检查" → prefer chain that includes "reviewer"
        if any(kw in content_lower for kw in ("review", "审查", "检查")):
            for template in self._templates:
                roles = template.roles
                if "reviewer" in roles:
                    return template

        # Check for "test"/"测试" → prefer chain that includes "tester"
        if any(kw in content_lower for kw in ("test", "测试")):
            for template in self._templates:
                roles = template.roles
                if "tester" in roles:
                    return template

        # Default: prefer the most comprehensive chain (planner→coder→reviewer→tester pattern)
        # rather than the shortest chain, to ensure thorough collaboration
        return max(self._templates, key=lambda t: len(t.steps))

    def get_template_by_name(self, name: str) -> Optional[ChainTemplate]:
        """Look up a chain template by its name (the "role->role->role" string).

        Args:
            name: Exact template name to search for.

        Returns:
            The matching ChainTemplate, or None if not found.
        """
        for template in self._templates:
            if template.name == name:
                return template
        return None

    def list_templates(self) -> list[dict]:
        """Return serialized info about all configured templates for card rendering.

        Returns:
            List of dicts with keys: name, roles, step_count.
        """
        return [
            {
                "name": template.name,
                "roles": template.roles,
                "step_count": len(template.steps),
            }
            for template in self._templates
        ]

    @staticmethod
    def _parse_templates(config: str) -> list[ChainTemplate]:
        """Parse chain template configuration string.

        Format: "role1->role2->role3, roleA+roleB->roleC"
        Each comma-separated segment is one template.
        Within a segment, '->' separates sequential groups.
        Within a group, '+' separates parallel roles (same order).
        """
        templates: list[ChainTemplate] = []
        for segment in config.split(","):
            segment = segment.strip()
            if not segment:
                continue
            groups = [g.strip() for g in segment.split("->") if g.strip()]
            if len(groups) < 2:
                logger.warning("Ignoring invalid chain template (need >=2 groups): %s", segment)
                continue
            steps: list[ChainStep] = []
            for order, group in enumerate(groups):
                # Each group may contain parallel roles separated by '+'
                roles_in_group = [r.strip() for r in group.split("+") if r.strip()]
                for role in roles_in_group:
                    steps.append(ChainStep(role=role, order=order))
            template = ChainTemplate(name=segment.strip(), steps=steps)
            templates.append(template)
        return templates
