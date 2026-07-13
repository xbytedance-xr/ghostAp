#!/usr/bin/env python3
"""Run the pinned Channel SDK gate and emit development-only evidence."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

import pytest  # noqa: E402

from src.autonomous.ingress.sdk_capability import (  # noqa: E402
    CAPABILITY_NODEIDS,
    CapabilityDecision,
    CapabilityRunEvidence,
    CapabilityTestOutcome,
    collect_sdk_distribution_identity,
    prepare_controlled_sdk_import_cache,
)


class _CapabilityResultPlugin:
    def __init__(self) -> None:
        self.collected_nodeids: tuple[str, ...] = ()
        self._reports: dict[str, dict[str, str]] = {}

    def pytest_collection_finish(self, session: Any) -> None:
        self.collected_nodeids = tuple(item.nodeid for item in session.items)

    def pytest_runtest_logreport(self, report: Any) -> None:
        if report.nodeid not in CAPABILITY_NODEIDS:
            return
        outcome = report.outcome
        if getattr(report, "wasxfail", None) is not None:
            outcome = "failed"
        self._reports.setdefault(report.nodeid, {})[report.when] = outcome

    def outcomes(self) -> tuple[CapabilityTestOutcome, ...]:
        results: list[CapabilityTestOutcome] = []
        for nodeid in CAPABILITY_NODEIDS:
            phases = self._reports.get(nodeid, {})
            results.append(
                CapabilityTestOutcome(
                    nodeid=nodeid,
                    setup=phases.get("setup", "failed"),
                    call=phases.get("call", "failed"),
                    teardown=phases.get("teardown", "failed"),
                )
            )
        return tuple(results)


def _git_output(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return result.stdout.strip()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run employee Channel SDK capability tests",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="development evidence destination (use /tmp or a CI artifact path)",
    )
    return parser.parse_args()


def _collect_sdk_or_report() -> Any | None:
    try:
        return collect_sdk_distribution_identity(require_controlled_import_cache=True)
    except Exception:
        print(
            json.dumps(
                {
                    "blocker": "employee_channel_sdk_capability_mismatch",
                    "decision": CapabilityDecision.CAPABILITY_RED.value,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return None


def _prepare_output_path(path: Path) -> Path:
    output = path.expanduser().absolute()
    resolved = output.resolve(strict=False)
    try:
        resolved.relative_to(REPOSITORY.resolve())
    except ValueError:
        pass
    else:
        raise SystemExit("capability evidence output must be outside the repository")
    if output.is_symlink():
        raise SystemExit("capability evidence output must not be a symlink")
    if output.exists() and not output.is_file():
        raise SystemExit("capability evidence output must be a regular file path")
    output.unlink(missing_ok=True)
    return output


def main() -> int:
    args = _parse_args()
    output = _prepare_output_path(args.output)
    controlled_cache = tempfile.TemporaryDirectory(
        prefix="employee-sdk-import-cache-",
    )
    prepare_controlled_sdk_import_cache(Path(controlled_cache.name) / "cache")
    sdk = _collect_sdk_or_report()
    if sdk is None:
        controlled_cache.cleanup()
        return 2
    commit_before = _git_output("rev-parse", "HEAD")
    os.chdir(REPOSITORY)
    plugin = _CapabilityResultPlugin()
    exit_code = int(pytest.main(["-q", *CAPABILITY_NODEIDS], plugins=[plugin]))
    commit_after = _git_output("rev-parse", "HEAD")
    if commit_after != commit_before:
        exit_code = max(exit_code, 1)
    evidence = CapabilityRunEvidence.create(
        commit_sha=commit_before,
        worktree_clean=not bool(_git_output("status", "--porcelain", "--untracked-files=all")),
        sdk=sdk,
        requested_nodeids=CAPABILITY_NODEIDS,
        collected_nodeids=plugin.collected_nodeids,
        outcomes=plugin.outcomes(),
        pytest_exit_code=exit_code,
        created_at=dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
    )
    evidence.write_atomic(output)
    controlled_cache.cleanup()
    print(
        json.dumps(
            {
                "artifact_sha256": evidence.artifact_sha256,
                "decision": evidence.decision.value,
                "output": str(output),
                "promotable": evidence.promotable,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    if evidence.decision is CapabilityDecision.CAPABLE_PINNED_ADAPTER:
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
