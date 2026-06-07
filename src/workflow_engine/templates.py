"""Workflow template management — discover, load, save, and inject parameters."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .constants import (
    BUILTIN_TEMPLATES,
    GLOBAL_TEMPLATES_DIR,
    USER_WORKFLOW_DIR,
    WORKFLOW_GLOBAL_TEMPLATE_ALLOWLIST,
    WORKFLOW_TEMPLATES_DIR,
)
from .models import WorkflowMeta

logger = logging.getLogger(__name__)

# Path to bundled built-in templates (relative to this module)
_BUILTIN_TEMPLATES_DIR = Path(__file__).parent / "builtin_templates"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class TemplateInfo:
    """Describes a discovered workflow template."""

    name: str  # e.g. "code-audit"
    path: str  # full file path
    description: str  # from meta.description or ""
    scope: str  # "project" or "global"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _project_templates_dir(root_path: str) -> Path:
    """Return the project-level templates directory."""
    return Path(root_path) / WORKFLOW_TEMPLATES_DIR


def _global_templates_dir() -> Path:
    """Return the global templates directory (expanded from ~)."""
    return Path(os.path.expanduser(GLOBAL_TEMPLATES_DIR))


def _user_templates_dir(user_id: str) -> Path:
    """Return the user-level templates directory (namespaced by user_id).

    Each user gets their own directory to avoid cross-user template conflicts.
    """
    return Path(os.path.expanduser(USER_WORKFLOW_DIR.format(user_id=user_id)))


def _extract_meta_json(script_content: str) -> Optional[str]:
    """Extract the JSON-like object from `export const meta = {...}` in a JS script.

    Handles both single-line and multi-line meta declarations. Returns the raw
    JSON string or None if not found.
    """
    # Match `export const meta = { ... }` — greedy across newlines, stopping at
    # a closing brace followed by optional semicolon and newline/EOF.
    pattern = r"export\s+const\s+meta\s*=\s*(\{.*?\})\s*;?"

    match = re.search(pattern, script_content, re.DOTALL)
    if not match:
        return None

    raw = match.group(1)

    # Convert JS object literal to valid JSON:
    # 1. Unquoted keys -> quoted keys
    raw = re.sub(r"(?<=[{,])\s*(\w+)\s*:", r' "\1":', raw)
    # 2. Single quotes -> double quotes (for string values)
    raw = raw.replace("'", '"')
    # 3. Trailing commas before } or ]
    raw = re.sub(r",\s*([}\]])", r"\1", raw)

    return raw


def _template_info_from_file(file_path: Path, scope: str) -> TemplateInfo:
    """Build a TemplateInfo from a .js template file."""
    name = file_path.stem
    description = ""

    try:
        content = file_path.read_text(encoding="utf-8")
        meta = parse_template_meta(content)
        if meta is not None:
            description = meta.description
    except (OSError, PermissionError) as exc:
        logger.debug("Could not read template %s for meta: %s", file_path, repr(exc))

    return TemplateInfo(
        name=name,
        path=str(file_path),
        description=description,
        scope=scope,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Owner sidecar helpers (lightweight side-file ownership marker for templates)
# ---------------------------------------------------------------------------


def _owner_file_path(target_path: str) -> str:
    """Return the hidden owner-marker file path for a saved template file."""
    p = Path(target_path)
    return str(p.parent / f".{p.stem}.owner")


def _read_owner(target_path: str) -> str | None:
    """Read owner user_id from the sidecar owner file. Returns None if missing."""
    try:
        owner_file = Path(_owner_file_path(target_path))
        if owner_file.is_file():
            text = owner_file.read_text(encoding="utf-8").strip()
            return text or None
    except OSError:
        return None
    return None


def _write_owner(target_path: str, owner_id: str) -> None:
    """Write owner user_id to the sidecar owner file (best-effort)."""
    try:
        owner_file = Path(_owner_file_path(target_path))
        owner_file.parent.mkdir(parents=True, exist_ok=True)
        owner_file.write_text(owner_id, encoding="utf-8")
    except OSError as exc:
        logger.warning("Failed to write owner marker for %s: %s", target_path, repr(exc))


def _remove_owner(target_path: str) -> None:
    """Remove the owner marker alongside a deleted template (best-effort)."""
    try:
        owner_file = Path(_owner_file_path(target_path))
        if owner_file.is_file():
            owner_file.unlink()
    except OSError:
        pass


def can_delete_template(
    root_path: str,
    name: str,
    global_scope: bool,
    user_id: str | None,
    *,
    sender_id: str | None,
    admin_user_ids: list[str] | frozenset[str] | None = None,
) -> tuple[bool, str, str | None]:
    """Pre-flight authorization check for template deletion.

    Args:
        root_path: Project root directory.
        name: Template name (without .js extension).
        global_scope: Whether targeting global (admin) scope.
        user_id: Optional user-level namespace owner.
        sender_id: Current user requesting the delete.
        admin_user_ids: Admin user ID list (used for global scope + orphaned projects).

    Returns:
        (allowed, reason, target_path | None).

    Rules:
      - user-level: sender_id must equal user_id.
      - project-level: sender_id must equal the owner recorded in .<name>.owner,
        or sender must be an admin.
      - global-level: sender must be an admin.
      - built-in: never allowed (caller must check BUILTIN_TEMPLATES separately).
    """
    if name and name.endswith(".js"):
        name = name[:-3]
    ok, err = validate_template_name(name)
    if not ok:
        return False, err, None
    target_path, path_err = resolve_safe_template_path(
        root_path, name, global_scope=global_scope
    )
    if target_path is None:
        return False, path_err or "invalid target path", None
    scope = "user" if user_id else ("global" if global_scope else "project")
    if scope == "user":
        if not sender_id or sender_id != user_id:
            return False, "无权限：只能删除自己的用户级模板", target_path
        return True, "", target_path
    if scope == "global":
        if not is_admin_user(sender_id or "", admin_user_ids or []):
            return False, "无权限：删除全局模板需要管理员身份", target_path
        return True, "", target_path
    # project-level: owner or admin
    existing_owner = _read_owner(target_path)
    if existing_owner and sender_id == existing_owner:
        return True, "", target_path
    if is_admin_user(sender_id or "", admin_user_ids or []):
        return True, "", target_path
    if existing_owner is None:
        return False, "无权限：删除无主项目模板需要管理员身份", target_path
    return False, "无权限：项目模板只能由创建者或管理员删除", target_path


def validate_template_name(name: str) -> tuple[bool, str]:
    """校验模板名称是否合法。

    规则:
    - 禁止空字符串（去除空白后为空视为空）
    - 禁止包含路径分隔符： '/'  '\\'  '..'
    - 禁止以 '.' 开头或结尾（避免隐藏文件或后缀陷阱）
    - 仅允许字母、数字、下划线、短横线

    返回:
        (ok: bool, error_message: str)
        ok=True 时 error_message 为空；否则 error_message 是中文说明。
    """
    if name is None:
        return False, "模板名称为空"

    stripped = name.strip()
    if not stripped:
        return False, "模板名称为空，请输入有效的模板名称"

    # 路径遍历字符
    if "/" in stripped or "\\" in stripped:
        return False, "模板名称不能包含路径分隔符（/ 或 \\）"

    if ".." in stripped:
        return False, "模板名称不能包含 '..'"

    # 以 . 开头或结尾 — 规避隐藏文件 / 伪后缀
    if stripped.startswith("."):
        return False, "模板名称不能以 '.' 开头"
    if stripped.endswith("."):
        return False, "模板名称不能以 '.' 结尾"

    # 允许的字符: 字母、数字、下划线、短横线
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", stripped):
        return False, "模板名称只能包含字母、数字、短横线和下划线"

    return True, ""


def resolve_safe_template_path(
    root_path: str,
    name: str,
    *,
    global_scope: bool = False,
    user_id: str | None = None,
) -> tuple[str | None, str]:
    """计算并校验目标模板文件路径。

    目标文件必须位于一个受信任的模板根目录内部，用于避免路径遍历到
    非模板目录。

    作用域优先级（与 :func:`save_template` / :func:`delete_template` 一致）:
      - user_id: 保存到 ``~/.ghostap/workflows/{user_id}/``
      - global_scope=True: 保存到 ``~/.ghostap/workflows/``
      - 否则: 保存到 ``<root_path>/.ghostap/workflows/``

    流程:
    1. 规范化目标模板根目录（resolve）
    2. 拼接 name + ".js" 并 resolve
    3. 校验结果文件必须位于模板根目录内部

    返回:
        (absolute_path | None, error_message)
        成功时 error_message 为空；失败时 absolute_path 为 None。
    """
    try:
        if user_id:
            # Basic sanity: user_id is used as a path segment; refuse
            # obviously dangerous characters even though callers should
            # already validate it.
            if any(ch in user_id for ch in ("/", "\\", "..", ":", "\x00")):
                return None, "用户标识无效"
            root_dir = _user_templates_dir(user_id).resolve()
        elif global_scope:
            root_dir = _global_templates_dir().resolve()
        else:
            root_dir = _project_templates_dir(root_path).resolve()

        # 对名称进行基本清洗：去除可能的空白（validate_template_name 已排除
        # 路径分隔符，这里作为 defense-in-depth）
        clean_name = name.strip().replace("/", "").replace("\\", "").replace(":", "")
        if not clean_name:
            return None, "模板名称无效"

        target_file = (root_dir / f"{clean_name}.js").resolve()

        # 路径遍历防御：目标文件必须位于 root_dir 内部
        try:
            target_file.relative_to(root_dir)
        except ValueError:
            return None, "模板路径越界，禁止保存到模板目录之外"

        return str(target_file), ""
    except (OSError, ValueError) as exc:
        return None, f"无法解析模板路径: {exc}"


def is_admin_user(sender_id: str, admin_user_ids: list[str] | frozenset[str] | None) -> bool:
    """判断发送者是否属于管理员。

    Args:
        sender_id: 发送者 ID。为空时返回 False。
        admin_user_ids: 管理员 ID 列表；可为 list/frozenset/None。

    Returns:
        True 表示 sender_id 属于管理员。
    """
    if not sender_id:
        return False
    if not admin_user_ids:
        return False
    return sender_id in admin_user_ids


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def discover_templates(root_path: str, *, user_id: str | None = None) -> list[TemplateInfo]:
    """Discover available workflow templates from all directories.

    Priority (highest wins): user > project > global > built-in.
    User-level templates are namespaced per user and only visible to that user.

    Args:
        root_path: Project root directory.
        user_id: Optional user ID for user-level template lookup.

    Returns:
        Sorted list of TemplateInfo.
    """
    templates: dict[str, TemplateInfo] = {}

    # Built-in templates (lowest priority)
    if _BUILTIN_TEMPLATES_DIR.is_dir():
        for f in sorted(_BUILTIN_TEMPLATES_DIR.glob("*.js")):
            if f.is_file():
                info = _template_info_from_file(f, scope="builtin")
                templates[info.name] = info

    # Global templates (medium-low priority — admin-shared, allowlisted only)
    # Enforce WORKFLOW_GLOBAL_TEMPLATE_ALLOWLIST at discovery time so users
    # only see names they can actually resolve. This also keeps
    # discover_templates aligned with resolve_template_path, which gates on
    # the same allowlist — no "visible but unresolvable" names leak through.
    global_dir = _global_templates_dir()
    if global_dir.is_dir():
        try:
            for f in sorted(global_dir.glob("*.js")):
                if f.is_file():
                    stem = f.stem
                    if stem not in WORKFLOW_GLOBAL_TEMPLATE_ALLOWLIST:
                        logger.debug(
                            "Global template '%s' skipped: not in WORKFLOW_GLOBAL_TEMPLATE_ALLOWLIST",
                            stem,
                        )
                        continue
                    info = _template_info_from_file(f, scope="global")
                    templates[info.name] = info
        except PermissionError as exc:
            logger.warning("Cannot read global templates dir: %s", repr(exc))

    # Project templates (medium-high priority)
    project_dir = _project_templates_dir(root_path)
    if project_dir.is_dir():
        try:
            for f in sorted(project_dir.glob("*.js")):
                if f.is_file():
                    info = _template_info_from_file(f, scope="project")
                    templates[info.name] = info
        except PermissionError as exc:
            logger.warning("Cannot read project templates dir: %s", repr(exc))

    # User-level templates (highest priority — user-specific namespace)
    if user_id:
        user_dir = _user_templates_dir(user_id)
        if user_dir.is_dir():
            try:
                for f in sorted(user_dir.glob("*.js")):
                    if f.is_file():
                        info = _template_info_from_file(f, scope="user")
                        templates[info.name] = info
            except PermissionError as exc:
                logger.warning("Cannot read user templates dir: %s", repr(exc))

    return sorted(templates.values(), key=lambda t: t.name)


def load_template(root_path: str, name: str, *, user_id: str | None = None) -> Optional[str]:
    """Load a template by name (without .js extension).

    Search priority (highest first): user > project > global > built-in.

    Scope enforcement:
      - user-level: ~/.ghostap/workflows/{user_id}/  (only for this user_id)
      - project-level: <cwd>/.ghostap/workflows/     (always visible)
      - global-level:  ~/.ghostap/workflows/         (honors
        WORKFLOW_GLOBAL_TEMPLATE_ALLOWLIST)
      - built-in:      bundled with the application  (always visible)

    Safety: template names are validated against ``validate_template_name``
    and restricted to those returned by ``discover_templates`` before any
    file-system path is constructed. This prevents path-traversal payloads
    (e.g. ``../../etc/passwd``) from reaching the filesystem.

    Args:
        root_path: Project root directory.
        name: Template name (without .js extension).
        user_id: Optional user ID for user-level template lookup.

    Returns:
        File content or None if not found / invalid / forbidden.
    """
    # Sanitize: strip .js if caller accidentally includes it
    if name.endswith(".js"):
        name = name[:-3]

    # 1) Structural name validation (fail-closed).
    ok, reason = validate_template_name(name)
    if not ok:
        logger.warning("Rejected template name '%s': %s", name, reason)
        return None

    # 2) Only accept names that are discoverable on disk for the caller's
    #    scope. This prevents guessing names of files that were never meant
    #    to be loadable.
    try:
        available = {t.name for t in discover_templates(root_path, user_id=user_id)}
    except OSError as exc:
        logger.warning("Cannot enumerate templates: %s", repr(exc))
        return None
    if name not in available:
        logger.warning("Template '%s' is not discoverable at user_id=%s", name, user_id)
        return None

    # 3) Resolve path through the authoritative helper so the resolved
    #    path is guaranteed to live inside a template directory.
    resolved = resolve_template_path(root_path, name, user_id=user_id)
    if not resolved:
        return None

    try:
        with open(resolved, "r", encoding="utf-8") as f:
            return f.read()
    except (OSError, PermissionError) as exc:
        logger.error("Failed to read template '%s': %s", name, repr(exc))
        return None


def resolve_template_path(
    root_path: str,
    name: str,
    *,
    user_id: str | None = None,
) -> Optional[str]:
    """Resolve a template name to its absolute file path.

    Search order (highest first): user > project > global > built-in.

    Scope enforcement (same as :func:`load_template`):
      - user-level: ~/.ghostap/workflows/{user_id}/  (only when user_id given)
      - project-level: <root_path>/.ghostap/workflows/ (always visible)
      - global-level:  ~/.ghostap/workflows/         (honors
        WORKFLOW_GLOBAL_TEMPLATE_ALLOWLIST — names not in the allowlist are
        treated as if absent)
      - built-in:      bundled with the application  (always visible)

    Returns the absolute path if found, otherwise None.
    """
    if name.endswith(".js"):
        name = name[:-3]

    # 名称合法性前置校验 — 与 bridge.workflow / save_template / delete_template
    # 保持一致，避免 name 携带 '/' '\\' '..' 或为绝对路径。
    ok, err = validate_template_name(name)
    if not ok:
        logger.debug("resolve_template_path rejected name '%s': %s", name, err)
        return None

    # User-level (highest priority)
    if user_id:
        user_file = _user_templates_dir(user_id) / f"{name}.js"
        if user_file.is_file():
            return str(user_file.resolve())

    # Project-level
    project_file = _project_templates_dir(root_path) / f"{name}.js"
    if project_file.is_file():
        return str(project_file.resolve())

    # Global-level — require allowlist membership
    if name in WORKFLOW_GLOBAL_TEMPLATE_ALLOWLIST:
        global_file = _global_templates_dir() / f"{name}.js"
        if global_file.is_file():
            return str(global_file.resolve())
    else:
        logger.debug(
            "Global template '%s' skipped: not in WORKFLOW_GLOBAL_TEMPLATE_ALLOWLIST",
            name,
        )

    # Built-in
    builtin_file = _BUILTIN_TEMPLATES_DIR / f"{name}.js"
    if builtin_file.is_file():
        return str(builtin_file.resolve())

    return None


def save_template(
    root_path: str,
    name: str,
    script_content: str,
    global_scope: bool = False,
    *,
    user_id: str | None = None,
    owner_id: str | None = None,
) -> str:
    """Save a workflow template to disk.

    Scope priority: user_id > global_scope > project.
    Built-in templates are protected by the BUILTIN_TEMPLATES whitelist and
    cannot be overwritten or deleted by users.

    安全:
    - 先调用 validate_template_name 进行名称校验
    - 再使用 resolve_safe_template_path 计算并校验目标路径
    - global_scope=True 时要求调用方先做 is_admin_user 检查

    Args:
        root_path: Project root directory.
        name: Template name (without .js extension).
        script_content: The JavaScript source to save.
        global_scope: If True and no user_id, save to global (admin) dir.
        user_id: If provided, save to user-specific namespace (highest priority).

    Returns:
        The absolute path of the saved file.

    Raises:
        PermissionError: If attempting to overwrite a built-in template.
        ValueError: If the template name or target path is invalid.
        OSError: If the file cannot be written.

    Owner tracking:
        If owner_id is supplied and the scope is project or global, a hidden
        sidecar file `.<name>.owner` is written alongside the template JS so
        that future deletions can be gated to the creator (or admins).
    """
    if name.endswith(".js"):
        name = name[:-3]

    # 名称合法性校验
    ok, err = validate_template_name(name)
    if not ok:
        raise ValueError(err)

    # Built-in template protection: cannot overwrite
    if name in BUILTIN_TEMPLATES:
        raise PermissionError(
            f"Template '{name}' is a built-in template and cannot be overwritten. "
            f"Use a different name to save your custom version."
        )

    # 目标路径安全解析（unified: user_id 优先于 global/project）
    target_path, path_err = resolve_safe_template_path(
        root_path, name, global_scope=global_scope, user_id=user_id
    )
    if target_path is None:
        raise ValueError(path_err)

    target_file = Path(target_path)
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text(script_content, encoding="utf-8")
    resolved_scope = "user" if user_id else ("global" if global_scope else "project")
    if owner_id and resolved_scope in ("project", "global"):
        _write_owner(str(target_file), owner_id)

    scope = "user" if user_id else ("global" if global_scope else "project")
    logger.info("Saved template '%s' (%s scope) to %s", name, scope, target_file)
    return str(target_file.resolve())


def delete_template(
    root_path: str,
    name: str,
    global_scope: bool = False,
    *,
    user_id: str | None = None,
) -> bool:
    """Delete a workflow template by name.

    Scope priority: user_id > global_scope > project.
    Built-in templates are protected and cannot be deleted.

    安全:
    - 先调用 validate_template_name 进行名称校验
    - 再使用 resolve_safe_template_path 计算并校验目标路径

    Args:
        root_path: Project root directory.
        name: Template name (without .js extension).
        global_scope: If True and no user_id, delete from global (admin) dir.
        user_id: If provided, delete from user-specific namespace.

    Returns:
        True if the file was deleted, False if not found.

    Raises:
        PermissionError: If attempting to delete a built-in template.
        ValueError: If the template name or target path is invalid.
    """
    if name.endswith(".js"):
        name = name[:-3]

    # 名称合法性校验
    ok, err = validate_template_name(name)
    if not ok:
        raise ValueError(err)

    # Built-in template protection: cannot delete
    if name in BUILTIN_TEMPLATES:
        raise PermissionError(
            f"Template '{name}' is a built-in template and cannot be deleted."
        )

    # 目标路径安全解析（unified: user_id 优先于 global/project）
    target_path, path_err = resolve_safe_template_path(
        root_path, name, global_scope=global_scope, user_id=user_id
    )
    if target_path is None:
        raise ValueError(path_err)

    target_file = Path(target_path)

    if target_file.is_file():
        try:
            target_file.unlink()
            _remove_owner(str(target_file))  # also clean up owner sidecar
            scope = "user" if user_id else ("global" if global_scope else "project")
            logger.info("Deleted template '%s' (%s scope) from %s", name, scope, target_file)
            return True
        except OSError as exc:
            logger.error("Failed to delete template %s: %s", target_file, repr(exc))
            return False

    return False


def inject_args(script_content: str, args: dict[str, Any]) -> str:
    """Replace `args.KEY` and `args['KEY']` patterns with actual values.

    String values are wrapped in double quotes; booleans, numbers, and null are
    inlined as JSON literals; objects/arrays are JSON-serialized.
    """
    if not args:
        return script_content

    result = script_content

    for key, value in args.items():
        serialized = _serialize_value(value)

        # Pattern 1: args.KEY (dot access)
        dot_pattern = re.compile(r"\bargs\." + re.escape(key) + r"\b")
        result = dot_pattern.sub(serialized, result)

        # Pattern 2: args['KEY'] or args["KEY"] (bracket access)
        bracket_single = re.compile(r"\bargs\['" + re.escape(key) + r"'\]")
        bracket_double = re.compile(r'\bargs\["' + re.escape(key) + r'"\]')
        result = bracket_single.sub(serialized, result)
        result = bracket_double.sub(serialized, result)

    return result


def parse_template_meta(script_content: str) -> Optional[WorkflowMeta]:
    """Extract and parse workflow metadata from a JS template script.

    Looks for `export const meta = { ... }` and parses the object into a
    WorkflowMeta model. Returns None if no meta block is found or parsing fails.
    """
    raw_json = _extract_meta_json(script_content)
    if raw_json is None:
        return None

    try:
        data = json.loads(raw_json)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.debug("Failed to parse meta JSON: %s", repr(exc))
        return None

    try:
        return WorkflowMeta.model_validate(data)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Failed to validate WorkflowMeta: %s", repr(exc))
        return None


# ---------------------------------------------------------------------------
# Value serialization for inject_args
# ---------------------------------------------------------------------------


def _serialize_value(value: Any) -> str:
    """Serialize a Python value into a JS-compatible inline literal."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        # Escape backslashes, double quotes, backticks, template expressions,
        # and newlines to prevent JS injection through user-supplied args.
        escaped = (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("`", "\\`")
            .replace("${", "\\${")
            .replace("\n", "\\n")
            .replace("\r", "\\r")
        )
        return f'"{escaped}"'
    # For complex types (list, dict), use JSON serialization
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        # Last resort: stringify
        return json.dumps(str(value))
