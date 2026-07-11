"""Default-deny authorization gate for the autonomous work system.

All operations require explicit authorization via principal/operation/resource
checks. Manager `is_admin` is derived from settings and principal mapping,
never from callback payloads.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, FrozenSet, Mapping, Optional, Sequence

from ..domain.control import GoalActivationAuthorization, Principal
from ..domain.ids import canonical_hash, freeze, new_id, thaw


class Operation(str, Enum):
    """Operations subject to authorization checks."""

    CREATE = "create"
    READ = "read"
    UPDATE = "update"
    DELETE = "delete"
    EXECUTE = "execute"
    APPROVE = "approve"
    ACTIVATE = "activate"
    CANCEL = "cancel"


class AuthorizationDenied(Exception):
    """Raised when an operation is denied by the authorization gate."""

    def __init__(
        self,
        principal_id: str,
        operation: Operation,
        resource: str,
        reason: str = "default deny",
    ):
        self.principal_id = principal_id
        self.operation = operation
        self.resource = resource
        self.reason = reason
        super().__init__(
            f"authorization denied: principal={principal_id} "
            f"op={operation.value} resource={resource} reason={reason}"
        )


@dataclass(frozen=True)
class AuthorizationResult:
    """Immutable result of an authorization check."""

    allowed: bool
    principal_id: str
    operation: Operation
    resource: str
    reason: str = ""
    timestamp: float = field(default_factory=time.time)
    envelope_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "principal_id": self.principal_id,
            "operation": self.operation.value,
            "resource": self.resource,
            "reason": self.reason,
            "timestamp": self.timestamp,
            "envelope_hash": self.envelope_hash,
        }


@dataclass(frozen=True)
class AuthorizationEnvelope:
    """Immutable authorization envelope binding payload, recipients, and budget.

    Once committed, this record must never be mutated.
    """

    envelope_id: str = field(default_factory=lambda: new_id("env"))
    principal_id: str = ""
    tenant_key: str = ""
    operation: str = ""
    resource: str = ""
    canonical_payload_hash: str = ""
    canonical_render_hash: str = ""
    recipients: tuple[str, ...] = ()
    resource_labels: tuple[str, ...] = ()
    data_labels: tuple[str, ...] = ()
    budget_id: str = ""
    budget_amount: float = 0.0
    version: int = 0
    epoch: int = 0
    nonce: str = field(default_factory=lambda: uuid.uuid4().hex)
    expires_at: float = 0.0
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        object.__setattr__(self, "recipients", tuple(self.recipients))
        object.__setattr__(self, "resource_labels", tuple(self.resource_labels))
        object.__setattr__(self, "data_labels", tuple(self.data_labels))

    @property
    def content_hash(self) -> str:
        """Canonical hash of binding fields for integrity verification."""
        payload = {
            "envelope_id": self.envelope_id,
            "principal_id": self.principal_id,
            "tenant_key": self.tenant_key,
            "operation": self.operation,
            "resource": self.resource,
            "canonical_payload_hash": self.canonical_payload_hash,
            "canonical_render_hash": self.canonical_render_hash,
            "recipients": list(self.recipients),
            "resource_labels": list(self.resource_labels),
            "data_labels": list(self.data_labels),
            "budget_id": self.budget_id,
            "budget_amount": self.budget_amount,
            "version": self.version,
            "epoch": self.epoch,
            "nonce": self.nonce,
            "expires_at": self.expires_at,
        }
        return canonical_hash(payload)

    def is_expired(self, *, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        return self.expires_at > 0 and current >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "envelope_id": self.envelope_id,
            "principal_id": self.principal_id,
            "tenant_key": self.tenant_key,
            "operation": self.operation,
            "resource": self.resource,
            "canonical_payload_hash": self.canonical_payload_hash,
            "canonical_render_hash": self.canonical_render_hash,
            "recipients": list(self.recipients),
            "resource_labels": list(self.resource_labels),
            "data_labels": list(self.data_labels),
            "budget_id": self.budget_id,
            "budget_amount": self.budget_amount,
            "version": self.version,
            "epoch": self.epoch,
            "nonce": self.nonce,
            "expires_at": self.expires_at,
            "created_at": self.created_at,
        }


@dataclass
class ResourceACL:
    """Access control list for a resource."""

    resource: str
    owner_principal_id: str
    tenant_key: str
    allowed_principals: set[str] = field(default_factory=set)
    allowed_roles: set[str] = field(default_factory=set)
    allowed_operations: dict[str, set[Operation]] = field(default_factory=dict)


class ControlAuthorizationGate:
    """Default-deny authorization gate.

    All reads/writes include tenant/owner/ACL checks. Manager's `is_admin`
    is derived from settings and principal mapping, never trusted from
    callback payload.
    """

    def __init__(
        self,
        *,
        admin_principal_ids: Sequence[str] = (),
        admin_roles: Sequence[str] = ("admin", "system"),
    ):
        self._admin_principal_ids: frozenset[str] = frozenset(admin_principal_ids)
        self._admin_roles: frozenset[str] = frozenset(admin_roles)
        self._acls: dict[str, ResourceACL] = {}
        self._consumed_nonces: set[str] = set()
        self._envelopes: dict[str, AuthorizationEnvelope] = {}

    def register_acl(self, acl: ResourceACL) -> None:
        """Register or update a resource ACL."""
        self._acls[acl.resource] = acl

    def is_admin(self, principal: Principal) -> bool:
        """Determine admin status from settings and principal mapping only."""
        if principal.principal_id in self._admin_principal_ids:
            return True
        return bool(set(principal.roles) & self._admin_roles)

    def authorize(
        self,
        principal: Principal,
        operation: Operation,
        resource: str,
    ) -> AuthorizationResult:
        """Check authorization. Returns result or raises AuthorizationDenied.

        Default-deny: if no explicit grant can be found, access is denied.
        """
        # Admin bypass
        if self.is_admin(principal):
            return AuthorizationResult(
                allowed=True,
                principal_id=principal.principal_id,
                operation=operation,
                resource=resource,
                reason="admin",
            )

        # Look up ACL for the resource
        acl = self._acls.get(resource)
        if acl is None:
            # Default deny - no ACL means no access
            raise AuthorizationDenied(
                principal.principal_id, operation, resource, "no ACL registered"
            )

        # Tenant isolation: cross-tenant always denied
        if acl.tenant_key and principal.tenant_key != acl.tenant_key:
            raise AuthorizationDenied(
                principal.principal_id,
                operation,
                resource,
                "tenant mismatch",
            )

        # Owner always has full access within same tenant
        if principal.principal_id == acl.owner_principal_id:
            return AuthorizationResult(
                allowed=True,
                principal_id=principal.principal_id,
                operation=operation,
                resource=resource,
                reason="owner",
            )

        # Check explicit principal grant
        if principal.principal_id in acl.allowed_principals:
            # Check operation-specific permissions if defined
            if acl.allowed_operations:
                principal_ops = acl.allowed_operations.get(
                    principal.principal_id, set()
                )
                if operation in principal_ops:
                    return AuthorizationResult(
                        allowed=True,
                        principal_id=principal.principal_id,
                        operation=operation,
                        resource=resource,
                        reason="principal grant",
                    )
            else:
                return AuthorizationResult(
                    allowed=True,
                    principal_id=principal.principal_id,
                    operation=operation,
                    resource=resource,
                    reason="principal grant",
                )

        # Check role-based grant
        if set(principal.roles) & acl.allowed_roles:
            return AuthorizationResult(
                allowed=True,
                principal_id=principal.principal_id,
                operation=operation,
                resource=resource,
                reason="role grant",
            )

        # Default deny
        raise AuthorizationDenied(
            principal.principal_id, operation, resource, "default deny"
        )

    def consume_nonce(self, nonce: str) -> bool:
        """Consume a one-time nonce. Returns True if fresh, False if replay."""
        if nonce in self._consumed_nonces:
            return False
        self._consumed_nonces.add(nonce)
        return True

    def commit_envelope(self, envelope: AuthorizationEnvelope) -> bool:
        """Commit an authorization envelope, consuming its nonce atomically.

        Returns True if committed, False if nonce was already consumed (replay).
        """
        if not self.consume_nonce(envelope.nonce):
            return False
        if envelope.is_expired():
            # Remove consumed nonce on expired envelope
            self._consumed_nonces.discard(envelope.nonce)
            return False
        self._envelopes[envelope.envelope_id] = envelope
        return True

    def get_envelope(self, envelope_id: str) -> Optional[AuthorizationEnvelope]:
        """Retrieve a committed envelope by ID."""
        return self._envelopes.get(envelope_id)

    def create_dependent_authorization(
        self,
        parent_envelope: AuthorizationEnvelope,
        principal: Principal,
        operation: Operation,
        resource: str,
        *,
        budget_id: str = "",
        budget_amount: float = 0.0,
        expires_at: float = 0.0,
    ) -> AuthorizationEnvelope:
        """Create a dependent authorization from a parent envelope.

        Both nonce consumption and dependent creation happen atomically
        within the same logical frame.
        """
        if parent_envelope.envelope_id not in self._envelopes:
            raise AuthorizationDenied(
                principal.principal_id,
                operation,
                resource,
                "parent envelope not committed",
            )

        child = AuthorizationEnvelope(
            principal_id=principal.principal_id,
            tenant_key=principal.tenant_key,
            operation=operation.value,
            resource=resource,
            budget_id=budget_id,
            budget_amount=budget_amount,
            version=parent_envelope.version + 1,
            epoch=parent_envelope.epoch,
            expires_at=expires_at,
        )

        if not self.commit_envelope(child):
            raise AuthorizationDenied(
                principal.principal_id,
                operation,
                resource,
                "nonce collision on dependent authorization",
            )

        return child
