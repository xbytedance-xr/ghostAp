"""Helpers for turning raw tool payloads into card-safe display text."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

_MAX_LABEL_CHARS = 80
_JSON_EDGE_LINES = {"{", "}", "[", "]"}
_AGENT_TOOL_TITLES = {"agent", "subagent", "task"}
_OPAQUE_CALL_ID_RE = re.compile(r"(?<!\w)call_[A-Za-z0-9_-]+", re.IGNORECASE)
_LITERAL_ESCAPE_RE = re.compile(r"""\\(?:["'nrtbfv0])""")
_CONTROL_SEPARATOR_RE = re.compile(r"(?:\\[nrtbfv0]|[\x00-\x1f\x7f])")
_INLINE_STRUCTURED_RE = re.compile(
    r"""[{\[]\s*["'][^"']+["']\s*:""",
    re.IGNORECASE,
)
_MALFORMED_ARRAY_RE = re.compile(
    r"""^\[\s*(?:\{|\[|["'])"""
)
_CODE_FRAGMENT_RE = re.compile(
    r"(?:"
    r"^\s*(?:assert|class|def|elif|else|except|for|from|if|import|lambda|raise|return|try|while|with)\b"
    r"|\b(?:is\s+not|not\s+in)\b"
    r"|```"
    r"|==|!=|<=|>=|:=|=>|&&|\|\|"
    r")",
    re.IGNORECASE,
)

_READ_TYPES = {"read", "read_file", "cat", "head", "tail", "list", "ls", "tree"}
_SEARCH_TYPES = {"grep", "search", "find", "glob", "search_codebase"}
_EDIT_TYPES = {
    "write",
    "write_file",
    "edit",
    "edit_file",
    "multi_edit",
    "patch",
    "apply_patch",
    "apply_diff",
    "delete",
    "delete_file",
}
_RUN_TYPES = {"run", "exec", "execute", "shell", "bash", "command", "read"}


def summarize_tool_call_content(content: str, *, fallback: str = "", max_chars: int = _MAX_LABEL_CHARS) -> str:
    """Return concise readable text for a tool payload.

    Structured tool JSON often contains noisy metadata plus stdout/stderr. Cards
    need a human action summary instead of the raw payload.
    """
    text = str(content or "").strip()
    fallback = str(fallback or "").strip()
    if not text:
        return _truncate(fallback, max_chars)

    parsed = _parse_json(text)
    if parsed is not None:
        summary = _describe_json_payload(parsed) or fallback
        return _truncate(_first_display_line(summary) or fallback, max_chars)

    first_line = _first_display_line(text)
    return _truncate(first_line or fallback, max_chars)


def sanitize_tool_event_content(content: str, *, fallback: str = "") -> str:
    """Clean tool input/output before it enters renderable card state."""
    text = str(content or "").strip()
    parsed = _parse_json(text)
    if parsed is None:
        return text
    return summarize_tool_call_content(text, fallback=fallback, max_chars=160)


def extract_tool_call_label(
    tool_call: Any,
    *,
    generic_labels: Iterable[str] = (),
    fallback: str = "子任务",
    max_chars: int = 60,
) -> str:
    """Extract a task/subagent label without leaking structured JSON."""
    title = str(getattr(tool_call, "title", "") or "").strip()
    content = str(getattr(tool_call, "content", "") or "").strip()
    generic = {str(item or "").strip().lower() for item in generic_labels}

    label = summarize_tool_call_content(content, fallback="", max_chars=max_chars)
    if label and not is_unhelpful_display_label(label):
        return label

    if title and title.lower() not in generic and not is_unhelpful_display_label(title):
        return _truncate(title, max_chars)
    safe_fallback = str(fallback or "").strip()
    if is_unhelpful_display_label(safe_fallback):
        safe_fallback = "子任务"
    return _truncate(safe_fallback, max_chars)


def extract_agent_tool_name(
    tool_call: Any,
    *,
    fallback: str = "子代理",
    max_chars: int = 24,
) -> str:
    """Extract a concise agent identity without exposing source fragments."""
    content = str(getattr(tool_call, "content", "") or "").strip()
    marker = "子代理："
    for line in content.splitlines():
        if marker not in line:
            continue
        candidate = line.split(marker, 1)[1].strip()
        candidate = _CONTROL_SEPARATOR_RE.split(candidate, maxsplit=1)[0].strip()
        if (
            candidate
            and not _INLINE_STRUCTURED_RE.search(candidate)
            and not is_unhelpful_display_label(candidate)
        ):
            return _truncate(candidate, max_chars)

    title = str(getattr(tool_call, "title", "") or "").strip()
    if (
        title
        and not _INLINE_STRUCTURED_RE.search(title)
        and not is_unhelpful_display_label(title)
    ):
        normalized_title = title.lower()
        return _truncate(normalized_title if normalized_title in _AGENT_TOOL_TITLES else title, max_chars)

    safe_fallback = str(fallback or "").strip()
    if is_unhelpful_display_label(safe_fallback):
        safe_fallback = "子代理"
    return _truncate(safe_fallback, max_chars)


def is_unhelpful_display_label(value: str) -> bool:
    """Whether a display label is just JSON syntax or empty noise."""
    text = str(value or "").strip()
    if not text:
        return True
    if text in _JSON_EDGE_LINES:
        return True
    if _OPAQUE_CALL_ID_RE.search(text):
        return True
    if _LITERAL_ESCAPE_RE.search(text):
        return True
    if text.startswith(("{", "[")) and _parse_json(text) is not None:
        return True
    if text.startswith("{") or _MALFORMED_ARRAY_RE.search(text):
        return True
    if _INLINE_STRUCTURED_RE.search(text):
        return True
    if _CODE_FRAGMENT_RE.search(text):
        return True
    return False


def _parse_json(text: str) -> Any | None:
    if not text or text[0] not in "{[":
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _describe_json_payload(data: Any) -> str:
    if isinstance(data, Sequence) and not isinstance(data, (str, bytes, bytearray)):
        for item in data:
            desc = _describe_json_payload(item)
            if desc:
                return desc
        return ""

    if not isinstance(data, Mapping):
        return ""

    parsed_cmd = data.get("parsed_cmd")
    if isinstance(parsed_cmd, Sequence) and not isinstance(parsed_cmd, (str, bytes, bytearray)):
        for item in parsed_cmd:
            if isinstance(item, Mapping):
                desc = _describe_parsed_cmd(item)
                if desc:
                    return desc

    for key in ("description", "summary", "query", "content", "name"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    path = _first_string(data, ("path", "file_path", "file", "directory", "dir"))
    if path:
        return f"读取 {path}"

    command = _command_text(data.get("command") or data.get("cmd"))
    if command:
        return f"运行 {command}"
    return ""


def _describe_parsed_cmd(item: Mapping[str, Any]) -> str:
    cmd_type = str(item.get("type") or "").strip().lower()
    path = _first_string(item, ("path", "file_path", "file", "directory", "dir", "name"))
    query = _first_string(item, ("query", "pattern", "keyword"))
    command = _command_text(item.get("cmd") or item.get("command"))

    if cmd_type in _SEARCH_TYPES:
        target = " · ".join(part for part in (query, path) if part)
        return f"搜索 {target}" if target else (f"运行 {command}" if command else "")
    if cmd_type in _EDIT_TYPES:
        return f"编辑 {path}" if path else (f"运行 {command}" if command else "")
    if cmd_type in _READ_TYPES:
        return f"读取 {path}" if path else (f"运行 {command}" if command else "")
    if cmd_type in _RUN_TYPES or command:
        return f"运行 {command}" if command else ""
    return path or command


def _first_string(data: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _command_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        parts = [str(part).strip() for part in value if str(part).strip()]
        if "-lc" in parts:
            idx = parts.index("-lc")
            if idx + 1 < len(parts):
                return parts[idx + 1]
        return " ".join(parts)
    return ""


def _first_display_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and stripped not in _JSON_EDGE_LINES:
            return stripped
    return ""


def _truncate(value: str, max_chars: int) -> str:
    value = str(value or "").strip()
    if max_chars <= 0 or len(value) <= max_chars:
        return value
    return value[: max_chars - 1] + "…"
