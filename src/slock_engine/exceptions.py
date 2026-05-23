"""Slock engine custom exceptions."""


class SecurityPolicyDegradedError(RuntimeError):
    """Raised when security policy cannot be enforced due to missing capabilities.

    This occurs when slock_tool_path_restrictions are configured but the ACP
    session does not support set_tool_filter, meaning security sandboxing would
    silently degrade.

    Also raised when memory operations attempt to access paths outside the
    allowed base directory or whitelist.
    """

    def __init__(self, message: str, *args, **kwargs) -> None:
        # Support both old signature (agent_id, restriction_paths) and new (message)
        if args and isinstance(args[0], list):
            # Old signature: SecurityPolicyDegradedError(agent_id, restriction_paths)
            agent_id = message
            restriction_paths = args[0]
            self.agent_id = agent_id
            self.restriction_paths = restriction_paths
            super().__init__(
                f"Security policy degraded for agent '{agent_id}': "
                f"session lacks set_tool_filter but restrictions are configured: "
                f"{restriction_paths}"
            )
        else:
            # New signature: SecurityPolicyDegradedError(message)
            self.agent_id = kwargs.get('agent_id', '')
            self.restriction_paths = kwargs.get('restriction_paths', [])
            super().__init__(message)
