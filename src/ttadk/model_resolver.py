"""TTADK model-id resolution helpers.

This module is the focused entry point for resolving model ids, display names,
aliases, and conservative fallbacks.  It intentionally keeps the public result
shape compatible with ``src.ttadk.models``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ModelDescriptor:
    """Model descriptor used to map display names and aliases to real model ids."""

    model_id: str
    display_name: str = ""
    aliases: list[str] = field(default_factory=list)
    source: str = ""
    verified: bool = False


@dataclass
class ResolvedModelResult:
    """Result of resolving a user-supplied model name to a real model id."""

    tool_name: str
    input_name: str
    real_name: str
    source: str
    validated: bool = False
    warnings: list[str] = field(default_factory=list)


def normalize_model_key(name: object) -> str:
    try:
        s = str(name or "")
    except Exception:
        s = ""
    return s.strip().lower()


def _descriptor_parts(descriptor: object) -> tuple[str, str, list[str]]:
    try:
        if isinstance(descriptor, ModelDescriptor):
            return (
                str(descriptor.model_id or "").strip(),
                str(descriptor.display_name or "").strip(),
                [str(x).strip() for x in (descriptor.aliases or []) if str(x).strip()],
            )

        model_id = str(getattr(descriptor, "model_id", "") or getattr(descriptor, "name", "") or "").strip()
        display_name = str(
            getattr(descriptor, "display_name", "") or getattr(descriptor, "friendly_name", "") or ""
        ).strip()
        raw_aliases = getattr(descriptor, "aliases", None)
        aliases = [str(x).strip() for x in raw_aliases if str(x).strip()] if isinstance(raw_aliases, list) else []
        return model_id, display_name, aliases
    except Exception:
        return "", "", []


def build_model_id_index(descriptors: list[object]) -> tuple[dict[str, str], list[str]]:
    """Build a deterministic name/alias/display/model_id -> model_id index."""
    idx: dict[str, str] = {}
    warnings: list[str] = []

    for descriptor in descriptors or []:
        model_id, display_name, aliases = _descriptor_parts(descriptor)
        if not model_id:
            continue

        keys: list[str] = []
        for candidate in [model_id, display_name, *aliases]:
            key = normalize_model_key(candidate)
            if key and key not in keys:
                keys.append(key)

        for key in keys:
            previous = idx.get(key)
            if previous is None:
                idx[key] = model_id
            elif previous != model_id:
                warnings.append(f"model_alias_conflict:{key}:{previous}->{model_id}")

    return idx, warnings


def _descriptor_items(descriptors: list[object]) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for descriptor in descriptors or []:
        model_id, display_name, _aliases = _descriptor_parts(descriptor)
        if model_id:
            items.append((model_id, display_name))
    return items


def _find_display(descriptors: list[object], model_id: str) -> str:
    mid = str(model_id or "").strip()
    if not mid:
        return ""
    for candidate_id, display_name in _descriptor_items(descriptors):
        if candidate_id == mid:
            return display_name
    return ""


def _build_candidates(
    *,
    query: str,
    descriptors: list[object],
    index: dict[str, str],
    max_candidates: int,
) -> list[dict]:
    q = normalize_model_key(query)
    if not q:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for key, model_id in (index or {}).items():
        try:
            if q not in (key or "") or model_id in seen:
                continue
            seen.add(model_id)
            out.append({"model_id": model_id, "display": _find_display(descriptors, model_id)})
            if len(out) >= int(max_candidates or 20):
                break
        except Exception:
            continue
    return out


def resolve_model_id(
    *,
    tool_name: str,
    input_name: str,
    descriptors: list[object],
    allow_unknown_passthrough: bool = False,
    max_candidates: int = 20,
    is_model_token_fn=None,
) -> tuple[ResolvedModelResult, dict]:
    """Resolve user input to a real model id and return diagnostics."""
    tool = (tool_name or "").strip().lower()
    raw = (input_name or "").strip()
    descriptor_list = list(descriptors or [])
    idx, idx_warnings = build_model_id_index(descriptor_list)

    warnings: list[str] = list((idx_warnings or [])[:10])

    if not raw:
        result = ResolvedModelResult(
            tool_name=tool,
            input_name="",
            real_name="",
            source="unknown",
            validated=False,
            warnings=["missing_model_intent"],
        )
        return result, {
            "model_display": "",
            "resolution_source": "unknown",
            "resolution_reason": "empty_input",
            "candidates": [],
            "warnings": list(warnings),
        }

    key = normalize_model_key(raw)
    resolved = idx.get(key)
    if resolved:
        source = "exact" if normalize_model_key(resolved) == key else "friendly"
        reason = "model_id_hit" if source == "exact" else "friendly_or_alias_hit"
        result = ResolvedModelResult(
            tool_name=tool,
            input_name=raw,
            real_name=resolved,
            source=source,
            validated=True,
            warnings=list(warnings),
        )
        return result, {
            "model_display": _find_display(descriptor_list, resolved),
            "resolution_source": source,
            "resolution_reason": reason,
            "candidates": [],
            "warnings": list(warnings),
        }

    raw_key = normalize_model_key(raw)
    if raw_key:
        for model_id, display in _descriptor_items(descriptor_list):
            try:
                if normalize_model_key(model_id).startswith(raw_key) or (
                    display and normalize_model_key(display).startswith(raw_key)
                ):
                    result = ResolvedModelResult(
                        tool_name=tool,
                        input_name=raw,
                        real_name=model_id,
                        source="prefix",
                        validated=True,
                        warnings=list(warnings),
                    )
                    return result, {
                        "model_display": display or _find_display(descriptor_list, model_id),
                        "resolution_source": "prefix",
                        "resolution_reason": "prefix_match",
                        "candidates": [],
                        "warnings": list(warnings),
                    }
            except Exception:
                continue

        for model_id, display in _descriptor_items(descriptor_list):
            try:
                if raw_key in normalize_model_key(model_id) or (display and raw_key in normalize_model_key(display)):
                    result = ResolvedModelResult(
                        tool_name=tool,
                        input_name=raw,
                        real_name=model_id,
                        source="partial",
                        validated=True,
                        warnings=list(warnings),
                    )
                    return result, {
                        "model_display": display or _find_display(descriptor_list, model_id),
                        "resolution_source": "partial",
                        "resolution_reason": "partial_match",
                        "candidates": [],
                        "warnings": list(warnings),
                    }
            except Exception:
                continue

    is_model_token = is_model_token_fn or _default_is_model_token
    if allow_unknown_passthrough and is_model_token(raw):
        warnings2 = list(warnings) + ["unknown_model_passthrough"]
        result = ResolvedModelResult(
            tool_name=tool,
            input_name=raw,
            real_name=raw,
            source="passthrough",
            validated=False,
            warnings=warnings2,
        )
        return result, {
            "model_display": _find_display(descriptor_list, raw),
            "resolution_source": "passthrough",
            "resolution_reason": "token_passthrough",
            "candidates": [],
            "warnings": list(warnings2),
        }

    warnings2 = list(warnings) + ["unknown_model_input"]
    result = ResolvedModelResult(
        tool_name=tool,
        input_name=raw,
        real_name=raw,
        source="unknown",
        validated=False,
        warnings=warnings2,
    )
    return result, {
        "model_display": "",
        "resolution_source": "unknown",
        "resolution_reason": "no_index_match",
        "candidates": _build_candidates(
            query=raw,
            descriptors=descriptor_list,
            index=idx,
            max_candidates=max_candidates,
        ),
        "warnings": list(warnings2),
    }


def choose_best_available_model(*, input_model: str, available_models: list[str]) -> str | None:
    """Choose the best conservative fallback from available model ids."""
    intent = (input_model or "").strip()
    candidates = [str(x).strip() for x in (available_models or []) if str(x).strip()]
    if not intent or not candidates:
        return None

    seen: set[str] = set()
    unique: list[str] = []
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)

    for candidate in unique:
        if candidate == intent:
            return candidate
    for candidate in unique:
        if candidate.startswith(intent + "-"):
            return candidate
    for candidate in unique:
        if intent in candidate:
            return candidate

    try:
        match = re.search(r"([A-Za-z]+-[0-9]+\.[0-9]+)", intent)
        family = match.group(1) if match else ""
    except Exception:
        family = ""
    if family:
        for candidate in unique:
            if family in candidate and "ttadk" in candidate:
                return candidate
        for candidate in unique:
            if family in candidate:
                return candidate

    return None


def _default_is_model_token(name: str) -> bool:
    try:
        from .models import is_model_token

        return bool(is_model_token(name))
    except Exception:
        return False
