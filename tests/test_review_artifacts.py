"""Tests for ReviewArtifacts collection + persistence."""

from __future__ import annotations

import json
import os
import types

import pytest

from src.spec_engine.review_artifacts import (
    ReviewArtifacts,
    collect_review_artifacts,
    persist_review_artifacts,
)


def _mock_cycle(**overrides):
    c = types.SimpleNamespace(
        cycle_number=3,
        spec_content="SPEC body",
        plan_content="PLAN body",
        tasks=[types.SimpleNamespace(title="T1"), types.SimpleNamespace(title="T2")],
        build_output="BUILD body",
        spec_path="/tmp/spec.json",
        plan_path="/tmp/plan.json",
        tasks_path="/tmp/tasks.json",
        build_path="/tmp/build.txt",
    )
    for k, v in overrides.items():
        setattr(c, k, v)
    return c


def _mock_project(requirement="build a chatbot"):
    return types.SimpleNamespace(requirement=requirement)


def test_collect_basic(tmp_path):
    a = collect_review_artifacts(
        cycle=_mock_cycle(),
        project=_mock_project(),
        cwd=str(tmp_path),
        include_diff=False,
    )
    assert a.cycle_number == 3
    assert a.requirement == "build a chatbot"
    assert a.spec_output == "SPEC body"
    assert a.plan_output == "PLAN body"
    assert a.tasks_output == "T1\nT2"
    assert a.build_output == "BUILD body"
    assert a.diff_patch == ""
    assert a.touched_files == []


def test_truncation():
    long_build = "x" * 50_000
    a = collect_review_artifacts(
        cycle=_mock_cycle(build_output=long_build),
        project=_mock_project(),
        cwd="/tmp",
        build_output_max=1_000,
        include_diff=False,
    )
    assert len(a.build_output) <= 1_000


def test_roundtrip():
    a = ReviewArtifacts(
        cycle_number=5, requirement="req", cwd="/x", build_output="b", touched_files=["a.py"]
    )
    d = a.to_dict()
    b = ReviewArtifacts.from_dict(d)
    assert b.cycle_number == 5
    assert b.touched_files == ["a.py"]
    assert b.build_output == "b"


def test_persist(tmp_path):
    a = ReviewArtifacts(cycle_number=7, requirement="r", cwd=str(tmp_path))
    path = persist_review_artifacts(a, str(tmp_path))
    assert path is not None
    assert os.path.exists(path)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    assert data["cycle_number"] == 7


def test_persist_bad_dir(monkeypatch):
    a = ReviewArtifacts(cycle_number=1, requirement="r", cwd="/")
    path = persist_review_artifacts(a, "/nonexistent_fs_location/that/should/fail/deeply/\x00")
    assert path is None


def test_git_diff_nongit(tmp_path):
    a = collect_review_artifacts(
        cycle=_mock_cycle(),
        project=_mock_project(),
        cwd=str(tmp_path),
        include_diff=True,
    )
    assert a.diff_patch == ""
    assert a.touched_files == []
