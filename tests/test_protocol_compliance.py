"""Protocol compliance tests: verify CardSession and SessionRotator satisfy Dispatchable."""

import inspect

from src.card.engine_snapshot import Snapshotable
from src.card.protocols import Dispatchable
from src.card.render.worktree import WorktreeCallbacks


class TestDispatchableProtocol:
    """Verify Dispatchable protocol is satisfied by key classes."""

    def test_card_session_is_dispatchable(self):
        from unittest.mock import MagicMock

        from src.card.delivery.engine import CardDelivery
        from src.card.session import CardSession
        from src.card.session.config import SessionConfig
        from src.card.state.models import CardMetadata

        client = MagicMock()
        delivery = CardDelivery(client)
        config = SessionConfig(metadata=CardMetadata(engine_type="deep"))
        session = CardSession(
            delivery=delivery,
            chat_id="test",
            config=config,
        )
        assert isinstance(session, Dispatchable)
        session.close()

    def test_session_rotator_is_dispatchable(self):
        from unittest.mock import MagicMock

        from src.card.delivery.engine import CardDelivery
        from src.card.session import CardSession
        from src.card.session.config import SessionConfig
        from src.card.session.rotator import SessionRotator
        from src.card.state.models import CardMetadata

        client = MagicMock()
        delivery = CardDelivery(client)
        config = SessionConfig(metadata=CardMetadata(engine_type="deep"))
        session = CardSession(
            delivery=delivery,
            chat_id="test",
            config=config,
        )
        rotator = SessionRotator(session)
        assert isinstance(rotator, Dispatchable)
        rotator.close()


class TestSnapshotableRealClass:
    """Verify real engine managers satisfy Snapshotable protocol."""

    def test_spec_engine_manager_satisfies_snapshotable(self):
        from src.spec_engine.manager import SpecEngineManager
        assert isinstance(SpecEngineManager, type)
        # Check the protocol method signatures exist
        assert hasattr(SpecEngineManager, "snapshot")
        assert hasattr(SpecEngineManager, "snapshot_active")

    def test_deep_engine_manager_satisfies_snapshotable(self):
        from src.deep_engine.engine import DeepEngineManager
        assert hasattr(DeepEngineManager, "snapshot")
        assert hasattr(DeepEngineManager, "snapshot_active")

    def test_snapshot_method_signature_alignment(self):
        """Verify snapshot() method params align with Snapshotable protocol."""
        from src.engine_base import BaseEngineManager

        proto_sig = inspect.signature(Snapshotable.snapshot)
        real_sig = inspect.signature(BaseEngineManager.snapshot)

        proto_params = set(proto_sig.parameters.keys()) - {"self"}
        real_params = set(real_sig.parameters.keys()) - {"self"}
        assert proto_params <= real_params, (
            f"Protocol requires {proto_params}, real has {real_params}"
        )

    def test_snapshot_active_method_signature_alignment(self):
        """Verify snapshot_active() method params align with Snapshotable protocol."""
        from src.engine_base import BaseEngineManager

        proto_sig = inspect.signature(Snapshotable.snapshot_active)
        real_sig = inspect.signature(BaseEngineManager.snapshot_active)

        proto_params = set(proto_sig.parameters.keys()) - {"self"}
        real_params = set(real_sig.parameters.keys()) - {"self"}
        assert proto_params <= real_params, (
            f"Protocol requires {proto_params}, real has {real_params}"
        )


class TestWorktreeCallbacksProtocol:
    """Verify WorktreeReporter satisfies WorktreeCallbacks protocol."""

    def test_worktree_reporter_satisfies_protocol(self):
        from src.card.render.worktree import WorktreeCallbacks
        from src.worktree_engine.reporter import WorktreeReporter

        reporter = WorktreeReporter()
        assert isinstance(reporter, WorktreeCallbacks)


class TestNegativeProtocolCompliance:
    """Negative tests: objects missing methods must NOT satisfy protocols."""

    def test_plain_object_not_dispatchable(self):
        from src.card.protocols import Dispatchable

        class NotDispatchable:
            pass

        assert not isinstance(NotDispatchable(), Dispatchable)

    def test_wrong_dispatch_name_not_dispatchable(self):
        from src.card.protocols import Dispatchable

        class WrongMethod:
            def send(self, event) -> None:
                pass

        assert not isinstance(WrongMethod(), Dispatchable)

    def test_plain_object_not_worktree_callbacks(self):
        from src.card.render.worktree import WorktreeCallbacks

        class Empty:
            pass

        assert not isinstance(Empty(), WorktreeCallbacks)

    def test_partial_worktree_callbacks_not_satisfied(self):
        from src.card.render.worktree import WorktreeCallbacks

        class Partial:
            def refresh_state(self, state):
                return state

        assert not isinstance(Partial(), WorktreeCallbacks)


class TestTTLProtocolCompliance:
    """Verify CardSession satisfies TTLDecider protocol and owns a TTLActuator."""

    def test_card_session_has_all_ttl_interface_methods(self):
        from unittest.mock import MagicMock

        from src.card.delivery.engine import CardDelivery
        from src.card.session import CardSession
        from src.card.session._ttl_mixin import TTLActuator
        from src.card.session.config import SessionConfig
        from src.card.state.models import CardMetadata

        client = MagicMock()
        delivery = CardDelivery(client)
        config = SessionConfig(metadata=CardMetadata(engine_type="deep"))
        session = CardSession(
            delivery=delivery,
            chat_id="test",
            config=config,
        )

        # Verify TTLDecider methods on CardSession itself
        assert callable(session.get_ttl_state)
        assert isinstance(session.engine_cmd, str)
        assert isinstance(session.engine_name, str)

        # Verify TTLActuator is composition-based (not inherited)
        actuator = session._ttl_actuator
        assert isinstance(actuator, TTLActuator)

        # Verify all TTLActuator methods exist and are callable
        assert callable(actuator.reduce_and_render)
        assert callable(actuator.mark_ttl_expired)
        assert callable(actuator.rollback_ttl_warned)
        assert callable(actuator.force_terminate)
        assert callable(actuator.deliver_terminal)
        assert callable(actuator.deliver_update)
        assert callable(actuator.force_deliver)
        assert callable(actuator.close_delivery)
        assert callable(actuator.notify_user)
        assert callable(actuator.fire_terminal_hook)
        assert callable(actuator.schedule_ttl_retry)
        assert callable(actuator.cancel_timers)
        assert callable(actuator.schedule_retry)
        assert callable(actuator.defer_idle_timeout)
        assert callable(actuator.flag_retry_pending)

        session.close()

    def test_get_ttl_state_returns_named_tuple(self):
        from unittest.mock import MagicMock

        from src.card.delivery.engine import CardDelivery
        from src.card.protocols import TTLState
        from src.card.session import CardSession
        from src.card.session.config import SessionConfig
        from src.card.state.models import CardMetadata

        client = MagicMock()
        delivery = CardDelivery(client)
        config = SessionConfig(metadata=CardMetadata(engine_type="deep"), ttl_seconds=300.0)
        session = CardSession(
            delivery=delivery,
            chat_id="test",
            config=config,
        )

        state = session.get_ttl_state()
        assert isinstance(state, TTLState)
        assert state.closed is False
        assert state.ttl_warned is False
        assert state.ttl_seconds == 300.0
        assert state.session_id == session.session_id
        assert state.idle_seconds >= 0

        session.close()

    def test_engine_cmd_and_name_properties(self):
        from unittest.mock import MagicMock

        from src.card.delivery.engine import CardDelivery
        from src.card.session import CardSession
        from src.card.session.config import SessionConfig
        from src.card.state.models import CardMetadata

        client = MagicMock()
        delivery = CardDelivery(client)

        config = SessionConfig(metadata=CardMetadata(engine_type="deep"))
        session = CardSession(
            delivery=delivery,
            chat_id="test",
            config=config,
        )
        assert session.engine_cmd == "/deep"
        assert session.engine_name == "Deep"
        session.close()

        config2 = SessionConfig(metadata=CardMetadata(engine_type="spec"))
        session2 = CardSession(
            delivery=CardDelivery(MagicMock()),
            chat_id="test",
            config=config2,
        )
        assert session2.engine_cmd == "/spec"
        assert session2.engine_name == "Spec"
        session2.close()


class TestProtocolsReExport:
    """Verify Dispatchable and WorktreeCallbacks are importable from canonical locations."""

    def test_import_dispatchable_from_protocols(self):
        from src.card.protocols import Dispatchable as D
        assert D is Dispatchable

    def test_import_worktree_callbacks_from_render(self):
        from src.card.render.worktree import WorktreeCallbacks as WC
        assert WC is WorktreeCallbacks
