"""Display payload helpers for Spec PLAN/TASK artifacts."""

from __future__ import annotations

from .artifacts import parse_plan_artifact, parse_tasks


def build_plan_display_payload(output: str, cycle_num: int) -> dict:
    """Convert PLAN phase output into a structured card payload."""
    artifact, errors = parse_plan_artifact(output)
    if artifact is not None:
        payload = artifact.to_dict()
    else:
        notes = _plain_notes(output)
        payload = {
            "architecture": "",
            "tech_stack": [],
            "steps": [],
            "file_changes": [],
            "test_plan": [],
            "risks": [],
            "notes": notes or [str(item) for item in errors if str(item).strip()],
        }
    payload["cycle_num"] = cycle_num
    if errors:
        payload["artifact_errors"] = [str(item) for item in errors if str(item).strip()]
    return payload


def build_task_display_payloads(output: str, cycle_num: int) -> list[dict]:
    """Convert TASK phase output into one card payload per task."""
    tasks = parse_tasks(output)
    payloads: list[dict] = []
    for index, task in enumerate(tasks, start=1):
        data = task.to_dict()
        data["cycle_num"] = cycle_num
        data["task_index"] = index
        payloads.append(data)
    return payloads


def _plain_notes(output: str) -> list[str]:
    lines: list[str] = []
    in_fence = False
    for raw in str(output or "").splitlines():
        line = raw.strip()
        if line.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if not line or in_fence:
            continue
        lines.append(line)
    return lines
