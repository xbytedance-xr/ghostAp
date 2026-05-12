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
