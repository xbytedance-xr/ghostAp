"""Fail-closed filesystem sandbox for employee-owned CLI backends."""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path


class EmployeeCLISandboxError(RuntimeError):
    """Raised when a CLI backend cannot be isolated before spawn."""


class EmployeeCLISandbox:
    """Build a minimal bubblewrap namespace for one employee CLI session."""

    def __init__(
        self,
        *,
        cwd: str,
        process_env: Mapping[str, str],
    ) -> None:
        self._cwd = os.path.realpath(cwd)
        self._env = dict(process_env)
        home = self._env.get("HOME", "")
        if not self._cwd or not os.path.isabs(self._cwd):
            raise EmployeeCLISandboxError("employee CLI cwd must be absolute")
        if not home or not os.path.isabs(home):
            raise EmployeeCLISandboxError("employee CLI HOME must be absolute")
        self._home = os.path.realpath(home)
        self._prefix: tuple[str, ...] = ()
        self._executable = ""

    @property
    def configured(self) -> bool:
        return bool(self._prefix and self._executable)

    @property
    def process_env(self) -> dict[str, str]:
        return dict(self._env)

    @property
    def launch_prefix(self) -> tuple[str, ...]:
        return self._prefix

    def configure(
        self,
        *,
        command: str,
        read_only_roots: Sequence[str],
        writable_roots: Sequence[str],
    ) -> None:
        bwrap = shutil.which("bwrap")
        executable = shutil.which(command, path=self._env.get("PATH"))
        if not bwrap or not executable:
            raise EmployeeCLISandboxError(
                "employee CLI requires bubblewrap and an explicit executable"
            )
        executable_path = Path(executable).resolve(strict=True)
        read_roots = self._normalize_roots(read_only_roots)
        write_roots = self._normalize_roots(writable_roots)
        read_only_mounts = tuple(root for root in read_roots if root not in write_roots)
        if not any(self._under(self._cwd, root) for root in (*read_roots, *write_roots)):
            raise EmployeeCLISandboxError("employee CLI cwd is outside authorized roots")

        package_root = self._package_root(executable_path)
        bind_roots = {Path(self._home), package_root, *map(Path, read_roots), *map(Path, write_roots)}
        directory_targets: set[Path] = set()
        for target in bind_roots:
            parent = target
            while parent != parent.parent:
                directory_targets.add(parent)
                parent = parent.parent

        args: list[str] = [
            bwrap,
            "--die-with-parent",
            "--new-session",
            "--unshare-user",
            "--unshare-pid",
            "--unshare-uts",
            "--unshare-ipc",
            "--as-pid-1",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
        ]
        for target in sorted(directory_targets, key=lambda item: (len(item.parts), str(item))):
            if target != Path("/"):
                args.extend(("--dir", str(target)))
        for path in (Path("/usr"), Path("/bin"), Path("/lib"), Path("/lib64")):
            if path.exists():
                args.extend(("--ro-bind", str(path), str(path)))
        for path in (
            Path("/etc/hosts"),
            Path("/etc/nsswitch.conf"),
            Path("/etc/resolv.conf"),
            Path("/etc/ssl"),
            Path("/etc/ca-certificates"),
        ):
            if path.exists():
                args.extend(("--ro-bind", str(path), str(path)))
        args.extend(("--bind", self._home, self._home))
        args.extend(("--ro-bind", str(package_root), str(package_root)))
        for root in write_roots:
            args.extend(("--bind", root, root))
        for root in read_only_mounts:
            args.extend(("--ro-bind", root, root))
        for kind, target in self._sensitive_overlays((*read_roots, *write_roots)):
            args.extend((kind, "/dev/null" if kind == "--ro-bind" else target, target) if kind == "--ro-bind" else (kind, target))
        args.extend(("--chdir", self._cwd, "--"))
        self._prefix = tuple(args)
        self._executable = str(executable_path)

    def wrap_argv(self, argv: Sequence[str]) -> list[str]:
        if not self.configured:
            raise EmployeeCLISandboxError(
                "employee CLI filesystem sandbox is not configured"
            )
        if not argv:
            raise EmployeeCLISandboxError("employee CLI argv is empty")
        return [*self._prefix, self._executable, *argv[1:]]

    @staticmethod
    def _normalize_roots(roots: Sequence[str]) -> tuple[str, ...]:
        normalized: list[str] = []
        for root in roots:
            if not isinstance(root, str) or not root or not os.path.isabs(root):
                raise EmployeeCLISandboxError("employee CLI roots must be absolute")
            real = os.path.realpath(root)
            if not os.path.isdir(real):
                raise EmployeeCLISandboxError("employee CLI root is unavailable")
            if real not in normalized:
                normalized.append(real)
        return tuple(normalized)

    @staticmethod
    def _under(path: str, root: str) -> bool:
        return path == root or path.startswith(root + os.sep)

    @staticmethod
    def _package_root(executable: Path) -> Path:
        parts = executable.parts
        if "node_modules" in parts:
            index = parts.index("node_modules")
            package_end = index + 2
            if len(parts) > index + 1 and parts[index + 1].startswith("@"):
                package_end += 1
            return Path(*parts[:package_end])
        return executable.parent

    @staticmethod
    def _sensitive_overlays(
        roots: Sequence[str],
    ) -> tuple[tuple[str, str], ...]:
        overlays: list[tuple[str, str]] = []
        visited = 0
        for root in roots:
            for current, directories, files in os.walk(root, followlinks=False):
                visited += 1
                if visited > 10_000:
                    raise EmployeeCLISandboxError(
                        "employee CLI sensitive-path scan exceeded limit"
                    )
                blocked_dirs = [
                    name for name in directories if name.casefold() in {"vault", "journal"}
                ]
                for name in blocked_dirs:
                    overlays.append(("--tmpfs", os.path.join(current, name)))
                    directories.remove(name)
                for name in files:
                    if name == ".env":
                        overlays.append(("--ro-bind", os.path.join(current, name)))
        return tuple(dict.fromkeys(overlays))


__all__ = ["EmployeeCLISandbox", "EmployeeCLISandboxError"]
