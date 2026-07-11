"""Integration tests for the autonomous work system.

Tests the full lifecycle: goal creation → admission → planning → scheduling →
execution → verification → reporting, plus crash recovery and kill switch.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.autonomous.models import (
    AttemptState,
    AutonomyMode,
    BudgetLedger,
    CapabilityDescriptor,
    Effect,
    EffectState,
    EpochSet,
    GoalCriterion,
    GoalDefinition,
    GoalSpec,
    GoalState,
    GoalType,
    OracleType,
    Plan,
    PlanStep,
    ProgressSnapshot,
    RiskLevel,
    Run,
    RunState,
    StepState,
    TurnOutputType,
    VerificationResult,
)
from src.autonomous.journal.journal import JournalEntry, JournalWriter, TransactionFrame
from src.autonomous.scheduler.scheduler import DurableScheduler, LeaseGrant, QueueEntry
from src.autonomous.runtime.runtime import AgentRuntime, RuntimeResult, TurnInput, TurnOutput, TurnRecord
from src.autonomous.policy.policy_engine import PolicyContext, PolicyDecision, PolicyEngine, PolicyResult
from src.autonomous.policy.budget_manager import BudgetManager
from src.autonomous.policy.kill_switch import KillSwitch, KillState
from src.autonomous.broker.tool_broker import CapabilityRegistry, DispatchRequest, DispatchResult, ToolBroker
from src.autonomous.verifier.verifier import Verifier, VerificationAttestation, VerificationReport
from src.autonomous.reporter.reporter import DeliveryState, OutboxEntry, Reporter, ReportType
from src.autonomous.supervisor.supervisor import Supervisor, WorkerProcess, WorkerState
from src.autonomous.manager.admission import Admission, AdmissionResult, GoalInbox, InboxEvent
from src.autonomous.manager.plan_compiler import CompilationResult, PlanCompiler
from src.autonomous.manager.handler import CommandContext, CommandResult, ManagerHandler


@pytest.fixture
def tmp_dir():
    d = tempfile.mkdtemp(prefix="ghostap_test_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# Journal Tests
# ---------------------------------------------------------------------------

class TestJournal:
    def test_create_and_replay(self, tmp_dir: str):
        journal = JournalWriter(tmp_dir)
        entry = JournalEntry(
            entry_type="goal_created",
            entity_id="goal_123",
            data={"objective": "test goal"},
        )
        frame = asyncio.run(journal.commit_frame([entry]))
        assert frame.sequence >= 0
        assert len(frame.entries) == 1

        frames = list(journal.replay())
        assert len(frames) == 1
        assert frames[0].frame_id == frame.frame_id

    def test_hash_chain_integrity(self, tmp_dir: str):
        journal = JournalWriter(tmp_dir)
        for i in range(5):
            entry = JournalEntry(
                entry_type="run_created",
                entity_id=f"run_{i}",
                data={"index": i},
            )
            asyncio.run(journal.commit_frame([entry]))

        valid, errors = journal.verify_chain()
        assert valid
        assert errors == []

    def test_crash_recovery(self, tmp_dir: str):
        journal = JournalWriter(tmp_dir)
        entry = JournalEntry(
            entry_type="goal_created",
            entity_id="goal_1",
            data={"x": 1},
        )
        asyncio.run(journal.commit_frame([entry]))

        # Simulate crash: append incomplete JSON
        journal_path = os.path.join(tmp_dir, "journal.jsonl")
        with open(journal_path, "a") as f:
            f.write('{"incomplete":')

        # Recovery should truncate the incomplete line
        journal2 = JournalWriter(tmp_dir)
        frames = list(journal2.replay())
        assert len(frames) == 1  # Only the valid frame

    def test_multi_entry_frame(self, tmp_dir: str):
        journal = JournalWriter(tmp_dir)
        entries = [
            JournalEntry(entry_type="goal_created", entity_id="g1", data={}),
            JournalEntry(entry_type="run_created", entity_id="r1", data={}),
            JournalEntry(entry_type="budget_reserved", entity_id="b1", data={}),
        ]
        frame = asyncio.run(journal.commit_frame(entries))
        assert len(frame.entries) == 3


# ---------------------------------------------------------------------------
# Scheduler Tests
# ---------------------------------------------------------------------------

class TestScheduler:
    def _make_scheduler(self):
        journal_mock = MagicMock()
        journal_mock.write_event = AsyncMock()
        return DurableScheduler(journal=journal_mock, max_concurrent=3)

    def test_enqueue_and_acquire(self):
        sched = self._make_scheduler()
        step = PlanStep(step_id="step_1", name="test")
        asyncio.run(sched.enqueue_step(step, "run_1", 1))

        lease = asyncio.run(sched.acquire_lease("step_1", "worker_a"))
        assert lease is not None
        assert lease.step_id == "step_1"
        assert lease.worker_id == "worker_a"
        assert lease.fencing_token == 1

    def test_max_concurrent_limit(self):
        sched = self._make_scheduler()
        for i in range(4):
            step = PlanStep(step_id=f"step_{i}", name=f"test_{i}")
            asyncio.run(sched.enqueue_step(step, "run_1", 1))
            if i < 3:
                lease = asyncio.run(sched.acquire_lease(f"step_{i}", f"worker_{i}"))
                assert lease is not None

        # 4th should fail (max_concurrent=3)
        lease = asyncio.run(sched.acquire_lease("step_3", "worker_3"))
        assert lease is None

    def test_lease_expiry(self):
        sched = self._make_scheduler()
        sched._default_lease_seconds = 0.01  # very short
        step = PlanStep(step_id="step_1", name="test")
        asyncio.run(sched.enqueue_step(step, "run_1", 1))

        lease = asyncio.run(sched.acquire_lease("step_1", "worker_a"))
        assert lease is not None

        time.sleep(0.02)
        expired = asyncio.run(sched.check_expired_leases())
        assert len(expired) == 1
        assert expired[0].lease_id == lease.lease_id

    def test_dead_letter(self):
        sched = self._make_scheduler()
        step = PlanStep(step_id="step_1", name="test")
        asyncio.run(sched.enqueue_step(step, "run_1", 1))
        asyncio.run(sched.mark_dead_letter("step_1", "permanent failure"))

        stats = sched.get_stats()
        assert stats.dead_letters == 1


# ---------------------------------------------------------------------------
# Policy Tests
# ---------------------------------------------------------------------------

class TestPolicy:
    def test_assist_mode_blocks_writes(self, tmp_dir: str):
        ks = KillSwitch(tmp_dir)
        engine = PolicyEngine(ks)
        ctx = PolicyContext(
            run_id="run_1",
            step_id="step_1",
            attempt_id="att_1",
            capability="file_write",
            risk_level=RiskLevel.R1,
            autonomy_mode=AutonomyMode.ASSIST,
            employee_id="emp_1",
        )
        result = engine.evaluate(ctx, EpochSet())
        assert result.decision == PolicyDecision.DENY

    def test_supervised_mode_allows_r0(self, tmp_dir: str):
        ks = KillSwitch(tmp_dir)
        engine = PolicyEngine(ks)
        ctx = PolicyContext(
            run_id="run_1",
            step_id="step_1",
            attempt_id="att_1",
            capability="file_read",
            risk_level=RiskLevel.R0,
            autonomy_mode=AutonomyMode.SUPERVISED,
            employee_id="emp_1",
        )
        result = engine.evaluate(ctx, EpochSet())
        assert result.decision == PolicyDecision.ALLOW

    def test_supervised_mode_requires_approval_for_r2(self, tmp_dir: str):
        ks = KillSwitch(tmp_dir)
        engine = PolicyEngine(ks)
        ctx = PolicyContext(
            run_id="run_1",
            step_id="step_1",
            attempt_id="att_1",
            capability="api_call",
            risk_level=RiskLevel.R2,
            autonomy_mode=AutonomyMode.SUPERVISED,
            employee_id="emp_1",
        )
        result = engine.evaluate(ctx, EpochSet())
        assert result.decision == PolicyDecision.REQUIRE_APPROVAL

    def test_kill_switch_blocks_all(self, tmp_dir: str):
        ks = KillSwitch(tmp_dir)
        ks.activate(scope="global", reason="test")
        engine = PolicyEngine(ks)
        ctx = PolicyContext(
            run_id="run_1",
            step_id="step_1",
            attempt_id="att_1",
            capability="file_read",
            risk_level=RiskLevel.R0,
            autonomy_mode=AutonomyMode.BOUNDED_AUTONOMOUS,
            employee_id="emp_1",
        )
        result = engine.evaluate(ctx, EpochSet())
        assert result.decision == PolicyDecision.DENY


# ---------------------------------------------------------------------------
# Kill Switch Tests
# ---------------------------------------------------------------------------

class TestKillSwitch:
    def test_activate_deactivate(self, tmp_dir: str):
        ks = KillSwitch(tmp_dir)
        assert not ks.is_killed()

        epoch = ks.activate(reason="test kill")
        assert epoch >= 1
        assert ks.is_killed()

        ks.deactivate()
        assert not ks.is_killed()

    def test_persistence(self, tmp_dir: str):
        ks = KillSwitch(tmp_dir)
        ks.activate(reason="persist test")

        # Reload from disk
        ks2 = KillSwitch(tmp_dir)
        assert ks2.is_killed()

    def test_scoped_kill(self, tmp_dir: str):
        ks = KillSwitch(tmp_dir)
        ks.activate(scope="tool:file_write", reason="block writes")

        assert ks.is_killed("tool:file_write")
        assert not ks.is_killed("global")


# ---------------------------------------------------------------------------
# Budget Tests
# ---------------------------------------------------------------------------

class TestBudget:
    def test_reserve_and_settle(self, tmp_dir: str):
        bm = BudgetManager(tmp_dir)
        ledger = bm.get_or_create_ledger("run_1", "goal_1", {"model_cost": 100.0})
        entry_id = bm.reserve(ledger.ledger_id, "model_cost", 10.0)
        assert entry_id is not None

        assert bm.settle(entry_id, 8.0)
        summary = bm.get_usage_summary(ledger.ledger_id)
        assert summary["model_cost"]["settled"] == 8.0

    def test_over_budget_rejected(self, tmp_dir: str):
        bm = BudgetManager(tmp_dir)
        ledger = bm.get_or_create_ledger("run_1", "goal_1", {"tool_calls": 5.0})
        for _ in range(5):
            assert bm.reserve(ledger.ledger_id, "tool_calls", 1.0) is not None

        # 6th should fail
        assert bm.reserve(ledger.ledger_id, "tool_calls", 1.0) is None


# ---------------------------------------------------------------------------
# Tool Broker Tests
# ---------------------------------------------------------------------------

class TestToolBroker:
    def test_dispatch_success(self):
        registry = CapabilityRegistry()
        cap = CapabilityDescriptor(
            capability_id="shell_read",
            name="Shell Read",
            risk_level=RiskLevel.R0,
            idempotent=True,
        )
        registry.register(cap, AsyncMock(return_value={"output": "hello"}))

        broker = ToolBroker(
            registry=registry,
            policy_check_fn=lambda req, desc: {"decision": "allow"},
            budget_reserve_fn=lambda *a: "entry_1",
            budget_settle_fn=lambda *a: True,
            kill_check_fn=lambda cap: True,
            epoch_check_fn=lambda *a: True,
        )

        req = DispatchRequest(
            capability="shell_read",
            arguments={"cmd": "ls"},
            run_id="run_1",
            step_id="step_1",
            attempt_id="att_1",
            plan_epoch=1,
            employee_id="emp_1",
        )
        result = asyncio.run(broker.dispatch(req, EpochSet()))
        assert result.success

    def test_kill_switch_blocks_dispatch(self):
        registry = CapabilityRegistry()
        cap = CapabilityDescriptor(capability_id="file_write", risk_level=RiskLevel.R1)
        registry.register(cap, AsyncMock())

        broker = ToolBroker(
            registry=registry,
            policy_check_fn=lambda *a: {"decision": "allow"},
            budget_reserve_fn=lambda *a: "e1",
            budget_settle_fn=lambda *a: True,
            kill_check_fn=lambda cap: False,  # killed
            epoch_check_fn=lambda *a: True,
        )

        req = DispatchRequest(
            capability="file_write", arguments={},
            run_id="r1", step_id="s1", attempt_id="a1",
            plan_epoch=1, employee_id="e1",
        )
        result = asyncio.run(broker.dispatch(req, EpochSet()))
        assert not result.success
        assert "Kill switch" in result.error

    def test_idempotency_dedup(self):
        registry = CapabilityRegistry()
        call_count = {"n": 0}

        async def adapter(args):
            call_count["n"] += 1
            return {"ok": True}

        cap = CapabilityDescriptor(capability_id="api_call", risk_level=RiskLevel.R0, idempotent=True)
        registry.register(cap, adapter)

        broker = ToolBroker(
            registry=registry,
            policy_check_fn=lambda *a: {"decision": "allow"},
            budget_reserve_fn=lambda *a: "e1",
            budget_settle_fn=lambda *a: True,
            kill_check_fn=lambda *a: True,
            epoch_check_fn=lambda *a: True,
        )

        req = DispatchRequest(
            capability="api_call",
            arguments={"key": "value"},
            run_id="r1", step_id="s1", attempt_id="a1",
            plan_epoch=1, employee_id="e1",
            semantic_action_key="same_action",
        )
        r1 = asyncio.run(broker.dispatch(req, EpochSet()))
        r2 = asyncio.run(broker.dispatch(req, EpochSet()))
        assert r1.success
        assert r2.success
        assert r2.idempotent_hit
        assert call_count["n"] == 1


# ---------------------------------------------------------------------------
# Verifier Tests
# ---------------------------------------------------------------------------

class TestVerifier:
    def test_command_oracle_pass(self, tmp_dir: str):
        v = Verifier(workspace_dir=tmp_dir)
        criterion = GoalCriterion(
            description="echo test passes",
            oracle_type=OracleType.COMMAND,
            oracle_config={"command": "echo ok"},
        )
        att = asyncio.run(v.verify_criterion(criterion, {}, tmp_dir))
        assert att.result == VerificationResult.PASSED

    def test_command_oracle_fail(self, tmp_dir: str):
        v = Verifier(workspace_dir=tmp_dir)
        criterion = GoalCriterion(
            description="exit 1 fails",
            oracle_type=OracleType.COMMAND,
            oracle_config={"command": "exit 1"},
        )
        att = asyncio.run(v.verify_criterion(criterion, {}, tmp_dir))
        assert att.result == VerificationResult.EXECUTION_DEFECT

    def test_resource_oracle_missing(self, tmp_dir: str):
        v = Verifier(workspace_dir=tmp_dir)
        criterion = GoalCriterion(
            description="file must exist",
            oracle_type=OracleType.RESOURCE,
            oracle_config={"resource_url": "/nonexistent/path"},
        )
        att = asyncio.run(v.verify_criterion(criterion, {}, tmp_dir))
        assert att.result == VerificationResult.EXECUTION_DEFECT

    def test_human_oracle_unverifiable(self, tmp_dir: str):
        v = Verifier(workspace_dir=tmp_dir)
        criterion = GoalCriterion(
            description="needs human review",
            oracle_type=OracleType.HUMAN,
            oracle_config={},
        )
        att = asyncio.run(v.verify_criterion(criterion, {}, tmp_dir))
        assert att.result == VerificationResult.UNVERIFIABLE

    def test_verify_all(self, tmp_dir: str):
        v = Verifier(workspace_dir=tmp_dir)
        criteria = [
            GoalCriterion(description="c1", oracle_type=OracleType.COMMAND, oracle_config={"command": "true"}),
            GoalCriterion(description="c2", oracle_type=OracleType.COMMAND, oracle_config={"command": "true"}),
        ]
        report = asyncio.run(v.verify_all(criteria, {}, "run_1", tmp_dir))
        assert report.all_passed


# ---------------------------------------------------------------------------
# Reporter Tests
# ---------------------------------------------------------------------------

class TestReporter:
    def test_enqueue_and_flush(self):
        delivered = []
        reporter = Reporter(deliver_fn=lambda t, p: (delivered.append((t, p)) or True))

        asyncio.run(reporter.enqueue(ReportType.RUN_COMPLETED, "user_1", {"run_id": "r1"}))
        count = asyncio.run(reporter.flush())
        assert count == 1
        assert len(delivered) == 1

    def test_retry_on_failure(self):
        attempts = {"n": 0}

        def fail_then_succeed(target, payload):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError("temp failure")
            return True

        reporter = Reporter(deliver_fn=fail_then_succeed, backoff_base=0.01)
        asyncio.run(reporter.enqueue(ReportType.BLOCKED, "u1", {}))

        # First flush fails
        asyncio.run(reporter.flush())
        assert reporter.get_pending()

    def test_idempotency_dedup(self):
        delivered = []
        reporter = Reporter(deliver_fn=lambda t, p: (delivered.append(p) or True))

        asyncio.run(reporter.enqueue(ReportType.RUN_COMPLETED, "u1", {"x": 1}, idempotency_key="key1"))
        asyncio.run(reporter.flush())
        # Second enqueue with same key should be skipped
        entry_id = asyncio.run(reporter.enqueue(ReportType.RUN_COMPLETED, "u1", {"x": 1}, idempotency_key="key1"))
        assert entry_id == ""


# ---------------------------------------------------------------------------
# Admission Tests
# ---------------------------------------------------------------------------

class TestAdmission:
    def _make_admission(self, tmp_dir: str):
        journal = JournalWriter(tmp_dir)
        return Admission(journal), journal

    def test_admit_and_activate_goal(self, tmp_dir: str):
        adm, _ = self._make_admission(tmp_dir)
        goal = GoalDefinition(spec=GoalSpec(objective="test"))

        decision = asyncio.run(adm.admit_goal(goal))
        assert decision.result == AdmissionResult.ACCEPTED

        err = asyncio.run(adm.activate_goal(goal.goal_id))
        assert err is None
        assert adm.get_goal(goal.goal_id).state == GoalState.ACTIVE

    def test_one_shot_single_active_run(self, tmp_dir: str):
        adm, _ = self._make_admission(tmp_dir)
        goal = GoalDefinition(spec=GoalSpec(objective="one shot"), goal_type=GoalType.ONE_SHOT)
        asyncio.run(adm.admit_goal(goal))
        asyncio.run(adm.activate_goal(goal.goal_id))

        d1 = asyncio.run(adm.create_run(goal.goal_id))
        assert d1.result == AdmissionResult.ACCEPTED

        d2 = asyncio.run(adm.create_run(goal.goal_id))
        assert d2.result == AdmissionResult.REJECTED

    def test_occurrence_dedup(self, tmp_dir: str):
        adm, _ = self._make_admission(tmp_dir)
        goal = GoalDefinition(spec=GoalSpec(objective="scheduled"), goal_type=GoalType.SCHEDULED)
        asyncio.run(adm.admit_goal(goal))
        asyncio.run(adm.activate_goal(goal.goal_id))

        d1 = asyncio.run(adm.create_run(goal.goal_id, occurrence_key="2026-01-01"))
        assert d1.result == AdmissionResult.ACCEPTED

        d2 = asyncio.run(adm.create_run(goal.goal_id, occurrence_key="2026-01-01"))
        assert d2.result == AdmissionResult.DUPLICATE

    def test_cancel_goal_cancels_queued_runs(self, tmp_dir: str):
        adm, _ = self._make_admission(tmp_dir)
        goal = GoalDefinition(spec=GoalSpec(objective="cancel test"), goal_type=GoalType.SCHEDULED)
        asyncio.run(adm.admit_goal(goal))
        asyncio.run(adm.activate_goal(goal.goal_id))
        asyncio.run(adm.create_run(goal.goal_id, occurrence_key="a"))

        err = asyncio.run(adm.cancel_goal(goal.goal_id))
        assert err is None
        assert adm.get_goal(goal.goal_id).state == GoalState.CANCELED


# ---------------------------------------------------------------------------
# Plan Compiler Tests
# ---------------------------------------------------------------------------

class TestPlanCompiler:
    def test_valid_plan(self):
        cap = CapabilityDescriptor(capability_id="shell", name="Shell")
        compiler = PlanCompiler({"shell": cap})

        criterion = GoalCriterion(criterion_id="c1", description="tests pass")
        plan = Plan(steps=[
            PlanStep(
                step_id="s1", name="implement", capability="shell",
                verifier_oracle={"type": "command"}, criterion_ids=["c1"],
            ),
        ])
        result = compiler.compile(plan, [criterion])
        assert result.valid

    def test_missing_capability(self):
        compiler = PlanCompiler({})
        plan = Plan(steps=[PlanStep(step_id="s1", capability="nonexistent")])
        result = compiler.compile(plan, [])
        assert not result.valid
        assert any(e.error_type == "capability_missing" for e in result.errors)

    def test_dag_cycle_detection(self):
        cap = CapabilityDescriptor(capability_id="shell", name="Shell")
        compiler = PlanCompiler({"shell": cap})
        plan = Plan(steps=[
            PlanStep(step_id="s1", capability="shell", depends_on=["s2"]),
            PlanStep(step_id="s2", capability="shell", depends_on=["s1"]),
        ])
        result = compiler.compile(plan, [])
        assert not result.valid
        assert any(e.error_type == "dag" for e in result.errors)


# ---------------------------------------------------------------------------
# Manager Handler Tests
# ---------------------------------------------------------------------------

class TestManagerHandler:
    def _make_handler(self, tmp_dir: str):
        journal = JournalWriter(tmp_dir)
        admission = Admission(journal)
        kill_switch = KillSwitch(tmp_dir)
        policy = PolicyEngine(kill_switch)
        compiler = PlanCompiler({})
        reporter = Reporter(deliver_fn=lambda t, p: True)
        scheduler_mock = MagicMock()

        handler = ManagerHandler(
            admission=admission,
            plan_compiler=compiler,
            scheduler=scheduler_mock,
            policy_engine=policy,
            reporter=reporter,
            kill_switch=kill_switch,
        )
        return handler

    def test_create_goal(self, tmp_dir: str):
        handler = self._make_handler(tmp_dir)
        ctx = CommandContext(user_id="user_1", chat_id="chat_1", command="/goal", args="Build a login page")
        result = asyncio.run(handler.handle(ctx))
        assert result.success
        assert result.goal_id is not None

    def test_list_goals(self, tmp_dir: str):
        handler = self._make_handler(tmp_dir)
        # Create a goal first
        ctx = CommandContext(user_id="u1", chat_id="c1", command="/goal", args="Test goal")
        asyncio.run(handler.handle(ctx))

        ctx2 = CommandContext(user_id="u1", chat_id="c1", command="/goals", args="")
        result = asyncio.run(handler.handle(ctx2))
        assert result.success
        assert "Test goal" in result.message

    def test_create_and_start_run(self, tmp_dir: str):
        handler = self._make_handler(tmp_dir)
        # Create goal
        ctx = CommandContext(user_id="u1", chat_id="c1", command="/goal", args="Implement feature")
        r1 = asyncio.run(handler.handle(ctx))
        goal_id = r1.goal_id

        # Start run (auto-activates draft)
        ctx2 = CommandContext(user_id="u1", chat_id="c1", command="/run", args=goal_id)
        r2 = asyncio.run(handler.handle(ctx2))
        assert r2.success
        assert r2.run_id is not None

    def test_kill_switch_requires_admin(self, tmp_dir: str):
        handler = self._make_handler(tmp_dir)
        ctx = CommandContext(user_id="u1", chat_id="c1", command="/kill", args="", is_admin=False)
        result = asyncio.run(handler.handle(ctx))
        assert not result.success
        assert "admin" in result.message.lower()

    def test_unknown_command(self, tmp_dir: str):
        handler = self._make_handler(tmp_dir)
        ctx = CommandContext(user_id="u1", chat_id="c1", command="/unknown", args="")
        result = asyncio.run(handler.handle(ctx))
        assert not result.success


# ---------------------------------------------------------------------------
# End-to-End Lifecycle Test
# ---------------------------------------------------------------------------

class TestEndToEnd:
    def test_full_lifecycle(self, tmp_dir: str):
        """Test: goal → admission → run → verify → report."""
        journal = JournalWriter(tmp_dir)
        admission = Admission(journal)
        kill_switch = KillSwitch(tmp_dir)

        # 1. Create and activate goal
        goal = GoalDefinition(
            spec=GoalSpec(
                objective="Create hello.txt",
                criteria=[GoalCriterion(
                    description="file exists",
                    oracle_type=OracleType.COMMAND,
                    oracle_config={"command": f"test -f {tmp_dir}/hello.txt"},
                )],
            ),
            owner_id="user_1",
        )
        d = asyncio.run(admission.admit_goal(goal))
        assert d.result == AdmissionResult.ACCEPTED
        asyncio.run(admission.activate_goal(goal.goal_id))

        # 2. Create run
        run_decision = asyncio.run(admission.create_run(goal.goal_id))
        assert run_decision.result == AdmissionResult.ACCEPTED
        run_id = run_decision.run_id

        # 3. Simulate execution - create the file
        with open(os.path.join(tmp_dir, "hello.txt"), "w") as f:
            f.write("hello world")

        # 4. Verify
        verifier = Verifier(workspace_dir=tmp_dir)
        report = asyncio.run(verifier.verify_all(
            goal.spec.criteria, {}, run_id, tmp_dir
        ))
        assert report.all_passed

        # 5. Report
        delivered = []
        reporter = Reporter(deliver_fn=lambda t, p: (delivered.append(p) or True))
        asyncio.run(reporter.report_completion("user_1", run_id, {"verified": True}))
        asyncio.run(reporter.flush())
        assert len(delivered) == 1
        assert delivered[0]["run_id"] == run_id

        # 6. Verify journal integrity
        valid, errors = journal.verify_chain()
        assert valid

    def test_kill_switch_stops_execution(self, tmp_dir: str):
        """Kill switch prevents new dispatches."""
        kill_switch = KillSwitch(tmp_dir)
        registry = CapabilityRegistry()
        cap = CapabilityDescriptor(capability_id="shell", risk_level=RiskLevel.R0)
        registry.register(cap, AsyncMock(return_value={"ok": True}))

        broker = ToolBroker(
            registry=registry,
            policy_check_fn=lambda *a: {"decision": "allow"},
            budget_reserve_fn=lambda *a: "e1",
            budget_settle_fn=lambda *a: True,
            kill_check_fn=lambda cap: not kill_switch.is_killed(),
            epoch_check_fn=lambda *a: True,
        )

        req = DispatchRequest(
            capability="shell", arguments={},
            run_id="r1", step_id="s1", attempt_id="a1",
            plan_epoch=1, employee_id="e1",
        )

        # Before kill: works
        r1 = asyncio.run(broker.dispatch(req, EpochSet()))
        assert r1.success

        # After kill: blocked
        kill_switch.activate(reason="emergency")
        r2 = asyncio.run(broker.dispatch(req, EpochSet()))
        assert not r2.success
