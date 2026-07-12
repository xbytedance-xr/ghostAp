"""Employee workforce infrastructure boundaries."""

from .authority import (
    AuthorityMode,
    AuthoritySnapshot,
    LegacyMutationGuard,
    StaleAuthorityEpoch,
)
from .credential_vault import (
    CredentialKeyring,
    CredentialReceipt,
    CredentialVault,
    CredentialVaultConfigurationError,
    CredentialVaultError,
)
from .projection import (
    EmployeeIdentityMaterializer,
    WorkforceProjectionState,
    apply_workforce_event,
    commit_workforce_events,
    validate_workforce_events,
)
from .registry import AmbiguousEmployeeName, ProjectedAgentRegistry

__all__ = [
    "CredentialKeyring",
    "CredentialReceipt",
    "CredentialVault",
    "CredentialVaultConfigurationError",
    "CredentialVaultError",
    "AuthorityMode",
    "AuthoritySnapshot",
    "LegacyMutationGuard",
    "StaleAuthorityEpoch",
    "AmbiguousEmployeeName",
    "EmployeeIdentityMaterializer",
    "ProjectedAgentRegistry",
    "WorkforceProjectionState",
    "apply_workforce_event",
    "commit_workforce_events",
    "validate_workforce_events",
]
