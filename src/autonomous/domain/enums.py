"""State, risk, and protocol enums for the autonomous work system."""

from enum import Enum


class GoalType(str, Enum):
    ONE_SHOT = "one_shot"
    SCHEDULED = "scheduled"
    STANDING = "standing"


class GoalState(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    DEGRADED_SOURCE = "degraded_source"
    BLOCKED_SOURCE = "blocked_source"
    CANCELED = "canceled"
    EXPIRED = "expired"


class RunState(str, Enum):
    RECEIVED = "received"
    CLARIFYING = "clarifying"
    PLAN_READY = "plan_ready"
    APPROVAL_PENDING = "approval_pending"
    SCHEDULED = "scheduled"
    QUEUED = "queued"
    PLANNING = "plan_ready"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    REPLAN_PENDING = "replan_pending"
    PAUSED = "paused"
    BLOCKED = "blocked"
    ACCEPTANCE_PENDING = "acceptance_pending"
    CANCELLING = "cancelling"
    RECONCILIATION_PENDING = "reconciliation_pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"
    EXPIRED = "expired"
    SUPERSEDED_PENDING_DRAIN = "superseded_pending_drain"


class PlanState(str, Enum):
    DRAFT = "draft"
    COMPILED = "compiled"
    VALIDATED = "validated"
    ACTIVE = "active"
    SUPERSEDED_PENDING_DRAIN = "superseded_pending_drain"
    SUPERSEDED = "superseded"
    INVALID = "invalid"


class StepState(str, Enum):
    PENDING = "pending"
    READY = "ready"
    LEASED = "leased"
    RUNNING = "running"
    EXECUTING = "running"
    OUTPUT_STAGED = "output_staged"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    RETRY_WAIT = "retry_wait"
    ORPHANED = "orphaned"
    RECONCILING = "reconciling"
    REJECTED = "rejected"
    CANCELLING = "cancelling"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELED = "canceled"


class AttemptState(str, Enum):
    ACTIVE = "active"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLING = "cancelling"
    CANCELED = "canceled"
    ORPHANED = "orphaned"
    RECONCILING = "reconciling"


class EffectState(str, Enum):
    PROPOSED = "proposed"
    POLICY_ALLOWED = "policy_allowed"
    POLICY_DENIED = "policy_denied"
    PREPARED = "prepared"
    ABORTED_NO_DISPATCH = "aborted_no_dispatch"
    EXECUTING = "executing"
    COMMITTED = "committed"
    UNKNOWN_EFFECT = "unknown_effect"
    RECONCILING = "reconciling"
    RETRY_AUTHORIZED = "retry_authorized"
    MANUAL_RECONCILIATION = "manual_reconciliation"
    FAILED_SAFE = "failed_safe"
    COMPENSATING = "compensating"
    COMPENSATED = "compensated"
    COMPENSATION_FAILED = "compensation_failed"
    ABANDONED_ACCEPTED = "abandoned_accepted"


class RiskLevel(str, Enum):
    R0 = "r0"
    R1 = "r1"
    R2 = "r2"
    R3 = "r3"
    R4 = "r4"


class AutonomyMode(str, Enum):
    ASSIST = "assist"
    SUPERVISED = "supervised"
    BOUNDED_AUTONOMOUS = "bounded_autonomous"


class OracleType(str, Enum):
    COMMAND = "command"
    RESOURCE = "resource"
    DATA = "data"
    SCHEMA = "schema"
    REVIEW = "review"
    HUMAN = "human"


class VerificationResult(str, Enum):
    PASSED = "passed"
    EXECUTION_DEFECT = "execution_defect"
    PLAN_DEFECT = "plan_defect"
    ENVIRONMENT_BLOCKED = "environment_blocked"
    PERMISSION_BLOCKED = "permission_blocked"
    UNVERIFIABLE = "unverifiable"
    TERMINAL_FAILURE = "terminal_failure"


class TurnOutputType(str, Enum):
    TOOL_PROPOSAL = "tool_proposal"
    REQUEST_CONTEXT = "request_context"
    SUBMIT_OUTPUT = "submit_output"
    REPLAN_REQUEST = "replan_request"
    BLOCKED = "blocked"


class MisfirePolicy(str, Enum):
    RUN_ALL = "run_all"
    SKIP = "skip"
    RUN_LATEST = "run_latest"


class OverlapPolicy(str, Enum):
    FORBID = "forbid"
    QUEUE = "queue"
    ALLOW_PARALLEL = "allow_parallel"


class WorkerType(str, Enum):
    LOGICAL = "logical"
    VISIBLE = "visible"
    EPHEMERAL = "ephemeral"


class EmployeeState(str, Enum):
    DRAFT = "draft"
    PROVISIONING_APP = "provisioning_app"
    STORING_CREDENTIAL = "storing_credential"
    CONFIGURING = "configuring"
    VALIDATING = "validating"
    ACTIVE = "active"
    RETIRING = "retiring"
    ARCHIVED = "archived"


class RunEvent(str, Enum):
    CLARIFICATION_REQUIRED = "clarification_required"
    GOAL_DEFINED = "goal_defined"
    PLAN_VALIDATED = "plan_validated"
    APPROVAL_REQUIRED = "approval_required"
    ACTIVATED = "activated"
    STARTED = "started"
    OUTPUT_SUBMITTED = "output_submitted"
    VERIFICATION_FAILED = "verification_failed"
    VERIFICATION_PASSED = "verification_passed"
    REPLAN_REQUESTED = "replan_requested"
    SUPERSEDE_REQUESTED = "supersede_requested"
    SUPERSEDE_DRAINED = "supersede_drained"
    BLOCKED = "blocked"
    PAUSED = "paused"
    RESUMED = "resumed"
    ACCEPTANCE_REQUIRED = "acceptance_required"
    HUMAN_ACCEPTED = "human_accepted"
    HUMAN_REJECTED = "human_rejected"
    CANCEL_REQUESTED = "cancel_requested"
    CANCEL_DRAINED = "cancel_drained"
    FAILED = "failed"
    EXPIRED = "expired"
    RECONCILIATION_COMPLETED = "reconciliation_completed"


class PlanEvent(str, Enum):
    COMPILED = "compiled"
    VALIDATED = "validated"
    ACTIVATED = "activated"
    SUPERSEDE_REQUESTED = "supersede_requested"
    DRAINED = "drained"
    INVALIDATED = "invalidated"


class StepEvent(str, Enum):
    DEPENDENCIES_SATISFIED = "dependencies_satisfied"
    LEASE_GRANTED = "lease_granted"
    WORKER_STARTED = "worker_started"
    OUTPUT_STAGED = "output_staged"
    VERIFY_STARTED = "verify_started"
    VERIFIED = "verified"
    ATTEMPT_FAILED = "attempt_failed"
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"
    RETRY_READY = "retry_ready"
    RETRY_AUTHORIZED = "retry_authorized"
    WORKER_LOST = "worker_lost"
    RECONCILE_STARTED = "reconcile_started"
    RECONCILE_RETRY = "reconcile_retry"
    REJECTED = "rejected"
    SKIP_AUTHORIZED = "skip_authorized"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELED = "canceled"


class EffectEvent(str, Enum):
    POLICY_ALLOWED = "policy_allowed"
    POLICY_DENIED = "policy_denied"
    PREPARED = "prepared"
    PREPARE_FAILED = "prepare_failed"
    ABORT_BEFORE_DISPATCH = "abort_before_dispatch"
    DISPATCH_STARTED = "dispatch_started"
    DISPATCH_COMMITTED = "dispatch_committed"
    DISPATCH_FAILED_SAFE = "dispatch_failed_safe"
    DISPATCH_UNKNOWN = "dispatch_unknown"
    RECONCILE_STARTED = "reconcile_started"
    REMOTE_COMMITTED = "remote_committed"
    REMOTE_NOT_EXECUTED = "remote_not_executed"
    RETRY_AUTHORIZED = "retry_authorized"
    MANUAL_REQUIRED = "manual_required"
    COMPENSATE_STARTED = "compensate_started"
    COMPENSATED = "compensated"
    COMPENSATION_FAILED = "compensation_failed"
    ABANDONED_ACCEPTED = "abandoned_accepted"


class EffectDispositionType(str, Enum):
    RETAINED = "retained"
    COMPENSATED = "compensated"
    ABANDONED_ACCEPTED = "abandoned_accepted"
    FAILED_SAFE = "failed_safe"
