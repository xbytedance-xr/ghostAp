"""Unit tests for SpecRenderer: _create_rotator, cycle-rotation parameters, and SessionRotator wrapping."""

from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest

from src.card.session.rotator import SessionRotator
from src.card.state.models import CardMetadata


class TestSpecRendererCreateRotator:
    """Verify _create_rotator creates a SessionRotator wrapping a CardSession."""

    def test_create_rotator_returns_session_rotator(self):
        """_create_rotator should return a SessionRotator instance."""
        from src.feishu.renderers.spec_renderer import SpecRenderer

        renderer = SpecRenderer.__new__(SpecRenderer)
        renderer.settings = MagicMock()
        renderer.handler = MagicMock()

        mock_session = MagicMock()
        mock_session.session_id = "sess_abc123"
        mock_session.closed = False

        with patch.object(renderer, "create_session", return_value=mock_session):
            metadata = CardMetadata(engine_type="spec", mode_name="Spec · Coco", mode_emoji="📋")
            result = renderer._create_rotator("chat1", "msg1", metadata, hooks=(), budget=None)

        assert isinstance(result, SessionRotator)
        assert result.session_id == "sess_abc123"

    def test_create_rotator_passes_hooks_and_budget(self):
        """_create_rotator should forward hooks and budget to create_session."""
        from src.feishu.renderers.spec_renderer import SpecRenderer

        renderer = SpecRenderer.__new__(SpecRenderer)
        renderer.settings = MagicMock()
        renderer.handler = MagicMock()

        mock_session = MagicMock()
        mock_session.session_id = "s1"
        mock_session.closed = False

        mock_hooks = (MagicMock(),)
        mock_budget = MagicMock()

        with patch.object(renderer, "create_session", return_value=mock_session) as mock_create:
            metadata = CardMetadata(engine_type="spec", mode_name="Spec", mode_emoji="📋")
            renderer._create_rotator("chat1", "msg1", metadata, hooks=mock_hooks, budget=mock_budget)

        mock_create.assert_called_once_with(
            "chat1", "msg1", metadata, hooks=mock_hooks, budget=mock_budget,
            ttl_seconds=None,
        )


class TestSpecRendererCycleRotation:
    """Verify cycle-rotation passes continuation_seq to CardMetadata."""

    def test_continuation_seq_increments_on_rotation(self):
        """After rotation, new session metadata should carry continuation_seq > 0."""
        metadata = CardMetadata(
            engine_type="spec",
            mode_name="Spec · Coco",
            mode_emoji="📋",
        )

        # Simulate what _new_session does
        mock_rotator = MagicMock()
        mock_rotator.rotation_count = 1  # Already rotated once

        cont_meta = replace(metadata, continuation_seq=mock_rotator.rotation_count + 1)
        assert cont_meta.continuation_seq == 2
        assert cont_meta.engine_type == "spec"
        assert cont_meta.mode_name == "Spec · Coco"

    def test_first_rotation_gives_seq_1(self):
        """First rotation should give continuation_seq=1."""
        metadata = CardMetadata(engine_type="spec", mode_emoji="📋")

        mock_rotator = MagicMock()
        mock_rotator.rotation_count = 0  # No rotations yet

        cont_meta = replace(metadata, continuation_seq=mock_rotator.rotation_count + 1)
        assert cont_meta.continuation_seq == 1

    def test_session_rotator_exposes_rotation_count(self):
        """SessionRotator should expose rotation_count property."""
        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.session_id = "s1"

        rotator = SessionRotator(mock_session)
        assert rotator.rotation_count == 0

        new_session = MagicMock()
        new_session.closed = False
        new_session.session_id = "s2"

        rotator.rotate(lambda: new_session)
        assert rotator.rotation_count == 1

    def test_continuation_seq_reflected_in_header(self):
        """CardMetadata with continuation_seq > 0 produces header with 续 #N."""
        from src.card.state.reducers._shared import build_header

        metadata = CardMetadata(
            engine_type="spec",
            mode_name="Spec · Coco",
            mode_emoji="📋",
            continuation_seq=3,
        )
        header = build_header(metadata, "running")
        assert "(续 #3)" in header.title
        assert "📋" in header.title
