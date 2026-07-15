"""Fail-closed employee activation verification without lifecycle mutation.

The router validates facts that a durable hire Saga has already recorded.  It
never changes an employee projection: a READY decision is immutable evidence
that the Saga may submit in its own serialized Journal transaction.
"""

from __future__ import annotations

import math
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol


class VerificationOutcome(str, Enum):
    READY = "ready"
    REJECTED = "rejected"


class VerificationRejection(str, Enum):
    MISSING_EVIDENCE = "missing_evidence"
    BINDING_MISMATCH = "binding_mismatch"
    STALE_GENERATION = "stale_generation"
    EXPIRED_CHALLENGE = "expired_challenge"
    EVIDENCE_TIME_MISMATCH = "evidence_time_mismatch"
    SLASH_NOT_RECONCILED = "slash_not_reconciled"
    CHANNEL_IDENTITY_MISMATCH = "channel_identity_mismatch"
    INVALID_TENANT_INGRESS = "invalid_tenant_ingress"
    NONCE_REPLAY = "nonce_replay"
    NONCE_STORE_FAILURE = "nonce_store_failure"


def _required(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required")


def _positive_generation(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("generation must be a positive integer")


def _timestamp(value: float, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be finite")
    if not math.isfinite(float(value)):
        raise ValueError(f"{field_name} must be finite")


@dataclass(frozen=True, slots=True)
class VerificationBinding:
    hire_intent_id: str
    tenant_key: str
    app_id: str
    agent_id: str
    generation: int
    requester_principal_id: str
    requester_union_id: str
    expected_slash_spec_hash: str

    def __post_init__(self) -> None:
        for field_name in (
            "hire_intent_id",
            "tenant_key",
            "app_id",
            "agent_id",
            "requester_principal_id",
            "requester_union_id",
            "expected_slash_spec_hash",
        ):
            _required(getattr(self, field_name), field_name)
        _positive_generation(self.generation)


@dataclass(frozen=True, slots=True)
class VerificationChallenge:
    hire_intent_id: str
    tenant_key: str
    app_id: str
    agent_id: str
    generation: int
    requester_principal_id: str
    requester_union_id: str
    expected_slash_spec_hash: str
    nonce: str = field(repr=False)
    issued_at: float
    expires_at: float

    def __post_init__(self) -> None:
        VerificationBinding(
            hire_intent_id=self.hire_intent_id,
            tenant_key=self.tenant_key,
            app_id=self.app_id,
            agent_id=self.agent_id,
            generation=self.generation,
            requester_principal_id=self.requester_principal_id,
            requester_union_id=self.requester_union_id,
            expected_slash_spec_hash=self.expected_slash_spec_hash,
        )
        _required(self.nonce, "nonce")
        if len(self.nonce) < 16:
            raise ValueError("nonce is too short")
        _timestamp(self.issued_at, "issued_at")
        _timestamp(self.expires_at, "expires_at")
        if self.expires_at <= self.issued_at:
            raise ValueError("challenge expiry must follow issuance")


@dataclass(frozen=True, slots=True)
class VerificationCoordinates:
    """Unforgeable routing coordinates copied onto every persisted fact."""

    hire_intent_id: str
    tenant_key: str
    app_id: str
    agent_id: str
    generation: int
    nonce: str = field(repr=False)

    def __post_init__(self) -> None:
        for field_name in (
            "hire_intent_id",
            "tenant_key",
            "app_id",
            "agent_id",
            "nonce",
        ):
            _required(getattr(self, field_name), field_name)
        _positive_generation(self.generation)
        if len(self.nonce) < 16:
            raise ValueError("nonce is too short")


@dataclass(frozen=True, slots=True)
class SlashVerificationEvidence:
    coordinates: VerificationCoordinates
    desired_spec_hash: str
    observed_spec_hash: str
    reconciled: bool
    verified_at: float

    def __post_init__(self) -> None:
        _required(self.desired_spec_hash, "desired_spec_hash")
        _required(self.observed_spec_hash, "observed_spec_hash")
        _timestamp(self.verified_at, "verified_at")


@dataclass(frozen=True, slots=True)
class ChannelVerificationEvidence:
    coordinates: VerificationCoordinates
    identity_app_id: str
    connection_id: str
    ready: bool
    verified_at: float

    def __post_init__(self) -> None:
        _required(self.identity_app_id, "identity_app_id")
        _required(self.connection_id, "connection_id")
        _timestamp(self.verified_at, "verified_at")


@dataclass(frozen=True, slots=True)
class TenantIngressEvidence:
    coordinates: VerificationCoordinates
    event_id: str
    message_id: str
    sender_principal_id: str
    sender_union_id: str
    command: str
    is_p2p: bool
    reply_succeeded: bool
    reply_app_id: str
    employee_send_request_id: str
    main_bot_send_count: int
    received_at: float

    def __post_init__(self) -> None:
        for field_name in (
            "event_id",
            "message_id",
            "sender_principal_id",
            "sender_union_id",
            "command",
            "reply_app_id",
            "employee_send_request_id",
        ):
            _required(getattr(self, field_name), field_name)
        _timestamp(self.received_at, "received_at")
        if (
            isinstance(self.main_bot_send_count, bool)
            or not isinstance(self.main_bot_send_count, int)
            or self.main_bot_send_count < 0
        ):
            raise ValueError("main_bot_send_count must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class ActivationVerificationEvidence:
    coordinates: VerificationCoordinates
    slash_spec_hash: str
    channel_connection_id: str
    ingress_event_id: str
    ingress_message_id: str
    employee_send_request_id: str
    reply_app_id: str
    main_bot_send_count: int
    sender_union_id: str
    verified_at: float


@dataclass(frozen=True, slots=True)
class VerificationDecision:
    outcome: VerificationOutcome
    rejection: VerificationRejection | None
    activation_evidence: ActivationVerificationEvidence | None
    evaluated_at: float

    def __post_init__(self) -> None:
        _timestamp(self.evaluated_at, "evaluated_at")
        if self.outcome is VerificationOutcome.READY:
            if self.rejection is not None or self.activation_evidence is None:
                raise ValueError("READY decision requires activation evidence only")
        elif self.outcome is VerificationOutcome.REJECTED:
            if self.rejection is None or self.activation_evidence is not None:
                raise ValueError("REJECTED decision requires a rejection only")
        else:
            raise ValueError("decision outcome is invalid")


class VerificationNonceConsumer(Protocol):
    """Atomically persist a nonce consumption before READY is returned.

    Production composition must back this port with the Journal/Saga.  False
    means the exact intent/nonce was already consumed; exceptions are treated
    as an unavailable store and fail closed.
    """

    def consume_once(
        self,
        challenge: VerificationChallenge,
        *,
        consumed_at: float,
    ) -> bool: ...


class VerificationRouter:
    """Validate activation evidence and emit a decision for serialized commit."""

    MAX_TTL_SECONDS = 600.0

    def __init__(
        self,
        *,
        nonce_consumer: VerificationNonceConsumer,
        clock: Callable[[], float] = time.time,
        nonce_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
    ) -> None:
        self._nonce_consumer = nonce_consumer
        self._clock = clock
        self._nonce_factory = nonce_factory

    def issue_challenge(
        self,
        binding: VerificationBinding,
        *,
        ttl_seconds: float = MAX_TTL_SECONDS,
    ) -> VerificationChallenge:
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, (int, float))
            or not math.isfinite(float(ttl_seconds))
            or ttl_seconds <= 0
            or ttl_seconds > self.MAX_TTL_SECONDS
        ):
            raise ValueError("ttl_seconds must be in (0, 600]")
        issued_at = float(self._clock())
        _timestamp(issued_at, "issued_at")
        nonce = self._nonce_factory()
        return VerificationChallenge(
            hire_intent_id=binding.hire_intent_id,
            tenant_key=binding.tenant_key,
            app_id=binding.app_id,
            agent_id=binding.agent_id,
            generation=binding.generation,
            requester_principal_id=binding.requester_principal_id,
            requester_union_id=binding.requester_union_id,
            expected_slash_spec_hash=binding.expected_slash_spec_hash,
            nonce=nonce,
            issued_at=issued_at,
            expires_at=issued_at + float(ttl_seconds),
        )

    def evaluate(
        self,
        challenge: VerificationChallenge,
        *,
        slash: SlashVerificationEvidence | None = None,
        channel: ChannelVerificationEvidence | None = None,
        ingress: TenantIngressEvidence | None = None,
        current_generation: int,
        now: float | None = None,
    ) -> VerificationDecision:
        return self._evaluate(
            challenge,
            slash=slash,
            channel=channel,
            ingress=ingress,
            current_generation=current_generation,
            now=now,
            consume_nonce=True,
        )

    def evaluate_for_atomic_commit(
        self,
        challenge: VerificationChallenge,
        *,
        slash: SlashVerificationEvidence | None = None,
        channel: ChannelVerificationEvidence | None = None,
        ingress: TenantIngressEvidence | None = None,
        current_generation: int,
        now: float | None = None,
    ) -> VerificationDecision:
        """Validate evidence while leaving nonce consumption to the atomic commit."""

        return self._evaluate(
            challenge,
            slash=slash,
            channel=channel,
            ingress=ingress,
            current_generation=current_generation,
            now=now,
            consume_nonce=False,
        )

    def _evaluate(
        self,
        challenge: VerificationChallenge,
        *,
        slash: SlashVerificationEvidence | None,
        channel: ChannelVerificationEvidence | None,
        ingress: TenantIngressEvidence | None,
        current_generation: int,
        now: float | None,
        consume_nonce: bool,
    ) -> VerificationDecision:
        evaluated_at = float(self._clock() if now is None else now)
        _timestamp(evaluated_at, "evaluated_at")
        if current_generation != challenge.generation:
            return self._reject(
                VerificationRejection.STALE_GENERATION,
                evaluated_at,
            )
        if evaluated_at < challenge.issued_at or evaluated_at > challenge.expires_at:
            return self._reject(
                VerificationRejection.EXPIRED_CHALLENGE,
                evaluated_at,
            )
        if slash is None or channel is None or ingress is None:
            return self._reject(VerificationRejection.MISSING_EVIDENCE, evaluated_at)
        coordinates = self._coordinates(challenge)
        if any(evidence.coordinates != coordinates for evidence in (slash, channel, ingress)):
            return self._reject(VerificationRejection.BINDING_MISMATCH, evaluated_at)
        if not (
            slash.verified_at
            <= channel.verified_at
            <= challenge.issued_at
            <= ingress.received_at
            <= evaluated_at
            <= challenge.expires_at
        ):
            return self._reject(
                VerificationRejection.EVIDENCE_TIME_MISMATCH,
                evaluated_at,
            )
        if (
            slash.reconciled is not True
            or slash.desired_spec_hash != challenge.expected_slash_spec_hash
            or slash.observed_spec_hash != slash.desired_spec_hash
        ):
            return self._reject(
                VerificationRejection.SLASH_NOT_RECONCILED,
                evaluated_at,
            )
        if channel.ready is not True or channel.identity_app_id != challenge.app_id or not channel.connection_id:
            return self._reject(
                VerificationRejection.CHANNEL_IDENTITY_MISMATCH,
                evaluated_at,
            )
        if (
            ingress.sender_union_id != challenge.requester_union_id
            or ingress.is_p2p is not True
            or ingress.command.strip() != "/status"
            or ingress.reply_succeeded is not True
            or ingress.reply_app_id != challenge.app_id
            or ingress.main_bot_send_count != 0
        ):
            return self._reject(
                VerificationRejection.INVALID_TENANT_INGRESS,
                evaluated_at,
            )
        if consume_nonce:
            try:
                consumed = self._nonce_consumer.consume_once(
                    challenge,
                    consumed_at=evaluated_at,
                )
            except Exception:
                return self._reject(
                    VerificationRejection.NONCE_STORE_FAILURE,
                    evaluated_at,
                )
            if consumed is not True:
                rejection = (
                    VerificationRejection.NONCE_REPLAY
                    if consumed is False
                    else VerificationRejection.NONCE_STORE_FAILURE
                )
                return self._reject(rejection, evaluated_at)
        activation_evidence = ActivationVerificationEvidence(
            coordinates=coordinates,
            slash_spec_hash=slash.observed_spec_hash,
            channel_connection_id=channel.connection_id,
            ingress_event_id=ingress.event_id,
            ingress_message_id=ingress.message_id,
            employee_send_request_id=ingress.employee_send_request_id,
            reply_app_id=ingress.reply_app_id,
            main_bot_send_count=ingress.main_bot_send_count,
            sender_union_id=ingress.sender_union_id,
            verified_at=evaluated_at,
        )
        return VerificationDecision(
            outcome=VerificationOutcome.READY,
            rejection=None,
            activation_evidence=activation_evidence,
            evaluated_at=evaluated_at,
        )

    @staticmethod
    def _coordinates(challenge: VerificationChallenge) -> VerificationCoordinates:
        return VerificationCoordinates(
            hire_intent_id=challenge.hire_intent_id,
            tenant_key=challenge.tenant_key,
            app_id=challenge.app_id,
            agent_id=challenge.agent_id,
            generation=challenge.generation,
            nonce=challenge.nonce,
        )

    @staticmethod
    def _reject(
        rejection: VerificationRejection,
        evaluated_at: float,
    ) -> VerificationDecision:
        return VerificationDecision(
            outcome=VerificationOutcome.REJECTED,
            rejection=rejection,
            activation_evidence=None,
            evaluated_at=evaluated_at,
        )
