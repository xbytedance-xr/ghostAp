from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urldefrag


ROOT = Path(__file__).resolve().parents[1]
DOC_PATHS = [
    ROOT / "README.md",
    ROOT / "CHANGELOG.md",
    ROOT / "AGENTS.md",
    ROOT / "CLAUDE.md",
    *sorted((ROOT / "docs").glob("*.md")),
]


REMOVED_ARTIFACT_REFERENCES = {
    "2025-04-25-" + "multi-chat-isolation-design",
    "2026-04-29-" + "new-chat-project-design",
    "2026-04-30-" + "card-refactor-design",
    "2026-04-30-" + "card-refactor-plan",
    "acp_" + "architecture.md",
    "card-migration-" + "faq.md",
    "docs/" + "plan.md",
    "docs/" + "superpowers",
    "unified_card_" + "v1",
    "unified_card_" + "v2",
    "check_shim_" + "deadline",
}


def test_retained_docs_do_not_reference_removed_cleanup_artifacts() -> None:
    violations: list[str] = []
    for path in DOC_PATHS:
        text = path.read_text(encoding="utf-8")
        for needle in REMOVED_ARTIFACT_REFERENCES:
            if needle in text:
                violations.append(f"{path.relative_to(ROOT)} references {needle}")

    assert violations == []


def test_local_markdown_links_in_retained_docs_resolve() -> None:
    violations: list[str] = []
    link_pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")

    for path in DOC_PATHS:
        text = path.read_text(encoding="utf-8")
        for match in link_pattern.finditer(text):
            raw_target = match.group(1).strip()
            target, _fragment = urldefrag(raw_target)
            if not target or "://" in target or target.startswith("mailto:"):
                continue

            candidate = (path.parent / target).resolve()
            if not candidate.exists():
                violations.append(f"{path.relative_to(ROOT)} -> {raw_target}")

    assert violations == []


def test_readme_card_tree_documents_current_pipeline_directories() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")

    for directory in (
        "actions/",
        "delivery/",
        "events/",
        "render/",
        "session/",
        "state/",
        "timers/",
    ):
        assert f"│   │   ├── {directory}" in text

    old_card_summary = "CardBuilder（schema 2.0）" + "+ 流式更新 + 统一布局"
    assert old_card_summary not in text
