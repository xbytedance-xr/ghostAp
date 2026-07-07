"""Shared, pure model-cascade rendering helpers.

This module is the single source of truth for turning a flat model list
(e.g. Traex's ~90 models) into a "family × dimension" cascade of Feishu
``select_static`` dropdowns. It was originally implemented inline inside the
Workflow (``/wf``) selection flow; the algorithm lives here so both the
Workflow cascade and the normal programming-mode ACP model selection can
reuse it without duplicating the fiddly token-splitting / grouping logic.

Everything here is pure: no Feishu I/O, no session state. Callers supply the
action names and a ``value_builder`` callback, so the same element builder can
emit Workflow-shaped or normal-mode-shaped button payloads.

Key capabilities:

* :func:`split_model_variant` / :func:`dimensions_from_tokens` — decode a
  model name like ``openrouter-3o/max/high`` into ``base`` + ``(profile,
  effort)`` dimensions, while leaving plain names like
  ``anthropic/claude-sonnet`` untouched.
* :func:`build_model_groups` — group models by base into
  ``{key, label, variants[...]}``.
* :func:`resolve_default_selection` — **reverse-solve** the default selected
  group/profile/effort from a ``current_model`` string. This is what powers
  "remember the last chosen model": the cascade opens pre-selected on the
  user's previous choice instead of always the first list item.
* :func:`build_model_cascade_elements` — assemble the group/profile/effort
  ``select_static`` dropdowns plus a confirm button and an optional default
  button, using caller-provided action names and a ``value_builder``.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Optional

# Tokens that, when trailing a ``a/b/c`` model name, are treated as
# variant dimensions rather than part of the base name.
VARIANT_TOKENS = {"low", "medium", "high", "xhigh", "max"}
PROFILE_ORDER = {"standard": 0, "max": 1}
EFFORT_ORDER = {"default": 0, "low": 1, "medium": 2, "high": 3, "xhigh": 4, "max": 5}

_SELECT_LABEL_MAX_CHARS = 72
_BUTTON_LABEL_MAX_CHARS = 40

# A ``value_builder`` receives the target action name and a dict of extra
# fields and must return the final button ``value`` dict.
ValueBuilder = Callable[[str, dict[str, Any]], dict[str, Any]]


def split_model_variant(model_name: str) -> tuple[str, tuple[str, ...]]:
    """Split ``base/variant/tokens`` into ``(base, trailing_variant_tokens)``.

    Only trailing segments that belong to :data:`VARIANT_TOKENS` are peeled
    off. Plain names (no ``/`` or no variant suffix) are returned unchanged
    with an empty token tuple.
    """
    parts = [p for p in str(model_name or "").split("/") if p]
    if len(parts) <= 1:
        return str(model_name or ""), ()
    suffix: list[str] = []
    while parts and parts[-1].lower() in VARIANT_TOKENS:
        suffix.insert(0, parts.pop())
    if not suffix or not parts:
        return str(model_name or ""), ()
    return "/".join(parts), tuple(suffix)


def dimensions_from_tokens(tokens: tuple[str, ...]) -> tuple[str, str]:
    """Map trailing variant tokens to ``(profile, effort)`` dimensions."""
    lowered = tuple(t.lower() for t in tokens)
    if not lowered:
        return "standard", "default"
    if len(lowered) == 1:
        if lowered[0] == "max":
            return "max", "default"
        return "standard", lowered[0]
    return lowered[0], "/".join(lowered[1:])


def build_model_groups(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group a flat model list into base families with variants.

    Each group is ``{"key": base, "label": str, "variants": [
    {"name", "display_name", "profile", "effort", "tokens"}, ...]}``.
    Insertion order of first appearance is preserved.
    """
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for model in models or []:
        name = str(model.get("name") or "").strip()
        if not name:
            continue
        display = str(model.get("display_name") or name).strip() or name
        base, tokens = split_model_variant(name)
        profile, effort = dimensions_from_tokens(tokens)
        if base not in groups:
            groups[base] = {
                "key": base,
                "label": base if tokens else display,
                "variants": [],
            }
            order.append(base)
        elif tokens:
            groups[base]["label"] = base
        groups[base]["variants"].append({
            "name": name,
            "display_name": display,
            "profile": profile,
            "effort": effort,
            "tokens": tokens,
        })
    return [groups[key] for key in order]


def ordered_unique(values: Iterable[Any], *, kind: str) -> list[str]:
    """Return unique non-empty values ordered by profile/effort ranking."""
    result: list[str] = []
    for value in values:
        text = str(value or "")
        if text and text not in result:
            result.append(text)
    order = PROFILE_ORDER if kind == "profile" else EFFORT_ORDER
    return sorted(result, key=lambda x: (order.get(x, 50), x))


def choose_variant(
    variants: list[dict[str, Any]],
    *,
    profile: str,
    effort: str,
) -> Optional[dict[str, Any]]:
    """Pick the variant matching ``profile``+``effort``, with fallbacks."""
    for variant in variants:
        if variant["profile"] == profile and variant["effort"] == effort:
            return variant
    for variant in variants:
        if variant["profile"] == profile:
            return variant
    return variants[0] if variants else None


def resolve_default_selection(
    groups: list[dict[str, Any]],
    current_model: Optional[str],
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Reverse-solve default ``(group_key, profile, effort)`` from a model name.

    This implements "remember the last chosen model": given the project's
    stored ``current_model`` (e.g. ``openrouter-3o/max/high``), locate the
    group whose variant list contains it and return that variant's
    group/profile/effort so the cascade opens pre-selected.

    Returns ``(None, None, None)`` when ``current_model`` is empty or not
    present in any group's variants (caller should fall back to the first
    list item).
    """
    name = str(current_model or "").strip()
    if not name or not groups:
        return None, None, None
    for group in groups:
        for variant in group.get("variants", []):
            if str(variant.get("name")) == name:
                return group["key"], variant["profile"], variant["effort"]
    return None, None, None


def _clamp(text: str, limit: int) -> str:
    label = str(text or "").strip()
    if len(label) <= limit:
        return label
    return label[: limit - 1].rstrip() + "…"


def _select_option(label: str, value: str) -> dict[str, Any]:
    return {
        "text": {"tag": "plain_text", "content": _clamp(label, _SELECT_LABEL_MAX_CHARS)},
        "value": value,
    }


def _select_static(
    *,
    placeholder: str,
    value: dict[str, Any],
    options: list[dict[str, Any]],
    initial_option: Optional[str] = None,
) -> dict[str, Any]:
    select: dict[str, Any] = {
        "tag": "select_static",
        "placeholder": {"tag": "plain_text", "content": placeholder},
        "value": value,
        "options": options,
    }
    if initial_option:
        select["initial_option"] = initial_option
    return select


def _profile_label(profile: str) -> str:
    return profile


def _effort_label(effort: str) -> str:
    return effort


def has_cascade_variants(models: list[dict[str, Any]]) -> bool:
    """Return True when at least one model exposes splittable variant tokens.

    Callers use this to decide whether the cascade UI adds value or whether a
    plain button list (single-group / few models) is the better fit.
    """
    for model in models or []:
        _base, tokens = split_model_variant(str(model.get("name") or ""))
        if tokens:
            return True
    return False


def build_model_cascade_elements(
    *,
    models: list[dict[str, Any]],
    value_builder: ValueBuilder,
    group_action: str,
    profile_action: str,
    effort_action: str,
    select_action: str,
    default_action: Optional[str] = None,
    pending_group: Optional[str] = None,
    pending_profile: Optional[str] = None,
    pending_effort: Optional[str] = None,
    current_model: Optional[str] = None,
    button_row_builder: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
    default_label: str = "默认模型（推荐）",
    confirm_label_prefix: str = "确认此模型",
    empty_hint: str = "_未配置额外模型列表；请选择默认模型。_",
) -> list[dict[str, Any]]:
    """Assemble cascade dropdown elements for a model list.

    Pure element builder shared by Workflow and normal ACP model selection.

    Selection precedence for the initially-selected group/profile/effort:

    1. ``pending_*`` (explicit in-flight user choice carried in button value)
    2. reverse-solved from ``current_model`` (remember last chosen model)
    3. first list item (fallback)

    ``value_builder(action, extra)`` produces the caller-specific button
    ``value`` dict; ``button_row_builder(buttons)`` lays out a button row
    (e.g. ``build_responsive_button_row`` / ``build_responsive_layout``).
    """
    elements: list[dict[str, Any]] = []

    # Optional "default model" shortcut button.
    if default_action:
        default_value = value_builder(default_action, {"use_default_model": True})
        elements.extend(
            button_row_builder([
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": default_label},
                    "type": "primary" if not current_model else "default",
                    "value": default_value,
                    "behaviors": [{"type": "callback", "value": default_value}],
                }
            ])
        )

    groups = build_model_groups(models)
    if not groups:
        elements.append({"tag": "markdown", "content": empty_hint})
        return elements

    # Reverse-solve default selection from current_model (remember last model).
    default_group, default_profile, default_effort = resolve_default_selection(groups, current_model)

    group_keys = [g["key"] for g in groups]
    if pending_group in group_keys:
        selected_group_key = pending_group
    elif default_group in group_keys:
        selected_group_key = default_group
    else:
        selected_group_key = group_keys[0]
    selected_group = next(g for g in groups if g["key"] == selected_group_key)

    group_value = value_builder(group_action, {})
    elements.append({"tag": "markdown", "content": "**模型族**"})
    elements.append(
        _select_static(
            placeholder="选择模型",
            value=group_value,
            options=[_select_option(g["label"], g["key"]) for g in groups],
            initial_option=selected_group_key,
        )
    )

    variants = list(selected_group["variants"])
    profiles = ordered_unique((v["profile"] for v in variants), kind="profile")
    # Only honor default profile/effort when they belong to the resolved group.
    profile_default = default_profile if selected_group_key == default_group else None
    if pending_profile in profiles:
        selected_profile = pending_profile
    elif profile_default in profiles:
        selected_profile = profile_default
    else:
        selected_profile = profiles[0]

    if len(profiles) > 1:
        profile_value = value_builder(profile_action, {"model_group": selected_group_key})
        elements.append({"tag": "markdown", "content": "**Profile**"})
        elements.append(
            _select_static(
                placeholder="选择 Profile",
                value=profile_value,
                options=[_select_option(_profile_label(p), p) for p in profiles],
                initial_option=selected_profile,
            )
        )

    profile_variants = [v for v in variants if v["profile"] == selected_profile]
    efforts = ordered_unique((v["effort"] for v in profile_variants), kind="effort")
    effort_default = (
        default_effort
        if (selected_group_key == default_group and selected_profile == default_profile)
        else None
    )
    if pending_effort in efforts:
        selected_effort = pending_effort
    elif effort_default in efforts:
        selected_effort = effort_default
    else:
        selected_effort = efforts[0]

    if len(efforts) > 1:
        effort_value = value_builder(
            effort_action,
            {"model_group": selected_group_key, "model_profile": selected_profile},
        )
        elements.append({"tag": "markdown", "content": "**Effort**"})
        elements.append(
            _select_static(
                placeholder="选择 Effort",
                value=effort_value,
                options=[_select_option(_effort_label(e), e) for e in efforts],
                initial_option=selected_effort,
            )
        )

    selected_variant = choose_variant(variants, profile=selected_profile, effort=selected_effort)
    if not selected_variant:
        selected_variant = variants[0]
    model_name = str(selected_variant["name"])
    confirm_value = value_builder(
        select_action,
        {
            "model_name": model_name,
            "name": model_name,
            "model_group": selected_group_key,
            "model_profile": selected_variant["profile"],
            "model_effort": selected_variant["effort"],
        },
    )
    elements.extend(
        button_row_builder([
            {
                "tag": "button",
                "text": {
                    "tag": "plain_text",
                    "content": _clamp(f"{confirm_label_prefix}: {model_name}", _BUTTON_LABEL_MAX_CHARS),
                },
                "type": "primary",
                "value": confirm_value,
                "behaviors": [{"type": "callback", "value": confirm_value}],
            }
        ])
    )
    return elements
