"""Compatibility re-exports for immutable autonomous domain models."""

from .domain import *  # noqa: F403
from .domain import __all__ as _DOMAIN_ALL
from .domain.ids import new_id as _new_id

__all__ = [*_DOMAIN_ALL, "_new_id"]

'''
# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------

def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class GoalType(Enum):
    ONE_SHOT = "one_shot"
    SCHEDULED = "scheduled"
    STANDING = "standing"


class GoalState(Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    DEGRADED_SOURCE = "degraded_source"
    BLOCKED_SOURCE = "blocked_source"
    CANCELED = "canceled"
    EXPIRED = "expired"


class RunState(Enum):
    QUEUED = "queued"
    PLANNING = "planning"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    REPLAN_PENDING = "replan_pending"
    BLOCKED = "blocked"
    RECONCILIATION_PENDING = "reconciliation_pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"
    SUPERSEDED_PENDING_DRAIN = "superseded_pending_drain"


class StepState(Enum):
    PENDING = "pending"
    READY = "ready"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELED = "canceled"


class AttemptState(Enum):
    ACTIVE = "active"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELED = "canceled"


class EffectState(Enum):
    PREPARED = "prepared"
    EXECUTING = "executing"
    COMMITTED = "committed"
    UNKNOWN_EFFECT = "unknown_effect"
    COMPENSATING = "compensating"
    COMPENSATED = "compensated"
    MANUAL_RECONCILIATION = "manual_reconciliation"
    RELEASED = "released"


class RiskLevel(Enum):
    R0 = "r0"  # Read-only, deterministic queries
    R1 = "r1"  # Idempotent writes, safe retries
    R2 = "r2"  # Sensitive reads/writes, idempotent delete
    R3 = "r3"  # Messaging, non-idempotent writes, permission changes
    R4 = "r4"  # Financial, production changes, irreversible


class AutonomyMode(Enum):
    ASSIST = "assist"
    SUPERVISED = "supervised"
    BOUNDED_AUTONOMOUS = "bounded_autonomous"


class OracleType(Enum):
    COMMAND = "command"
    RESOURCE = "resource"
    DATA = "data"
    SCHEMA = "schema"
    REVIEW = "review"
    HUMAN = "human"


class VerificationResult(Enum):
    PASSED = "passed"
    EXECUTION_DEFECT = "execution_defect"
    PLAN_DEFECT = "plan_defect"
    ENVIRONMENT_BLOCKED = "environment_blocked"
    PERMISSION_BLOCKED = "permission_blocked"
    UNVERIFIABLE = "unverifiable"
    TERMINAL_FAILURE = "terminal_failure"


class TurnOutputType(Enum):
    TOOL_PROPOSAL = "tool_proposal"
    REQUEST_CONTEXT = "request_context"
    SUBMIT_OUTPUT = "submit_output"
    REPLAN_REQUEST = "replan_request"
    BLOCKED = "blocked"


class MisfirePolicy(Enum):
    RUN_ALL = "run_all"
    SKIP = "skip"
    RUN_LATEST = "run_latest"


class OverlapPolicy(Enum):
    FORBID = "forbid"
    QUEUE = "queue"
    ALLOW_PARALLEL = "allow_parallel"


class WorkerType(Enum):
    LOGICAL = "logical"
    VISIBLE = "visible"
    EPHEMERAL = "ephemeral"


class EmployeeState(Enum):
    DRAFT = "draft"
    PROVISIONING_APP = "provisioning_app"
    STORING_CREDENTIAL = "storing_credential"
    CONFIGURING = "configuring"
    VALIDATING = "validating"
    ACTIVE = "active"
    RETIRING = "retiring"
    ARCHIVED = "archived"


# ---------------------------------------------------------------------------
# Epoch tracking
# ---------------------------------------------------------------------------

@dataclass
class EpochSet:
    """All epoch counters for a goal/run lifecycle."""
    definition_version: int = 1
    admission_epoch: int = 1
    revocation_epoch: int = 0
    run_control_epoch: int = 0
    plan_epoch: int = 0
    kill_epoch: int = 0

    def to_dict(self) -> dict:
        return {
            "definition_version": self.definition_version,
            "admission_epoch": self.admission_epoch,
            "revocation_epoch": self.revocation_epoch,
            "run_control_epoch": self.run_control_epoch,
            "plan_epoch": self.plan_epoch,
            "kill_epoch": self.kill_epoch,
        }

    @classmethod
    def from_dict(cls, data: dict) -> EpochSet:
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


# ---------------------------------------------------------------------------
# GoalDefinition
# ---------------------------------------------------------------------------

@dataclass
class GoalCriterion:
    """A single acceptance criterion."""
    criterion_id: str = field(default_factory=lambda: _new_id("crit"))
    description: str = ""
    oracle_type: OracleType = OracleType.COMMAND
    oracle_config: dict = field(default_factory=dict)
    criterion_hash: str = ""

    def compute_hash(self) -> str:
        content = f"{self.description}|{self.oracle_type.value}|{self.oracle_config}"
        self.criterion_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
        return self.criterion_hash

    def to_dict(self) -> dict:
        return {
            "criterion_id": self.criterion_id,
            "description": self.description,
            "oracle_type": self.oracle_type.value,
            "oracle_config": self.oracle_config,
            "criterion_hash": self.criterion_hash,
        }

    @classmethod
    def from_dict(cls, data: dict) -> GoalCriterion:
        return cls(
            criterion_id=data["criterion_id"],
            description=data["description"],
            oracle_type=OracleType(data["oracle_type"]),
            oracle_config=data.get("oracle_config", {}),
            criterion_hash=data.get("criterion_hash", ""),
        )


@dataclass
class GoalSpec:
    """Structured goal specification."""
    objective: str = ""
    deliverables: list[str] = field(default_factory=list)
    scope: str = ""
    constraints: list[str] = field(default_factory=list)
    deadline: Optional[float] = None
    criteria: list[GoalCriterion] = field(default_factory=list)
    data_sources: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    budget: Optional[dict] = None
    risks: list[str] = field(default_factory=list)
    notification_policy: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "objective": self.objective,
            "deliverables": self.deliverables,
            "scope": self.scope,
            "constraints": self.constraints,
            "deadline": self.deadline,
            "criteria": [c.to_dict() for c in self.criteria],
            "data_sources": self.data_sources,
            "tools": self.tools,
            "budget": self.budget,
            "risks": self.risks,
            "notification_policy": self.notification_policy,
        }

    @classmethod
    def from_dict(cls, data: dict) -> GoalSpec:
        criteria = [GoalCriterion.from_dict(c) for c in data.get("criteria", [])]
        return cls(
            objective=data.get("objective", ""),
            deliverables=data.get("deliverables", []),
            scope=data.get("scope", ""),
            constraints=data.get("constraints", []),
            deadline=data.get("deadline"),
            criteria=criteria,
            data_sources=data.get("data_sources", []),
            tools=data.get("tools", []),
            budget=data.get("budget"),
            risks=data.get("risks", []),
            notification_policy=data.get("notification_policy", {}),
        )


@dataclass
class GoalDefinition:
    """Long-lived goal definition, not terminated by a single run."""
    goal_id: str = field(default_factory=lambda: _new_id("goal"))
    goal_type: GoalType = GoalType.ONE_SHOT
    state: GoalState = GoalState.DRAFT
    spec: GoalSpec = field(default_factory=GoalSpec)
    epochs: EpochSet = field(default_factory=EpochSet)
    autonomy_mode: AutonomyMode = AutonomyMode.SUPERVISED
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    owner_id: str = ""
    standing_order: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "goal_id": self.goal_id,
            "goal_type": self.goal_type.value,
            "state": self.state.value,
            "spec": self.spec.to_dict(),
            "epochs": self.epochs.to_dict(),
            "autonomy_mode": self.autonomy_mode.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "owner_id": self.owner_id,
            "standing_order": self.standing_order,
        }

    @classmethod
    def from_dict(cls, data: dict) -> GoalDefinition:
        return cls(
            goal_id=data["goal_id"],
            goal_type=GoalType(data["goal_type"]),
            state=GoalState(data["state"]),
            spec=GoalSpec.from_dict(data.get("spec", {})),
            epochs=EpochSet.from_dict(data.get("epochs", {})),
            autonomy_mode=AutonomyMode(data.get("autonomy_mode", "supervised")),
            created_at=data.get("created_at", 0),
            updated_at=data.get("updated_at", 0),
            owner_id=data.get("owner_id", ""),
            standing_order=data.get("standing_order"),
        )


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

@dataclass
class Run:
    """A single execution of a goal."""
    run_id: str = field(default_factory=lambda: _new_id("run"))
    goal_id: str = ""
    goal_version: int = 1
    occurrence_key: str = ""
    trigger_event_id: str = ""
    state: RunState = RunState.QUEUED
    plan_epoch: int = 0
    budget_ledger_id: str = ""
    created_at: float = field(default_factory=time.time)
    deadline: Optional[float] = None
    supersedes_run_id: Optional[str] = None
    authorization_snapshot_id: str = ""
    run_control_epoch: int = 0

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "goal_id": self.goal_id,
            "goal_version": self.goal_version,
            "occurrence_key": self.occurrence_key,
            "trigger_event_id": self.trigger_event_id,
            "state": self.state.value,
            "plan_epoch": self.plan_epoch,
            "budget_ledger_id": self.budget_ledger_id,
            "created_at": self.created_at,
            "deadline": self.deadline,
            "supersedes_run_id": self.supersedes_run_id,
            "authorization_snapshot_id": self.authorization_snapshot_id,
            "run_control_epoch": self.run_control_epoch,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Run:
        return cls(
            run_id=data["run_id"],
            goal_id=data.get("goal_id", ""),
            goal_version=data.get("goal_version", 1),
            occurrence_key=data.get("occurrence_key", ""),
            trigger_event_id=data.get("trigger_event_id", ""),
            state=RunState(data.get("state", "queued")),
            plan_epoch=data.get("plan_epoch", 0),
            budget_ledger_id=data.get("budget_ledger_id", ""),
            created_at=data.get("created_at", 0),
            deadline=data.get("deadline"),
            supersedes_run_id=data.get("supersedes_run_id"),
            authorization_snapshot_id=data.get("authorization_snapshot_id", ""),
            run_control_epoch=data.get("run_control_epoch", 0),
        )


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

@dataclass
class PlanStep:
    """A single step in a plan DAG."""
    step_id: str = field(default_factory=lambda: _new_id("step"))
    name: str = ""
    description: str = ""
    state: StepState = StepState.PENDING
    depends_on: list[str] = field(default_factory=list)
    capability: str = ""
    arguments_schema: dict = field(default_factory=dict)
    verifier_oracle: Optional[dict] = None
    assigned_employee: str = ""
    max_attempts: int = 3
    timeout_seconds: float = 600.0
    criterion_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "step_id": self.step_id,
            "name": self.name,
            "description": self.description,
            "state": self.state.value,
            "depends_on": self.depends_on,
            "capability": self.capability,
            "arguments_schema": self.arguments_schema,
            "verifier_oracle": self.verifier_oracle,
            "assigned_employee": self.assigned_employee,
            "max_attempts": self.max_attempts,
            "timeout_seconds": self.timeout_seconds,
            "criterion_ids": self.criterion_ids,
        }

    @classmethod
    def from_dict(cls, data: dict) -> PlanStep:
        return cls(
            step_id=data["step_id"],
            name=data.get("name", ""),
            description=data.get("description", ""),
            state=StepState(data.get("state", "pending")),
            depends_on=data.get("depends_on", []),
            capability=data.get("capability", ""),
            arguments_schema=data.get("arguments_schema", {}),
            verifier_oracle=data.get("verifier_oracle"),
            assigned_employee=data.get("assigned_employee", ""),
            max_attempts=data.get("max_attempts", 3),
            timeout_seconds=data.get("timeout_seconds", 600.0),
            criterion_ids=data.get("criterion_ids", []),
        )


@dataclass
class GoalCriteriaCoverage:
    """Maps each criterion to steps and oracles."""
    mappings: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"mappings": self.mappings}

    @classmethod
    def from_dict(cls, data: dict) -> GoalCriteriaCoverage:
        return cls(mappings=data.get("mappings", {}))


@dataclass
class Plan:
    """Versioned DAG plan for a run."""
    plan_id: str = field(default_factory=lambda: _new_id("plan"))
    run_id: str = ""
    epoch: int = 1
    steps: list[PlanStep] = field(default_factory=list)
    criteria_coverage: GoalCriteriaCoverage = field(default_factory=GoalCriteriaCoverage)
    budget_estimate: Optional[dict] = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "run_id": self.run_id,
            "epoch": self.epoch,
            "steps": [s.to_dict() for s in self.steps],
            "criteria_coverage": self.criteria_coverage.to_dict(),
            "budget_estimate": self.budget_estimate,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Plan:
        steps = [PlanStep.from_dict(s) for s in data.get("steps", [])]
        coverage = GoalCriteriaCoverage.from_dict(data.get("criteria_coverage", {}))
        return cls(
            plan_id=data["plan_id"],
            run_id=data.get("run_id", ""),
            epoch=data.get("epoch", 1),
            steps=steps,
            criteria_coverage=coverage,
            budget_estimate=data.get("budget_estimate"),
            created_at=data.get("created_at", 0),
        )

    def get_ready_steps(self) -> list[PlanStep]:
        """Return steps whose dependencies are all SUCCEEDED."""
        done_ids = {s.step_id for s in self.steps if s.state == StepState.SUCCEEDED}
        return [
            s for s in self.steps
            if s.state == StepState.PENDING
            and all(dep in done_ids for dep in s.depends_on)
        ]

    def validate_dag(self) -> list[str]:
        """Check for cycles. Returns list of error messages."""
        visited: set[str] = set()
        path: set[str] = set()
        step_map = {s.step_id: s for s in self.steps}
        errors: list[str] = []

        def dfs(sid: str) -> bool:
            if sid in path:
                errors.append(f"Cycle detected involving step {sid}")
                return True
            if sid in visited:
                return False
            visited.add(sid)
            path.add(sid)
            step = step_map.get(sid)
            if step:
                for dep in step.depends_on:
                    if dep not in step_map:
                        errors.append(f"Step {sid} depends on unknown step {dep}")
                    elif dfs(dep):
                        return True
            path.discard(sid)
            return False

        for s in self.steps:
            dfs(s.step_id)
        return errors


# ---------------------------------------------------------------------------
# Attempt
# ---------------------------------------------------------------------------

@dataclass
class Attempt:
    """A single execution attempt for a step."""
    attempt_id: str = field(default_factory=lambda: _new_id("att"))
    step_id: str = ""
    run_id: str = ""
    state: AttemptState = AttemptState.ACTIVE
    lease_id: str = ""
    lease_expires: float = 0.0
    started_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    worker_id: str = ""
    turn_count: int = 0
    checkpoint_path: str = ""
    last_heartbeat: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "attempt_id": self.attempt_id,
            "step_id": self.step_id,
            "run_id": self.run_id,
            "state": self.state.value,
            "lease_id": self.lease_id,
            "lease_expires": self.lease_expires,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "worker_id": self.worker_id,
            "turn_count": self.turn_count,
            "checkpoint_path": self.checkpoint_path,
            "last_heartbeat": self.last_heartbeat,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Attempt:
        return cls(
            attempt_id=data["attempt_id"],
            step_id=data.get("step_id", ""),
            run_id=data.get("run_id", ""),
            state=AttemptState(data.get("state", "active")),
            lease_id=data.get("lease_id", ""),
            lease_expires=data.get("lease_expires", 0.0),
            started_at=data.get("started_at", 0),
            completed_at=data.get("completed_at"),
            worker_id=data.get("worker_id", ""),
            turn_count=data.get("turn_count", 0),
            checkpoint_path=data.get("checkpoint_path", ""),
            last_heartbeat=data.get("last_heartbeat", 0),
        )

    def is_lease_valid(self) -> bool:
        return self.lease_expires > time.time()


# ---------------------------------------------------------------------------
# Effect
# ---------------------------------------------------------------------------

@dataclass
class Effect:
    """Tracks a side-effect produced by a tool call."""
    effect_id: str = field(default_factory=lambda: _new_id("eff"))
    action_intent_id: str = ""
    execution_seq: int = 0
    state: EffectState = EffectState.PREPARED
    capability: str = ""
    resource_id: str = ""
    semantic_action_key: str = ""
    risk_level: RiskLevel = RiskLevel.R0
    attempt_id: str = ""
    run_id: str = ""
    created_at: float = field(default_factory=time.time)
    committed_at: Optional[float] = None
    evidence_hash: str = ""
    cleanup_grant_id: str = ""

    def to_dict(self) -> dict:
        return {
            "effect_id": self.effect_id,
            "action_intent_id": self.action_intent_id,
            "execution_seq": self.execution_seq,
            "state": self.state.value,
            "capability": self.capability,
            "resource_id": self.resource_id,
            "semantic_action_key": self.semantic_action_key,
            "risk_level": self.risk_level.value,
            "attempt_id": self.attempt_id,
            "run_id": self.run_id,
            "created_at": self.created_at,
            "committed_at": self.committed_at,
            "evidence_hash": self.evidence_hash,
            "cleanup_grant_id": self.cleanup_grant_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Effect:
        return cls(
            effect_id=data["effect_id"],
            action_intent_id=data.get("action_intent_id", ""),
            execution_seq=data.get("execution_seq", 0),
            state=EffectState(data.get("state", "prepared")),
            capability=data.get("capability", ""),
            resource_id=data.get("resource_id", ""),
            semantic_action_key=data.get("semantic_action_key", ""),
            risk_level=RiskLevel(data.get("risk_level", "r0")),
            attempt_id=data.get("attempt_id", ""),
            run_id=data.get("run_id", ""),
            created_at=data.get("created_at", 0),
            committed_at=data.get("committed_at"),
            evidence_hash=data.get("evidence_hash", ""),
            cleanup_grant_id=data.get("cleanup_grant_id", ""),
        )


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

@dataclass
class Evidence:
    """Verification evidence with provenance."""
    evidence_id: str = field(default_factory=lambda: _new_id("evd"))
    source: str = ""
    content_hash: str = ""
    sensitivity: str = "normal"
    taint_labels: list[str] = field(default_factory=list)
    freshness_at: float = field(default_factory=time.time)
    blob_ref: str = ""

    def to_dict(self) -> dict:
        return {
            "evidence_id": self.evidence_id,
            "source": self.source,
            "content_hash": self.content_hash,
            "sensitivity": self.sensitivity,
            "taint_labels": self.taint_labels,
            "freshness_at": self.freshness_at,
            "blob_ref": self.blob_ref,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Evidence:
        return cls(
            evidence_id=data["evidence_id"],
            source=data.get("source", ""),
            content_hash=data.get("content_hash", ""),
            sensitivity=data.get("sensitivity", "normal"),
            taint_labels=data.get("taint_labels", []),
            freshness_at=data.get("freshness_at", 0),
            blob_ref=data.get("blob_ref", ""),
        )


# ---------------------------------------------------------------------------
# TriggerSubscription & ScheduleCursor
# ---------------------------------------------------------------------------

@dataclass
class ScheduleCursor:
    """Tracks schedule execution position."""
    last_occurrence: Optional[str] = None
    next_planned: Optional[str] = None
    last_success_at: Optional[float] = None
    misfire_count: int = 0

    def to_dict(self) -> dict:
        return {
            "last_occurrence": self.last_occurrence,
            "next_planned": self.next_planned,
            "last_success_at": self.last_success_at,
            "misfire_count": self.misfire_count,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ScheduleCursor:
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


@dataclass
class TriggerSubscription:
    """Trigger definition for scheduled/standing goals."""
    subscription_id: str = field(default_factory=lambda: _new_id("trig"))
    goal_id: str = ""
    timezone: str = "UTC"
    cron_expr: str = ""
    event_rule: Optional[dict] = None
    misfire_policy: MisfirePolicy = MisfirePolicy.RUN_LATEST
    overlap_policy: OverlapPolicy = OverlapPolicy.FORBID
    cursor: ScheduleCursor = field(default_factory=ScheduleCursor)
    definition_version: int = 1
    admission_epoch: int = 1
    active: bool = True

    def to_dict(self) -> dict:
        return {
            "subscription_id": self.subscription_id,
            "goal_id": self.goal_id,
            "timezone": self.timezone,
            "cron_expr": self.cron_expr,
            "event_rule": self.event_rule,
            "misfire_policy": self.misfire_policy.value,
            "overlap_policy": self.overlap_policy.value,
            "cursor": self.cursor.to_dict(),
            "definition_version": self.definition_version,
            "admission_epoch": self.admission_epoch,
            "active": self.active,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TriggerSubscription:
        return cls(
            subscription_id=data["subscription_id"],
            goal_id=data.get("goal_id", ""),
            timezone=data.get("timezone", "UTC"),
            cron_expr=data.get("cron_expr", ""),
            event_rule=data.get("event_rule"),
            misfire_policy=MisfirePolicy(data.get("misfire_policy", "run_latest")),
            overlap_policy=OverlapPolicy(data.get("overlap_policy", "forbid")),
            cursor=ScheduleCursor.from_dict(data.get("cursor", {})),
            definition_version=data.get("definition_version", 1),
            admission_epoch=data.get("admission_epoch", 1),
            active=data.get("active", True),
        )


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------

@dataclass
class GoalActivationAuthorization:
    """One-time authorization to activate a goal/plan."""
    auth_id: str = field(default_factory=lambda: _new_id("auth"))
    goal_id: str = ""
    plan_hash: str = ""
    criteria_hash: str = ""
    budget_hash: str = ""
    nonce: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    expires_at: float = 0.0
    consumed: bool = False
    consumed_at: Optional[float] = None

    def is_valid(self) -> bool:
        return not self.consumed and self.expires_at > time.time()

    def consume(self) -> None:
        self.consumed = True
        self.consumed_at = time.time()

    def to_dict(self) -> dict:
        return {
            "auth_id": self.auth_id,
            "goal_id": self.goal_id,
            "plan_hash": self.plan_hash,
            "criteria_hash": self.criteria_hash,
            "budget_hash": self.budget_hash,
            "nonce": self.nonce,
            "expires_at": self.expires_at,
            "consumed": self.consumed,
            "consumed_at": self.consumed_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> GoalActivationAuthorization:
        obj = cls(
            auth_id=data["auth_id"],
            goal_id=data.get("goal_id", ""),
            plan_hash=data.get("plan_hash", ""),
            criteria_hash=data.get("criteria_hash", ""),
            budget_hash=data.get("budget_hash", ""),
            nonce=data.get("nonce", ""),
            expires_at=data.get("expires_at", 0),
            consumed=data.get("consumed", False),
            consumed_at=data.get("consumed_at"),
        )
        return obj


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------

@dataclass
class BudgetEntry:
    """A single budget reservation or settlement."""
    entry_id: str = field(default_factory=lambda: _new_id("bud"))
    ledger_id: str = ""
    amount: float = 0.0
    state: str = "reserved"  # reserved | settled | released
    dimension: str = ""  # model_cost | tool_calls | time_seconds
    reserved_at: float = field(default_factory=time.time)
    settled_at: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "entry_id": self.entry_id,
            "ledger_id": self.ledger_id,
            "amount": self.amount,
            "state": self.state,
            "dimension": self.dimension,
            "reserved_at": self.reserved_at,
            "settled_at": self.settled_at,
        }


@dataclass
class BudgetLedger:
    """Budget ledger for a run/goal."""
    ledger_id: str = field(default_factory=lambda: _new_id("ledger"))
    run_id: str = ""
    goal_id: str = ""
    limits: dict[str, float] = field(default_factory=dict)
    entries: list[BudgetEntry] = field(default_factory=list)

    def available(self, dimension: str) -> float:
        limit = self.limits.get(dimension, float("inf"))
        reserved = sum(
            e.amount for e in self.entries
            if e.dimension == dimension and e.state == "reserved"
        )
        settled = sum(
            e.amount for e in self.entries
            if e.dimension == dimension and e.state == "settled"
        )
        return limit - reserved - settled

    def reserve(self, dimension: str, amount: float) -> Optional[BudgetEntry]:
        if self.available(dimension) < amount:
            return None
        entry = BudgetEntry(
            ledger_id=self.ledger_id,
            amount=amount,
            dimension=dimension,
        )
        self.entries.append(entry)
        return entry

    def settle(self, entry_id: str, actual: float) -> bool:
        for e in self.entries:
            if e.entry_id == entry_id and e.state == "reserved":
                e.amount = actual
                e.state = "settled"
                e.settled_at = time.time()
                return True
        return False

    def release(self, entry_id: str) -> bool:
        for e in self.entries:
            if e.entry_id == entry_id and e.state == "reserved":
                e.state = "released"
                return True
        return False


# ---------------------------------------------------------------------------
# Capability
# ---------------------------------------------------------------------------

@dataclass
class CapabilityDescriptor:
    """Describes a tool capability in the registry."""
    capability_id: str = ""
    name: str = ""
    description: str = ""
    risk_level: RiskLevel = RiskLevel.R0
    idempotent: bool = False
    supports_query: bool = False
    supports_compensation: bool = False
    parameters_schema: dict = field(default_factory=dict)
    stop_capability: str = ""

    def to_dict(self) -> dict:
        return {
            "capability_id": self.capability_id,
            "name": self.name,
            "description": self.description,
            "risk_level": self.risk_level.value,
            "idempotent": self.idempotent,
            "supports_query": self.supports_query,
            "supports_compensation": self.supports_compensation,
            "parameters_schema": self.parameters_schema,
            "stop_capability": self.stop_capability,
        }

    @classmethod
    def from_dict(cls, data: dict) -> CapabilityDescriptor:
        return cls(
            capability_id=data.get("capability_id", ""),
            name=data.get("name", ""),
            description=data.get("description", ""),
            risk_level=RiskLevel(data.get("risk_level", "r0")),
            idempotent=data.get("idempotent", False),
            supports_query=data.get("supports_query", False),
            supports_compensation=data.get("supports_compensation", False),
            parameters_schema=data.get("parameters_schema", {}),
            stop_capability=data.get("stop_capability", ""),
        )


# ---------------------------------------------------------------------------
# Employee
# ---------------------------------------------------------------------------

@dataclass
class EmployeeDefinition:
    """Employee definition - personality, model, tools, permissions."""
    employee_id: str = field(default_factory=lambda: _new_id("emp"))
    name: str = ""
    persona: str = ""
    worker_type: WorkerType = WorkerType.LOGICAL
    state: EmployeeState = EmployeeState.DRAFT
    model_config: dict = field(default_factory=dict)
    capabilities: list[str] = field(default_factory=list)
    budget_template: dict = field(default_factory=dict)
    bot_principal_id: Optional[str] = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "employee_id": self.employee_id,
            "name": self.name,
            "persona": self.persona,
            "worker_type": self.worker_type.value,
            "state": self.state.value,
            "model_config": self.model_config,
            "capabilities": self.capabilities,
            "budget_template": self.budget_template,
            "bot_principal_id": self.bot_principal_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> EmployeeDefinition:
        return cls(
            employee_id=data["employee_id"],
            name=data.get("name", ""),
            persona=data.get("persona", ""),
            worker_type=WorkerType(data.get("worker_type", "logical")),
            state=EmployeeState(data.get("state", "draft")),
            model_config=data.get("model_config", {}),
            capabilities=data.get("capabilities", []),
            budget_template=data.get("budget_template", {}),
            bot_principal_id=data.get("bot_principal_id"),
            created_at=data.get("created_at", 0),
        )


# ---------------------------------------------------------------------------
# Progress Snapshot
# ---------------------------------------------------------------------------

@dataclass
class ProgressSnapshot:
    """Queryable progress state for a run."""
    run_id: str = ""
    run_state: RunState = RunState.QUEUED
    plan_version: int = 0
    completed_steps: int = 0
    total_steps: int = 0
    current_step: Optional[str] = None
    current_attempt: Optional[str] = None
    last_heartbeat: float = 0.0
    eta: Optional[float] = None
    deadline: Optional[float] = None
    budget_used: dict[str, float] = field(default_factory=dict)
    budget_remaining: dict[str, float] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "run_state": self.run_state.value,
            "plan_version": self.plan_version,
            "completed_steps": self.completed_steps,
            "total_steps": self.total_steps,
            "current_step": self.current_step,
            "current_attempt": self.current_attempt,
            "last_heartbeat": self.last_heartbeat,
            "eta": self.eta,
            "deadline": self.deadline,
            "budget_used": self.budget_used,
            "budget_remaining": self.budget_remaining,
            "blockers": self.blockers,
            "updated_at": self.updated_at,
        }


# Compatibility surface: all public model names resolve to the immutable
# domain package while legacy source remains temporarily available for the
# remaining migration tasks.
for _domain_name in _domain.__all__:
    globals()[_domain_name] = getattr(_domain, _domain_name)

_new_id = _domain.new_id
__all__ = [*_domain.__all__, "_new_id"]
del _domain_name
'''
