"""TTADK-related command handlers extracted from SystemHandler (God Class split)."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from ...card import CardBuilder
from ...card.builders.system import SystemBuilder
from ...card.ui_text import UI_TEXT
from ...ttadk import get_ttadk_manager
from ...ttadk.manager import auto_update_ttadk
from ...utils.errors import get_error_detail
from ...utils.path import normalize_ttadk_cwd

if TYPE_CHECKING:
    from ...project import ProjectContext

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# TTADK flow-timing state size limit (kept as module constant so tests can
# reference it without reaching into instance internals).
# ---------------------------------------------------------------------------
_TTADK_FLOW_DURATION_MAX_SIZE = 200


class TTADKCommandsMixin:
    """Mixin providing TTADK tool/model selection, yolo toggle, and diagnostics.

    Intended for use with ``SystemHandler(... TTADKCommandsMixin, ..., BaseHandler)``.
    All ``self.*`` helper methods (``reply_text``, ``reply_card``, ``update_card``,
    ``send_card_to_chat``, ``send_text_to_chat``, ``settings``, ``ctx``, ``project_manager``, ``get_working_dir``,
    ``get_handler``, ``mode_manager``) are resolved via MRO from ``BaseHandler``.

    TTADK state fields (``_ttadk_flow_start_times``, ``_ttadk_flow_last_duration_ms``,
    ``_TTADK_FLOW_DURATION_MAX_SIZE``) are initialised in ``SystemHandler.__init__``.
    """

    def refresh_ttadk_models(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        """强制刷新 TTADK 当前工具的真实模型列表（优先 probe），并返回诊断摘要。"""
        manager = get_ttadk_manager()
        cwd = None
        try:
            raw_cwd = self._resolve_ttadk_cwd(chat_id, project=project)
            cwd = normalize_ttadk_cwd(raw_cwd)
            self._maybe_log_ttadk_cwd(where="SystemHandler.refresh_ttadk_models", raw_cwd=raw_cwd, normalized_cwd=cwd)
        except Exception:
            cwd = None

        tool = manager.get_current_tool() or ""
        try:
            result = manager.refresh_models(tool_name=tool or None, cwd=cwd)
        except Exception as e:
            self.reply_error(
                message_id, get_error_detail(e), title=UI_TEXT["system_ttadk_refresh_error"]
            )
            return

        msg_type, card_content = CardBuilder.build_ttadk_refresh_result_card(tool, result)
        self.reply_card(message_id, card_content)

    def _maybe_log_ttadk_cwd(self, *, where: str, raw_cwd: Optional[str], normalized_cwd: Optional[str]) -> None:
        """TTADK cwd 归一化的可观测日志（debug + 配置开关）。"""
        try:
            from ...config import get_settings

            if not bool(getattr(get_settings(), "ttadk_cwd_debug_enabled", False)):
                return
        except Exception:
            logger.debug("Failed to load ttadk_cwd_debug_enabled setting", exc_info=True)
            return
        if not logger.isEnabledFor(logging.DEBUG):
            return
        try:
            is_abs = bool(normalized_cwd) and Path(str(normalized_cwd)).is_absolute()
        except Exception:
            is_abs = False
        logger.debug(
            "[TTADK:CWD] where=%s raw_cwd=%r normalized_cwd=%r is_abs=%s",
            str(where or ""),
            raw_cwd,
            normalized_cwd,
            bool(is_abs),
        )

    def _resolve_ttadk_cwd(
        self,
        chat_id: str,
        project: Optional["ProjectContext"] = None,
        project_id: Optional[str] = None,
    ) -> Optional[str]:
        if project:
            return project.root_path
        if project_id:
            ctx = self.project_manager.get_project_for_chat(project_id, chat_id)
            if ctx:
                return ctx.root_path
        active = self.project_manager.get_active_project(chat_id)
        if active:
            return active.root_path
        return None

    def _resolve_ttadk_yolo_enabled(
        self,
        chat_id: str,
        project: Optional["ProjectContext"] = None,
        project_id: Optional[str] = None,
    ) -> bool:
        if project is not None:
            return bool(getattr(project, "ttadk_yolo_enabled", False))
        if project_id:
            ctx = self.project_manager.get_project_for_chat(project_id, chat_id)
            if ctx is not None:
                return bool(getattr(ctx, "ttadk_yolo_enabled", False))
        active = self.project_manager.get_active_project(chat_id)
        if active is not None:
            return bool(getattr(active, "ttadk_yolo_enabled", False))
        return bool(getattr(self.settings, "ttadk_yolo_default_enabled", False))

    def _apply_ttadk_yolo_enabled(
        self,
        chat_id: str,
        enabled: bool,
        project: Optional["ProjectContext"] = None,
        project_id: Optional[str] = None,
    ) -> Optional["ProjectContext"]:
        target = project
        if target is None and project_id:
            target = self.project_manager.get_project_for_chat(project_id, chat_id)
        if target is None:
            target = self.project_manager.get_active_project(chat_id)
        if target is not None:
            target.ttadk_yolo_enabled = bool(enabled)
        return target

    def _pick_ttadk_auto_model(
        self,
        models: list,
        *,
        project: Optional["ProjectContext"] = None,
        current_model: Optional[str] = None,
    ) -> Optional[str]:
        if not models:
            return None
        normalized = [m for m in models if getattr(m, "name", None)]
        if not normalized:
            return None
        model_names = {m.name: m for m in normalized}

        if project:
            project_model = str(getattr(project, "ttadk_model_name", "") or "").strip()
            if project_model and project_model in model_names:
                return project_model

        for model in normalized:
            if bool(getattr(model, "is_default", False)):
                return model.name

        settings_model = str(getattr(self.settings, "ttadk_default_model", "") or "").strip()
        if settings_model and settings_model in model_names:
            return settings_model

        if current_model and current_model in model_names:
            return current_model

        if len(normalized) == 1:
            return normalized[0].name
        return None

    def _pick_ttadk_auto_tool(
        self,
        tools: list,
        *,
        project: Optional["ProjectContext"] = None,
        current_tool: Optional[str] = None,
    ) -> Optional[str]:
        if not tools:
            return None
        normalized = [t for t in tools if getattr(t, "name", None)]
        if not normalized:
            return None
        tool_names = {t.name: t for t in normalized}

        if project:
            project_tool = str(getattr(project, "ttadk_tool_name", "") or "").strip().lower()
            if project_tool and project_tool in tool_names:
                return project_tool

        settings_tool = str(getattr(self.settings, "ttadk_default_tool", "") or "").strip().lower()
        if settings_tool and settings_tool in tool_names:
            return settings_tool

        if current_tool and current_tool in tool_names:
            return current_tool

        if len(normalized) == 1:
            return normalized[0].name
        return None

    def _mark_ttadk_flow_start(self, chat_id: str) -> None:
        self._ttadk_flow_start_times[chat_id] = time.perf_counter()

    def _report_ttadk_flow_duration(self, chat_id: str, project_id: Optional[str], where: str) -> None:
        start = self._ttadk_flow_start_times.pop(chat_id, None)
        if start is None:
            return
        elapsed = time.perf_counter() - start
        if elapsed > 600:  # 超过 10 分钟视为过期，丢弃不记录
            return
        duration_ms = int(round(elapsed * 1000))
        self._ttadk_flow_last_duration_ms[chat_id] = duration_ms
        if len(self._ttadk_flow_last_duration_ms) > self._TTADK_FLOW_DURATION_MAX_SIZE:
            self._ttadk_flow_last_duration_ms.popitem(last=False)  # 淘汰最旧条目
        logger.info(
            "ttadk_flow_duration_ms=%s chat_id=%s project_id=%s where=%s",
            duration_ms,
            chat_id,
            project_id,
            where,
        )

    def _reply_ttadk_load_hint(self, message_id: str, text: str, project_id: Optional[str] = None) -> None:
        msg_type, card_content = CardBuilder.build_ttadk_soft_failure_card_for(text, project_id=project_id)
        self.reply_card(message_id, card_content)

    def handle_ttadk_command(
        self,
        message_id: str,
        chat_id: str,
        project: Optional["ProjectContext"] = None,
        force_select: bool = False,
    ):
        project = project or self.project_manager.get_active_project(chat_id)
        project_id = project.project_id if project else None
        manager = get_ttadk_manager()

        auto_update_ttadk()

        self._mark_ttadk_flow_start(chat_id)

        result = manager.get_tools()
        if result.error:
            self._ttadk_flow_start_times.pop(chat_id, None)
            self._reply_ttadk_load_hint(
                message_id, UI_TEXT["system_ttadk_list_load_error"].format(error=result.error), project_id=project_id
            )
            return

        # Fetch models for each tool to build combined card
        try:
            raw_cwd = self._resolve_ttadk_cwd(chat_id, project=project, project_id=project_id)
            cwd = normalize_ttadk_cwd(raw_cwd)
        except Exception:
            cwd = None

        models_by_tool: dict[str, list] = {}
        for tool in (result.tools or []):
            try:
                prev_tool = manager.get_current_tool()
                if prev_tool != tool.name:
                    manager.set_tool(tool.name)
                models_result = manager.get_models(cwd=cwd)
                models_by_tool[tool.name] = models_result.models or []
                # Restore previous tool
                if prev_tool and prev_tool != tool.name:
                    manager.set_tool(prev_tool)
            except Exception:
                models_by_tool[tool.name] = []

        yolo_enabled = self._resolve_ttadk_yolo_enabled(chat_id, project=project, project_id=project_id)
        current_tool = project.ttadk_tool_name if project else None
        current_model = project.ttadk_model_name if project else None
        msg_type, card_content = CardBuilder.build_ttadk_combined_select_card(
            result.tools, models_by_tool, project_id, yolo_enabled=yolo_enabled, current_tool=current_tool, current_model=current_model
        )
        self.reply_card(message_id, card_content)

    def show_ttadk_info(self, message_id: str, chat_id: str):
        manager = get_ttadk_manager()
        current_tool = manager.get_current_tool()
        current_model = manager.get_current_model()
        tools_result = manager.get_tools()
        raw_cwd = self._resolve_ttadk_cwd(chat_id)
        norm_cwd = normalize_ttadk_cwd(raw_cwd)
        self._maybe_log_ttadk_cwd(where="SystemHandler.show_ttadk_info", raw_cwd=raw_cwd, normalized_cwd=norm_cwd)
        models_result = manager.get_models(cwd=norm_cwd)
        tool_desc = {t.name: t.description for t in (tools_result.tools or [])}
        model_desc = {m.name: m.description for m in (models_result.models or [])}

        content = SystemBuilder.build_ttadk_info_content(
            current_tool, current_model, tool_desc, model_desc
        )
        self.reply_text(message_id, content)

    def handle_select_ttadk_tool(self, message_id: str, chat_id: str, tool_name: str, project_id: Optional[str] = None):
        manager = get_ttadk_manager()
        project = self.project_manager.get_project_for_chat(project_id, chat_id) if project_id else self.project_manager.get_active_project(chat_id)
        try:
            raw_cwd = self._resolve_ttadk_cwd(chat_id, project_id=project_id)
            cwd = normalize_ttadk_cwd(raw_cwd)
            self._maybe_log_ttadk_cwd(
                where="SystemHandler.handle_select_ttadk_tool", raw_cwd=raw_cwd, normalized_cwd=cwd
            )
        except Exception:
            cwd = None
        logger.info(
            "[TTADK] 选择工具: chat_id=%s project_id=%s tool=%s cwd=%s",
            chat_id,
            project_id,
            tool_name,
            cwd,
        )
        success = manager.set_tool(tool_name)
        if not success:
            self._reply_ttadk_load_hint(
                message_id, UI_TEXT["system_ttadk_switch_tool_error"].format(tool=tool_name), project_id=project_id
            )
            return
        if project:
            project.ttadk_tool_name = tool_name
            current_model = manager.get_current_model()
            if current_model:
                project.ttadk_model_name = current_model

        result = manager.get_models(cwd=cwd)
        if result.error:
            self._reply_ttadk_load_hint(
                message_id, UI_TEXT["system_ttadk_model_load_error"].format(error=result.error), project_id=project_id
            )
            return

        # 只有在模型列表为空且有警告时才发送单独的警告消息
        # 其他情况（如 official_cli_disabled）不影响使用，不单独发送
        warnings = getattr(result, "warnings", None) or []
        has_models = bool(result.models)
        critical_warnings = [w for w in warnings if w in ("models_untrusted", "missing_tool")]

        if (not has_models and warnings) or critical_warnings:
            # 模型列表为空且有警告，或者有严重警告（如 models_untrusted），发送警告消息
            w_str = "; ".join(critical_warnings if critical_warnings else warnings)
            msg = UI_TEXT["system_ttadk_model_warning"].format(
                warnings=w_str
            )
            self.reply_text(message_id, msg)

        yolo_enabled = self._resolve_ttadk_yolo_enabled(chat_id, project=project, project_id=project_id)
        current_model = project.ttadk_model_name if project else None
        msg_type, card_content = CardBuilder.build_ttadk_model_select_card(
            result.models, tool_name, project_id, yolo_enabled=yolo_enabled, current_model=current_model
        )
        patched = self.update_card(message_id, card_content)
        if not patched:
            self.reply_card(message_id, card_content)

    def handle_select_ttadk_model(
        self,
        message_id: str,
        chat_id: str,
        tool_name: str,
        model_name: str,
        project: Optional["ProjectContext"] = None,
        silent: bool = False,
    ):
        project_id = project.project_id if project else None
        if not silent:
            # 立即给予用户反馈，避免"没反应"
            msg_type, card_content = CardBuilder.build_switching_status_card(tool_name, model_name)
            self.reply_card(message_id, card_content)

        manager = get_ttadk_manager()
        logger.info(
            "[TTADK] 选择模型: chat_id=%s project_id=%s tool=%s model=%s",
            chat_id,
            getattr(project, "project_id", None),
            tool_name,
            model_name,
        )
        success = manager.set_model(model_name)
        if not success:
            self._reply_ttadk_load_hint(
                message_id, UI_TEXT["system_ttadk_switch_model_error"].format(model=model_name), project_id=project_id
            )
            return

        target_project = project or self.project_manager.get_active_project(chat_id)
        if target_project:
            target_project.ttadk_tool_name = tool_name or manager.get_current_tool()
            target_project.ttadk_model_name = model_name

        ttadk_handler = self.get_handler("ttadk")
        if ttadk_handler:
            ttadk_handler.current_tool = tool_name
            ttadk_handler.current_model = model_name
            ttadk_handler.enter_mode(message_id, chat_id, project=target_project)
            project_id = target_project.project_id if target_project else None
            self._report_ttadk_flow_duration(chat_id, project_id, "enter_mode")
        else:
            self.reply_error(message_id, UI_TEXT["system_ttadk_handler_uninitialized"])

    def handle_select_ttadk_combined(
        self,
        message_id: str,
        chat_id: str,
        tool_name: str,
        model_name: str,
        project: Optional["ProjectContext"] = None,
    ):
        """Handle the combined tool+model selection from the single-step card."""
        manager = get_ttadk_manager()
        project = project or self.project_manager.get_active_project(chat_id)
        project_id = project.project_id if project else None

        # Set tool first
        tool = (tool_name or "").strip().lower()
        model = (model_name or "").strip()
        if not tool or not model:
            self.reply_error(message_id, UI_TEXT["system_ttadk_no_tool"])
            return

        success = manager.set_tool(tool)
        if not success:
            self._reply_ttadk_load_hint(
                message_id, UI_TEXT["system_ttadk_switch_tool_error"].format(tool=tool), project_id=project_id
            )
            return

        if project:
            project.ttadk_tool_name = tool

        # Then delegate to the existing model selection handler
        self.handle_select_ttadk_model(
            message_id, chat_id, tool, model, project=project, silent=False
        )

    def handle_select_ttadk_combined_tool(
        self,
        message_id: str,
        chat_id: str,
        tool_name: str,
        project: Optional["ProjectContext"] = None,
    ):
        """Handle tool selection from the combined TTADK card (tool select_static component)."""
        manager = get_ttadk_manager()
        project = project or self.project_manager.get_active_project(chat_id)
        project_id = project.project_id if project else None

        tool = (tool_name or "").strip().lower()
        if not tool:
            return

        success = manager.set_tool(tool)
        if not success:
            self._reply_ttadk_load_hint(
                message_id, UI_TEXT["system_ttadk_switch_tool_error"].format(tool=tool), project_id=project_id
            )
            return

        if project:
            project.ttadk_tool_name = tool

        # Refresh combined card with new tool's models
        try:
            raw_cwd = self._resolve_ttadk_cwd(chat_id, project_id=project_id)
            cwd = normalize_ttadk_cwd(raw_cwd)
        except Exception:
            cwd = None

        result = manager.get_models(cwd=cwd)
        yolo_enabled = self._resolve_ttadk_yolo_enabled(chat_id, project=project, project_id=project_id)
        current_model = project.ttadk_model_name if project else None
        msg_type, card_content = CardBuilder.build_ttadk_model_select_card(
            result.models or [], tool, project_id, yolo_enabled=yolo_enabled, current_model=current_model
        )
        patched = self.update_card(message_id, card_content)
        if not patched:
            self.reply_card(message_id, card_content)

    def handle_refresh_ttadk_models(self, message_id: str, chat_id: str, tool_name: str, project_id: Optional[str] = None):
        manager = get_ttadk_manager()
        try:
            raw_cwd = self._resolve_ttadk_cwd(chat_id, project_id=project_id)
            cwd = normalize_ttadk_cwd(raw_cwd)
            self._maybe_log_ttadk_cwd(
                where="SystemHandler.handle_refresh_ttadk_models", raw_cwd=raw_cwd, normalized_cwd=cwd
            )
        except Exception:
            cwd = None

        tool = (tool_name or manager.get_current_tool() or "").strip().lower()
        if not tool:
            self.reply_text(message_id, UI_TEXT["system_ttadk_no_tool"])
            return

        try:
            result = manager.refresh_models(tool_name=tool, cwd=cwd)
        except Exception as e:
            self.reply_error(message_id, get_error_detail(e), title=UI_TEXT["system_ttadk_refresh_error"])
            return

        yolo_enabled = self._resolve_ttadk_yolo_enabled(chat_id, project_id=project_id)
        project = self.project_manager.get_project_for_chat(project_id, chat_id) if project_id else self.project_manager.get_active_project(chat_id)
        current_model = project.ttadk_model_name if project else None
        msg_type, card_content = CardBuilder.build_ttadk_model_select_card(
            result.models or [], tool, project_id, yolo_enabled=yolo_enabled, current_model=current_model
        )
        patched = self.update_card(message_id, card_content)
        if not patched:
            self.reply_card(message_id, card_content)

    def handle_toggle_ttadk_yolo(
        self,
        message_id: str,
        chat_id: str,
        enabled: bool,
        view: str = "tool_select",
        tool_name: str = "",
        project_id: Optional[str] = None,
    ):
        manager = get_ttadk_manager()
        target_project = self._apply_ttadk_yolo_enabled(chat_id, enabled, project_id=project_id)
        yolo_enabled = self._resolve_ttadk_yolo_enabled(chat_id, project=target_project, project_id=project_id)

        if view == "model_select":
            tool = (tool_name or manager.get_current_tool() or "").strip().lower()
            if not tool:
                self.reply_text(message_id, UI_TEXT["system_ttadk_no_tool"])
                return
            try:
                raw_cwd = self._resolve_ttadk_cwd(chat_id, project_id=project_id)
                cwd = normalize_ttadk_cwd(raw_cwd)
                self._maybe_log_ttadk_cwd(
                    where="SystemHandler.handle_toggle_ttadk_yolo", raw_cwd=raw_cwd, normalized_cwd=cwd
                )
            except Exception:
                cwd = None

            if manager.get_current_tool() != tool:
                manager.set_tool(tool)
            result = manager.get_models(cwd=cwd)
            if result.error:
                self.reply_error(message_id, result.error, title=UI_TEXT["system_ttadk_get_tools_error"])
                return
            current_model = target_project.ttadk_model_name if target_project else None
            msg_type, card_content = CardBuilder.build_ttadk_model_select_card(
                result.models or [], tool, project_id, yolo_enabled=yolo_enabled, current_model=current_model
            )
            patched = self.update_card(message_id, card_content)
            if not patched:
                self.reply_card(message_id, card_content)
            return

        tools_result = manager.get_tools()
        if tools_result.error:
            self.reply_error(message_id, tools_result.error, title=UI_TEXT["system_ttadk_get_tools_error"])
            return
        current_tool = target_project.ttadk_tool_name if target_project else None
        msg_type, card_content = CardBuilder.build_ttadk_tool_select_card(
            tools_result.tools, project_id, yolo_enabled=yolo_enabled, current_tool=current_tool
        )
        patched = self.update_card(message_id, card_content)
        if not patched:
            self.reply_card(message_id, card_content)
