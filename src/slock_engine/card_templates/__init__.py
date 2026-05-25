"""Slock card templates package.

This package replaces the monolithic card_templates.py module.
All public build_* functions are re-exported here for backward compatibility.
"""

# Re-export common utilities (new canonical location)
# Re-export remaining build_* from the legacy monolithic module
# (excluding those already migrated to submodules above)
from ..card_templates_legacy import (  # noqa: F401
    _build_callback_button,
    _build_chat_multi_url,
    _build_slock_group_jump_button,
    _truncate_dynamic_label,
    build_agent_action_buttons,
    build_agent_message_card,
    build_agent_move_confirm_card,
    build_agent_move_departure_card,
    build_agent_move_notification_card,
    build_budget_warning_card,
    build_chitchat_hint_card,
    build_cmd_arg_error_card,
    build_conclusion_notification_card,
    build_confirm_cancel_card,
    build_conflict_escalation_card,
    build_console_card,
    build_crash_recovery_card,
    build_discussion_card,
    build_discussion_card_from_thread,
    build_discussion_expand_card,
    build_discussion_history_card,
    build_discussion_summary_card,
    build_discussion_summary_card_from_thread,
    build_dissolve_confirm_card,
    build_dissolve_undo_card,
    build_error_suggestion_card,
    build_memory_display_card,
    build_memory_manage_card,
    build_nli_feedback_card,
    build_queue_full_card,
    build_queue_waiting_card,
    build_review_degradation_card,
    build_role_arg_error_card,
    build_role_switch_card,
    build_status_refresh_card,
    build_team_created_card,
    build_team_list_card,
    build_transfer_suggestion_card,
)
from .command import (  # noqa: F401
    build_command_hub_card,
    build_command_panel_card,
    build_command_panel_extended_card,
)
from .common import (  # noqa: F401
    AGENT_STATUS_BG_COLOR_MAP,
    COUNCIL_STATUS_LABEL_ZH,
    DISPLAY_TZ,
    STATUS_BG_STYLE_MAP,
    STATUS_ICON_MAP,
    STATUS_LABEL_ZH,
    TASK_STATUS_BG_COLOR_MAP,
    TASK_STATUS_ICONS,
    TASK_STATUS_LABEL_ZH,
    build_callback_button,
    build_card_wrapper,
    build_chat_multi_url,
    build_collapsible_panel,
    build_slock_group_jump_button,
    truncate_dynamic_label,
)
from .council import (  # noqa: F401
    build_council_card,
    build_council_detail_card,
    build_council_expandable_card,
    build_council_result_card,
)
from .discussion import (  # noqa: F401
    build_discussion_conclusion_card,
    build_discussion_history_list_card,
    build_discussion_live_card,
)
from .escalation import (  # noqa: F401
    build_escalation_card,
    build_resolved_escalation_card,
)
from .memory import build_memory_group_card  # noqa: F401
from .progress import (  # noqa: F401
    build_collaboration_plan_card,
    build_progress_overview_card,
    build_task_overview_card,
)
from .queue_feedback import (  # noqa: F401
    build_activation_confirm_card,
    build_clarification_confirmed_card,
    build_clarification_ignored_card,
    build_queue_wait_card,
    build_result_card,
    build_retry_swap_card,
    build_timeout_notify_card,
)

# Migrated submodule cards
from .role import build_role_info_card, build_role_list_card  # noqa: F401

# New canonical status panel card (migrated from legacy)
from .status import build_status_panel_card  # noqa: F401
from .task import build_task_board_card  # noqa: F401
from .welcome import build_welcome_card  # noqa: F401

# Backward-compatible aliases: old underscore-prefixed names → new public names
_DISPLAY_TZ = DISPLAY_TZ
_STATUS_LABEL_ZH = STATUS_LABEL_ZH
_TASK_STATUS_LABEL_ZH = TASK_STATUS_LABEL_ZH
_STATUS_ICON_MAP = STATUS_ICON_MAP
_TASK_STATUS_ICONS = TASK_STATUS_ICONS
_TASK_STATUS_BG_COLOR_MAP = TASK_STATUS_BG_COLOR_MAP
_AGENT_STATUS_BG_COLOR_MAP = AGENT_STATUS_BG_COLOR_MAP
_COUNCIL_STATUS_LABEL_ZH = COUNCIL_STATUS_LABEL_ZH
