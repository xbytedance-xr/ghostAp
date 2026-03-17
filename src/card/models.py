from dataclasses import dataclass, field
from typing import Optional

@dataclass
class DeepCardState:
    """State object for building deep engine cards, avoiding parameter explosion."""
    title: str = ""
    content: str = ""
    progress_bar: Optional[str] = None
    deep_project_id: Optional[str] = None
    is_executing: bool = False
    is_paused: bool = False
    engine_name: str = "Coco"
    show_buttons: bool = True
    working_dir: Optional[str] = None
    status_line: Optional[str] = None
    duration_line: Optional[str] = None
    criteria_section: Optional[str] = None
    footer_note: Optional[str] = None
    compact: bool = False
    expanded: bool = False
    expand_ac: bool = False
    action_prefix: str = "deep"
    # Optional additional buttons (e.g., retry/recover). Each item should be a Feishu button element.
    extra_buttons: Optional[list[dict]] = None
