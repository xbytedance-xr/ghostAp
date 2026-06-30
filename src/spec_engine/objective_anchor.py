"""Objective Anchoring Layer for Spec Engine Graded Completion Assessment.

Anchors subjective LLM criteria evaluation to ground-truth signals:
verify_command exit codes, file checks, test results, build status,
git diff analysis, and pattern matching.

Design principles:
- Signals MODIFY or CAP LLM scores; they don't replace the LLM entirely.
- Hard failures (verify_command exit 1) override LLM optimism.
- Staleness tracking invalidates old signals when code changes.
- All command execution goes through SandboxExecutor with timeouts.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Signal Types
# ---------------------------------------------------------------------------


class SignalType(str, Enum):
    """Categories of objective verification signals."""

    VERIFY_COMMAND = "verify_command"      # User-provided shell command
    FILE_CHECK = "file_check"             # File existence/content assertion
    TEST_RESULT = "test_result"           # Test framework output parsing
    BUILD_STATUS = "build_status"         # Compilation/type-check exit code
    GIT_DIFF = "git_diff"                 # Diff analysis (files touched, size)
    PATTERN_MATCH = "pattern_match"       # Regex against command output or files


class SignalOutcome(str, Enum):
    """Result of a signal collection attempt."""

    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"       # Collection itself crashed/timed out
    SKIPPED = "skipped"   # Signal not applicable or binding missing


class AnchorMode(str, Enum):
    """How a signal interacts with LLM judgment."""

    OVERRIDE = "override"   # Signal verdict replaces LLM (hard gate)
    MODIFIER = "modifier"   # Signal adjusts confidence up/down (soft)


# ---------------------------------------------------------------------------
# Core Data Models
# ---------------------------------------------------------------------------


@dataclass
class SignalSpec:
    """Specification for one verification signal to collect.

    Created at spec/plan time; consumed at review time.
    """

    signal_type: SignalType
    command: str = ""                       # Shell command (for VERIFY_COMMAND, TEST_RESULT, BUILD_STATUS)
    file_path: str = ""                     # For FILE_CHECK
    pattern: str = ""                       # Regex for PATTERN_MATCH or expected content
    timeout_seconds: float = 60.0           # Max execution time
    anchor_mode: AnchorMode = AnchorMode.MODIFIER
    confidence_boost: float = 0.25          # Added to score on PASS
    confidence_cap: float = 0.4             # Max score allowed on FAIL (for OVERRIDE mode)
    description: str = ""                   # Human-readable explanation

    def to_dict(self) -> dict:
        return {
            "signal_type": self.signal_type.value,
            "command": self.command,
            "file_path": self.file_path,
            "pattern": self.pattern,
            "timeout_seconds": self.timeout_seconds,
            "anchor_mode": self.anchor_mode.value,
            "confidence_boost": self.confidence_boost,
            "confidence_cap": self.confidence_cap,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SignalSpec":
        return cls(
            signal_type=SignalType(data.get("signal_type", "verify_command")),
            command=data.get("command", ""),
            file_path=data.get("file_path", ""),
            pattern=data.get("pattern", ""),
            timeout_seconds=float(data.get("timeout_seconds", 60.0)),
            anchor_mode=AnchorMode(data.get("anchor_mode", "modifier")),
            confidence_boost=float(data.get("confidence_boost", 0.25)),
            confidence_cap=float(data.get("confidence_cap", 0.4)),
            description=data.get("description", ""),
        )


@dataclass
class SignalResult:
    """Outcome of collecting one signal."""

    spec: SignalSpec
    outcome: SignalOutcome
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    matched_pattern: bool = False
    elapsed_seconds: float = 0.0
    collected_at: float = field(default_factory=time.time)
    # For staleness: hash of relevant files at collection time
    context_hash: str = ""
    error_detail: str = ""

    @property
    def is_stale(self) -> bool:
        """Placeholder; real staleness requires comparing context_hash to current."""
        return False  # Computed externally via check_staleness()

    def to_dict(self) -> dict:
        return {
            "spec": self.spec.to_dict(),
            "outcome": self.outcome.value,
            "exit_code": self.exit_code,
            "stdout": self.stdout[:2000],  # Truncate for storage
            "stderr": self.stderr[:1000],
            "matched_pattern": self.matched_pattern,
            "elapsed_seconds": self.elapsed_seconds,
            "collected_at": self.collected_at,
            "context_hash": self.context_hash,
            "error_detail": self.error_detail,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SignalResult":
        return cls(
            spec=SignalSpec.from_dict(data.get("spec", {})),
            outcome=SignalOutcome(data.get("outcome", "skipped")),
            exit_code=data.get("exit_code"),
            stdout=data.get("stdout", ""),
            stderr=data.get("stderr", ""),
            matched_pattern=data.get("matched_pattern", False),
            elapsed_seconds=float(data.get("elapsed_seconds", 0.0)),
            collected_at=float(data.get("collected_at", 0.0)),
            context_hash=data.get("context_hash", ""),
            error_detail=data.get("error_detail", ""),
        )


@dataclass
class CriterionBinding:
    """Binds verification signals to a specific acceptance criterion.

    criterion_index: Index into SpecProject.acceptance_criteria
    signals: Ordered list of signal specs to collect for this criterion
    binding_source: How binding was established
    """

    criterion_index: int
    signals: list[SignalSpec] = field(default_factory=list)
    binding_source: str = "global"  # "user", "auto_inferred", "global"

    def to_dict(self) -> dict:
        return {
            "criterion_index": self.criterion_index,
            "signals": [s.to_dict() for s in self.signals],
            "binding_source": self.binding_source,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CriterionBinding":
        return cls(
            criterion_index=int(data.get("criterion_index", 0)),
            signals=[SignalSpec.from_dict(s) for s in data.get("signals", [])],
            binding_source=data.get("binding_source", "global"),
        )


@dataclass
class AnchoredScore:
    """Final score for one criterion after objective anchoring."""

    criterion_index: int
    llm_score: float              # Raw LLM judgment: 1.0 = PASS, 0.0 = FAIL
    anchored_score: float         # After signal modification
    signals_applied: list[SignalResult] = field(default_factory=list)
    overridden: bool = False      # True if a hard signal overrode LLM
    stale_signals: int = 0        # Count of signals ignored due to staleness
    explanation: str = ""         # Human-readable scoring rationale


@dataclass
class VerificationState:
    """Persistent state for objective anchoring across cycles.

    Stored alongside SpecProject state; survives cycle boundaries.
    """

    bindings: list[CriterionBinding] = field(default_factory=list)
    # Per-cycle signal results, keyed by cycle number
    signal_history: dict[int, list[SignalResult]] = field(default_factory=dict)
    # Git context for staleness detection
    last_diff_hash: str = ""
    last_collection_cycle: int = 0

    def to_dict(self) -> dict:
        return {
            "bindings": [b.to_dict() for b in self.bindings],
            "signal_history": {
                str(k): [r.to_dict() for r in v]
                for k, v in self.signal_history.items()
            },
            "last_diff_hash": self.last_diff_hash,
            "last_collection_cycle": self.last_collection_cycle,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "VerificationState":
        state = cls()
        state.bindings = [CriterionBinding.from_dict(b) for b in data.get("bindings", [])]
        state.signal_history = {
            int(k): [SignalResult.from_dict(r) for r in v]
            for k, v in data.get("signal_history", {}).items()
        }
        state.last_diff_hash = data.get("last_diff_hash", "")
        state.last_collection_cycle = int(data.get("last_collection_cycle", 0))
        return state


# ---------------------------------------------------------------------------
# Signal Collection
# ---------------------------------------------------------------------------

# Type alias for the sandbox execution function injected from SandboxExecutor
ExecuteFn = Callable[[str, str, float], tuple[bool, int, str, str]]
"""(command, cwd, timeout) -> (success, exit_code, stdout, stderr)"""


def collect_signals(
    bindings: list[CriterionBinding],
    cwd: str,
    execute_fn: ExecuteFn,
    cycle: int,
    modified_files: Optional[set[str]] = None,
) -> list[SignalResult]:
    """Collect all verification signals for the given bindings.

    Called after build phase completes, before/during review.

    Args:
        bindings: Criterion-to-signal mappings.
        cwd: Working directory for command execution.
        execute_fn: Sandboxed command runner (injected for testability).
        cycle: Current spec cycle number.
        modified_files: Files changed in this build (for context hashing).

    Returns:
        Flat list of SignalResults. Each result carries its spec so callers
        can correlate back to criteria.
    """
    results: list[SignalResult] = []
    context_hash = _compute_context_hash(modified_files or set())

    # Deduplicate: same command should only run once even if bound to multiple criteria
    seen_commands: dict[str, SignalResult] = {}

    for binding in bindings:
        for spec in binding.signals:
            cache_key = f"{spec.signal_type.value}:{spec.command}:{spec.file_path}:{spec.pattern}"
            if cache_key in seen_commands:
                results.append(seen_commands[cache_key])
                continue

            result = _collect_single_signal(spec, cwd, execute_fn, context_hash)
            seen_commands[cache_key] = result
            results.append(result)

    return results


def _collect_single_signal(
    spec: SignalSpec,
    cwd: str,
    execute_fn: ExecuteFn,
    context_hash: str,
) -> SignalResult:
    """Collect one signal with error containment."""
    start = time.time()
    try:
        match spec.signal_type:
            case SignalType.VERIFY_COMMAND | SignalType.BUILD_STATUS:
                return _run_command_signal(spec, cwd, execute_fn, context_hash, start)
            case SignalType.TEST_RESULT:
                return _run_test_signal(spec, cwd, execute_fn, context_hash, start)
            case SignalType.FILE_CHECK:
                return _run_file_check(spec, cwd, context_hash, start)
            case SignalType.PATTERN_MATCH:
                return _run_pattern_signal(spec, cwd, execute_fn, context_hash, start)
            case SignalType.GIT_DIFF:
                return _run_git_diff_signal(spec, cwd, execute_fn, context_hash, start)
            case _:
                return SignalResult(
                    spec=spec, outcome=SignalOutcome.SKIPPED,
                    elapsed_seconds=time.time() - start,
                    context_hash=context_hash,
                    error_detail=f"Unknown signal type: {spec.signal_type}",
                )
    except Exception as e:
        return SignalResult(
            spec=spec, outcome=SignalOutcome.ERROR,
            elapsed_seconds=time.time() - start,
            context_hash=context_hash,
            error_detail=str(e)[:500],
        )


def _run_command_signal(
    spec: SignalSpec, cwd: str, execute_fn: ExecuteFn,
    context_hash: str, start: float,
) -> SignalResult:
    success, exit_code, stdout, stderr = execute_fn(spec.command, cwd, spec.timeout_seconds)
    outcome = SignalOutcome.PASS if success else SignalOutcome.FAIL
    # If pattern is specified, also check output
    matched = True
    if spec.pattern:
        matched = bool(re.search(spec.pattern, stdout, re.MULTILINE))
        if not matched:
            outcome = SignalOutcome.FAIL
    return SignalResult(
        spec=spec, outcome=outcome, exit_code=exit_code,
        stdout=stdout, stderr=stderr, matched_pattern=matched,
        elapsed_seconds=time.time() - start, context_hash=context_hash,
    )


def _run_test_signal(
    spec: SignalSpec, cwd: str, execute_fn: ExecuteFn,
    context_hash: str, start: float,
) -> SignalResult:
    """Run test command and parse pass/fail counts from pytest-style output."""
    success, exit_code, stdout, stderr = execute_fn(spec.command, cwd, spec.timeout_seconds)
    # Parse pytest summary: "X passed, Y failed"
    passed_match = re.search(r"(\d+)\s+passed", stdout)
    failed_match = re.search(r"(\d+)\s+failed", stdout)
    has_failures = failed_match and int(failed_match.group(1)) > 0
    outcome = SignalOutcome.PASS if (success and not has_failures) else SignalOutcome.FAIL
    return SignalResult(
        spec=spec, outcome=outcome, exit_code=exit_code,
        stdout=stdout, stderr=stderr,
        matched_pattern=passed_match is not None,
        elapsed_seconds=time.time() - start, context_hash=context_hash,
    )


def _run_file_check(
    spec: SignalSpec, cwd: str, context_hash: str, start: float,
) -> SignalResult:
    """Check file existence and optionally match content pattern."""
    target = Path(cwd) / spec.file_path if not Path(spec.file_path).is_absolute() else Path(spec.file_path)
    if not target.exists():
        return SignalResult(
            spec=spec, outcome=SignalOutcome.FAIL,
            elapsed_seconds=time.time() - start, context_hash=context_hash,
            error_detail=f"File not found: {target}",
        )
    matched = True
    if spec.pattern:
        try:
            content = target.read_text(errors="replace")[:50_000]
            matched = bool(re.search(spec.pattern, content, re.MULTILINE))
        except OSError as e:
            return SignalResult(
                spec=spec, outcome=SignalOutcome.ERROR,
                elapsed_seconds=time.time() - start, context_hash=context_hash,
                error_detail=f"Read error: {e}",
            )
    outcome = SignalOutcome.PASS if matched else SignalOutcome.FAIL
    return SignalResult(
        spec=spec, outcome=outcome, matched_pattern=matched,
        elapsed_seconds=time.time() - start, context_hash=context_hash,
    )


def _run_pattern_signal(
    spec: SignalSpec, cwd: str, execute_fn: ExecuteFn,
    context_hash: str, start: float,
) -> SignalResult:
    """Run command, then match pattern against output."""
    if not spec.command:
        return _run_file_check(spec, cwd, context_hash, start)
    return _run_command_signal(spec, cwd, execute_fn, context_hash, start)


def _run_git_diff_signal(
    spec: SignalSpec, cwd: str, execute_fn: ExecuteFn,
    context_hash: str, start: float,
) -> SignalResult:
    """Analyze git diff for expected file changes."""
    cmd = spec.command or "git diff --stat HEAD~1"
    success, exit_code, stdout, stderr = execute_fn(cmd, cwd, spec.timeout_seconds)
    matched = True
    if spec.pattern:
        matched = bool(re.search(spec.pattern, stdout, re.MULTILINE))
    outcome = SignalOutcome.PASS if (success and matched) else SignalOutcome.FAIL
    return SignalResult(
        spec=spec, outcome=outcome, exit_code=exit_code,
        stdout=stdout, stderr=stderr, matched_pattern=matched,
        elapsed_seconds=time.time() - start, context_hash=context_hash,
    )


# ---------------------------------------------------------------------------
# Staleness Detection
# ---------------------------------------------------------------------------


def check_staleness(
    result: SignalResult,
    current_modified_files: set[str],
) -> bool:
    """A signal is stale if files changed since collection.

    Compares the context_hash at collection time against the current
    set of modified files. If they differ, the signal may no longer
    reflect reality.
    """
    if not result.context_hash:
        return True  # No hash means we can't verify freshness
    current_hash = _compute_context_hash(current_modified_files)
    return result.context_hash != current_hash


def _compute_context_hash(modified_files: set[str]) -> str:
    """Deterministic hash of the set of modified files."""
    if not modified_files:
        return ""
    sorted_files = sorted(modified_files)
    return hashlib.sha256("|".join(sorted_files).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Score Anchoring
# ---------------------------------------------------------------------------


def apply_objective_anchors(
    llm_results: dict[int, bool],
    bindings: list[CriterionBinding],
    signal_results: list[SignalResult],
    current_modified_files: Optional[set[str]] = None,
) -> list[AnchoredScore]:
    """Modify LLM criterion scores based on collected objective signals.

    Args:
        llm_results: {criterion_index: True/False} from LLM evaluation.
        bindings: Criterion-to-signal mappings.
        signal_results: Collected signals from collect_signals().
        current_modified_files: For staleness check.

    Returns:
        AnchoredScore per criterion with final verdict and explanation.

    Anchoring rules:
        - OVERRIDE + FAIL: Cap score at confidence_cap regardless of LLM.
        - OVERRIDE + PASS: Score = max(llm_score, confidence_boost).
        - MODIFIER + FAIL: Reduce score by (1 - confidence_boost).
        - MODIFIER + PASS: Increase score by confidence_boost.
        - ERROR/SKIPPED signals are noted but don't modify score.
        - Stale signals are logged but ignored (count reported).
    """
    modified_files = current_modified_files or set()

    # Index signal results by spec identity for lookup
    binding_map: dict[int, CriterionBinding] = {b.criterion_index: b for b in bindings}

    scores: list[AnchoredScore] = []

    for criterion_idx, llm_pass in llm_results.items():
        llm_score = 1.0 if llm_pass else 0.0
        binding = binding_map.get(criterion_idx)

        if not binding or not binding.signals:
            # No objective signals bound; pass through LLM score unchanged
            scores.append(AnchoredScore(
                criterion_index=criterion_idx,
                llm_score=llm_score,
                anchored_score=llm_score,
                explanation="No objective signals bound.",
            ))
            continue

        anchored = llm_score
        overridden = False
        applied: list[SignalResult] = []
        stale_count = 0
        explanations: list[str] = []

        for spec in binding.signals:
            # Find matching result
            matching = [
                r for r in signal_results
                if r.spec.signal_type == spec.signal_type
                and r.spec.command == spec.command
                and r.spec.file_path == spec.file_path
            ]
            if not matching:
                continue
            result = matching[0]

            # Staleness gate
            if check_staleness(result, modified_files):
                stale_count += 1
                explanations.append(f"[stale] {spec.signal_type.value} ignored")
                continue

            # Skip non-conclusive outcomes
            if result.outcome in (SignalOutcome.ERROR, SignalOutcome.SKIPPED):
                explanations.append(
                    f"[{result.outcome.value}] {spec.signal_type.value}: {result.error_detail[:80]}"
                )
                continue

            applied.append(result)
            signal_passed = result.outcome == SignalOutcome.PASS

            if spec.anchor_mode == AnchorMode.OVERRIDE:
                if not signal_passed:
                    # Hard cap: objective failure overrides LLM optimism
                    anchored = min(anchored, spec.confidence_cap)
                    overridden = True
                    explanations.append(
                        f"OVERRIDE FAIL: {spec.signal_type.value} capped score at {spec.confidence_cap}"
                    )
                else:
                    anchored = max(anchored, 1.0 - spec.confidence_cap)
                    explanations.append(
                        f"OVERRIDE PASS: {spec.signal_type.value} floor at {1.0 - spec.confidence_cap}"
                    )
            else:  # MODIFIER
                if signal_passed:
                    anchored = min(1.0, anchored + spec.confidence_boost)
                    explanations.append(
                        f"MODIFIER +{spec.confidence_boost}: {spec.signal_type.value} passed"
                    )
                else:
                    anchored = max(0.0, anchored - spec.confidence_boost)
                    explanations.append(
                        f"MODIFIER -{spec.confidence_boost}: {spec.signal_type.value} failed"
                    )

        scores.append(AnchoredScore(
            criterion_index=criterion_idx,
            llm_score=llm_score,
            anchored_score=max(0.0, min(1.0, anchored)),
            signals_applied=applied,
            overridden=overridden,
            stale_signals=stale_count,
            explanation="; ".join(explanations) if explanations else "Signals applied.",
        ))

    return scores


def scores_to_pass_fail(
    anchored_scores: list[AnchoredScore],
    pass_threshold: float = 0.6,
) -> dict[int, bool]:
    """Convert anchored float scores to boolean PASS/FAIL for CriteriaTracker.

    Args:
        anchored_scores: Output of apply_objective_anchors().
        pass_threshold: Minimum anchored_score to count as PASS.

    Returns:
        {criterion_index: bool} compatible with CriteriaTracker.batch_update().
    """
    return {
        s.criterion_index: s.anchored_score >= pass_threshold
        for s in anchored_scores
    }


# ---------------------------------------------------------------------------
# Binding Helpers
# ---------------------------------------------------------------------------


def create_global_binding(
    criteria_count: int,
    verify_command: str,
    anchor_mode: AnchorMode = AnchorMode.OVERRIDE,
) -> list[CriterionBinding]:
    """Create bindings that apply verify_command to ALL criteria.

    This is the simplest binding mode: the project's verify_command
    acts as a global gate for all criteria.
    """
    if not verify_command:
        return []
    spec = SignalSpec(
        signal_type=SignalType.VERIFY_COMMAND,
        command=verify_command,
        anchor_mode=anchor_mode,
        confidence_boost=0.3,
        confidence_cap=0.3,
        description="Global verify_command gate",
    )
    return [
        CriterionBinding(
            criterion_index=i,
            signals=[spec],
            binding_source="global",
        )
        for i in range(criteria_count)
    ]


def create_test_binding(
    criterion_index: int,
    test_command: str,
    anchor_mode: AnchorMode = AnchorMode.OVERRIDE,
) -> CriterionBinding:
    """Bind a specific test command to one criterion."""
    spec = SignalSpec(
        signal_type=SignalType.TEST_RESULT,
        command=test_command,
        anchor_mode=anchor_mode,
        confidence_boost=0.35,
        confidence_cap=0.25,
        description=f"Test binding for criterion {criterion_index}",
    )
    return CriterionBinding(
        criterion_index=criterion_index,
        signals=[spec],
        binding_source="user",
    )
