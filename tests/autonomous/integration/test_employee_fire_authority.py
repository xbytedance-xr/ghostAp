from __future__ import annotations

from contextlib import contextmanager

from src.autonomous.ingress.projection import IngressProjectionState
from src.autonomous.ingress.service import EmployeeIngressService
from src.autonomous.journal.blob_store import AesGcmEncryptionProvider, BlobStore
from src.autonomous.journal.frame import JournalEvent
from src.autonomous.journal.projections import ProjectionState, apply_frame
from src.autonomous.provisioning.fire_authority import JournalFireAuthority
from src.autonomous.provisioning.fire_service import EmployeeFireRequest
from tests.autonomous.workforce_helpers import (
    bot_binding_events,
    commit_events,
    employee_created,
    make_writer,
)


class _HireProjectionOwner:
    def __init__(self, state: ProjectionState) -> None:
        self.projection_state = state

    @contextmanager
    def employee_dispatch_guard(self):
        yield

    def synchronize_projection(self):
        return self.projection_state

    def synchronize_projection_unlocked(self):
        return self.projection_state

    def apply_committed_frame_unlocked(self, frame):
        apply_frame(self.projection_state, frame)


def test_admission_atomically_commits_retiring_and_employee_ingress_closure(tmp_path):
    writer = make_writer(tmp_path)
    state = ProjectionState()
    commit_events(writer, state, employee_created())
    commit_events(writer, state, *bot_binding_events())
    commit_events(
        writer,
        state,
        JournalEvent(
            event_type="employee.state_changed",
            aggregate_id="agt_1",
            payload={"state": "active"},
        ),
    )
    ingress = EmployeeIngressService(
        writer=writer,
        blob_store=BlobStore(
            tmp_path / "blobs",
            AesGcmEncryptionProvider(lambda _key: b"fire-ingress-data-key-32-bytes!!"),
        ),
        ingress_state=IngressProjectionState(),
        active_key_id="k1",
    )
    hire = _HireProjectionOwner(state)
    authority = JournalFireAuthority(
        writer=writer,
        hire_service=hire,
        ingress_service=ingress,
        admin_principal_ids=frozenset({"ou_admin"}),
    )
    request = EmployeeFireRequest(
        employee="Atlas",
        tenant_key="tenant_1",
        message_id="om_fire",
        chat_id="oc_dm",
        requester_principal_id="ou_admin",
    )
    target = authority.resolve(request)

    authority.admit(request, target, "fire_intent")

    assert state.employees["agt_1"].state.value == "retiring"
    assert ("tenant_1", "agt_1") in ingress.state.closed_employees
    final = writer.get_last_frame()
    assert final is not None
    assert [event.event_type for event in final.events] == [
        "fire.requested",
        "employee.state_changed",
        "employee.ingress.closed",
    ]
    ingress.close()
    writer.close()
