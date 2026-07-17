"""Descriptor-relative no-follow layout helpers for employee workspaces."""

from __future__ import annotations

import os
import stat
import uuid
from pathlib import Path

from .models import WorkspaceProjectionError

REQUIRED_WORKSPACE_FILES = (
    "workspace/AGENTS.md",
    "workspace/IDENTITY.md",
    "workspace/NOW.md",
    "workspace/purpose.md",
    "workspace/schema.md",
    "workspace/wiki/index.md",
    "workspace/wiki/overview.md",
    "workspace/wiki/log.md",
    "workspace/tasks/active.md",
    "workspace/tasks/archive/index.md",
    "workspace/sources/manifest.yaml",
    "runtime/codex-home/AGENTS.md",
)


def open_directory_tree(path: Path) -> int:
    """Open or create a directory through safe components without symlinks."""

    expanded = path.expanduser()
    parts = expanded.parts[1:] if expanded.is_absolute() else expanded.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise WorkspaceProjectionError("workspace root has unsafe components")
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open("/" if expanded.is_absolute() else ".", flags)
    try:
        for part in parts:
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=descriptor)
            if not stat.S_ISDIR(os.fstat(child).st_mode):
                os.close(child)
                raise OSError("workspace component is not a directory")
            os.close(descriptor)
            descriptor = child
        os.fchmod(descriptor, 0o700)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def open_child_directory(parent_fd: int, name: str) -> int:
    if not name or name in {".", ".."} or "/" in name:
        raise WorkspaceProjectionError("unsafe workspace path component")
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    os.fchmod(descriptor, 0o700)
    return descriptor


def atomic_write_relative(root_fd: int, relative_path: str, content: bytes) -> None:
    parts = tuple(relative_path.split("/"))
    if len(parts) < 2 or any(part in {"", ".", ".."} for part in parts):
        raise WorkspaceProjectionError("unsafe workspace relative path")
    parent_fd = os.dup(root_fd)
    try:
        for component in parts[:-1]:
            child = open_child_directory(parent_fd, component)
            os.close(parent_fd)
            parent_fd = child
        filename = parts[-1]
        temp_name = f".{filename}.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
        try:
            os.fchmod(descriptor, 0o600)
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short workspace projection write")
                view = view[written:]
            os.fsync(descriptor)
        except BaseException:
            os.close(descriptor)
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            raise
        os.close(descriptor)
        try:
            os.replace(
                temp_name,
                filename,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            target_fd = os.open(
                filename,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            try:
                os.fchmod(target_fd, 0o600)
            finally:
                os.close(target_fd)
            os.fsync(parent_fd)
        except BaseException:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            raise
    finally:
        os.close(parent_fd)


__all__ = [
    "REQUIRED_WORKSPACE_FILES",
    "atomic_write_relative",
    "open_child_directory",
    "open_directory_tree",
]
