"""Agent Runtime: structured turn-loop execution with broker-mediated model and tool calls.

Constructor REQUIRES ModelBroker and ToolBroker instances.
Raw model_fn / tool_executor callables are explicitly rejected.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..broker.model_broker import ModelBroker, ModelCallResult
from ..broker.tool_broker import DispatchRequest, DispatchResult, ToolBroker
from ..domain import (
    Attempt,
    EpochSet,
    GoalActivationAuthorization,
    TurnOutputType,
    new_id,
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ToolProposal:
    """A proposed tool invocation from the model."""

    capability: str
    arguments: dict
    proposal_index: int = 0


@dataclass
class TurnInput:
    """Everything the model needs for one turn."""

    turn_seq: int
    goal_summary: str
    plan_summary: str
    step_contract: dict
    attempt_id: str
    available_capabilities: list[str]
    remaining_budget: dict
    deadline: Optional[float]
    history: list[TurnRecord]
    checkpoint: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "turn_seq": self.turn_seq,
            "goal_summary": self.goal_summary,
            "plan_summary": self.plan_summary,
            "step_contract": self.step_contract,
            "attempt_id": self.attempt_id,
            "available_capabilities": self.available_capabilities,
            "remaining_budget": self.remaining_budget,
            "deadline": self.deadline,
            "history": [r.to_dict() for r in self.history],
            "checkpoint": self.checkpoint,
        }


@dataclass
class TurnRecord:
    """Immutable record of a completed turn."""

    turn_seq: int
    input_hash: str
    output_type: str  # TurnOutputType value
    output_hash: str
    tool_proposals: list[dict]
    tool_results: list[dict]
    timestamp: float

    def to_dict(self) -> dict:
        return {
            "turn_seq": self.turn_seq,
            "input_hash": self.input_hash,
            "output_type": self.output_type,
            "output_hash": self.output_hash,
            "tool_proposals": self.tool_proposals,
            "tool_results": self.tool_results,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TurnRecord:
        return cls(
            turn_seq=data["turn_seq"],
            input_hash=data["input_hash"],
            output_type=data["output_type"],
            output_hash=data["output_hash"],
            tool_proposals=data.get("tool_proposals", []),
            tool_results=data.get("tool_results", []),
            timestamp=data["timestamp"],
        )


@dataclass
class TurnOutput:
    """Structured output from a single model turn."""

    output_type: TurnOutputType
    content: dict
    tool_proposals: list[ToolProposal] = field(default_factory=list)


@dataclass
class ContextSnapshot:
    """Compressed context for long-running tasks."""

    goal_summary: str
    plan_state: dict
    active_evidence: list[str]
    recent_results: list[dict]
    compression_version: str
    token_count: int


@dataclass
class RuntimeResult:
    """Final result of a turn loop execution."""

    final_output: Optional[TurnOutput]
    turn_count: int
    stop_reason: str  # "completed" | "no_progress" | "budget_exceeded" | "deadline" | "max_turns" | "blocked" | "model_error"
    history: list[TurnRecord]
    checkpoint_path: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256(data: Any) -> str:
    """Compute sha256 hex digest of JSON-serialized data."""
    raw = json.dumps(data, sort_keys=True, default=str).encode()
    return hashlib.sha256(raw).hexdigest()


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


# ---------------------------------------------------------------------------
# AgentRuntime
# ---------------------------------------------------------------------------


class AgentRuntime:
    """Manages model turns with broker-mediated calls, persistence, and budget enforcement.

    Constructor REQUIRES model_broker and tool_broker.
    Raw callables are explicitly rejected with a clear error message.

    Each turn: persist input hash -> call model via broker -> persist output ->
    parse structured output -> dispatch tools via broker -> checkpoint.
    """

    def __init__(
        self,
        model_broker: ModelBroker,
        tool_broker: ToolBroker,
        checkpoint_dir: str,
        max_turns: int = 50,
        no_progress_threshold: int = 5,
        timeout_seconds: float = 3600.0,
        context_overflow_tokens: int = 128000,
    ):
        # Reject raw callables masquerading as brokers
        if callable(model_broker) and not isinstance(model_broker, ModelBroker):
            raise TypeError(
                "AgentRuntime requires a ModelBroker instance, not a raw callable. "
                "Wrap your model function in a ModelBroker for budget/rate-limit enforcement."
            )
        if callable(tool_broker) and not isinstance(tool_broker, ToolBroker):
            raise TypeError(
                "AgentRuntime requires a ToolBroker instance, not a raw callable. "
                "Wrap your tool executor in a ToolBroker for policy/epoch enforcement."
            )
        if not isinstance(model_broker, ModelBroker):
            raise TypeError(
                f"model_broker must be a ModelBroker instance, got {type(model_broker).__name__}"
            )
        if not isinstance(tool_broker, ToolBroker):
            raise TypeError(
                f"tool_broker must be a ToolBroker instance, got {type(tool_broker).__name__}"
            )

        self.model_broker = model_broker
        self.tool_broker = tool_broker
        self.checkpoint_dir = checkpoint_dir
        self.max_turns = max_turns
        self.no_progress_threshold = no_progress_threshold
        self.timeout_seconds = timeout_seconds
        self.context_overflow_tokens = context_overflow_tokens
        _ensure_dir(checkpoint_dir)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute_attempt(
        self,
        attempt_id: str,
        authorization: GoalActivationAuthorization,
        initial_input: TurnInput,
        epochs: EpochSet,
        run_id: str = "",
        step_id: str = "",
        employee_id: str = "",
    ) -> RuntimeResult:
        """Execute a full attempt as a structured turn loop.

        This is the primary execution entrypoint. Each turn:
        1. Check budget / deadline / max_turns / context overflow
        2. Call model via model_broker (with budget+rate-limit)
        3. Parse structured output
        4. If TOOL_PROPOSAL: dispatch tools via tool_broker, feed results
        5. If SUBMIT_OUTPUT/BLOCKED/REPLAN_REQUEST: stop
        6. If REQUEST_CONTEXT: compress and continue
        7. Checkpoint after each turn
        8. Detect no-progress
        """
        history: list[TurnRecord] = list(initial_input.history)
        current_input = initial_input
        final_output: Optional[TurnOutput] = None
        stop_reason = "max_turns"
        checkpoint_path = ""
        start_time = time.time()

        for turn_seq in range(current_input.turn_seq, self.max_turns):
            # Timeout check
            elapsed = time.time() - start_time
            if elapsed > self.timeout_seconds:
                stop_reason = "deadline"
                break

            # Deadline check
            if current_input.deadline and time.time() > current_input.deadline:
                stop_reason = "deadline"
                break

            # Context overflow check
            context_size = self._estimate_context_tokens(current_input)
            if context_size > self.context_overflow_tokens:
                # Compress context before continuing
                snapshot = self._build_context_snapshot(current_input)
                current_input = self._rebuild_input_from_snapshot(
                    current_input, snapshot, turn_seq, history
                )

            # Update turn_seq
            current_input.turn_seq = turn_seq

            # Execute model turn via broker
            input_hash = _sha256(current_input.to_dict())
            model_result = await self.model_broker.call(
                authorization=authorization,
                prompt_ref=current_input.to_dict(),
                run_id=run_id,
                attempt_id=attempt_id,
            )

            if not model_result.success:
                # Model call failed (budget, rate-limit, auth, or model error)
                stop_reason = "model_error"
                if "Budget" in model_result.error:
                    stop_reason = "budget_exceeded"
                elif "Rate" in model_result.error:
                    stop_reason = "model_error"
                break

            # Parse the model response into structured TurnOutput
            raw_output = model_result.response
            turn_output = self._parse_turn_output(raw_output)
            output_hash = _sha256({
                "output_type": turn_output.output_type.value,
                "content": turn_output.content,
            })

            # Persist output blob
            self._persist_blob(attempt_id, turn_seq, input_hash, raw_output, output_hash)

            # Execute tools if proposed via tool_broker
            tool_results: list[dict] = []
            if turn_output.output_type == TurnOutputType.TOOL_PROPOSAL and turn_output.tool_proposals:
                for proposal in turn_output.tool_proposals:
                    dispatch_req = DispatchRequest(
                        capability=proposal.capability,
                        arguments=proposal.arguments,
                        run_id=run_id,
                        step_id=step_id,
                        attempt_id=attempt_id,
                        plan_epoch=epochs.plan_epoch,
                        employee_id=employee_id,
                    )
                    dispatch_result = await self.tool_broker.dispatch(dispatch_req, epochs)
                    tool_results.append({
                        "success": dispatch_result.success,
                        "effect_id": dispatch_result.effect_id,
                        "result_data": dispatch_result.result_data,
                        "error": dispatch_result.error,
                    })

            # Record turn
            record = TurnRecord(
                turn_seq=turn_seq,
                input_hash=input_hash,
                output_type=turn_output.output_type.value,
                output_hash=output_hash,
                tool_proposals=[
                    {"capability": p.capability, "arguments": p.arguments, "proposal_index": p.proposal_index}
                    for p in turn_output.tool_proposals
                ],
                tool_results=tool_results,
                timestamp=time.time(),
            )
            history.append(record)

            # Checkpoint
            checkpoint_path = await self._save_checkpoint(
                attempt_id,
                {
                    "turn_seq": turn_seq,
                    "history": [r.to_dict() for r in history],
                    "last_output_type": turn_output.output_type.value,
                },
            )

            # No-progress detection
            if self._detect_no_progress(history):
                stop_reason = "no_progress"
                final_output = turn_output
                break

            # Route based on output type
            if turn_output.output_type == TurnOutputType.SUBMIT_OUTPUT:
                stop_reason = "completed"
                final_output = turn_output
                break
            elif turn_output.output_type == TurnOutputType.BLOCKED:
                stop_reason = "blocked"
                final_output = turn_output
                break
            elif turn_output.output_type == TurnOutputType.REPLAN_REQUEST:
                stop_reason = "completed"
                final_output = turn_output
                break
            elif turn_output.output_type == TurnOutputType.REQUEST_CONTEXT:
                snapshot = self._build_context_snapshot(current_input)
                current_input = self._rebuild_input_from_snapshot(
                    current_input, snapshot, turn_seq + 1, history
                )
            elif turn_output.output_type == TurnOutputType.TOOL_PROPOSAL:
                # Continue with tool results in history
                current_input = TurnInput(
                    turn_seq=turn_seq + 1,
                    goal_summary=current_input.goal_summary,
                    plan_summary=current_input.plan_summary,
                    step_contract=current_input.step_contract,
                    attempt_id=current_input.attempt_id,
                    available_capabilities=current_input.available_capabilities,
                    remaining_budget=current_input.remaining_budget,
                    deadline=current_input.deadline,
                    history=history,
                    checkpoint=current_input.checkpoint,
                )
            else:
                stop_reason = "blocked"
                final_output = turn_output
                break

        return RuntimeResult(
            final_output=final_output,
            turn_count=len(history) - len(initial_input.history),
            stop_reason=stop_reason,
            history=history,
            checkpoint_path=checkpoint_path,
        )

    # ------------------------------------------------------------------
    # Checkpoint persistence
    # ------------------------------------------------------------------

    async def _save_checkpoint(self, attempt_id: str, state: dict) -> str:
        """Save checkpoint JSON file, return path."""
        attempt_dir = os.path.join(self.checkpoint_dir, attempt_id)
        _ensure_dir(attempt_dir)
        turn_seq = state.get("turn_seq", 0)
        path = os.path.join(attempt_dir, f"checkpoint_{turn_seq}.json")
        with open(path, "w") as f:
            json.dump(state, f)
        return path

    async def load_checkpoint(self, attempt_id: str) -> Optional[dict]:
        """Load latest checkpoint for an attempt (highest turn_seq)."""
        attempt_dir = os.path.join(self.checkpoint_dir, attempt_id)
        if not os.path.isdir(attempt_dir):
            return None

        checkpoints = [
            f for f in os.listdir(attempt_dir)
            if f.startswith("checkpoint_") and f.endswith(".json")
        ]
        if not checkpoints:
            return None

        def _seq(name: str) -> int:
            try:
                return int(name.removeprefix("checkpoint_").removesuffix(".json"))
            except ValueError:
                return -1

        latest = max(checkpoints, key=_seq)
        path = os.path.join(attempt_dir, latest)
        with open(path) as f:
            return json.load(f)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _parse_turn_output(self, raw: dict) -> TurnOutput:
        """Parse raw model response into structured TurnOutput."""
        output_type = TurnOutputType(raw.get("output_type", "blocked"))
        content = raw.get("content", {})
        proposals = [
            ToolProposal(
                capability=p.get("capability", ""),
                arguments=p.get("arguments", {}),
                proposal_index=p.get("proposal_index", i),
            )
            for i, p in enumerate(raw.get("tool_proposals", []))
        ]
        return TurnOutput(
            output_type=output_type,
            content=content,
            tool_proposals=proposals,
        )

    def _persist_blob(
        self, attempt_id: str, turn_seq: int, input_hash: str, raw_output: dict, output_hash: str
    ) -> None:
        """Persist output blob to disk."""
        blob_dir = os.path.join(self.checkpoint_dir, attempt_id, "blobs")
        _ensure_dir(blob_dir)
        blob_path = os.path.join(blob_dir, f"turn_{turn_seq}_{output_hash[:12]}.json")
        with open(blob_path, "w") as f:
            json.dump({"input_hash": input_hash, "output": raw_output}, f)

    def _detect_no_progress(self, history: list[TurnRecord]) -> bool:
        """Check if last N turns made no meaningful progress."""
        n = self.no_progress_threshold
        if len(history) < n:
            return False

        recent = history[-n:]

        # All same output type AND same output hash
        types = {r.output_type for r in recent}
        hashes = {r.output_hash for r in recent}
        if len(types) == 1 and len(hashes) == 1:
            return True

        # No tool results in last N turns (no new work) and not terminal
        if all(len(r.tool_results) == 0 for r in recent):
            terminal = {TurnOutputType.SUBMIT_OUTPUT.value, TurnOutputType.REPLAN_REQUEST.value}
            if all(r.output_type not in terminal for r in recent):
                return True

        return False

    def _estimate_context_tokens(self, turn_input: TurnInput) -> int:
        """Rough token estimation (4 chars per token)."""
        content = json.dumps(turn_input.to_dict(), default=str)
        return len(content) // 4

    def _build_context_snapshot(self, turn_input: TurnInput) -> ContextSnapshot:
        """Compress context for long-running tasks."""
        recent_results: list[dict] = []
        for record in turn_input.history[-5:]:
            if record.tool_results:
                recent_results.extend(record.tool_results)

        active_evidence = list({
            r.output_hash[:16] for r in turn_input.history[-10:]
        })

        plan_state = {
            "step_contract": turn_input.step_contract,
            "turns_completed": len(turn_input.history),
            "remaining_budget": turn_input.remaining_budget,
        }

        snapshot_content = json.dumps({
            "goal": turn_input.goal_summary,
            "plan": plan_state,
            "evidence": active_evidence,
            "results": recent_results,
        }, default=str)
        token_count = len(snapshot_content) // 4

        return ContextSnapshot(
            goal_summary=turn_input.goal_summary,
            plan_state=plan_state,
            active_evidence=active_evidence,
            recent_results=recent_results,
            compression_version="v1",
            token_count=token_count,
        )

    def _rebuild_input_from_snapshot(
        self,
        current_input: TurnInput,
        snapshot: ContextSnapshot,
        next_turn_seq: int,
        history: list[TurnRecord],
    ) -> TurnInput:
        """Rebuild TurnInput from a compressed snapshot."""
        return TurnInput(
            turn_seq=next_turn_seq,
            goal_summary=snapshot.goal_summary,
            plan_summary=current_input.plan_summary,
            step_contract=current_input.step_contract,
            attempt_id=current_input.attempt_id,
            available_capabilities=current_input.available_capabilities,
            remaining_budget=current_input.remaining_budget,
            deadline=current_input.deadline,
            history=history,
            checkpoint={
                "context_snapshot": {
                    "plan_state": snapshot.plan_state,
                    "active_evidence": snapshot.active_evidence,
                    "recent_results": snapshot.recent_results,
                }
            },
        )
