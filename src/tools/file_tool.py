import os
import json
import logging
from dataclasses import dataclass
from typing import Optional, Literal
from enum import Enum
from pathlib import Path
from langchain_core.tools import BaseTool
from pydantic import Field

logger = logging.getLogger(__name__)


class FileFormat(Enum):
    TEXT = "text"
    JSON = "json"
    YAML = "yaml"
    MARKDOWN = "markdown"
    PYTHON = "python"
    JAVASCRIPT = "javascript"
    UNKNOWN = "unknown"


@dataclass
class FileOperationResult:
    success: bool
    operation: str
    path: str
    content: Optional[str] = None
    error_message: Optional[str] = None
    file_format: FileFormat = FileFormat.UNKNOWN
    file_size: int = 0
    line_count: int = 0

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "operation": self.operation,
            "path": self.path,
            "content": self.content,
            "error_message": self.error_message,
            "file_format": self.file_format.value,
            "file_size": self.file_size,
            "line_count": self.line_count,
        }

    def to_message(self) -> str:
        if not self.success:
            return f"❌ {self.operation}失败: {self.error_message}"

        if self.operation == "read":
            return f"📄 文件内容 ({self.path}):\n```\n{self.content}\n```\n📊 {self.line_count} 行, {self.file_size} 字节"
        elif self.operation == "write":
            return f"✅ 文件已写入: {self.path} ({self.file_size} 字节)"
        elif self.operation == "delete":
            return f"🗑️ 文件已删除: {self.path}"
        elif self.operation == "list":
            return f"📁 目录内容:\n{self.content}"
        else:
            return f"✅ {self.operation}成功: {self.path}"


@dataclass
class FileSecurityPolicy:
    allowed_extensions: list[str] = None
    blocked_extensions: list[str] = None
    allowed_paths: list[str] = None
    blocked_paths: list[str] = None
    max_file_size_mb: float = 10.0
    max_read_lines: int = 1000
    allow_delete: bool = False
    allow_overwrite: bool = True

    def __post_init__(self):
        if self.allowed_extensions is None:
            self.allowed_extensions = []
        if self.blocked_extensions is None:
            self.blocked_extensions = [".exe", ".dll", ".so", ".dylib", ".bin"]
        if self.allowed_paths is None:
            self.allowed_paths = []
        if self.blocked_paths is None:
            self.blocked_paths = [
                "/etc", "/usr", "/bin", "/sbin", "/boot", "/root",
                "/System", "/Library", "/Applications",
                "C:\\Windows", "C:\\Program Files",
            ]

    @classmethod
    def default(cls) -> "FileSecurityPolicy":
        return cls()

    @classmethod
    def strict(cls) -> "FileSecurityPolicy":
        return cls(
            allowed_extensions=[".txt", ".md", ".json", ".yaml", ".yml", ".py", ".js", ".ts"],
            max_file_size_mb=1.0,
            max_read_lines=500,
            allow_delete=False,
            allow_overwrite=False,
        )


class FileEditorTool(BaseTool):
    name: str = "file_editor"
    description: str = """Read, write, and manage files with security controls.
    Supports multiple file formats including text, JSON, YAML, and code files.
    Operations: read, write, append, delete, list, exists, info."""

    security_policy: FileSecurityPolicy = Field(default_factory=FileSecurityPolicy.default)
    root_path: Optional[str] = None
    encoding: str = "utf-8"

    model_config = {"arbitrary_types_allowed": True}

    def _detect_format(self, path: str) -> FileFormat:
        ext = Path(path).suffix.lower()
        format_map = {
            ".txt": FileFormat.TEXT,
            ".md": FileFormat.MARKDOWN,
            ".json": FileFormat.JSON,
            ".yaml": FileFormat.YAML,
            ".yml": FileFormat.YAML,
            ".py": FileFormat.PYTHON,
            ".js": FileFormat.JAVASCRIPT,
            ".ts": FileFormat.JAVASCRIPT,
            ".jsx": FileFormat.JAVASCRIPT,
            ".tsx": FileFormat.JAVASCRIPT,
        }
        return format_map.get(ext, FileFormat.UNKNOWN)

    def _resolve_path(self, path: str) -> str:
        if self.root_path:
            if not path.startswith("/") and not path.startswith(self.root_path):
                path = os.path.join(self.root_path, path)
        return os.path.abspath(os.path.expanduser(path))

    def _check_security(self, path: str, operation: str) -> tuple[bool, Optional[str]]:
        resolved_path = self._resolve_path(path)

        for blocked in self.security_policy.blocked_paths:
            if resolved_path.startswith(blocked):
                return False, f"路径被禁止访问: {blocked}"

        if self.security_policy.allowed_paths:
            allowed = False
            for allowed_path in self.security_policy.allowed_paths:
                if resolved_path.startswith(allowed_path):
                    allowed = True
                    break
            if not allowed:
                return False, f"路径不在允许列表中: {resolved_path}"

        ext = Path(resolved_path).suffix.lower()
        if ext in self.security_policy.blocked_extensions:
            return False, f"文件扩展名被禁止: {ext}"

        if self.security_policy.allowed_extensions:
            if ext and ext not in self.security_policy.allowed_extensions:
                return False, f"文件扩展名不在允许列表中: {ext}"

        if operation == "delete" and not self.security_policy.allow_delete:
            return False, "删除操作被禁止"

        if operation == "write" and os.path.exists(resolved_path):
            if not self.security_policy.allow_overwrite:
                return False, "覆盖写入被禁止"

        return True, None

    def _run(self, action: str, path: str, content: str = None) -> str:
        if action == "read":
            result = self.read(path)
        elif action == "write":
            result = self.write(path, content or "")
        elif action == "append":
            result = self.append(path, content or "")
        elif action == "delete":
            result = self.delete(path)
        elif action == "list":
            result = self.list_directory(path)
        elif action == "exists":
            result = self.exists(path)
        elif action == "info":
            result = self.get_info(path)
        else:
            return f"❌ 未知操作: {action}"
        return result.to_message()

    async def _arun(self, action: str, path: str, content: str = None) -> str:
        return self._run(action, path, content)

    def read(self, path: str, start_line: int = 1, max_lines: Optional[int] = None) -> FileOperationResult:
        is_safe, reason = self._check_security(path, "read")
        if not is_safe:
            return FileOperationResult(
                success=False,
                operation="read",
                path=path,
                error_message=reason,
            )

        resolved_path = self._resolve_path(path)

        try:
            if not os.path.exists(resolved_path):
                return FileOperationResult(
                    success=False,
                    operation="read",
                    path=path,
                    error_message="文件不存在",
                )

            file_size = os.path.getsize(resolved_path)
            max_size = self.security_policy.max_file_size_mb * 1024 * 1024
            if file_size > max_size:
                return FileOperationResult(
                    success=False,
                    operation="read",
                    path=path,
                    error_message=f"文件过大: {file_size / 1024 / 1024:.2f}MB > {self.security_policy.max_file_size_mb}MB",
                )

            with open(resolved_path, "r", encoding=self.encoding) as f:
                lines = f.readlines()

            total_lines = len(lines)
            max_read = max_lines or self.security_policy.max_read_lines
            end_line = min(start_line - 1 + max_read, total_lines)
            selected_lines = lines[start_line - 1:end_line]
            content = "".join(selected_lines)

            if end_line < total_lines:
                content += f"\n... (显示 {start_line}-{end_line} 行，共 {total_lines} 行)"

            logger.info(f"读取文件成功: {resolved_path}")
            return FileOperationResult(
                success=True,
                operation="read",
                path=path,
                content=content,
                file_format=self._detect_format(path),
                file_size=file_size,
                line_count=total_lines,
            )

        except UnicodeDecodeError:
            return FileOperationResult(
                success=False,
                operation="read",
                path=path,
                error_message="文件编码错误，可能是二进制文件",
            )
        except Exception as e:
            logger.error(f"读取文件失败: {resolved_path}, 错误: {e}")
            return FileOperationResult(
                success=False,
                operation="read",
                path=path,
                error_message=str(e),
            )

    def write(self, path: str, content: str) -> FileOperationResult:
        is_safe, reason = self._check_security(path, "write")
        if not is_safe:
            return FileOperationResult(
                success=False,
                operation="write",
                path=path,
                error_message=reason,
            )

        resolved_path = self._resolve_path(path)

        try:
            dir_path = os.path.dirname(resolved_path)
            if dir_path and not os.path.exists(dir_path):
                os.makedirs(dir_path, exist_ok=True)

            with open(resolved_path, "w", encoding=self.encoding) as f:
                f.write(content)

            file_size = os.path.getsize(resolved_path)
            line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)

            logger.info(f"写入文件成功: {resolved_path}")
            return FileOperationResult(
                success=True,
                operation="write",
                path=path,
                file_format=self._detect_format(path),
                file_size=file_size,
                line_count=line_count,
            )

        except Exception as e:
            logger.error(f"写入文件失败: {resolved_path}, 错误: {e}")
            return FileOperationResult(
                success=False,
                operation="write",
                path=path,
                error_message=str(e),
            )

    def append(self, path: str, content: str) -> FileOperationResult:
        is_safe, reason = self._check_security(path, "write")
        if not is_safe:
            return FileOperationResult(
                success=False,
                operation="append",
                path=path,
                error_message=reason,
            )

        resolved_path = self._resolve_path(path)

        try:
            with open(resolved_path, "a", encoding=self.encoding) as f:
                f.write(content)

            file_size = os.path.getsize(resolved_path)

            logger.info(f"追加文件成功: {resolved_path}")
            return FileOperationResult(
                success=True,
                operation="append",
                path=path,
                file_format=self._detect_format(path),
                file_size=file_size,
            )

        except Exception as e:
            logger.error(f"追加文件失败: {resolved_path}, 错误: {e}")
            return FileOperationResult(
                success=False,
                operation="append",
                path=path,
                error_message=str(e),
            )

    def delete(self, path: str) -> FileOperationResult:
        is_safe, reason = self._check_security(path, "delete")
        if not is_safe:
            return FileOperationResult(
                success=False,
                operation="delete",
                path=path,
                error_message=reason,
            )

        resolved_path = self._resolve_path(path)

        try:
            if not os.path.exists(resolved_path):
                return FileOperationResult(
                    success=False,
                    operation="delete",
                    path=path,
                    error_message="文件不存在",
                )

            os.remove(resolved_path)

            logger.info(f"删除文件成功: {resolved_path}")
            return FileOperationResult(
                success=True,
                operation="delete",
                path=path,
            )

        except Exception as e:
            logger.error(f"删除文件失败: {resolved_path}, 错误: {e}")
            return FileOperationResult(
                success=False,
                operation="delete",
                path=path,
                error_message=str(e),
            )

    def list_directory(self, path: str) -> FileOperationResult:
        is_safe, reason = self._check_security(path, "read")
        if not is_safe:
            return FileOperationResult(
                success=False,
                operation="list",
                path=path,
                error_message=reason,
            )

        resolved_path = self._resolve_path(path)

        try:
            if not os.path.exists(resolved_path):
                return FileOperationResult(
                    success=False,
                    operation="list",
                    path=path,
                    error_message="目录不存在",
                )

            if not os.path.isdir(resolved_path):
                return FileOperationResult(
                    success=False,
                    operation="list",
                    path=path,
                    error_message="路径不是目录",
                )

            entries = []
            for entry in sorted(os.listdir(resolved_path)):
                full_path = os.path.join(resolved_path, entry)
                if os.path.isdir(full_path):
                    entries.append(f"📁 {entry}/")
                else:
                    size = os.path.getsize(full_path)
                    entries.append(f"📄 {entry} ({size} bytes)")

            content = "\n".join(entries) if entries else "(空目录)"

            logger.info(f"列出目录成功: {resolved_path}")
            return FileOperationResult(
                success=True,
                operation="list",
                path=path,
                content=content,
            )

        except Exception as e:
            logger.error(f"列出目录失败: {resolved_path}, 错误: {e}")
            return FileOperationResult(
                success=False,
                operation="list",
                path=path,
                error_message=str(e),
            )

    def exists(self, path: str) -> FileOperationResult:
        resolved_path = self._resolve_path(path)
        exists = os.path.exists(resolved_path)
        return FileOperationResult(
            success=True,
            operation="exists",
            path=path,
            content=str(exists),
        )

    def get_info(self, path: str) -> FileOperationResult:
        is_safe, reason = self._check_security(path, "read")
        if not is_safe:
            return FileOperationResult(
                success=False,
                operation="info",
                path=path,
                error_message=reason,
            )

        resolved_path = self._resolve_path(path)

        try:
            if not os.path.exists(resolved_path):
                return FileOperationResult(
                    success=False,
                    operation="info",
                    path=path,
                    error_message="文件不存在",
                )

            stat = os.stat(resolved_path)
            is_dir = os.path.isdir(resolved_path)

            info = {
                "path": resolved_path,
                "type": "directory" if is_dir else "file",
                "size": stat.st_size,
                "modified": stat.st_mtime,
                "created": stat.st_ctime,
            }

            if not is_dir:
                info["format"] = self._detect_format(path).value

            content = json.dumps(info, indent=2, ensure_ascii=False)

            return FileOperationResult(
                success=True,
                operation="info",
                path=path,
                content=content,
                file_size=stat.st_size,
            )

        except Exception as e:
            logger.error(f"获取文件信息失败: {resolved_path}, 错误: {e}")
            return FileOperationResult(
                success=False,
                operation="info",
                path=path,
                error_message=str(e),
            )

    def str_replace(self, path: str, old_str: str, new_str: str) -> FileOperationResult:
        read_result = self.read(path)
        if not read_result.success:
            return FileOperationResult(
                success=False,
                operation="str_replace",
                path=path,
                error_message=read_result.error_message,
            )

        if old_str not in read_result.content:
            return FileOperationResult(
                success=False,
                operation="str_replace",
                path=path,
                error_message="未找到要替换的字符串",
            )

        new_content = read_result.content.replace(old_str, new_str, 1)
        return self.write(path, new_content)

    def insert_at_line(self, path: str, line_number: int, content: str) -> FileOperationResult:
        read_result = self.read(path)
        if not read_result.success:
            return FileOperationResult(
                success=False,
                operation="insert",
                path=path,
                error_message=read_result.error_message,
            )

        lines = read_result.content.split("\n")
        if line_number < 1 or line_number > len(lines) + 1:
            return FileOperationResult(
                success=False,
                operation="insert",
                path=path,
                error_message=f"行号超出范围: {line_number}",
            )

        lines.insert(line_number - 1, content)
        new_content = "\n".join(lines)
        return self.write(path, new_content)

    def read_json(self, path: str) -> tuple[bool, Optional[dict], Optional[str]]:
        result = self.read(path)
        if not result.success:
            return False, None, result.error_message

        try:
            data = json.loads(result.content)
            return True, data, None
        except json.JSONDecodeError as e:
            return False, None, f"JSON 解析错误: {e}"

    def write_json(self, path: str, data: dict, indent: int = 2) -> FileOperationResult:
        try:
            content = json.dumps(data, indent=indent, ensure_ascii=False)
            return self.write(path, content)
        except Exception as e:
            return FileOperationResult(
                success=False,
                operation="write_json",
                path=path,
                error_message=f"JSON 序列化错误: {e}",
            )
