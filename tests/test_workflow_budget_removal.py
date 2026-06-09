"""Tests for verifying budget-related code removal from Workflow mode.

Validates:
- AC9: constants.py has no budget constants
- AC10: PendingConfirmation model has no selected_budget field
- AC11: StateManager has no try_reserve/settle methods
- Budget-related fields are deprecated but handled properly for backward compatibility
"""

import unittest
import inspect


class TestBudgetConstantsRemoved(unittest.TestCase):
    """Verify budget constants have been removed from constants.py."""

    def test_no_budget_constants_in_constants(self):
        """AC9: constants.py must not contain budget-related constants."""
        from src.workflow_engine import constants

        # These constants should NOT exist
        self.assertFalse(
            hasattr(constants, 'DEFAULT_BUDGET_TOKENS'),
            "DEFAULT_BUDGET_TOKENS should be removed"
        )
        self.assertFalse(
            hasattr(constants, 'BUDGET_OPTIONS'),
            "BUDGET_OPTIONS should be removed"
        )
        self.assertFalse(
            hasattr(constants, 'RESERVE_PER_AGENT_TOKENS'),
            "RESERVE_PER_AGENT_TOKENS should be removed"
        )


class TestPendingConfirmationNoBudgetField(unittest.TestCase):
    """Verify PendingConfirmation model has no budget-related fields."""

    def test_no_selected_budget_field(self):
        """AC10: PendingConfirmation must not have selected_budget field."""
        from src.workflow_engine.models import PendingConfirmation

        # Check that selected_budget is NOT a field
        fields = PendingConfirmation.model_fields.keys()
        self.assertNotIn('selected_budget', fields,
                        "selected_budget field should be removed from PendingConfirmation")


class TestStateManagerNoBudgetMethods(unittest.TestCase):
    """Verify WorkflowStateManager has no budget-related methods."""

    def test_no_budget_methods_in_state_manager(self):
        """AC11: WorkflowStateManager must not have try_reserve/settle methods."""
        from src.workflow_engine.state_manager import WorkflowStateManager

        # These methods should NOT exist
        self.assertFalse(
            hasattr(WorkflowStateManager, 'try_reserve'),
            "try_reserve method should be removed from WorkflowStateManager"
        )
        self.assertFalse(
            hasattr(WorkflowStateManager, 'settle'),
            "settle method should be removed from WorkflowStateManager"
        )
        self.assertFalse(
            hasattr(WorkflowStateManager, 'rollback'),
            "rollback method should be removed from WorkflowStateManager"
        )


class TestBudgetFieldsDeprecatedButHandled(unittest.TestCase):
    """Verify deprecated budget fields in payloads are handled properly."""

    def test_workflow_progress_payload_deprecated_fields(self):
        """Budget fields in WorkflowProgressPayload should be deprecated but present."""
        from src.card.events.payloads import WorkflowProgressPayload

        # These fields should exist but be deprecated
        # Check by inspecting the TypedDict annotations
        annotations = WorkflowProgressPayload.__annotations__
        self.assertIn('budget_consumed', annotations,
                     "budget_consumed should exist for backward compatibility")
        self.assertIn('budget_remaining', annotations,
                     "budget_remaining should exist for backward compatibility")

    def test_workflow_confirm_payload_deprecated_field(self):
        """Budget field in WorkflowConfirmPayload should be deprecated but present."""
        from src.card.events.payloads import WorkflowConfirmPayloadOptional

        # budget_total should exist but be deprecated
        annotations = WorkflowConfirmPayloadOptional.__annotations__
        self.assertIn('budget_total', annotations,
                     "budget_total should exist for backward compatibility")


class TestBudgetExhaustedErrorDeprecated(unittest.TestCase):
    """Verify BUDGET_EXHAUSTED error category is deprecated but kept."""

    def test_budget_exhausted_category_exists_as_deprecated(self):
        """BUDGET_EXHAUSTED should exist for backward compatibility."""
        from src.workflow_engine.errors import ErrorCategory

        # BUDGET_EXHAUSTED should still exist (deprecated but kept)
        self.assertTrue(hasattr(ErrorCategory, 'BUDGET_EXHAUSTED'),
                       "BUDGET_EXHAUSTED should exist for backward compatibility")


if __name__ == "__main__":
    unittest.main()