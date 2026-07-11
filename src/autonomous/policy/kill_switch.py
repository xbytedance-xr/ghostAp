from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class KillState:
    active: bool = False
    kill_epoch: int = 0
    scope: str = "global"
    activated_at: Optional[float] = None
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "active": self.active,
            "kill_epoch": self.kill_epoch,
            "scope": self.scope,
            "activated_at": self.activated_at,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict) -> KillState:
        return cls(
            active=data.get("active", False),
            kill_epoch=data.get("kill_epoch", 0),
            scope=data.get("scope", "global"),
            activated_at=data.get("activated_at"),
            reason=data.get("reason", ""),
        )


class KillSwitch:
    def __init__(self, state_dir: str):
        self._state_dir = state_dir
        self._file_path = os.path.join(state_dir, "kill.switch")
        self._states: dict[str, KillState] = {}
        self._global_epoch: int = 0
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self._file_path):
            return
        with open(self._file_path, "r") as f:
            data = json.load(f)
        self._global_epoch = data.get("global_epoch", 0)
        for item in data.get("states", []):
            ks = KillState.from_dict(item)
            self._states[ks.scope] = ks

    def _persist(self) -> None:
        os.makedirs(self._state_dir, exist_ok=True)
        payload = {
            "global_epoch": self._global_epoch,
            "states": [s.to_dict() for s in self._states.values()],
        }
        fd, tmp_path = tempfile.mkstemp(dir=self._state_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._file_path)
        except BaseException:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    def activate(self, scope: str = "global", reason: str = "") -> int:
        self._global_epoch += 1
        self._states[scope] = KillState(
            active=True,
            kill_epoch=self._global_epoch,
            scope=scope,
            activated_at=time.time(),
            reason=reason,
        )
        self._persist()
        return self._global_epoch

    def deactivate(self, scope: str = "global") -> None:
        if scope in self._states:
            self._states[scope].active = False
            self._persist()

    def is_killed(self, scope: str = "global") -> bool:
        state = self._states.get(scope)
        if state and state.active:
            return True
        if scope != "global":
            global_state = self._states.get("global")
            if global_state and global_state.active:
                return True
        return False

    def get_epoch(self) -> int:
        return self._global_epoch

    def check_gate(self, scope: str, current_epoch: int) -> bool:
        if self.is_killed(scope):
            return False
        return current_epoch >= self._global_epoch

    def load_state(self) -> list[KillState]:
        self._load()
        return list(self._states.values())
