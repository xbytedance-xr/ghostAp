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
