from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .unified_context import UnifiedProjectContext

def parse_version_number(s: str) -> Optional[int]:
    """Parse a version number string (e.g., 'v1', '5') to an integer."""
    s = (s or "").strip().lower()
    if s.startswith("v"):
        s = s[1:]
    if not s.isdigit():
        return None
    try:
        return int(s)
    except Exception:
        return None

def resolve_diff_range(arg: str, versions: list) -> tuple[bool, Optional[int], Optional[int], bool, Optional[str]]:
    """
    Resolve the version range for a diff report based on input argument.
    Returns (success, from_vnum, to_vnum, show_current, error_key).
    """
    arg_lower = (arg or "").lower().strip()
    from_vnum: Optional[int] = None
    to_vnum: Optional[int] = None
    show_current = False

    if arg_lower in ("", "last"):
        if len(versions) >= 2:
            from_vnum = versions[-2].version_number
            to_vnum = versions[-1].version_number
        elif len(versions) == 1:
            from_vnum = versions[-1].version_number
            show_current = True
        else:
            return False, None, None, False, "diag_diff_no_bookmarks"
    elif arg_lower in ("current", "now"):
        if not versions:
            return False, None, None, False, "diag_diff_no_current"
        from_vnum = versions[-1].version_number
        show_current = True
    elif ".." in arg_lower:
        a, b = arg_lower.split("..", 1)
        from_vnum = parse_version_number(a)
        to_vnum = parse_version_number(b)
        if from_vnum is None or to_vnum is None:
            return False, None, None, False, "diag_diff_usage_error"
    else:
        v = parse_version_number(arg_lower)
        if v is None:
            return False, None, None, False, "diag_diff_usage_hint"
        from_vnum = v
        show_current = True

    return True, from_vnum, to_vnum, show_current, None

def filter_context_entries(ctx: "UnifiedProjectContext", from_vnum: Optional[int], to_vnum: Optional[int], show_current: bool = False):
    """Filter context entries based on version range."""
    from_v = ctx.get_version(from_vnum) if from_vnum is not None else None
    to_v = ctx.get_version(to_vnum) if to_vnum is not None else None

    if not from_v:
        return None, None, []

    def _entry_seq(e) -> int:
        try:
            return int(getattr(e, "seq", 0) or 0)
        except Exception:
            return 0

    start_seq = int(getattr(from_v, "last_seq", 0) or 0)
    end_seq = int(getattr(to_v, "last_seq", 0) or 0) if to_v else None

    entries = []
    if start_seq > 0:
        for e in ctx.entries:
            s = _entry_seq(e)
            if s <= start_seq:
                continue
            if end_seq is not None and s > end_seq:
                continue
            entries.append(e)
    else:
        start_idx = from_v.entry_count
        all_entries = ctx.entries
        if to_v is not None:
            end_idx = min(len(all_entries), to_v.entry_count)
            start_idx = min(max(start_idx, 0), end_idx)
            entries = list(all_entries[start_idx:end_idx])
        else:
            start_idx = min(max(start_idx, 0), len(all_entries))
            entries = list(all_entries[start_idx:])

    return from_v, to_v, entries
