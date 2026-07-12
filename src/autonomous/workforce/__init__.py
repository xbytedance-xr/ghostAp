"""Employee workforce infrastructure boundaries."""

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

__all__ = [
    "CredentialKeyring",
    "CredentialReceipt",
    "CredentialVault",
    "CredentialVaultConfigurationError",
    "CredentialVaultError",
    "EmployeeIdentityMaterializer",
    "WorkforceProjectionState",
    "apply_workforce_event",
    "commit_workforce_events",
    "validate_workforce_events",
]
