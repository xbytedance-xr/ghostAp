import time

from src.card.events import CardEvent
from src.card.session import CardSession
from src.card.session.config import SessionConfig
from src.card.state.models import CardMetadata


class _Delivery:
    def acquire_lock(self, _session_id):
        return True

    def release_lock(self, _session_id):
        return None


def test_card_metadata_has_v2_defaults():
    metadata = CardMetadata()

    assert metadata.card_sequence == 1
    assert metadata.session_started_at is None
    assert metadata.working_dir is None
    assert metadata.is_subagent is False
    assert metadata.parent_card_seq is None
    assert metadata.final_state_for_freeze is None
    assert metadata.frozen is False
    assert metadata.frozen_total_elapsed is None


def test_card_session_exposes_v2_metadata_fields():
    started_at = time.monotonic() - 10
    session = CardSession(
        chat_id="",
        config=SessionConfig(
            metadata=CardMetadata(
                card_sequence="5.a",
                session_started_at=started_at,
                is_subagent=True,
                parent_card_seq="5",
            ),
            clock=lambda: started_at + 10,
        ),
        delivery=_Delivery(),
        session_id="s1",
    )

    assert session.sequence == "5.a"
    assert session.session_started_at == started_at
    assert session.is_subagent is True
    assert session.parent_card_seq == "5"
    assert session.final_state_for_freeze is None


def test_card_split_event_carries_optional_bridge_phrase():
    event = CardEvent.card_split("task_done", bridge_phrase="续接：")

    assert event.payload["reason"] == "task_done"
    assert event.payload["bridge_phrase"] == "续接："
