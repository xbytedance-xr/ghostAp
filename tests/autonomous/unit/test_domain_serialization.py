from dataclasses import FrozenInstanceError

import pytest

from src.autonomous.models import (
    BotPrincipal,
    CapabilityDescriptor,
    DecisionRequest,
    Effect,
    EffectState,
    EmployeeDefinition,
    GoalCriterion,
    GoalDefinition,
    GoalSpec,
    GoalState,
    GoalType,
    OracleType,
    Plan,
    PlanState,
    PlanStep,
    Principal,
    ProgressSnapshot,
    Report,
    ResourceQuarantine,
    RiskLevel,
    Run,
    RunState,
    StepState,
    TriggerSubscription,
    WorkerRuntime,
)


def test_goal_definition_round_trip_preserves_isolation_and_epochs() -> None:
    criterion = GoalCriterion(
        criterion_id="criterion_tests",
        description="all tests pass",
        oracle_type=OracleType.COMMAND,
        oracle_config={"command": "uv run pytest -q"},
    )
    goal = GoalDefinition(
        goal_id="goal_1",
        tenant_key="tenant_1",
        owner_principal_id="principal_1",
        owner_id="legacy_owner",
        goal_type=GoalType.SCHEDULED,
        state=GoalState.ACTIVE,
        spec=GoalSpec(
            objective="ship durable work",
            deliverables=("verified implementation",),
            criteria=(criterion,),
        ),
    )

    restored = GoalDefinition.from_dict(goal.to_dict())

    assert restored == goal
    assert restored.spec.criteria[0].criterion_hash == criterion.criterion_hash
    assert restored.tenant_key == "tenant_1"
    assert restored.owner_principal_id == "principal_1"


def test_run_round_trip_preserves_lineage_and_revision_links() -> None:
    run = Run(
        run_id="run_revision",
        tenant_key="tenant_1",
        goal_id="goal_1",
        root_run_lineage="run_root",
        revision_of_run_id="run_original",
        retry_of_run_id="run_failed",
        supersedes_run_id="run_old",
        state=RunState.REPLAN_PENDING,
        target_terminal_state=RunState.CANCELED,
        authorization_snapshot_id="auth_snapshot_1",
        aggregate_version=7,
    )

    assert Run.from_dict(run.to_dict()) == run


def test_run_rejects_non_failure_finalization_target() -> None:
    with pytest.raises(ValueError, match="terminal target"):
        Run(
            state=RunState.RECONCILIATION_PENDING,
            target_terminal_state=RunState.SUCCEEDED,
        )

    with pytest.raises(ValueError, match="terminal target"):
        Run.from_dict(
            {
                "run_id": "run_1",
                "state": "reconciliation_pending",
                "target_terminal_state": "succeeded",
            }
        )


def test_plan_round_trip_preserves_full_step_contract() -> None:
    step = PlanStep(
        step_id="step_1",
        name="publish",
        state=StepState.READY,
        capability="lark.docs.create",
        capability_version="1.0",
        arguments_schema={"title": "report"},
        principal_policy={"principal_types": ["user"]},
        resource_key="folder:fld_1/title:report",
        verifier_oracle={"type": "resource"},
        compensation="lark.docs.delete@1.0",
        criterion_ids=("criterion_1",),
    )
    plan = Plan(
        plan_id="plan_1",
        run_id="run_1",
        state=PlanState.ACTIVE,
        epoch=3,
        steps=(step,),
    )

    assert Plan.from_dict(plan.to_dict()) == plan


def test_effect_round_trip_preserves_full_lineage_and_dispatch_state() -> None:
    effect = Effect(
        effect_id="effect_lineage_1:3",
        effect_instance_id="effect_lineage_1:3",
        effect_lineage_id="effect_lineage_1",
        action_intent_id="intent_1",
        execution_seq=3,
        state=EffectState.UNKNOWN_EFFECT,
        capability="lark.docs.create",
        capability_version="1.0",
        resource_id="doc_1",
        resource_key="doc:doc_1",
        semantic_action_key="root+create+doc_1",
        provider_idempotency_key="provider_key_1",
        adapter_hash="sha256:adapter",
        schema_hash="sha256:schema",
        canonicalization_version="v1",
        active_dispatch=False,
        tenant_key="tenant_1",
        run_id="run_1",
    )

    assert Effect.from_dict(effect.to_dict()) == effect


def test_principal_employee_capability_and_progress_are_isolated() -> None:
    principal = Principal(
        principal_id="principal_1",
        tenant_key="tenant_1",
        union_id="union_1",
        roles=("owner",),
        app_open_ids={"manager": "ou_manager"},
    )
    employee = EmployeeDefinition(
        agent_id="employee_1",
        tenant_key="tenant_1",
        owner_principal_id=principal.principal_id,
        capabilities=("code.read@1.0",),
    )
    capability = CapabilityDescriptor(
        capability_id="code.read@1.0",
        version="1.0",
        business_operation_id="code.read",
        risk_level=RiskLevel.R0,
        adapter_hash="sha256:adapter",
        schema_hash="sha256:schema",
    )
    progress = ProgressSnapshot(
        run_id="run_1",
        tenant_key="tenant_1",
        run_state=RunState.EXECUTING,
        unresolved_effects=("effect_1",),
    )

    assert Principal.from_dict(principal.to_dict()) == principal
    assert EmployeeDefinition.from_dict(employee.to_dict()) == employee
    assert CapabilityDescriptor.from_dict(capability.to_dict()) == capability
    assert ProgressSnapshot.from_dict(progress.to_dict()) == progress


def test_aggregates_are_immutable_and_normalize_collections() -> None:
    goal = GoalDefinition(
        spec=GoalSpec(
            objective="immutable",
            constraints=["no database"],
        )
    )

    assert goal.spec.constraints == ("no database",)
    with pytest.raises(FrozenInstanceError):
        goal.state = GoalState.ACTIVE  # type: ignore[misc]


def test_trigger_subscription_records_reliability_contract() -> None:
    subscription = TriggerSubscription(
        subscription_id="trigger_1",
        tenant_key="tenant_1",
        goal_id="goal_1",
        delivery_semantics="at_least_once",
        replay_supported=True,
        cursor_format="event_id",
        gap_detection=True,
        max_recovery_window_seconds=86400,
        heartbeat_seconds=60,
    )

    restored = TriggerSubscription.from_dict(subscription.to_dict())

    assert restored == subscription
    assert restored.is_autonomous_eligible is True


def test_remaining_public_aggregates_round_trip() -> None:
    bot = BotPrincipal(
        bot_principal_id="bot_1",
        tenant_key="tenant_1",
        agent_id="employee_1",
        app_id="cli_app",
        credential_ref="keychain://bot_1",
        scopes=("im:message",),
    )
    worker = WorkerRuntime(
        worker_runtime_id="worker_1",
        tenant_key="tenant_1",
        employee_id="employee_1",
        run_id="run_1",
        step_id="step_1",
        attempt_id="attempt_1",
        pid=123,
        os_uid=1001,
        lease_id="lease_1",
        fencing_token=7,
        checkpoint_blob_ref={"blob_id": "a" * 64},
    )
    report = Report(
        report_id="report_1",
        tenant_key="tenant_1",
        run_id="run_1",
        report_type="terminal",
        payload_blob_ref={"blob_id": "b" * 64},
        payload_hash="c" * 64,
    )
    decision = DecisionRequest(
        decision_id="decision_1",
        tenant_key="tenant_1",
        run_id="run_1",
        plan_epoch=2,
        requester_principal_id="principal_owner",
        allowed_decider_principals=("principal_owner",),
        required_role="owner",
        action_scope="run.reconcile",
        question="Accept remote state?",
        options=("accept", "compensate"),
        default_behavior="wait",
        nonce="nonce_1",
        expires_at=123.0,
    )
    quarantine = ResourceQuarantine(
        quarantine_id="quarantine_1",
        tenant_key="tenant_1",
        resource_key="doc:1",
        source_effect_instance_id="effect_1",
        reason="remote state accepted",
    )

    assert BotPrincipal.from_dict(bot.to_dict()) == bot
    assert WorkerRuntime.from_dict(worker.to_dict()) == worker
    assert Report.from_dict(report.to_dict()) == report
    assert DecisionRequest.from_dict(decision.to_dict()) == decision
    assert ResourceQuarantine.from_dict(quarantine.to_dict()) == quarantine


@pytest.mark.parametrize(
    ("factory", "payload"),
    [
        (
            TriggerSubscription.from_dict,
            {
                "subscription_id": "trigger_1",
                "active": "false",
                "replay_supported": True,
                "gap_detection": True,
            },
        ),
        (
            Effect.from_dict,
            {
                "effect_id": "effect_1",
                "active_dispatch": "false",
                "risk_level": "r0",
            },
        ),
    ],
)
def test_untrusted_serialization_rejects_non_boolean_flags(
    factory,
    payload: dict,
) -> None:
    with pytest.raises(ValueError, match="boolean"):
        factory(payload)


@pytest.mark.parametrize(
    ("factory", "payload"),
    [
        (
            Effect.from_dict,
            {
                "effect_id": "effect_1",
                "execution_seq": True,
                "risk_level": "r1",
            },
        ),
        (
            Effect.from_dict,
            {
                "effect_id": None,
                "risk_level": "r1",
            },
        ),
        (
            Effect.from_dict,
            {
                "effect_id": "effect_1",
                "risk_level": "r1",
                "created_at": "123.0",
            },
        ),
        (
            CapabilityDescriptor.from_dict,
            {
                "capability_id": "write@1.0",
                "version": "1.0",
                "risk_level": None,
            },
        ),
        (
            CapabilityDescriptor.from_dict,
            {
                "capability_id": "write@1.0",
                "version": "1.0",
                "risk_level": "r1",
                "idempotency_ttl_seconds": True,
            },
        ),
        (
            CapabilityDescriptor.from_dict,
            {
                "capability_id": "write@1.0",
                "version": "1.0",
                "risk_level": "r1",
                "negative_observation_window_seconds": -1,
            },
        ),
        (
            PlanStep.from_dict,
            {
                "step_id": "step_1",
                "max_attempts": True,
            },
        ),
    ],
)
def test_untrusted_serialization_rejects_implicit_coercion(
    factory,
    payload: dict,
) -> None:
    with pytest.raises((TypeError, ValueError)):
        factory(payload)
