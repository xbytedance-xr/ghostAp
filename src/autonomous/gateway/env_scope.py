"""Explicit positive-list environment construction for employee ACP processes."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

_RUNTIME_KEYS = frozenset(
    {
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TERM",
        "TMPDIR",
    }
)
_PROVIDER_KEYS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "OPENAI_API_KEY",
    }
)


@dataclass(frozen=True, slots=True)
class EmployeeEnvironmentAuthority:
    """Secret-free identity passed to an employee credential provider."""

    tenant_key: str
    agent_id: str
    employee_version: int
    credential_ref: str

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (self.tenant_key, self.agent_id, self.credential_ref)
        ):
            raise ValueError("employee environment authority is incomplete")
        if type(self.employee_version) is not int or self.employee_version < 0:
            raise ValueError("employee environment version is invalid")


@dataclass(frozen=True, slots=True)
class EmployeeProcessEnvironmentMaterial:
    """Frozen employee-scoped runtime and provider credential material."""

    tenant_key: str
    agent_id: str
    employee_version: int
    credential_ref: str
    runtime_env: Mapping[str, str] = field(repr=False, compare=False)
    credential_env: Mapping[str, str] = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        EmployeeEnvironmentAuthority(
            self.tenant_key,
            self.agent_id,
            self.employee_version,
            self.credential_ref,
        )
        for name in ("runtime_env", "credential_env"):
            value = getattr(self, name)
            if not isinstance(value, Mapping) or any(
                not isinstance(key, str)
                or not key
                or not isinstance(item, str)
                or not item
                for key, item in value.items()
            ):
                raise ValueError(f"{name} must be a non-empty string mapping")
            object.__setattr__(self, name, MappingProxyType(dict(value)))

    @property
    def authority(self) -> EmployeeEnvironmentAuthority:
        return EmployeeEnvironmentAuthority(
            self.tenant_key,
            self.agent_id,
            self.employee_version,
            self.credential_ref,
        )


def build_employee_process_env(
    runtime_env: Mapping[str, str],
    *,
    employee_home: str,
    credential_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build one process env without inheriting shared application secrets."""

    if not isinstance(employee_home, str) or not employee_home.startswith("/"):
        raise ValueError("employee_home must be an absolute path")
    result = {
        key: value
        for key, value in runtime_env.items()
        if key in _RUNTIME_KEYS and isinstance(value, str) and value
    }
    result["HOME"] = employee_home
    for key, value in dict(credential_env or {}).items():
        if key not in _PROVIDER_KEYS:
            raise ValueError("employee credential env key is not allowed")
        if not isinstance(value, str) or not value:
            raise ValueError("employee credential env value is invalid")
        result[key] = value
    return dict(sorted(result.items()))


def runtime_only_employee_environment(
    authority: EmployeeEnvironmentAuthority,
) -> EmployeeProcessEnvironmentMaterial:
    """Provide only non-secret process runtime values for an employee.

    Provider credentials must come from a future employee-scoped authority;
    the manager Bot process environment is never an acceptable credential
    source.
    """

    runtime_env = {
        key: value
        for key, value in os.environ.items()
        if key in _RUNTIME_KEYS and isinstance(value, str) and value
    }
    return EmployeeProcessEnvironmentMaterial(
        authority.tenant_key,
        authority.agent_id,
        authority.employee_version,
        authority.credential_ref,
        runtime_env,
        {},
    )


__all__ = [
    "EmployeeEnvironmentAuthority",
    "EmployeeProcessEnvironmentMaterial",
    "build_employee_process_env",
    "runtime_only_employee_environment",
]
