from __future__ import annotations

from types import SimpleNamespace


def test_card_action_inspector_parses_action_project_and_fingerprint():
    from src.feishu.ws_card_action_handler import CardActionInspector

    action = SimpleNamespace(
        value='{"action":"show_status","project_id":"proj-1"}',
        option={"selected": "x"},
    )

    assert CardActionInspector.action_type(action) == "show_status"
    assert CardActionInspector.project_id(action) == "proj-1"
    assert CardActionInspector.is_system_action(action) is True
    assert CardActionInspector.dedup_fingerprint(action) == CardActionInspector.dedup_fingerprint(action)


def test_card_action_inspector_handles_invalid_payloads_safely():
    from src.feishu.ws_card_action_handler import CardActionInspector

    action = SimpleNamespace(value="not-json")

    assert CardActionInspector.action_type(action) == ""
    assert CardActionInspector.project_id(action) is None
    assert CardActionInspector.is_system_action(action) is False


def test_degraded_error_card_actions_are_system_actions():
    from src.card.actions import dispatch as action_ids
    from src.feishu.ws_card_action_handler import CardActionInspector

    for action_id in (
        action_ids.CONTINUE_DEGRADED,
        action_ids.SHOW_ERROR_DETAILS,
        action_ids.RETRY_ORIGINAL,
    ):
        action = SimpleNamespace(value={"action": action_id})
        assert CardActionInspector.is_system_action(action) is True


def test_card_action_failure_remains_user_reachable():
    from src.feishu.ws_card_action_handler import CardActionFailureAction, classify_card_action_error

    malformed = classify_card_action_error(RuntimeError("bad payload"), phase="payload_parse")
    assert malformed.action == CardActionFailureAction.ACK_AND_IGNORE
    assert malformed.user_reachable is False

    dispatch = classify_card_action_error(RuntimeError("handler failed"), phase="dispatch")
    assert dispatch.action == CardActionFailureAction.REPLY_FAILURE_CARD
    assert dispatch.user_reachable is True

    fatal = classify_card_action_error(RuntimeError("signature invalid"), phase="security")
    assert fatal.action == CardActionFailureAction.RAISE
    assert fatal.user_reachable is False


def test_extract_behavior_value_object_form():
    """AC18: _extract_behavior_value 正确处理 object 形式的 behavior。"""
    from src.feishu.ws_card_action_handler import _extract_behavior_value

    class MockBehavior:
        def __init__(self, value):
            self.value = value

    # Object with value
    behavior = MockBehavior({"action": "test", "data": "123"})
    result = _extract_behavior_value(behavior)
    assert result == {"action": "test", "data": "123"}

    # Object with None value
    behavior_none = MockBehavior(None)
    result_none = _extract_behavior_value(behavior_none)
    assert result_none is None


def test_extract_behavior_value_mapping_form():
    """AC18: _extract_behavior_value 正确处理 Mapping 形式的 behavior。"""
    from src.feishu.ws_card_action_handler import _extract_behavior_value

    # Dict (Mapping) with value
    behavior_dict = {"type": "callback", "value": {"action": "test_dict"}}
    result = _extract_behavior_value(behavior_dict)
    assert result == {"action": "test_dict"}

    # Dict without value key
    behavior_no_value = {"type": "callback"}
    result_no_value = _extract_behavior_value(behavior_no_value)
    assert result_no_value is None

    # Dict with None value
    behavior_none_value = {"type": "callback", "value": None}
    result_none_value = _extract_behavior_value(behavior_none_value)
    assert result_none_value is None


def test_value_dict_with_object_behavior():
    """AC18: CardActionInspector.value_dict 正确处理 object 形式的 behaviors。"""
    from src.feishu.ws_card_action_handler import CardActionInspector

    class MockBehavior:
        def __init__(self, value):
            self.value = value

    class MockAction:
        def __init__(self, behaviors, value=None):
            self.behaviors = behaviors
            self.value = value

    # Object form behaviors
    behavior = MockBehavior({"action": "select_tool", "tool_name": "coco"})
    action = MockAction(behaviors=[behavior], value=None)

    result = CardActionInspector.value_dict(action)
    assert result == {"action": "select_tool", "tool_name": "coco"}
    assert CardActionInspector.action_type(action) == "select_tool"


def test_value_dict_with_mapping_behavior():
    """AC18: CardActionInspector.value_dict 正确处理 Mapping 形式的 behaviors。"""
    from src.feishu.ws_card_action_handler import CardActionInspector

    class MockAction:
        def __init__(self, behaviors, value=None):
            self.behaviors = behaviors
            self.value = value

    # Mapping form behaviors (dict)
    behavior_dict = {"type": "callback", "value": {"action": "select_budget", "budget_tokens": 2000000}}
    action = MockAction(behaviors=[behavior_dict], value=None)

    result = CardActionInspector.value_dict(action)
    assert result == {"action": "select_budget", "budget_tokens": 2000000}
    assert CardActionInspector.action_type(action) == "select_budget"


def test_value_dict_behavior_priority_over_legacy_value():
    """AC18: behaviors[0].value 优先于 legacy action.value。"""
    from src.feishu.ws_card_action_handler import CardActionInspector

    class MockBehavior:
        def __init__(self, value):
            self.value = value

    class MockAction:
        def __init__(self, behaviors, value=None):
            self.behaviors = behaviors
            self.value = value

    # Both behaviors and legacy value present — behaviors should win
    behavior = MockBehavior({"action": "from_behavior"})
    action = MockAction(
        behaviors=[behavior],
        value={"action": "from_legacy_value", "should": "be_ignored"},
    )

    result = CardActionInspector.value_dict(action)
    assert result == {"action": "from_behavior"}, \
        "behaviors[0].value should take priority over legacy action.value"


def test_value_dict_fallback_to_legacy_value():
    """AC18: 无 behaviors 时回退到 legacy action.value。"""
    from src.feishu.ws_card_action_handler import CardActionInspector

    class MockAction:
        def __init__(self, behaviors, value=None):
            self.behaviors = behaviors
            self.value = value

    # No behaviors, only legacy value
    action = MockAction(
        behaviors=None,
        value={"action": "fallback_value"},
    )

    result = CardActionInspector.value_dict(action)
    assert result == {"action": "fallback_value"}, \
        "Should fallback to legacy action.value when no behaviors"
