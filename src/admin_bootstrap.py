"""One-time Bot admin bootstrap for /setadmin."""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import get_settings

audit_logger = logging.getLogger("ghostap.audit")

_RATE_LIMIT_SECONDS = 60


@dataclass(frozen=True)
class AdminBootstrapResult:
    success: bool
    code: str
    admin_id: str = ""
    target_id: str = ""


class AdminBootstrapService:
    """Persist ADMIN_USER_IDS with first-run bootstrap semantics.

    Contract:
    - when no admin exists, the sender becomes the sole admin;
    - once an admin exists, only an existing admin may replace the sole admin;
    - persistence goes to the local .env so restarts keep the same admin.
    """

    _global_lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock
    _last_attempt: dict[str, float] = {}

    def __init__(
        self,
        *,
        env_path: str | os.PathLike[str] = ".env",
        settings_getter: Callable = get_settings,
    ) -> None:
        self._env_path = Path(env_path)
        self._settings_getter = settings_getter

    def set_admin(
        self, sender_id: str, requested_target: str = "", chat_type: str = ""
    ) -> AdminBootstrapResult:
        sender_id = (sender_id or "").strip()
        requested_target = (requested_target or "").strip()
        if not sender_id:
            return AdminBootstrapResult(False, "missing_sender")

        # Rate limiting: 60s cooldown per sender
        now = time.time()
        last = self._last_attempt.get(sender_id, 0.0)
        if now - last < _RATE_LIMIT_SECONDS:
            return AdminBootstrapResult(False, "rate_limited", admin_id=sender_id)
        self._last_attempt[sender_id] = now

        with self._global_lock:
            settings = self._settings_getter()
            current_admins = self._normalize_admins(getattr(settings, "admin_user_ids", frozenset()))

            # First-time bootstrap requires p2p (private chat)
            if not current_admins and chat_type and chat_type != "p2p":
                return AdminBootstrapResult(False, "bootstrap_requires_p2p", admin_id=sender_id)

            if current_admins and sender_id not in current_admins:
                return AdminBootstrapResult(False, "not_admin", admin_id=sender_id)

            target_id = sender_id if not current_admins else (requested_target or sender_id)
            if not self._is_valid_admin_id(target_id):
                return AdminBootstrapResult(False, "invalid_target", admin_id=sender_id, target_id=target_id)

            self._write_admin_to_env(target_id)
            try:
                object.__setattr__(settings, "admin_user_ids", frozenset({target_id}))
            except Exception:
                pass

            result = AdminBootstrapResult(
                True,
                "bootstrap" if not current_admins else "updated",
                admin_id=sender_id,
                target_id=target_id,
            )
            audit_logger.info(
                "ADMIN_CHANGE: sender=%s target=%s code=%s",
                sender_id,
                target_id,
                result.code,
            )
            return result

    @staticmethod
    def _is_valid_admin_id(value: str) -> bool:
        return (
            bool(value)
            and "," not in value
            and "\n" not in value
            and "\r" not in value
            and not any(ch.isspace() for ch in value)
        )

    @staticmethod
    def _normalize_admins(value: object) -> frozenset[str]:
        if isinstance(value, str):
            return frozenset(part.strip() for part in value.split(",") if part.strip())
        try:
            return frozenset(str(part).strip() for part in (value or []) if str(part).strip())
        except TypeError:
            return frozenset()

    def _write_admin_to_env(self, admin_id: str) -> None:
        path = self._env_path
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        lines = existing.splitlines(keepends=True)
        replacement = f"ADMIN_USER_IDS={admin_id}\n"
        replaced = False
        new_lines: list[str] = []
        for line in lines:
            if self._is_admin_env_line(line):
                new_lines.append(replacement)
                replaced = True
            else:
                new_lines.append(line)
        if not replaced:
            if new_lines and not new_lines[-1].endswith(("\n", "\r")):
                new_lines[-1] = new_lines[-1] + "\n"
            new_lines.append(replacement)

        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text("".join(new_lines), encoding="utf-8")
        os.replace(tmp, path)
        os.chmod(path, 0o600)

    @staticmethod
    def _is_admin_env_line(line: str) -> bool:
        stripped = line.lstrip()
        return not stripped.startswith("#") and re.match(r"^(?:export\s+)?ADMIN_USER_IDS\s*=", stripped) is not None
