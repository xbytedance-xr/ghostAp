"""Pure cards for durable employee Team runs."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TeamAssignmentCardView:
    assignment_id: str
    agent_id: str
    employee_name: str
    status: str
    content: str = ""
    error_code: str = ""


@dataclass(frozen=True, slots=True)
class TeamRunCardView:
    run_id: str
    phase: str
    goal: str
    assignments: tuple[TeamAssignmentCardView, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "assignments", tuple(self.assignments))


@dataclass(frozen=True, slots=True)
class TeamRunCardBundle:
    summary_card: dict
    assignment_cards: tuple[dict, ...]
    continuation_cards: tuple[dict, ...]


class EmployeeTeamRenderer:
    def __init__(self, *, max_content_chars: int = 1_200) -> None:
        if max_content_chars < 64:
            raise ValueError("team card content limit is too small")
        self._limit = max_content_chars

    def render(self, view: TeamRunCardView) -> TeamRunCardBundle:
        summary = self._card(
            f"团队任务 · {view.phase}",
            f"**Run:** `{view.run_id}`\n**目标:** {view.goal}\n"
            f"**员工任务:** {len(view.assignments)}",
        )
        assignments: list[dict] = []
        continuations: list[dict] = []
        for assignment in view.assignments:
            chunks = self._chunks(assignment.content)
            first = chunks[0] if chunks else ""
            content = (
                f"**员工:** {assignment.employee_name} (`{assignment.agent_id}`)\n"
                f"**状态:** {assignment.status}"
            )
            if assignment.error_code:
                content += f"\n**Code:** `{assignment.error_code}`"
            if first:
                content += f"\n\n{first}"
            card = self._card(f"员工任务 · {assignment.employee_name}", content)
            card["assignment_id"] = assignment.assignment_id
            assignments.append(card)
            for index, chunk in enumerate(chunks[1:], start=2):
                continuation = self._card(
                    f"续接 · {assignment.employee_name} · {index}/{len(chunks)}",
                    chunk,
                )
                continuation["assignment_id"] = assignment.assignment_id
                continuations.append(continuation)
        return TeamRunCardBundle(summary, tuple(assignments), tuple(continuations))

    def _chunks(self, content: str) -> tuple[str, ...]:
        if not content:
            return ()
        return tuple(
            content[index : index + self._limit]
            for index in range(0, len(content), self._limit)
        )

    @staticmethod
    def _card(title: str, content: str) -> dict:
        return {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "blue",
            },
            "body": {"elements": [{"tag": "markdown", "content": content}]},
        }


__all__ = [
    "EmployeeTeamRenderer",
    "TeamAssignmentCardView",
    "TeamRunCardBundle",
    "TeamRunCardView",
]
