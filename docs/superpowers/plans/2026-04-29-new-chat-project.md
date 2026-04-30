# /new-chat Project-Chat Binding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `/new-chat` command that creates a project AND a dedicated Feishu group chat in one action, with 1:1:1 binding (project ↔ directory ↔ chat).

**Architecture:** New `src/project_chat/` module encapsulates all group-chat logic (create/delete/naming). Integration touches 4 points: `IntentType` enum, intent recognizer `/new-chat` parsing, dispatcher `_PROJECT_INTENTS` set, and `ProjectContext` data model fields. Handler delegates to `ProjectChatService` which orchestrates the flow with rollback on failure.

**Tech Stack:** Python 3.11+, `lark-oapi` (飞书 SDK `im.v1`), pydantic-settings, pytest

---

## File Structure

### New Files
| File | Responsibility |
|------|---------------|
| `src/project_chat/__init__.py` | Public exports: `ProjectChatService` |
| `src/project_chat/service.py` | Orchestrator: parse defaults, idempotency check, create chat, bind project, rollback |
| `src/project_chat/lark_chat_client.py` | Feishu Chat API wrapper: `create_chat`, `delete_chat`, `patch_description` |
| `src/project_chat/group_naming.py` | Name+suffix → group name; charset validation |
| `src/project_chat/cards.py` | Jump card / welcome card / error card builders |
| `src/project_chat/errors.py` | `ProjectChatError`, `CreateChatError`, `BindError` |
| `tests/test_project_chat/` | Test package |
| `tests/test_project_chat/__init__.py` | Empty |
| `tests/test_project_chat/test_service.py` | Service integration tests |
| `tests/test_project_chat/test_lark_chat_client.py` | Client unit tests |
| `tests/test_project_chat/test_group_naming.py` | Naming validation tests |
| `tests/test_project_chat/test_intent.py` | Intent recognizer extension tests |

### Modified Files
| File | Change |
|------|--------|
| `src/agent/intent_recognizer.py` | Add `NEW_CHAT_PROJECT` to `IntentType` enum + parsing branch |
| `src/feishu/dispatcher.py` | Add to `_PROJECT_INTENTS`, add case in `_dispatch_project` |
| `src/feishu/router.py` | Add `_handle_new_chat_project` to FORWARDING_MAP |
| `src/feishu/handlers/project.py` | Add `handle_new_chat_project` method |
| `src/project/context.py` | Add `bound_chat_id`, `bound_chat_name`, `bound_chat_created_at` fields + serialization |
| `src/config.py` | Add `project_chat_suffix: str = "dev"` |

---

## Task 1: Config + Data Model Fields

**Files:**
- Modify: `src/config.py`
- Modify: `src/project/context.py`
- Test: `tests/test_project_chat/test_intent.py` (created in Task 2)

- [ ] **Step 1: Add `project_chat_suffix` to Settings**

```python
# In src/config.py Settings class, after max_evicted_cache field (~line 567):
# ------------------------------------------------------------------
# Project Chat — /new-chat 群绑定配置
# ------------------------------------------------------------------
project_chat_suffix: str = "dev"  # /new-chat 默认群后缀
```

- [ ] **Step 2: Add bound_chat fields to ProjectContext**

In `src/project/context.py`, add after `worktree_state` field (line 123):

```python
# ── Bound chat (project-dedicated group) ──
bound_chat_id: str = ""              # 项目专属群 chat_id；空 = 没有专属群
bound_chat_name: str = ""            # 缓存群名，仅用于卡片展示
bound_chat_created_at: float = 0.0   # 建群时间戳
```

- [ ] **Step 3: Update `to_snapshot()` to serialize bound_chat fields**

In `to_snapshot()`, add after the `"allowed_chat_ids"` line:

```python
"bound_chat_id": self.bound_chat_id,
"bound_chat_name": self.bound_chat_name,
"bound_chat_created_at": self.bound_chat_created_at,
```

- [ ] **Step 4: Update `from_snapshot()` to deserialize bound_chat fields**

In `from_snapshot()`, add to the `cls(...)` call:

```python
# Inside the cls() kwargs, after allowed_chat_ids:
```

And after the existing `allowed_chat_ids` kwarg in `from_snapshot`:

```python
bound_chat_id=data.get("bound_chat_id", ""),
bound_chat_name=data.get("bound_chat_name", ""),
bound_chat_created_at=data.get("bound_chat_created_at", 0.0),
```

Note: `from_snapshot` currently does not pass `bound_chat_*` because those fields don't exist yet. We add them as keyword args with defaults.

- [ ] **Step 5: Run existing tests to verify no regression**

Run: `uv run python -m pytest tests/ -x -q --timeout=30 2>&1 | tail -5`
Expected: All existing tests pass (no regressions from adding fields with defaults).

- [ ] **Step 6: Commit**

```bash
git add src/config.py src/project/context.py
git commit -m "$(cat <<'EOF'
feat(project): add bound_chat fields to ProjectContext + project_chat_suffix config

Data model preparation for /new-chat command:
- ProjectContext: bound_chat_id, bound_chat_name, bound_chat_created_at
- Settings: project_chat_suffix (default "dev")
- to_snapshot/from_snapshot updated for serialization
EOF
)"
```

---

## Task 2: IntentType + Intent Recognizer

**Files:**
- Modify: `src/agent/intent_recognizer.py`
- Create: `tests/test_project_chat/__init__.py`
- Create: `tests/test_project_chat/test_intent.py`

- [ ] **Step 1: Write failing test for /new-chat intent parsing**

Create `tests/test_project_chat/__init__.py` (empty) and `tests/test_project_chat/test_intent.py`:

```python
"""Tests for /new-chat intent recognition."""
import pytest
from src.agent.intent_recognizer import IntentRecognizer, IntentType


@pytest.fixture
def recognizer():
    return IntentRecognizer()


class TestNewChatIntent:
    def test_bare_new_chat(self, recognizer):
        result = recognizer.recognize("/new-chat")
        assert result.primary_intent == IntentType.NEW_CHAT_PROJECT
        assert result.primary_data == {}

    def test_new_chat_with_name(self, recognizer):
        result = recognizer.recognize("/new-chat myproject")
        assert result.primary_intent == IntentType.NEW_CHAT_PROJECT
        assert result.primary_data == {"name": "myproject"}

    def test_new_chat_with_name_and_suffix(self, recognizer):
        result = recognizer.recognize("/new-chat myproject staging")
        assert result.primary_intent == IntentType.NEW_CHAT_PROJECT
        assert result.primary_data == {"name": "myproject", "suffix": "staging"}

    def test_new_chat_full_params(self, recognizer):
        result = recognizer.recognize("/new-chat myproject dev /home/user/code")
        assert result.primary_intent == IntentType.NEW_CHAT_PROJECT
        assert result.primary_data == {"name": "myproject", "suffix": "dev", "path": "/home/user/code"}

    def test_new_chat_no_false_match(self, recognizer):
        """'/new-chatbot' should NOT match /new-chat."""
        result = recognizer.recognize("/new-chatbot")
        assert result.primary_intent != IntentType.NEW_CHAT_PROJECT

    def test_new_chat_case_insensitive(self, recognizer):
        result = recognizer.recognize("/New-Chat myproject")
        assert result.primary_intent == IntentType.NEW_CHAT_PROJECT
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_project_chat/test_intent.py -v`
Expected: FAIL — `IntentType` has no `NEW_CHAT_PROJECT`

- [ ] **Step 3: Add IntentType.NEW_CHAT_PROJECT + parsing branch**

In `src/agent/intent_recognizer.py`:

a) Add to `IntentType` enum (after `PROJECT_STATUS = "project_status"`, line 37):
```python
NEW_CHAT_PROJECT = "new_chat_project"
```

b) Add to `INTENT_MAP` dict (after `"project_status"` entry, line 129):
```python
"new_chat_project": IntentType.NEW_CHAT_PROJECT,
```

c) In `_quick_match()`, add BEFORE the existing `/new ` check (before line 427):
```python
if text_lower == "/new-chat" or text_lower.startswith("/new-chat "):
    parts = text.split()
    data = {}
    if len(parts) >= 2:
        data["name"] = parts[1]
    if len(parts) >= 3:
        data["suffix"] = parts[2]
    if len(parts) >= 4:
        data["path"] = parts[3]
    return IntentResult.single(
        intent=IntentType.NEW_CHAT_PROJECT,
        confidence=1.0,
        data=data,
        original_text=text,
        reasoning="精确匹配: /new-chat 命令",
        description="创建项目专属群",
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_project_chat/test_intent.py -v`
Expected: All 6 tests PASS

- [ ] **Step 5: Run full test suite**

Run: `uv run python -m pytest tests/ -x -q --timeout=30 2>&1 | tail -5`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add src/agent/intent_recognizer.py tests/test_project_chat/
git commit -m "$(cat <<'EOF'
feat(intent): add NEW_CHAT_PROJECT intent type and /new-chat parsing

- IntentType.NEW_CHAT_PROJECT enum value
- Precise matching: "/new-chat" exact or "/new-chat " prefix
- Parses optional name, suffix, path params
- Tests cover bare/partial/full params + false-match guard
EOF
)"
```

---

## Task 3: Group Naming + Errors Module

**Files:**
- Create: `src/project_chat/__init__.py`
- Create: `src/project_chat/errors.py`
- Create: `src/project_chat/group_naming.py`
- Create: `tests/test_project_chat/test_group_naming.py`

- [ ] **Step 1: Write failing test for group_naming**

Create `tests/test_project_chat/test_group_naming.py`:

```python
"""Tests for group naming validation and formatting."""
import pytest
from src.project_chat.group_naming import format_group_name, validate_name_part


class TestFormatGroupName:
    def test_basic(self):
        assert format_group_name("myproject", "dev") == "myproject-dev"

    def test_strips_whitespace(self):
        assert format_group_name("  myproject  ", "  dev  ") == "myproject-dev"


class TestValidateNamePart:
    def test_valid_ascii(self):
        assert validate_name_part("myproject") is None

    def test_valid_chinese(self):
        assert validate_name_part("我的项目") is None

    def test_valid_with_dash_underscore(self):
        assert validate_name_part("my-project_1") is None

    def test_invalid_whitespace(self):
        err = validate_name_part("my project")
        assert err is not None
        assert "空格" in err or "whitespace" in err.lower()

    def test_invalid_empty(self):
        err = validate_name_part("")
        assert err is not None

    def test_invalid_too_long(self):
        err = validate_name_part("a" * 100)
        assert err is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_project_chat/test_group_naming.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement errors.py**

Create `src/project_chat/__init__.py`:

```python
"""Project-chat binding: /new-chat command support."""
```

Create `src/project_chat/errors.py`:

```python
"""Error types for project_chat module."""


class ProjectChatError(Exception):
    """Base error for project chat operations."""
    pass


class CreateChatError(ProjectChatError):
    """Failed to create Feishu group chat."""
    pass


class BindError(ProjectChatError):
    """Failed to bind project to chat."""
    pass
```

- [ ] **Step 4: Implement group_naming.py**

Create `src/project_chat/group_naming.py`:

```python
"""Group naming: format and validate name/suffix for Feishu group chat."""

import re
from typing import Optional

# Max length for each part (name or suffix)
_MAX_PART_LENGTH = 50

# Allowed characters: word chars (unicode), dash, dot
_VALID_PART_RE = re.compile(r"^[\w\-.]+$", re.UNICODE)


def format_group_name(name: str, suffix: str) -> str:
    """Format group name as '{name}-{suffix}'."""
    return f"{name.strip()}-{suffix.strip()}"


def validate_name_part(part: str) -> Optional[str]:
    """Validate a name or suffix part.

    Returns None if valid, or an error message string if invalid.
    """
    part = part.strip()
    if not part:
        return "名称不能为空"
    if len(part) > _MAX_PART_LENGTH:
        return f"名称过长（最大 {_MAX_PART_LENGTH} 字符）"
    if not _VALID_PART_RE.match(part):
        return "名称包含非法字符（不能包含空格或特殊符号，允许字母/数字/中文/下划线/短横/点）"
    return None
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_project_chat/test_group_naming.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/project_chat/ tests/test_project_chat/test_group_naming.py
git commit -m "$(cat <<'EOF'
feat(project_chat): add group_naming + errors modules

- group_naming: format_group_name, validate_name_part with charset validation
- errors: ProjectChatError, CreateChatError, BindError hierarchy
EOF
)"
```

---

## Task 4: LarkChatClient

**Files:**
- Create: `src/project_chat/lark_chat_client.py`
- Create: `tests/test_project_chat/test_lark_chat_client.py`

- [ ] **Step 1: Write failing test for LarkChatClient**

Create `tests/test_project_chat/test_lark_chat_client.py`:

```python
"""Tests for LarkChatClient — Feishu chat API wrapper."""
import pytest
from unittest.mock import MagicMock, patch, PropertyMock

from src.project_chat.lark_chat_client import LarkChatClient, CreateChatResult
from src.project_chat.errors import CreateChatError


@pytest.fixture
def mock_api_client():
    client = MagicMock()
    return client


@pytest.fixture
def chat_client(mock_api_client):
    return LarkChatClient(api_client_factory=lambda: mock_api_client)


class TestCreateChat:
    def test_create_chat_success(self, chat_client, mock_api_client):
        # Mock successful response
        response = MagicMock()
        response.success.return_value = True
        response.data = MagicMock()
        response.data.chat_id = "oc_test_chat_123"
        response.data.name = "myproject-dev"
        mock_api_client.im.v1.chat.create.return_value = response

        result = chat_client.create_chat(
            name="myproject-dev",
            description="test desc",
            user_id_list=["ou_user_1"],
        )

        assert isinstance(result, CreateChatResult)
        assert result.chat_id == "oc_test_chat_123"
        assert result.name == "myproject-dev"

    def test_create_chat_failure_raises(self, chat_client, mock_api_client):
        response = MagicMock()
        response.success.return_value = False
        response.code = 230001
        response.msg = "permission denied"
        mock_api_client.im.v1.chat.create.return_value = response

        with pytest.raises(CreateChatError, match="permission denied"):
            chat_client.create_chat(
                name="myproject-dev",
                description="test desc",
                user_id_list=["ou_user_1"],
            )


class TestDeleteChat:
    def test_delete_chat_success(self, chat_client, mock_api_client):
        response = MagicMock()
        response.success.return_value = True
        mock_api_client.im.v1.chat.delete.return_value = response

        # Should not raise
        chat_client.delete_chat("oc_test_chat_123")

    def test_delete_chat_failure_logs_warning(self, chat_client, mock_api_client):
        response = MagicMock()
        response.success.return_value = False
        response.code = 230099
        response.msg = "chat not found"
        mock_api_client.im.v1.chat.delete.return_value = response

        # delete_chat is best-effort for rollback, should not raise
        chat_client.delete_chat("oc_test_chat_123")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_project_chat/test_lark_chat_client.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement LarkChatClient**

Create `src/project_chat/lark_chat_client.py`:

```python
"""Feishu Chat API wrapper for project-chat binding."""

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .errors import CreateChatError

logger = logging.getLogger(__name__)


@dataclass
class CreateChatResult:
    """Result of creating a Feishu group chat."""
    chat_id: str
    name: str


class LarkChatClient:
    """Wraps Feishu IM v1 Chat API with retry and error handling.

    Follows the same retry/backoff pattern as FeishuIMClient._execute_with_retry.
    """

    def __init__(self, api_client_factory: Callable[[], Any], max_retries: int = 3):
        self._api_client_factory = api_client_factory
        self._max_retries = max_retries

    def _execute_with_retry(self, func: Callable[[], Any], action_name: str) -> Any:
        """Execute API call with exponential backoff retry."""
        last_error = None
        for attempt in range(self._max_retries):
            try:
                response = func()
                if response.success():
                    return response
                last_error = f"[{response.code}] {response.msg}"
                # Non-retryable error codes
                if response.code in (230001, 230020):
                    break
            except Exception as e:
                last_error = str(e)
            if attempt < self._max_retries - 1:
                time.sleep(0.3 * (2 ** attempt))
        return None, last_error

    def create_chat(
        self,
        *,
        name: str,
        description: str,
        user_id_list: list[str],
    ) -> CreateChatResult:
        """Create a Feishu group chat. Bot is auto-added as creator.

        Raises CreateChatError on failure.
        """
        from lark_oapi.api.im.v1 import CreateChatRequest, CreateChatRequestBody

        client = self._api_client_factory()
        body = CreateChatRequestBody.builder() \
            .name(name) \
            .description(description) \
            .user_id_list(user_id_list) \
            .chat_mode("group") \
            .chat_type("private") \
            .build()
        request = CreateChatRequest.builder() \
            .user_id_type("open_id") \
            .request_body(body) \
            .build()

        last_error = None
        for attempt in range(self._max_retries):
            try:
                response = client.im.v1.chat.create(request)
                if response.success():
                    return CreateChatResult(
                        chat_id=response.data.chat_id,
                        name=name,
                    )
                last_error = f"[{response.code}] {response.msg}"
                if response.code in (230001, 230020, 99991672):
                    break
            except Exception as e:
                last_error = str(e)
            if attempt < self._max_retries - 1:
                time.sleep(0.3 * (2 ** attempt))

        raise CreateChatError(f"建群失败: {last_error}")

    def delete_chat(self, chat_id: str) -> None:
        """Delete a Feishu group chat (best-effort, for rollback).

        Does NOT raise on failure — only logs warning.
        """
        from lark_oapi.api.im.v1 import DeleteChatRequest

        client = self._api_client_factory()
        request = DeleteChatRequest.builder().chat_id(chat_id).build()

        try:
            response = client.im.v1.chat.delete(request)
            if not response.success():
                logger.warning(
                    "delete_chat(%s) failed: [%s] %s",
                    chat_id[:12], response.code, response.msg,
                )
        except Exception as e:
            logger.warning("delete_chat(%s) exception: %s", chat_id[:12], e)

    def patch_description(self, chat_id: str, description: str) -> None:
        """Update group chat description (best-effort)."""
        from lark_oapi.api.im.v1 import UpdateChatRequest, UpdateChatRequestBody

        client = self._api_client_factory()
        body = UpdateChatRequestBody.builder().description(description).build()
        request = UpdateChatRequest.builder() \
            .chat_id(chat_id) \
            .request_body(body) \
            .build()

        try:
            response = client.im.v1.chat.update(request)
            if not response.success():
                logger.warning(
                    "patch_description(%s) failed: [%s] %s",
                    chat_id[:12], response.code, response.msg,
                )
        except Exception as e:
            logger.warning("patch_description(%s) exception: %s", chat_id[:12], e)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_project_chat/test_lark_chat_client.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/project_chat/lark_chat_client.py tests/test_project_chat/test_lark_chat_client.py
git commit -m "$(cat <<'EOF'
feat(project_chat): add LarkChatClient with create/delete/patch_description

- Retry + backoff pattern matching FeishuIMClient conventions
- create_chat raises CreateChatError on failure
- delete_chat is best-effort (rollback use)
EOF
)"
```

---

## Task 5: ProjectChatService (Core Orchestrator)

**Files:**
- Create: `src/project_chat/service.py`
- Create: `tests/test_project_chat/test_service.py`
- Modify: `src/project_chat/__init__.py`

- [ ] **Step 1: Write failing test for service**

Create `tests/test_project_chat/test_service.py`:

```python
"""Tests for ProjectChatService — the /new-chat orchestrator."""
import os
import tempfile
import pytest
from unittest.mock import MagicMock, patch

from src.project_chat.service import ProjectChatService
from src.project_chat.lark_chat_client import CreateChatResult
from src.project_chat.errors import CreateChatError
from src.project.manager import ProjectManager


@pytest.fixture
def tmp_storage(tmp_path):
    return str(tmp_path / "projects.json")


@pytest.fixture
def project_manager(tmp_storage):
    return ProjectManager(storage_path=tmp_storage)


@pytest.fixture
def mock_lark_client():
    client = MagicMock()
    client.create_chat.return_value = CreateChatResult(
        chat_id="oc_new_group_123",
        name="testproj-dev",
    )
    return client


@pytest.fixture
def mock_reply_fn():
    return MagicMock()


@pytest.fixture
def service(project_manager, mock_lark_client, mock_reply_fn):
    return ProjectChatService(
        project_manager=project_manager,
        lark_chat_client=mock_lark_client,
        reply_fn=mock_reply_fn,
        send_to_chat_fn=mock_reply_fn,
    )


class TestNewProject:
    """Branch C: no existing project → create chat + create project."""

    def test_creates_project_and_chat(self, service, project_manager, mock_lark_client, tmp_path):
        path = str(tmp_path / "mycode")
        os.makedirs(path)

        service.handle(
            message_id="msg_1",
            chat_id="oc_main_chat",
            sender_open_id="ou_user_1",
            data={"name": "mycode", "path": path},
        )

        # Verify chat was created
        mock_lark_client.create_chat.assert_called_once()
        call_kwargs = mock_lark_client.create_chat.call_args[1]
        assert "mycode" in call_kwargs["name"]
        assert "ou_user_1" in call_kwargs["user_id_list"]

        # Verify project was created with bound_chat_id
        ctx = project_manager.find_project_by_path(path)
        assert ctx is not None
        assert ctx.bound_chat_id == "oc_new_group_123"
        assert ctx.bound_chat_name == "testproj-dev"
        assert ctx.owner_chat_id == "oc_new_group_123"

    def test_idempotent_returns_existing(self, service, project_manager, mock_lark_client, tmp_path):
        """Branch A: project exists with bound_chat → no API call, return jump card."""
        path = str(tmp_path / "existing")
        os.makedirs(path)

        # Pre-create project with bound chat
        success, _, ctx = project_manager.create_project(
            project_id=None, project_name="existing", root_path=path, chat_id="oc_bound"
        )
        assert success
        ctx.bound_chat_id = "oc_bound"
        ctx.bound_chat_name = "existing-dev"
        project_manager._save_projects()

        service.handle(
            message_id="msg_2",
            chat_id="oc_main_chat",
            sender_open_id="ou_user_1",
            data={"name": "existing", "path": path},
        )

        # Should NOT create a new chat
        mock_lark_client.create_chat.assert_not_called()


class TestRollback:
    """Verify rollback on failure after chat creation."""

    def test_rollback_on_project_create_failure(self, service, project_manager, mock_lark_client, tmp_path):
        """If create_project fails, delete_chat should be called."""
        path = "/nonexistent/impossible/path/that/will/fail_create"

        # ProjectManager.create_project will fail for this path (can't mkdir)
        # Actually, let's mock it to fail
        with patch.object(project_manager, "create_project", return_value=(False, "disk error", None)):
            service.handle(
                message_id="msg_3",
                chat_id="oc_main",
                sender_open_id="ou_user_1",
                data={"name": "broken", "path": path},
            )

        mock_lark_client.delete_chat.assert_called_once_with("oc_new_group_123")

    def test_no_rollback_on_chat_create_failure(self, service, mock_lark_client, tmp_path):
        """If create_chat fails, no rollback needed."""
        path = str(tmp_path / "newdir")
        os.makedirs(path)
        mock_lark_client.create_chat.side_effect = CreateChatError("API error")

        service.handle(
            message_id="msg_4",
            chat_id="oc_main",
            sender_open_id="ou_user_1",
            data={"name": "proj", "path": path},
        )

        mock_lark_client.delete_chat.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_project_chat/test_service.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement ProjectChatService**

Create `src/project_chat/service.py`:

```python
"""ProjectChatService — orchestrator for /new-chat command."""

import logging
import os
import subprocess
import threading
import time
from typing import Any, Callable, Optional

from ..config import get_settings
from ..project.context import ProjectContext
from ..project.manager import ProjectManager
from .errors import BindError, CreateChatError, ProjectChatError
from .group_naming import format_group_name, validate_name_part
from .lark_chat_client import LarkChatClient

logger = logging.getLogger(__name__)

# Per-(chat_id, path) lock to prevent concurrent /new-chat races
_creation_locks: dict[str, threading.Lock] = {}
_creation_locks_guard = threading.Lock()


def _get_creation_lock(chat_id: str, path: str) -> threading.Lock:
    key = f"{chat_id}:{os.path.normpath(path)}"
    with _creation_locks_guard:
        if key not in _creation_locks:
            _creation_locks[key] = threading.Lock()
        return _creation_locks[key]


class ProjectChatService:
    """Orchestrates /new-chat: parse → idempotency check → create chat → bind project."""

    def __init__(
        self,
        project_manager: ProjectManager,
        lark_chat_client: LarkChatClient,
        reply_fn: Callable[[str, str, Optional[str]], Any],
        send_to_chat_fn: Callable[[str, str, str, Optional[str]], Any],
    ):
        self._pm = project_manager
        self._lark = lark_chat_client
        self._reply = reply_fn
        self._send_to_chat = send_to_chat_fn

    def handle(
        self,
        message_id: str,
        chat_id: str,
        sender_open_id: str,
        data: dict,
    ) -> None:
        """Main entry point for /new-chat command."""
        settings = get_settings()

        # 1. Parse defaults
        working_dir = self._pm._get_working_dir_for_chat(chat_id) if hasattr(self._pm, "_get_working_dir_for_chat") else os.getcwd()
        path = data.get("path") or working_dir
        path = os.path.expanduser(os.path.abspath(path))
        name = data.get("name") or os.path.basename(os.path.normpath(path)) or f"project_{int(time.time())}"
        suffix = data.get("suffix") or settings.project_chat_suffix

        # Validate name/suffix
        err = validate_name_part(name)
        if err:
            self._reply(message_id, f"❌ 项目名无效: {err}", None)
            return
        err = validate_name_part(suffix)
        if err:
            self._reply(message_id, f"❌ 后缀无效: {err}", None)
            return

        # 2. Acquire per-(chat, path) lock
        lock = _get_creation_lock(chat_id, path)
        if not lock.acquire(timeout=5):
            self._reply(message_id, "⏳ 正在处理中，请稍后再试", None)
            return

        try:
            self._handle_locked(message_id, chat_id, sender_open_id, name, suffix, path)
        finally:
            lock.release()

    def _handle_locked(
        self,
        message_id: str,
        chat_id: str,
        sender_open_id: str,
        name: str,
        suffix: str,
        path: str,
    ) -> None:
        # 3. Idempotency check — chat_id=None to skip visibility filter
        ctx = self._pm.find_project_by_path(path, chat_id=None)

        if ctx and ctx.bound_chat_id:
            # Branch A: already bound → return jump card
            self._reply_jump_card(message_id, ctx)
            return

        group_name = format_group_name(name, suffix)
        description = self._build_description(name, path)

        # 4. Create chat
        try:
            result = self._lark.create_chat(
                name=group_name,
                description=description,
                user_id_list=[sender_open_id],
            )
        except CreateChatError as e:
            logger.warning("create_chat failed for path=%s: %s", path, e)
            self._reply(message_id, f"❌ 建群失败: {e}", None)
            return

        new_chat_id = result.chat_id
        new_chat_name = result.name

        # 5. Bind
        try:
            if ctx:
                # Branch B: legacy project without bound chat
                ctx.bound_chat_id = new_chat_id
                ctx.bound_chat_name = new_chat_name
                ctx.bound_chat_created_at = time.time()
                ctx.add_chat_id(new_chat_id)
                self._pm._save_projects()
            else:
                # Branch C: new project
                success, msg, ctx_new = self._pm.create_project(
                    project_id=None,
                    project_name=name,
                    root_path=path,
                    chat_id=new_chat_id,
                )
                if not success or not ctx_new:
                    # Rollback: delete the created chat
                    self._lark.delete_chat(new_chat_id)
                    self._reply(message_id, f"❌ 创建项目失败: {msg}", None)
                    return
                ctx_new.bound_chat_id = new_chat_id
                ctx_new.bound_chat_name = new_chat_name
                ctx_new.bound_chat_created_at = time.time()
                self._pm._save_projects()
                ctx = ctx_new
        except Exception as e:
            # Rollback chat on any bind failure
            logger.error("bind failed, rolling back chat %s: %s", new_chat_id[:12], e)
            self._lark.delete_chat(new_chat_id)
            self._reply(message_id, f"❌ 绑定失败: {e}", None)
            return

        # 6. Reply in main chat + welcome in new chat
        self._reply_jump_card(message_id, ctx)
        self._send_welcome(new_chat_id, ctx)

    def _build_description(self, name: str, path: str) -> str:
        git_remote = self._detect_git_remote(path)
        lines = [
            f"🎯 项目: {name}",
            f"📁 目录: {path}",
        ]
        if git_remote:
            lines.append(f"🔗 仓库: {git_remote}")
        lines.append("🤖 在这个群直接对话即可：默认 Coco / 显式 /claude /codex 等。")
        return "\n".join(lines)

    @staticmethod
    def _detect_git_remote(path: str) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", path, "remote", "get-url", "origin"],
                capture_output=True, text=True, timeout=3,
            )
            return result.stdout.strip() if result.returncode == 0 else ""
        except Exception:
            return ""

    def _reply_jump_card(self, message_id: str, ctx: ProjectContext) -> None:
        """Reply with a jump card pointing to the bound chat."""
        text = (
            f"✅ 项目 **{ctx.project_name}** 已就绪\n"
            f"📂 目录: `{ctx.root_path}`\n"
            f"💬 群: **{ctx.bound_chat_name}**\n\n"
            f"在飞书侧边栏搜索「{ctx.bound_chat_name}」进入专属群开始编程"
        )
        self._reply(message_id, text, None)

    def _send_welcome(self, chat_id: str, ctx: ProjectContext) -> None:
        """Send welcome message in the newly created group."""
        text = (
            f"🎉 项目 **{ctx.project_name}** 专属群已就绪\n"
            f"📂 目录: `{ctx.root_path}`\n\n"
            f"直接在这里对话即可开始编程：\n"
            f"• 直接发消息 → 默认 Coco\n"
            f"• `/claude` → Claude 模式\n"
            f"• `/codex` → Codex 模式\n"
            f"• `/deep <需求>` → Deep 深度执行"
        )
        try:
            self._send_to_chat(chat_id, "text", text, None)
        except Exception as e:
            logger.warning("send_welcome to %s failed: %s", chat_id[:12], e)
```

- [ ] **Step 4: Update `__init__.py` exports**

Update `src/project_chat/__init__.py`:

```python
"""Project-chat binding: /new-chat command support."""

from .service import ProjectChatService

__all__ = ["ProjectChatService"]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_project_chat/test_service.py -v`
Expected: All PASS (may need minor adjustments based on ProjectManager's actual API)

- [ ] **Step 6: Run full test suite**

Run: `uv run python -m pytest tests/ -x -q --timeout=30 2>&1 | tail -5`
Expected: All pass

- [ ] **Step 7: Commit**

```bash
git add src/project_chat/service.py src/project_chat/__init__.py tests/test_project_chat/test_service.py
git commit -m "$(cat <<'EOF'
feat(project_chat): implement ProjectChatService orchestrator

Core flow: parse defaults → idempotency check → create chat → bind →
reply jump card + welcome card. Includes rollback on failure, per-(chat,path)
race-condition lock, git remote detection for description.
EOF
)"
```

---

## Task 6: Dispatcher + Router Integration

**Files:**
- Modify: `src/feishu/dispatcher.py`
- Modify: `src/feishu/router.py`
- Modify: `src/feishu/handlers/project.py`

- [ ] **Step 1: Add `NEW_CHAT_PROJECT` to dispatcher**

In `src/feishu/dispatcher.py`:

a) Add import (if not already imported):
```python
# IntentType is already imported via existing code
```

b) Add `IntentType.NEW_CHAT_PROJECT` to `_PROJECT_INTENTS` set (line ~233):
```python
_PROJECT_INTENTS: set = {
    IntentType.CREATE_PROJECT, IntentType.SWITCH_PROJECT,
    IntentType.LIST_PROJECTS, IntentType.CLOSE_PROJECT,
    IntentType.PROJECT_STATUS, IntentType.NEW_CHAT_PROJECT,
}
```

c) Add case in `_dispatch_project` method (after `PROJECT_STATUS` case, line ~396):
```python
elif intent == IntentType.NEW_CHAT_PROJECT:
    self.client._handle_new_chat_project(message_id, chat_id, data)
```

- [ ] **Step 2: Add handler method to ProjectHandler**

In `src/feishu/handlers/project.py`, add method:

```python
def handle_new_chat_project(self, message_id: str, chat_id: str, data: dict) -> None:
    """Handle /new-chat command: create project + dedicated Feishu group."""
    from ...thread import get_current_sender_id
    from ...project_chat import ProjectChatService

    sender_open_id = get_current_sender_id() or ""
    if not sender_open_id:
        self.reply_error(message_id, "❌ 无法获取发送者信息")
        return

    # Lazy-init service
    if not hasattr(self, "_project_chat_service"):
        from ...project_chat.lark_chat_client import LarkChatClient
        lark_client = LarkChatClient(api_client_factory=self.ctx.api_client_factory)
        self._project_chat_service = ProjectChatService(
            project_manager=self.project_manager,
            lark_chat_client=lark_client,
            reply_fn=lambda mid, text, _: self.reply_text(mid, text),
            send_to_chat_fn=lambda cid, msg_type, text, _: self.im_client.send_message(cid, msg_type, text),
        )

    self._project_chat_service.handle(message_id, chat_id, sender_open_id, data)
```

- [ ] **Step 3: Add to FORWARDING_MAP in router.py**

In `src/feishu/router.py`, add after `"_close_project"` entry (line ~144):
```python
"_handle_new_chat_project": ("project", "handle_new_chat_project"),
```

- [ ] **Step 4: Run full test suite**

Run: `uv run python -m pytest tests/ -x -q --timeout=30 2>&1 | tail -5`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add src/feishu/dispatcher.py src/feishu/router.py src/feishu/handlers/project.py
git commit -m "$(cat <<'EOF'
feat(feishu): integrate /new-chat into dispatcher + router + handler

- _PROJECT_INTENTS: add NEW_CHAT_PROJECT
- _dispatch_project: route to handler method
- ProjectHandler.handle_new_chat_project: lazy-init service, get sender via thread-local
- FORWARDING_MAP: register _handle_new_chat_project
EOF
)"
```

---

## Task 7: End-to-End Integration Test

**Files:**
- Create: `tests/test_project_chat/test_e2e.py`

- [ ] **Step 1: Write E2E test covering full flow**

Create `tests/test_project_chat/test_e2e.py`:

```python
"""End-to-end test: /new-chat from intent recognition through to project creation."""
import os
import pytest
from unittest.mock import MagicMock, patch

from src.agent.intent_recognizer import IntentRecognizer, IntentType
from src.project.manager import ProjectManager
from src.project_chat.service import ProjectChatService
from src.project_chat.lark_chat_client import CreateChatResult
from src.project_chat.errors import CreateChatError


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
        ctx = project_manager.find_project_by_path(path)
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
        project_manager.create_project(None, "legacy", path, chat_id="oc_old")

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

        ctx = project_manager.find_project_by_path(path)
        assert ctx is not None
        assert ctx.bound_chat_id == "oc_new_for_legacy"
        # project_id unchanged
        assert ctx.project_name == "legacy"
```

- [ ] **Step 2: Run E2E test**

Run: `uv run python -m pytest tests/test_project_chat/test_e2e.py -v`
Expected: All PASS

- [ ] **Step 3: Run full test suite**

Run: `uv run python -m pytest tests/ -x -q --timeout=30 2>&1 | tail -5`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add tests/test_project_chat/test_e2e.py
git commit -m "$(cat <<'EOF'
test(project_chat): add E2E integration tests for /new-chat flow

Covers: full new project, idempotency, legacy project binding.
EOF
)"
```

---

## Summary

| Task | Description | Est. Complexity |
|------|-------------|-----------------|
| 1 | Config + Data Model | Low |
| 2 | IntentType + Parser | Low |
| 3 | GroupNaming + Errors | Low |
| 4 | LarkChatClient | Medium |
| 5 | ProjectChatService | Medium-High |
| 6 | Dispatcher Integration | Low |
| 7 | E2E Test | Low |

Tasks 1-3 are independent and can be parallelized. Tasks 4-5 depend on Task 3. Task 6 depends on Tasks 2 and 5. Task 7 depends on all previous tasks.
