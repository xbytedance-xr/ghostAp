#!/usr/bin/env python3
"""Read-only inventory for reviewing the size and duplication of pytest suites."""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class TestLocation:
    path: Path
    line: int
    name: str


@dataclass(frozen=True)
class TestFileStat:
    path: Path
    function_count: int
    source_lines: int


@dataclass(frozen=True)
class TestInventory:
    root: Path
    file_count: int
    function_count: int
    source_lines: int
    files: tuple[TestFileStat, ...]
    exact_duplicate_groups: tuple[tuple[TestLocation, ...], ...]

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["root"] = str(self.root)
        for item in payload["files"]:  # type: ignore[union-attr]
            item["path"] = str(item["path"])
        for group in payload["exact_duplicate_groups"]:  # type: ignore[union-attr]
            for item in group:
                item["path"] = str(item["path"])
        return payload


def _normalized_test_digest(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    normalized = copy.deepcopy(node)
    normalized.name = "test"
    dumped = ast.dump(normalized, include_attributes=False)
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()


def scan_test_tree(root: Path) -> TestInventory:
    """Inventory test files without importing or executing them."""

    root = root.resolve()
    duplicate_candidates: dict[str, list[TestLocation]] = defaultdict(list)
    file_stats: list[TestFileStat] = []

    for path in sorted(root.rglob("test_*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        test_nodes = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test")
        ]
        relative_path = path.relative_to(root)
        file_stats.append(
            TestFileStat(
                path=relative_path,
                function_count=len(test_nodes),
                source_lines=len(source.splitlines()),
            )
        )
        for node in test_nodes:
            duplicate_candidates[_normalized_test_digest(node)].append(
                TestLocation(path=relative_path, line=node.lineno, name=node.name)
            )

    duplicate_groups = tuple(
        tuple(sorted(locations, key=lambda item: (str(item.path), item.line, item.name)))
        for locations in duplicate_candidates.values()
        if len(locations) > 1
    )
    duplicate_groups = tuple(
        sorted(duplicate_groups, key=lambda group: (str(group[0].path), group[0].line))
    )

    return TestInventory(
        root=root,
        file_count=len(file_stats),
        function_count=sum(item.function_count for item in file_stats),
        source_lines=sum(item.source_lines for item in file_stats),
        files=tuple(file_stats),
        exact_duplicate_groups=duplicate_groups,
    )


def _render_text(inventory: TestInventory) -> str:
    lines = [
        f"test root: {inventory.root}",
        f"files: {inventory.file_count}",
        f"test functions: {inventory.function_count}",
        f"source lines: {inventory.source_lines}",
        f"exact duplicate groups: {len(inventory.exact_duplicate_groups)}",
        "largest files:",
    ]
    for item in sorted(
        inventory.files,
        key=lambda stat: (stat.function_count, stat.source_lines, str(stat.path)),
        reverse=True,
    )[:20]:
        lines.append(
            f"  {item.function_count:4} tests  {item.source_lines:5} lines  {item.path}"
        )
    if inventory.exact_duplicate_groups:
        lines.append("exact duplicate candidates:")
        for group in inventory.exact_duplicate_groups:
            lines.append(
                "  " + " | ".join(f"{item.path}:{item.line}:{item.name}" for item in group)
            )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path, default=Path("tests"))
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    inventory = scan_test_tree(args.root)
    if args.as_json:
        print(json.dumps(inventory.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(_render_text(inventory))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
