"""Filesystem layout helpers for Spec Engine persistence.

Spec runtime artifacts are intentionally kept outside the target repository so
long-running Spec cycles do not pollute git status.  The cache mirrors the
project absolute path under ``~/.cache/ghostAp`` by default:

``/Users/me/project`` -> ``~/.cache/ghostAp/Users/me/project``.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


DEFAULT_SPEC_CACHE_ROOT = "~/.cache/ghostAp"
RUN_STATE_FILENAME = "state.json"


@dataclass(frozen=True)
class SpecRunSummary:
    run_id: str
    run_dir: str
    state_path: str
    status: str = ""
    requirement: str = ""
    current_cycle: int = 0
    total_cycles: int = 0
    saved_at: float = 0.0
    created_at: float = 0.0


def _setting_str(settings, name: str, default: str = "") -> str:
    value = getattr(settings, name, default)
    if isinstance(value, str):
        return value.strip()
    return default


def spec_cache_root(settings=None) -> str:
    configured = _setting_str(settings, "spec_cache_root", "")
    root = configured or DEFAULT_SPEC_CACHE_ROOT
    return os.path.abspath(os.path.expanduser(root))


def project_cache_root(root_path: str, settings=None) -> str:
    abs_project = os.path.abspath(os.path.expanduser(root_path or "."))
    drive, tail = os.path.splitdrive(abs_project)
    parts = [part for part in Path(tail).parts if part not in (os.sep, "")]
    if drive:
        parts.insert(0, drive.rstrip(":"))
    return os.path.join(spec_cache_root(settings), *parts)


def legacy_state_path(root_path: str, settings) -> str:
    return os.path.join(root_path, _setting_str(settings, "spec_state_filename", ".spec_engine_state.json"))


def legacy_artifact_base_dir(root_path: str, settings) -> str:
    return os.path.join(root_path, _setting_str(settings, "spec_artifacts_dirname", ".spec_engine"))


def get_state_path(root_path: str, settings) -> str:
    return os.path.join(project_cache_root(root_path, settings), _setting_str(settings, "spec_state_filename", ".spec_engine_state.json"))


def state_path_candidates(root_path: str, settings) -> list[str]:
    paths = [get_state_path(root_path, settings), legacy_state_path(root_path, settings)]
    result: list[str] = []
    for path in paths:
        if path and path not in result:
            result.append(path)
    return result


def artifact_base_dir(root_path: str, settings) -> str:
    return os.path.join(project_cache_root(root_path, settings), _setting_str(settings, "spec_artifacts_dirname", ".spec_engine"))


def artifact_root_dir(root_path: str, settings, project_id: str) -> str:
    return os.path.join(artifact_base_dir(root_path, settings), project_id or "unknown")


def run_state_path(root_path: str, settings, project_id: str) -> str:
    return os.path.join(artifact_root_dir(root_path, settings, project_id), RUN_STATE_FILENAME)


def iter_artifact_base_dirs(root_path: str, settings) -> Iterable[str]:
    seen: set[str] = set()
    for path in (artifact_base_dir(root_path, settings), legacy_artifact_base_dir(root_path, settings)):
        if path and path not in seen:
            seen.add(path)
            yield path


def state_path_for_run(root_path: str, settings, run_id: str) -> str:
    run_id = os.path.basename(str(run_id or "").strip())
    if not run_id:
        return ""
    for base in iter_artifact_base_dirs(root_path, settings):
        candidate = os.path.join(base, run_id, RUN_STATE_FILENAME)
        if os.path.isfile(candidate):
            return candidate
    repaired = _repair_run_state_from_project_state(root_path, settings, run_id)
    if repaired:
        return repaired
    return os.path.join(artifact_base_dir(root_path, settings), run_id, RUN_STATE_FILENAME)


def delete_spec_run(root_path: str, settings, run_id: str) -> bool:
    """Delete a cached Spec run and matching project-level state if present."""
    run_id = os.path.basename(str(run_id or "").strip())
    if not run_id:
        return False

    deleted = False
    for base in iter_artifact_base_dirs(root_path, settings):
        run_dir = os.path.join(base, run_id)
        if os.path.isdir(run_dir):
            shutil.rmtree(run_dir, ignore_errors=True)
            deleted = True

    for state_path in state_path_candidates(root_path, settings):
        data = _read_json_dict(state_path)
        project = data.get("project") if isinstance(data.get("project"), dict) else {}
        if str(project.get("project_id") or "").strip() != run_id:
            continue
        try:
            os.remove(state_path)
            deleted = True
        except FileNotFoundError:
            pass
        except Exception:
            continue

    return deleted


def list_spec_runs(root_path: str, settings, *, limit: int | None = None) -> list[SpecRunSummary]:
    runs: list[SpecRunSummary] = []
    seen: set[str] = set()

    for base in iter_artifact_base_dirs(root_path, settings):
        if not os.path.isdir(base):
            continue
        for entry in os.scandir(base):
            if not entry.is_dir() or entry.name in seen:
                continue
            seen.add(entry.name)
            runs.append(_summarize_run(entry.path, entry.name, root_path=root_path, settings=settings))

    for state_path in state_path_candidates(root_path, settings):
        data = _read_json_dict(state_path)
        project = data.get("project") if isinstance(data.get("project"), dict) else {}
        run_id = str(project.get("project_id") or "").strip()
        if not run_id or run_id in seen:
            continue
        seen.add(run_id)
        repaired = _repair_run_state_from_project_state(root_path, settings, run_id, data=data)
        run_dir = artifact_root_dir(root_path, settings, run_id)
        runs.append(_summarize_run(run_dir, run_id, root_path=root_path, settings=settings, state_path_override=repaired or state_path))

    runs.sort(key=lambda item: (item.saved_at or item.created_at or _safe_mtime(item.run_dir)), reverse=True)
    if limit is not None and limit >= 0:
        return runs[:limit]
    return runs


def _summarize_run(
    run_dir: str,
    run_id: str,
    *,
    root_path: str = "",
    settings=None,
    state_path_override: str = "",
) -> SpecRunSummary:
    state_path = state_path_override or os.path.join(run_dir, RUN_STATE_FILENAME)
    if not os.path.isfile(state_path) and root_path and settings is not None:
        repaired = _repair_run_state_from_project_state(root_path, settings, run_id)
        if repaired:
            state_path = repaired
    data = _read_json_dict(state_path)
    project = data.get("project") if isinstance(data.get("project"), dict) else {}

    cycles = project.get("cycles") if isinstance(project, dict) else None
    current_cycle = 0
    if isinstance(cycles, list) and cycles:
        last = cycles[-1] if isinstance(cycles[-1], dict) else {}
        try:
            current_cycle = int(last.get("cycle_number") or len(cycles))
        except Exception:
            current_cycle = len(cycles)

    if not current_cycle:
        try:
            current_cycle = int(project.get("cycle_count_total") or 0)
        except Exception:
            current_cycle = 0

    total_cycles = current_cycle
    try:
        total_cycles = max(total_cycles, int(project.get("cycle_count_total") or 0))
    except Exception:
        pass

    saved_at = _safe_float(data.get("saved_at"))
    created_at = _safe_float(project.get("created_at")) or _safe_mtime(run_dir)
    return SpecRunSummary(
        run_id=run_id,
        run_dir=run_dir,
        state_path=state_path if os.path.isfile(state_path) else "",
        status=str(project.get("status") or ""),
        requirement=str(project.get("requirement") or ""),
        current_cycle=current_cycle,
        total_cycles=total_cycles,
        saved_at=saved_at or _safe_mtime(state_path),
        created_at=created_at,
    )


def _repair_run_state_from_project_state(
    root_path: str,
    settings,
    run_id: str,
    *,
    data: dict | None = None,
) -> str:
    run_id = os.path.basename(str(run_id or "").strip())
    if not run_id:
        return ""

    for candidate in state_path_candidates(root_path, settings):
        state_data = data if data is not None else _read_json_dict(candidate)
        project = state_data.get("project") if isinstance(state_data.get("project"), dict) else {}
        if str(project.get("project_id") or "").strip() != run_id:
            continue

        run_path = run_state_path(root_path, settings, run_id)
        if os.path.isfile(run_path):
            return run_path
        try:
            _write_json_atomic(run_path, state_data)
            return run_path
        except Exception:
            return candidate if os.path.isfile(candidate) else ""
    return ""


def _write_json_atomic(filepath: str, data: dict) -> None:
    parent = os.path.dirname(filepath)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp_path = filepath + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, filepath)


def _read_json_dict(path: str) -> dict:
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _safe_float(value) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


def _safe_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except Exception:
        return 0.0
