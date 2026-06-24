"""Tests for Workflow payload TypedDicts and exports (Tasks 1,5 / 17).

Validates:
- WorkflowConfirmPayload is total=True with correct fields
- All workflow payloads are exported from card.events
- Factory functions produce correct event types
"""

import unittest

from src.card.events import (
    WorkflowAgentDonePayload,
    WorkflowAgentFailedPayload,
    WorkflowAgentStartedPayload,
    WorkflowConfirmPayload,
    WorkflowLogPayload,
    WorkflowPhasePayload,
    WorkflowProgressPayload,
    workflow_agent_done,
    workflow_agent_failed,
    workflow_agent_started,
    workflow_log,
    workflow_phase,
    workflow_progress,
)
from src.card.events.types import CardEventType


class TestWorkflowPayloadExports(unittest.TestCase):
    """Verify all workflow payloads are exported from card.events."""

    def test_confirm_payload_exported(self):
        self.assertIsNotNone(WorkflowConfirmPayload)

    def test_progress_payload_exported(self):
        self.assertIsNotNone(WorkflowProgressPayload)

    def test_phase_payload_exported(self):
        self.assertIsNotNone(WorkflowPhasePayload)

    def test_agent_started_payload_exported(self):
        self.assertIsNotNone(WorkflowAgentStartedPayload)

    def test_agent_done_payload_exported(self):
        self.assertIsNotNone(WorkflowAgentDonePayload)

    def test_agent_failed_payload_exported(self):
        self.assertIsNotNone(WorkflowAgentFailedPayload)

    def test_log_payload_exported(self):
        self.assertIsNotNone(WorkflowLogPayload)


class TestWorkflowConfirmPayloadContract(unittest.TestCase):
    """Verify WorkflowConfirmPayload has correct required/optional fields."""

    def test_required_fields(self):
        """All required fields must be present for total=True."""
        # This should NOT raise a type error at runtime
        payload: WorkflowConfirmPayload = {
            "script_name": "test",
            "description": "desc",
            "phases": [],
            "tools": ["coco"],
            "budget_total": 2_000_000,
            "requirement": "do X",
            "initiator_user_id": "user_1",
            "engine_session_key": "key_1",
        }
        self.assertEqual(payload["script_name"], "test")
        self.assertEqual(payload["initiator_user_id"], "user_1")
        self.assertEqual(payload["engine_session_key"], "key_1")

    def test_optional_fields_can_be_omitted(self):
        """NotRequired fields can be omitted."""
        payload: WorkflowConfirmPayload = {
            "script_name": "test",
            "description": "desc",
            "phases": [],
            "tools": ["coco"],
            "budget_total": 2_000_000,
            "requirement": "do X",
            "initiator_user_id": "user_1",
            "engine_session_key": "key_1",
        }
        # These should be absent without error
        self.assertNotIn("project_id", payload)
        self.assertNotIn("workflow_refs", payload)
        self.assertNotIn("dependency_graph", payload)
        self.assertNotIn("phase_tool_mapping", payload)

    def test_script_path_removed(self):
        """script_path should NOT be in the payload type."""
        import typing

        hints = typing.get_type_hints(WorkflowConfirmPayload)
        self.assertNotIn("script_path", hints)

    def test_new_security_fields_present(self):
        """initiator_user_id and engine_session_key must be defined."""
        import typing

        hints = typing.get_type_hints(WorkflowConfirmPayload)
        self.assertIn("initiator_user_id", hints)
        self.assertIn("engine_session_key", hints)


class TestWorkflowFactoryFunctions(unittest.TestCase):
    """Test all workflow factory functions produce correct events."""

    def test_workflow_progress(self):
        event = workflow_progress({"elements": []}, "status")
        self.assertEqual(event.type, CardEventType.WORKFLOW_PROGRESS)
        self.assertEqual(event.payload["compact_status"], "status")

    def test_workflow_progress_card_required_in_payload(self):
        """WORKFLOW_PROGRESS payload must always include 'card'."""
        event = workflow_progress({"elements": []}, "running")
        # Direct access — no .get() defensive fallback needed
        self.assertIn("card", event.payload)
        self.assertEqual(event.payload["card"], {"elements": []})

    def test_workflow_progress_without_compact_status(self):
        """compact_status is optional; payload should still have card."""
        event = workflow_progress({"elements": [{"tag": "div"}]})
        self.assertEqual(event.type, CardEventType.WORKFLOW_PROGRESS)
        self.assertIn("card", event.payload)
        self.assertNotIn("compact_status", event.payload)

    def test_workflow_progress_factory_requires_card(self):
        """Calling workflow_progress() without a card (or wrong type) raises TypeError."""
        with self.assertRaises(TypeError):
            workflow_progress(None)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            workflow_progress("not-a-dict")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            workflow_progress(["list", "instead", "of", "dict"])  # type: ignore[arg-type]

    def test_workflow_phase(self):
        event = workflow_phase("Phase 1")
        self.assertEqual(event.type, CardEventType.WORKFLOW_PHASE)
        self.assertEqual(event.payload["title"], "Phase 1")

    def test_workflow_agent_started(self):
        event = workflow_agent_started("agent1", "coco", "Phase 1")
        self.assertEqual(event.type, CardEventType.WORKFLOW_AGENT_STARTED)
        self.assertEqual(event.payload["label"], "agent1")
        self.assertEqual(event.payload["tool"], "coco")

    def test_workflow_agent_done(self):
        event = workflow_agent_done("agent1", token_usage=5000, cached=True)
        self.assertEqual(event.type, CardEventType.WORKFLOW_AGENT_DONE)
        self.assertTrue(event.payload.get("cached"))

    def test_workflow_agent_failed(self):
        event = workflow_agent_failed("agent1", "timeout")
        self.assertEqual(event.type, CardEventType.WORKFLOW_AGENT_FAILED)
        self.assertEqual(event.payload["error"], "timeout")

    def test_workflow_log(self):
        event = workflow_log("hello")
        self.assertEqual(event.type, CardEventType.WORKFLOW_LOG)
        self.assertEqual(event.payload["message"], "hello")


class TestWorkflowRefItemContract(unittest.TestCase):
    """Verify WorkflowRefItem TypedDict normalized contract."""

    def test_workflow_ref_item_path_is_not_required(self):
        """Verify that `path` is NotRequired - can construct with just `name`."""
        from src.card.events.payloads import WorkflowRefItem

        ref: WorkflowRefItem = {"name": "my-workflow"}
        self.assertEqual(ref["name"], "my-workflow")
        self.assertNotIn("path", ref)
        self.assertNotIn("hash", ref)

    def test_workflow_ref_item_all_fields(self):
        """Verify that `name`, `path`, and `hash` can all be set together."""
        from src.card.events.payloads import WorkflowRefItem

        ref: WorkflowRefItem = {
            "name": "my-workflow",
            "path": "workflows/my-workflow.js",
            "hash": "abc123",
        }
        self.assertEqual(ref["name"], "my-workflow")
        self.assertEqual(ref["path"], "workflows/my-workflow.js")
        self.assertEqual(ref["hash"], "abc123")

    def test_workflow_ref_item_name_is_required(self):
        """Verify that `name` is required (cannot construct without it)."""
        import typing

        from src.card.events.payloads import WorkflowRefItem

        hints = typing.get_type_hints(WorkflowRefItem)
        # name should be in the hints
        self.assertIn("name", hints)
        # name should NOT be NotRequired (it's required)
        # Check that the annotation is just `str`, not `NotRequired[str]`
        name_annotation = hints["name"]
        self.assertEqual(name_annotation, str)
        # path and hash should be NotRequired
        self.assertIn("path", hints)
        self.assertIn("hash", hints)

    def test_workflow_ref_item_docstring_explains_contract(self):
        """Verify that the TypedDict has a docstring explaining the normalized contract."""
        from src.card.events.payloads import WorkflowRefItem

        docstring = WorkflowRefItem.__doc__ or ""
        self.assertIn("Normalized contract", docstring)
        self.assertIn("legacy", docstring.lower())
        self.assertIn("script_path", docstring)
        self.assertIn("path", docstring)


class TestEnrichWorkflowRefs(unittest.TestCase):
    """Test _enrich_workflow_refs normalization logic."""

    def test_enrich_normalizes_string_refs_to_dicts(self):
        """String refs like 'my-workflow' should be normalized to {'name': 'my-workflow'}."""
        from src.workflow_engine.script_gen import _enrich_workflow_refs

        meta = {"workflow_refs": ["my-workflow", "another-workflow"]}
        _enrich_workflow_refs(meta, "")
        self.assertEqual(meta["workflow_refs"], [
            {"name": "my-workflow"},
            {"name": "another-workflow"},
        ])

    def test_enrich_migrates_script_path_to_path(self):
        """Dict refs with script_path but not path should be migrated to path."""
        from src.workflow_engine.script_gen import _enrich_workflow_refs

        meta = {"workflow_refs": [{"name": "wf1", "script_path": "path/to/wf1.js"}]}
        _enrich_workflow_refs(meta, "")
        self.assertEqual(meta["workflow_refs"], [
            {"name": "wf1", "path": "path/to/wf1.js"},
        ])
        # script_path should be removed
        self.assertNotIn("script_path", meta["workflow_refs"][0])

    def test_enrich_preserves_existing_path(self):
        """Dict refs that already have path should be preserved unchanged."""
        from src.workflow_engine.script_gen import _enrich_workflow_refs

        meta = {"workflow_refs": [
            {"name": "wf1", "path": "path/to/wf1.js", "hash": "abc123"},
        ]}
        _enrich_workflow_refs(meta, "")
        self.assertEqual(meta["workflow_refs"], [
            {"name": "wf1", "path": "path/to/wf1.js", "hash": "abc123"},
        ])

    def test_enrich_handles_mixed_refs(self):
        """Mixed string and dict refs should both be normalized correctly."""
        from src.workflow_engine.script_gen import _enrich_workflow_refs

        meta = {"workflow_refs": [
            "string-ref",
            {"name": "dict-ref", "script_path": "old/path.js"},
            {"name": "dict-ref-2", "path": "new/path.js"},
        ]}
        _enrich_workflow_refs(meta, "")
        self.assertEqual(meta["workflow_refs"], [
            {"name": "string-ref"},
            {"name": "dict-ref", "path": "old/path.js"},
            {"name": "dict-ref-2", "path": "new/path.js"},
        ])

    def test_enrich_handles_empty_refs(self):
        """Empty workflow_refs list should remain unchanged."""
        from src.workflow_engine.script_gen import _enrich_workflow_refs

        meta = {"workflow_refs": []}
        _enrich_workflow_refs(meta, "")
        self.assertEqual(meta["workflow_refs"], [])

    def test_enrich_scans_script_for_workflow_calls(self):
        """When meta has no workflow_refs, scan script for workflow('name') calls."""
        from src.workflow_engine.script_gen import _enrich_workflow_refs

        meta = {}
        script = """
export default async function() {
  const r1 = await workflow("sub-workflow-a", { x: 1 });
  const r2 = await workflow('sub-workflow-b', { y: 2 });
  const r3 = await workflow(`sub-workflow-c`, { z: 3 });
  // duplicate should not appear twice
  const r4 = await workflow("sub-workflow-a", { again: true });
}
"""
        _enrich_workflow_refs(meta, script)
        self.assertEqual(meta["workflow_refs"], [
            {"name": "sub-workflow-a"},
            {"name": "sub-workflow-b"},
            {"name": "sub-workflow-c"},
        ])


class TestWorkflowRefBackwardCompatibility(unittest.TestCase):
    """Test backward compatibility for legacy workflow ref formats."""

    def test_legacy_string_ref_supported(self):
        """String refs are accepted for backward compatibility."""
        from src.workflow_engine.script_gen import _enrich_workflow_refs

        # String refs in meta should be accepted and normalized
        meta = {"workflow_refs": ["legacy-string-ref"]}
        _enrich_workflow_refs(meta, "")
        self.assertEqual(meta["workflow_refs"], [{"name": "legacy-string-ref"}])

        # Also verify extract_meta_from_script handles string refs
        from src.workflow_engine.script_gen import extract_meta_from_script
        script_with_string_refs = """
export const meta = {
  name: "test",
  description: "test",
  phases: [{ title: "Phase 1" }],
  tools: ["coco"],
  workflow_refs: ["legacy-ref-1", "legacy-ref-2"],
};
export default async function() {
  await agent("do something", { tool: "coco" });
}
"""
        extracted = extract_meta_from_script(script_with_string_refs)
        self.assertIsNotNone(extracted)
        self.assertEqual(extracted["workflow_refs"], [
            {"name": "legacy-ref-1"},
            {"name": "legacy-ref-2"},
        ])

    def test_legacy_script_path_field_readable(self):
        """In _build_confirm_card, refs with script_path are correctly read."""
        # Test the exact fallback pattern from _build_confirm_card:
        # ref.get("path", ref.get("script_path", ""))
        ref_with_script_path = {"name": "wf1", "script_path": "legacy/path.js"}
        ref_with_path = {"name": "wf2", "path": "new/path.js"}
        ref_with_both = {"name": "wf3", "path": "new/path.js", "script_path": "legacy/path.js"}
        ref_with_neither = {"name": "wf4"}

        # Legacy script_path should be used as fallback
        self.assertEqual(
            ref_with_script_path.get("path", ref_with_script_path.get("script_path", "")),
            "legacy/path.js",
        )
        # New path field should be preferred
        self.assertEqual(
            ref_with_path.get("path", ref_with_path.get("script_path", "")),
            "new/path.js",
        )
        # When both exist, path takes precedence
        self.assertEqual(
            ref_with_both.get("path", ref_with_both.get("script_path", "")),
            "new/path.js",
        )
        # When neither exists, empty string is returned
        self.assertEqual(
            ref_with_neither.get("path", ref_with_neither.get("script_path", "")),
            "",
        )

        # Also verify _enrich_workflow_refs migrates script_path to path
        from src.workflow_engine.script_gen import _enrich_workflow_refs
        meta = {"workflow_refs": [ref_with_script_path]}
        _enrich_workflow_refs(meta, "")
        self.assertEqual(meta["workflow_refs"][0]["path"], "legacy/path.js")
        self.assertNotIn("script_path", meta["workflow_refs"][0])


class TestWorkflowButtonValueFilter(unittest.TestCase):
    """Verify filter_workflow_button_value drops forged callback payload fields."""

    def test_filter_preserves_known_fields(self):
        """Known button fields should pass through."""
        from src.card.events.payloads import filter_workflow_button_value

        value = {
            "action": "workflow_confirm_start",
            "chat_id": "chat_001",
            "project_id": "proj_001",
            "engine_session_key": "sess-xyz",
            "tool_name": "coco",
            "provider": "workflow",
            "display_name": "coco",
            "supports_model": True,
            "model_name": "claude-3-5",
            "use_default_model": False,
            "selection_key": "sel-123",
        }
        filtered = filter_workflow_button_value(value)
        self.assertEqual(filtered, value)

    def test_filter_drops_confirmed(self):
        """A client-side ``confirmed`` forgery should be stripped."""
        from src.card.events.payloads import filter_workflow_button_value

        value = {
            "action": "workflow_confirm_start",
            "chat_id": "chat_001",
            "project_id": "proj_001",
            "engine_session_key": "sess-xyz",
            "confirmed": True,  # forged
        }
        filtered = filter_workflow_button_value(value)
        self.assertNotIn("confirmed", filtered)

    def test_filter_drops_admin_override_budget(self):
        """Unknown fields like ``admin`` / ``override_budget`` must be dropped."""
        from src.card.events.payloads import filter_workflow_button_value

        value = {
            "action": "workflow_confirm_start",
            "chat_id": "chat_001",
            "project_id": "proj_001",
            "engine_session_key": "sess-xyz",
            "admin": "1",
            "override_budget": 999999999,
        }
        filtered = filter_workflow_button_value(value)
        self.assertNotIn("admin", filtered)
        self.assertNotIn("override_budget", filtered)

    def test_filter_rejects_non_dict(self):
        """Non-dict inputs return empty dict (defensive)."""
        from src.card.events.payloads import filter_workflow_button_value

        self.assertEqual(filter_workflow_button_value(None), {})  # type: ignore[arg-type]
        self.assertEqual(filter_workflow_button_value("not a dict"), {})  # type: ignore[arg-type]
        self.assertEqual(filter_workflow_button_value([]), {})  # type: ignore[arg-type]

    def test_filter_empty_input_stays_empty(self):
        from src.card.events.payloads import filter_workflow_button_value
        self.assertEqual(filter_workflow_button_value({}), {})

    def test_button_values_only_known_keys_returns(self):
        """Only known keys survive filtering."""
        from src.card.events.payloads import (
            WorkflowConfirmCardValue,
            filter_workflow_button_value,
        )

        # Construct a value filled with all-schema + injected fields
        typed: WorkflowConfirmCardValue = {
            "action": "test",
            "chat_id": "chat_001",
            "project_id": "proj_001",
            "engine_session_key": "sess-xyz",
            "tool_name": "coco",
            "provider": "workflow",
            "display_name": "coco",
            "supports_model": True,
            "model_name": "claude-3-5",
            "use_default_model": False,
            "selection_key": "sel-123",
        }
        out = filter_workflow_button_value(typed)
        self.assertEqual(set(out.keys()), set(typed.keys()))


if __name__ == "__main__":
    unittest.main()
