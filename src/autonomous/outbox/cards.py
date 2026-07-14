"""Pure renderer for the single employee-owned task status card."""

from __future__ import annotations

from typing import Any

from .models import EmployeeCardState

_TEMPLATES = {
    EmployeeCardState.QUEUED: "blue",
    EmployeeCardState.RUNNING: "wathet",
    EmployeeCardState.COMPLETED: "green",
    EmployeeCardState.FAILED: "red",
    EmployeeCardState.CANCELED: "grey",
    EmployeeCardState.ACTION_REQUIRED: "orange",
}
_LABELS = {
    EmployeeCardState.QUEUED: "排队中",
    EmployeeCardState.RUNNING: "执行中",
    EmployeeCardState.COMPLETED: "已完成",
    EmployeeCardState.FAILED: "失败",
    EmployeeCardState.CANCELED: "已取消",
    EmployeeCardState.ACTION_REQUIRED: "需要处理",
}


def build_employee_status_card(
    *,
    title: str,
    state: EmployeeCardState,
    summary: str,
    progress_percent: int,
    attempt_id: str,
) -> dict[str, Any]:
    """Render one bounded card snapshot without performing delivery."""

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title[:120]},
            "template": _TEMPLATES[state],
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": f"**状态：** {_LABELS[state]}\n\n{summary[:4000]}",
                },
                {
                    "tag": "column_set",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "elements": [
                                {
                                    "tag": "markdown",
                                    "content": f"**进度：** {progress_percent}%",
                                }
                            ],
                        },
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 2,
                            "elements": [
                                {
                                    "tag": "markdown",
                                    "content": f"**任务：** `{attempt_id}`",
                                }
                            ],
                        },
                    ],
                },
            ]
        },
    }


__all__ = ["build_employee_status_card"]
