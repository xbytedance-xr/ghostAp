"""Recoverable /hire provisioning saga for employee Bot creation.

Uses lark-oapi one-click app creation SDK to generate a temporary
creation link, then processes the callback with app_id/app_secret.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class ProvisioningError(RuntimeError):
    """Base class for provisioning failures."""


class SagaStateError(ProvisioningError):
    """Saga is in an invalid state for the requested transition."""


class ProvisioningTimeoutError(ProvisioningError):
    """The creation link expired before user confirmed."""


class SagaPhase(str, Enum):
    INITIATED = "initiated"
    LINK_GENERATED = "link_generated"
    USER_CONFIRMED = "user_confirmed"
    CREDENTIALS_RECEIVED = "credentials_received"
    VAULT_STORED = "vault_stored"
    CHANNEL_CONNECTED = "channel_connected"
    SLASH_REGISTERED = "slash_registered"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class HireIntent:
    """Immutable intent for one hire attempt."""

    intent_id: str
    employee_name: str
    tool: str
    model: str
    effort: str
    tenant_key: str
    owner_principal_id: str
    chat_id: str
    created_at: float = field(default_factory=time.time)

    @property
    def attempt_key(self) -> str:
        return hashlib.sha256(
            f"{self.intent_id}|{self.tenant_key}|{self.employee_name}".encode()
        ).hexdigest()[:16]


@dataclass
class SagaState:
    """Mutable saga progress for one /hire attempt."""

    intent: HireIntent
    phase: SagaPhase = SagaPhase.INITIATED
    creation_link: str = ""
    link_expires_at: float = 0.0
    app_id: str = ""
    credential_ref: str = ""
    agent_id: str = ""
    error: str = ""
    attempts: int = 0
    last_updated: float = field(default_factory=time.time)

    @property
    def is_terminal(self) -> bool:
        return self.phase in (SagaPhase.COMPLETED, SagaPhase.FAILED, SagaPhase.CANCELLED)

    @property
    def is_link_expired(self) -> bool:
        return self.phase == SagaPhase.LINK_GENERATED and time.time() > self.link_expires_at


class AppCreationPort(Protocol):
    """Port for lark-oapi one-click app creation SDK."""

    def generate_creation_link(
        self,
        *,
        app_name: str,
        description: str,
        scopes: list[str],
        event_subscriptions: list[str],
    ) -> tuple[str, float]:
        """Returns (creation_link_url, expires_at_timestamp)."""
        ...

    def validate_callback(
        self,
        callback_payload: dict[str, Any],
    ) -> tuple[str, str]:
        """Validate and extract (app_id, app_secret) from creation callback."""
        ...


class CredentialVaultPort(Protocol):
    """Port for storing credentials securely."""

    def store(
        self,
        *,
        agent_id: str,
        app_id: str,
        app_secret: str,
        hire_intent_id: str,
        attempt_id: str,
    ) -> str:
        """Store credentials, return credential_ref."""
        ...


class JournalPort(Protocol):
    """Port for recording saga events."""

    def record_hire_event(
        self,
        *,
        intent_id: str,
        phase: str,
        payload: dict[str, Any],
    ) -> None: ...


_DEFAULT_SCOPES = [
    "im:message",
    "im:message.group_at_msg",
    "im:message.p2p_msg",
    "im:chat",
    "im:chat:readonly",
]

_DEFAULT_EVENTS = [
    "im.message.receive_v1",
    "im.chat.member.bot.added_v1",
    "im.chat.member.bot.deleted_v1",
]

LINK_TTL_SECONDS = 600


class HireSaga:
    """Recoverable saga for /hire employee bot provisioning."""

    def __init__(
        self,
        *,
        app_creation: AppCreationPort,
        vault: CredentialVaultPort,
        journal: JournalPort,
    ) -> None:
        self._app_creation = app_creation
        self._vault = vault
        self._journal = journal
        self._active_sagas: dict[str, SagaState] = {}
        self._pending_names: dict[str, str] = {}

    def initiate(self, intent: HireIntent) -> SagaState:
        """Start a new hire saga. Generates creation link."""
        if intent.intent_id in self._active_sagas:
            existing = self._active_sagas[intent.intent_id]
            if not existing.is_terminal:
                return existing
        name_key = f"{intent.tenant_key}|{intent.employee_name.lower()}"
        existing_intent = self._pending_names.get(name_key)
        if existing_intent and existing_intent != intent.intent_id:
            existing_saga = self._active_sagas.get(existing_intent)
            if existing_saga and not existing_saga.is_terminal:
                state = SagaState(intent=intent)
                state.phase = SagaPhase.FAILED
                state.error = "hire already in progress for this name"
                return state
        self._pending_names[name_key] = intent.intent_id
        state = SagaState(intent=intent)
        state.attempts += 1
        try:
            link, expires = self._app_creation.generate_creation_link(
                app_name=intent.employee_name,
                description=f"GhostAP Employee Bot: {intent.employee_name}",
                scopes=_DEFAULT_SCOPES,
                event_subscriptions=_DEFAULT_EVENTS,
            )
            state.creation_link = link
            state.link_expires_at = expires
            state.phase = SagaPhase.LINK_GENERATED
            state.last_updated = time.time()
        except Exception as exc:
            state.phase = SagaPhase.FAILED
            state.error = str(exc)[:500]
        self._active_sagas[intent.intent_id] = state
        self._journal.record_hire_event(
            intent_id=intent.intent_id,
            phase=state.phase.value,
            payload={"link": state.creation_link[:50], "attempts": state.attempts},
        )
        return state

    def on_creation_callback(
        self,
        intent_id: str,
        callback_payload: dict[str, Any],
    ) -> SagaState:
        """Process the SDK callback after user confirms app creation."""
        state = self._active_sagas.get(intent_id)
        if state is None:
            raise SagaStateError(f"no active saga: {intent_id}")
        if state.is_terminal:
            raise SagaStateError(f"saga already terminal: {intent_id}")
        if state.is_link_expired:
            state.phase = SagaPhase.FAILED
            state.error = "creation link expired"
            self._journal.record_hire_event(
                intent_id=intent_id,
                phase="failed",
                payload={"reason": "link_expired"},
            )
            return state
        try:
            app_id, app_secret = self._app_creation.validate_callback(callback_payload)
            state.app_id = app_id
            state.phase = SagaPhase.CREDENTIALS_RECEIVED
            state.last_updated = time.time()
            credential_ref = self._vault.store(
                agent_id=state.agent_id or f"agt_{state.intent.attempt_key}",
                app_id=app_id,
                app_secret=app_secret,
                hire_intent_id=intent_id,
                attempt_id=str(state.attempts),
            )
            state.credential_ref = credential_ref
            state.agent_id = state.agent_id or f"agt_{state.intent.attempt_key}"
            state.phase = SagaPhase.VAULT_STORED
            state.last_updated = time.time()
        except Exception as exc:
            state.phase = SagaPhase.FAILED
            state.error = str(exc)[:500]
        self._journal.record_hire_event(
            intent_id=intent_id,
            phase=state.phase.value,
            payload={"app_id": state.app_id, "agent_id": state.agent_id},
        )
        return state

    def mark_channel_connected(self, intent_id: str) -> SagaState:
        """Mark that Channel SDK connection succeeded."""
        state = self._require_state(intent_id, SagaPhase.VAULT_STORED)
        state.phase = SagaPhase.CHANNEL_CONNECTED
        state.last_updated = time.time()
        self._journal.record_hire_event(
            intent_id=intent_id, phase="channel_connected", payload={}
        )
        return state

    def mark_slash_registered(self, intent_id: str) -> SagaState:
        """Mark that Slash Commands registered successfully."""
        state = self._require_state(intent_id, SagaPhase.CHANNEL_CONNECTED)
        state.phase = SagaPhase.SLASH_REGISTERED
        state.last_updated = time.time()
        self._journal.record_hire_event(
            intent_id=intent_id, phase="slash_registered", payload={}
        )
        return state

    def complete(self, intent_id: str) -> SagaState:
        """Mark saga as fully completed."""
        state = self._require_state(intent_id, SagaPhase.SLASH_REGISTERED)
        state.phase = SagaPhase.COMPLETED
        state.last_updated = time.time()
        self._journal.record_hire_event(
            intent_id=intent_id, phase="completed", payload={"agent_id": state.agent_id}
        )
        return state

    def cancel(self, intent_id: str) -> SagaState:
        """Cancel an in-progress saga."""
        state = self._active_sagas.get(intent_id)
        if state is None:
            raise SagaStateError(f"no active saga: {intent_id}")
        if state.is_terminal:
            return state
        state.phase = SagaPhase.CANCELLED
        state.last_updated = time.time()
        self._journal.record_hire_event(
            intent_id=intent_id, phase="cancelled", payload={}
        )
        return state

    def get_state(self, intent_id: str) -> SagaState | None:
        return self._active_sagas.get(intent_id)

    def _require_state(self, intent_id: str, expected_phase: SagaPhase) -> SagaState:
        state = self._active_sagas.get(intent_id)
        if state is None:
            raise SagaStateError(f"no active saga: {intent_id}")
        if state.phase != expected_phase:
            raise SagaStateError(
                f"expected phase {expected_phase.value}, got {state.phase.value}"
            )
        return state
