"""End-to-end test: /new-chat from intent recognition through to project creation."""
import os
from unittest.mock import MagicMock

import pytest

from src.agent.intent_recognizer import IntentRecognizer, IntentType
from src.project.manager import ProjectManager
from src.project_chat.lark_chat_client import CreateChatResult
from src.project_chat.service import ProjectChatService


@pytest.fixture
def project_manager(tmp_path):
    return ProjectManager(storage_path=str(tmp_path / "projects.json"))


@pytest.fixture
def recognizer():
    return IntentRecognizer()


class TestE2EFlow:
    def test_full_flow_new_project(self, recognizer, project_manager, tmp_path):
        """Complete flow: intent → service → project created with bound chat."""
        path = str(tmp_path / "myapp")
        os.makedirs(path)

        # 1. Intent recognition
        result = recognizer.recognize(f"/new-chat myapp dev {path}")
        assert result.primary_intent == IntentType.NEW_CHAT_PROJECT
        assert result.primary_data["name"] == "myapp"
        assert result.primary_data["suffix"] == "dev"
        assert result.primary_data["path"] == path

        # 2. Service execution
        mock_lark = MagicMock()
        mock_lark.create_chat.return_value = CreateChatResult(
            chat_id="oc_e2e_chat", name="myapp-dev"
        )
        reply_fn = MagicMock()

        service = ProjectChatService(
            project_manager=project_manager,
            lark_chat_client=mock_lark,
            reply_fn=reply_fn,
            send_to_chat_fn=MagicMock(),
        )

        service.handle(
            message_id="msg_e2e",
            chat_id="oc_main",
            sender_open_id="ou_tester",
            data=result.primary_data,
        )

        # 3. Verify result
        ctx = project_manager.find_project_by_path(path, chat_id=None)
        assert ctx is not None
        assert ctx.bound_chat_id == "oc_e2e_chat"
        assert ctx.project_name == "myapp"
        assert ctx.owner_chat_id == "oc_e2e_chat"
        assert "oc_e2e_chat" in ctx.allowed_chat_ids

        # 4. Idempotency: calling again should not create another chat
        mock_lark.reset_mock()
        service.handle(
            message_id="msg_e2e_2",
            chat_id="oc_main",
            sender_open_id="ou_tester",
            data=result.primary_data,
        )
        mock_lark.create_chat.assert_not_called()

    def test_legacy_project_gets_bound_chat(self, recognizer, project_manager, tmp_path):
        """Branch B: existing project without bound_chat gets one."""
        path = str(tmp_path / "legacy")
        os.makedirs(path)

        # Pre-create legacy project (no bound_chat_id)
        success, _, ctx = project_manager.create_project(
            project_id=None, project_name="legacy", root_path=path, chat_id="oc_old"
        )
        assert success
        # Ensure no bound_chat_id is set
        assert ctx.bound_chat_id == ""

        mock_lark = MagicMock()
        mock_lark.create_chat.return_value = CreateChatResult(
            chat_id="oc_new_for_legacy", name="legacy-dev"
        )

        service = ProjectChatService(
            project_manager=project_manager,
            lark_chat_client=mock_lark,
            reply_fn=MagicMock(),
            send_to_chat_fn=MagicMock(),
        )

        service.handle(
            message_id="msg_legacy",
            chat_id="oc_main",
            sender_open_id="ou_user",
            data={"name": "legacy", "path": path},
        )

        ctx = project_manager.find_project_by_path(path, chat_id=None)
        assert ctx is not None
        assert ctx.bound_chat_id == "oc_new_for_legacy"
        # project_name unchanged
        assert ctx.project_name == "legacy"
