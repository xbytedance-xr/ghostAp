import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Optional
from src.mode.manager import InteractionMode
from src.utils.markdown import safe_truncate_markdown

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

from ..config import get_settings
from .flow_control import FlowControlConfig, FlowControlState, FlowControlStrategy
from .shared import build_mode_buttons, build_responsive_layout, resolve_title_and_template
from .styles import THRESHOLDS

logger = logging.getLogger(__name__)


def _make_set_event() -> threading.Event:
    """Create an Event that starts in set (ready) state."""
    e = threading.Event()
    e.set()
    return e


@dataclass
class StreamingCard:
    chat_id: str
    title: str
    header_template: str
    project_path: Optional[str] = None
    project_id: Optional[str] = None
    reply_to_message_id: Optional[str] = None
    image_keys: Optional[list[str]] = None
    mode: Optional[InteractionMode] = None
    reply_in_thread: Optional[bool] = None  # 显式指定时优先使用
    thread_root_id: Optional[str] = None

    message_id: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    last_content: str = ""
    last_update_at: float = 0.0

    # Adaptive Flow Control
    flow_control_state: FlowControlState = field(default_factory=FlowControlState)

    size_threshold: int = 50  # Character count threshold for updates
    last_content_len: int = 0

    # Pagination support
    full_content: str = ""  # Complete content storage
    visible_chars: int = THRESHOLDS["STREAMING_VISIBLE_CHARS"]  # Current visibility limit
    pagination_step: int = THRESHOLDS["PAGINATION_STEP"]  # How much to add on "Load More"
    view_start: int = 0  # Window start offset when content is paged

    # Typing indicator state
    typing_state: int = 0  # 0-3 for cycling dots
    is_typing: bool = False
    last_typing_update: float = 0.0

    # Status and Metrics
    status_color: str = "blue"  # green, red, blue, grey
    error_count: int = 0
    progress_text: str = ""

    # Sticky Message
    sticky_message: Optional[str] = None
    sticky_expires_at: float = 0.0

    # Concurrency control
    is_inflight: bool = False
    pending_update: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)
    inflight_done: threading.Event = field(default_factory=lambda: _make_set_event())

    # Structured rendering support (collapsible panels / continuation)
    continuation_index: int = 0
    structured_sections: Optional[list] = None  # list[ContentSection] from renderer
    _collapsible_patch_failed: bool = False  # auto-fallback flag

    # Continuation callback — invoked when a continuation card is created.
    # Allows external code (e.g. programming handler) to reset renderer state.
    on_continuation: Optional[Callable[[], None]] = None


def _normalize_streaming_markdown(content: str, *, is_final: bool, max_chars: int) -> str:
    """让卡片在“增量渲染”时更稳定。

    主要处理：
    - 限制卡片内容长度，避免超过飞书消息大小限制导致更新失败
    - 流式过程中如果出现未闭合的 ``` 代码块，自动补齐闭合，避免渲染错乱
    """
    if content is None:
        content = ""

    if max_chars > 0 and len(content) > max_chars:
        # 默认流式保留尾部（最新内容）
        return safe_truncate_markdown(content, max_length=max_chars, keep_head=False)
    else:
        # 虽然没有超长，但流式过程中可能代码块未闭合，依然使用相同逻辑进行安全闭合
        fence_count = content.count("```")
        if fence_count % 2 == 1:
            content += "\n```"
        return content


# 飞书卡片流式动画参数（由飞书 SDK 协议定义）
_STREAMING_CONFIG: dict = {
    "print_frequency_ms": {"default": 30, "android": 30, "ios": 30, "pc": 30},
    "print_step": {"default": 3, "android": 3, "ios": 3, "pc": 3},
    "print_strategy": "fast",
}


class StreamingCardManager:
    def __init__(self, client: lark.Client):
        self._client = client
        self._settings = get_settings()
        # key 使用 message_id（发送成功后才会写入）
        self._cards: dict[str, StreamingCard] = {}
        self._lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock

        # Initialize Flow Control Strategy
        self._flow_control = FlowControlStrategy(
            FlowControlConfig(
                base_interval_s=self._settings.streaming_adaptive_interval_base,
                max_interval_s=self._settings.streaming_adaptive_interval_max,
                low_rate_threshold=self._settings.streaming_adaptive_rate_low,
                high_rate_threshold=self._settings.streaming_adaptive_rate_high,
                ema_alpha=0.3,
            )
        )

        self._max_card_chars = self._settings.card_max_chars
        self._last_cleanup: float = 0.0
        self._cleanup_interval: float = 300.0  # auto-cleanup every 5 minutes

        # Thread pool for asynchronous card updates
        self._executor = ThreadPoolExecutor(max_workers=10, thread_name_prefix="streaming_card_updater")

    # ---- 卡片 JSON 构建（共用） ----

    def _build_elements(
        self,
        project_path: Optional[str],
        initial_content: str,
        element_id: str,
        image_keys: Optional[list[str]],
        buttons: Optional[list[dict]],
        *,
        legacy: bool = False,
        status_color: str = "blue",
        error_count: int = 0,
        progress_text: str = "",
        sticky_message: Optional[str] = None,
    ) -> list[dict]:
        """构建卡片内容元素列表（委托给 UnifiedCardLayout）。

        注意：飞书消息 PATCH 更新（`im/v1/message.update`）对卡片字段校验更严格。
        PATCH 载荷使用 schema 2.0，但元素需采用 legacy-safe 字段（避免 `text_size`/`element_id`）。
        """
        from .builders.layout import UnifiedCardLayout
        from .models import CardLayoutSpec

        spec = CardLayoutSpec(
            project_path=project_path,
            image_keys=image_keys,
            buttons=buttons,
            status_color=status_color,
            error_count=error_count,
            progress_text=progress_text,
            sticky_message=sticky_message,
            content_markdown=initial_content,
            legacy_safe=legacy,
            content_element_id=element_id,
        )
        return UnifiedCardLayout.build(spec)

    def _build_card_json(
        self,
        title: str,
        header_template: str,
        project_path: Optional[str] = None,
        initial_content: str = "🔄 正在思考...",
        element_id: str = "content_md",
        image_keys: Optional[list[str]] = None,
        buttons: Optional[list[dict]] = None,
        streaming_mode: bool = True,
        status_color: str = "blue",
        error_count: int = 0,
        progress_text: str = "",
        sticky_message: Optional[str] = None,
    ) -> dict:
        """构建 schema 2.0 卡片 JSON（用于 create/reply）。"""
        elements = self._build_elements(
            project_path,
            initial_content,
            element_id,
            image_keys,
            buttons,
            legacy=False,
            status_color=status_color,
            error_count=error_count,
            progress_text=progress_text,
            sticky_message=sticky_message,
        )

        config: dict = {
            "wide_screen_mode": True,
            "update_multi": True,
        }
        if streaming_mode:
            config["streaming_mode"] = True
            config["streaming_config"] = _STREAMING_CONFIG

        return {
            "schema": "2.0",
            "config": config,
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": header_template,
            },
            "body": {
                "elements": elements,
            },
        }

    def _build_update_card_json(
        self,
        title: str,
        header_template: str,
        project_path: Optional[str] = None,
        initial_content: str = "🔄 正在思考...",
        element_id: str = "content_md",
        image_keys: Optional[list[str]] = None,
        buttons: Optional[list[dict]] = None,
        streaming_mode: bool = True,
        status_color: str = "blue",
        error_count: int = 0,
        progress_text: str = "",
        sticky_message: Optional[str] = None,
    ) -> dict:
        """构建适配 PATCH 更新的 schema 2.0 卡片 JSON。

        PATCH 需要 schema 2.0 包装，但元素需保持 legacy-safe，避免额外字段导致校验失败。
        """
        elements = self._build_elements(
            project_path,
            initial_content,
            element_id,
            image_keys,
            buttons,
            legacy=True,
            status_color=status_color,
            error_count=error_count,
            progress_text=progress_text,
            sticky_message=sticky_message,
        )

        config: dict = {
            "wide_screen_mode": True,
            "update_multi": True,
        }
        if streaming_mode:
            config["streaming_mode"] = True
            config["streaming_config"] = _STREAMING_CONFIG

        return {
            "schema": "2.0",
            "config": config,
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": header_template,
            },
            "body": {
                "elements": elements,
            },
        }

    def _resolve_title_and_template(
        self,
        project_name: Optional[str],
        mode: Optional[InteractionMode] = None,
        ttadk_tool_name: Optional[str] = None,
        ttadk_model_name: Optional[str] = None,
    ) -> tuple[str, str]:
        """根据模式和项目名生成标题与头部颜色模板。"""
        return resolve_title_and_template(
            project_name, mode=mode,
            ttadk_tool_name=ttadk_tool_name,
            ttadk_model_name=ttadk_model_name,
        )

    # ---- 创建流式卡片 ----

    def create_streaming_card(
        self,
        chat_id: str,
        project_name: Optional[str] = None,
        project_path: Optional[str] = None,
        project_id: Optional[str] = None,
        initial_content: str = "🔄 正在思考...",
        element_id: str = "content_md",
        mode: Optional[InteractionMode] = InteractionMode.COCO,
        reply_to_message_id: Optional[str] = None,
        image_keys: Optional[list[str]] = None,
        reply_in_thread: Optional[bool] = None,
        ttadk_tool_name: Optional[str] = None,
        ttadk_model_name: Optional[str] = None,
        thread_root_id: Optional[str] = None,
    ) -> Optional[StreamingCard]:
        title, header_template = self._resolve_title_and_template(
            project_name, mode=mode,
            ttadk_tool_name=ttadk_tool_name,
            ttadk_model_name=ttadk_model_name,
        )

        # "创建"阶段不做任何远端调用：先把卡片所需的元信息封装起来
        return StreamingCard(
            chat_id=chat_id,
            title=title,
            header_template=header_template,
            project_path=project_path,
            project_id=project_id,
            reply_to_message_id=reply_to_message_id,
            image_keys=image_keys,
            mode=mode,
            reply_in_thread=reply_in_thread,
            thread_root_id=thread_root_id,
            last_content=initial_content,
            flow_control_state=FlowControlState(min_update_interval_s=self._settings.streaming_adaptive_interval_base),
        )

    # ---- 非流式一次性发送（CardKit schema 2.0 但不开启打字动效） ----

    def create_and_send_card(
        self,
        chat_id: str,
        content: str,
        project_name: Optional[str] = None,
        project_path: Optional[str] = None,
        project_id: Optional[str] = None,
        mode: Optional[InteractionMode] = InteractionMode.COCO,
        reply_to_message_id: Optional[str] = None,
        image_keys: Optional[list[str]] = None,
        reply_in_thread: Optional[bool] = None,
    ) -> Optional[str]:
        """非流式发送：直接发送 schema 2.0 card JSON（不依赖 CardKit 卡片实体）。"""
        title, header_template = self._resolve_title_and_template(project_name, mode=mode)
        buttons = self._build_buttons(mode, project_id)
        card_json = self._build_card_json(
            title=title,
            header_template=header_template,
            project_path=project_path,
            initial_content=content,
            image_keys=image_keys,
            buttons=buttons,
            streaming_mode=False,
        )

        try:
            msg_content = json.dumps(card_json, ensure_ascii=False)
            if reply_to_message_id:
                # 根据配置和模式决定是否使用话题回复
                # 优先使用显式传入的 reply_in_thread 参数
                effective_reply_in_thread = reply_in_thread
                if effective_reply_in_thread is None:
                    if mode == InteractionMode.SMART:
                        effective_reply_in_thread = self._settings.smart_reply_mode == "thread"
                    else:
                        effective_reply_in_thread = self._settings.default_reply_mode == "thread"
                msg_request = (
                    ReplyMessageRequest.builder()
                    .message_id(reply_to_message_id)
                    .request_body(
                        ReplyMessageRequestBody.builder()
                        .msg_type("interactive")
                        .content(msg_content)
                        .reply_in_thread(effective_reply_in_thread)
                        .build()
                    )
                    .build()
                )
                msg_response = self._client.im.v1.message.reply(msg_request)
            else:
                msg_request = (
                    CreateMessageRequest.builder()
                    .receive_id_type("chat_id")
                    .request_body(
                        CreateMessageRequestBody.builder()
                        .receive_id(chat_id)
                        .msg_type("interactive")
                        .content(msg_content)
                        .build()
                    )
                    .build()
                )
                msg_response = self._client.im.v1.message.create(msg_request)

            if not msg_response.success():
                logger.error("发送卡片失败: code=%s, msg=%s", msg_response.code, msg_response.msg)
                return None

            return msg_response.data.message_id
        except Exception as e:
            logger.error("发送卡片异常: %s", e, exc_info=True)
            return None

    # ---- 按钮构建 ----

    def _build_buttons(
        self, mode: Optional[InteractionMode] = None, project_id: Optional[str] = None,
        *, thread_root_id: Optional[str] = None,
    ) -> list[dict]:
        effective_thread_root_id = thread_root_id
        if effective_thread_root_id is None:
            try:
                from ..thread import get_current_thread_id
                effective_thread_root_id = get_current_thread_id()
            except Exception:
                logger.debug("failed to get thread_id", exc_info=True)
        return build_mode_buttons(mode, project_id, thread_root_id=effective_thread_root_id)

    def _build_button_elements(self, buttons: list[dict], layout: str = "responsive") -> list[dict]:
        """为卡片生成按钮区。Delegates to shared.build_responsive_layout."""
        if not buttons:
            return []
        return build_responsive_layout(buttons)

    def _build_structured_elements(
        self,
        project_path: Optional[str],
        content_elements: list[dict],
        image_keys: Optional[list[str]],
        buttons: Optional[list[dict]],
        *,
        status_color: str = "blue",
        error_count: int = 0,
        progress_text: str = "",
        sticky_message: Optional[str] = None,
    ) -> list[dict]:
        """Build card elements using pre-built content elements (委托给 UnifiedCardLayout).

        This replaces the single markdown element with multiple elements that may
        include collapsible_panel sections.
        """
        from .builders.layout import UnifiedCardLayout
        from .models import CardLayoutSpec

        spec = CardLayoutSpec(
            project_path=project_path,
            image_keys=image_keys,
            buttons=buttons,
            status_color=status_color,
            error_count=error_count,
            progress_text=progress_text,
            sticky_message=sticky_message,
            content_elements=content_elements,
            legacy_safe=True,  # structured elements are used in PATCH context
        )
        return UnifiedCardLayout.build(spec)

    def update_structured(self, card: StreamingCard, rendered: "RenderedContent", force: bool = False, is_typing: bool = False) -> bool:
        """Update card with structured RenderedContent.

        Stores sections on card for _do_patch_task to build collapsible elements.
        Falls back to flat markdown via update_content when collapsible is disabled
        or card has experienced a collapsible PATCH failure.
        """
        if not card.message_id:
            return False

        with card.lock:
            if card._collapsible_patch_failed or not self._settings.card_collapsible_enabled:
                # Fallback to flat markdown
                pass
            else:
                card.structured_sections = rendered.sections

        # Always update full_content with flat markdown (for pagination, continuation, etc.)
        flat_md = rendered.to_markdown()
        return self.update_content(card, flat_md, force=force, is_typing=is_typing)

    # ---- 发送/更新/关闭 ----

    def _maybe_cleanup(self) -> None:
        """Amortized auto-cleanup: evict expired cards periodically."""
        now = time.time()
        if now - self._last_cleanup < self._cleanup_interval:
            return
        self._last_cleanup = now
        self.cleanup_expired_cards()

    def _slice_window(self, card: StreamingCard, content: str) -> tuple[str, bool, bool]:
        """返回 (display_content, has_prev, has_more)。

        - 默认从头开始展示；当 visible_chars 达到上限但仍有更多内容时，使用 view_start 做滑窗分页。
        - 永远保证 display_content 不超过 _max_card_chars（避免 PATCH 载荷过大）。
        """

        if content is None:
            content = ""

        total = len(content)
        if total <= 0:
            card.view_start = 0
            return "", False, False

        window = int(card.visible_chars or 0)
        if window <= 0:
            window = min(20000, self._max_card_chars)
        window = min(window, self._max_card_chars)

        max_start = max(0, total - window)
        start = int(card.view_start or 0)
        if start < 0:
            start = 0
        if start > max_start:
            start = max_start
        card.view_start = start

        end = min(total, start + window)
        display = content[start:end]
        has_prev = start > 0
        has_more = end < total
        return display, has_prev, has_more

    def send_streaming_card(self, card: StreamingCard) -> Optional[str]:
        self._maybe_cleanup()
        try:
            buttons = self._build_buttons(card.mode, card.project_id, thread_root_id=card.thread_root_id)
            # Ensure full_content is initialized (for pagination after send).
            if not card.full_content:
                card.full_content = card.last_content or ""
            display, has_prev, has_more = self._slice_window(card, card.full_content)
            initial = _normalize_streaming_markdown(display, is_final=False, max_chars=0)

            if has_prev:
                buttons.append(
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "⬆️ 上一段"},
                        "type": "default",
                        "value": {"action": "load_prev", "message_id": card.message_id or ""},
                    }
                )
            if has_more:
                buttons.append(
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "⬇️ 加载更多"},
                        "type": "primary",
                        "value": {"action": "load_more", "message_id": card.message_id or ""},
                    }
                )
            # 关键：流式卡片后续需要用 message.update(PATCH) 更新。
            # PATCH 更新要求 schema 2.0 包装，但元素需 legacy-safe（避免 text_size/element_id）。
            card_json = self._build_update_card_json(
                title=card.title,
                header_template=card.header_template,
                project_path=card.project_path,
                initial_content=initial,
                image_keys=card.image_keys,
                buttons=buttons,
                streaming_mode=True,
                status_color=card.status_color,
                error_count=card.error_count,
                progress_text=card.progress_text,
                sticky_message=card.sticky_message,
            )
            content = json.dumps(card_json, ensure_ascii=False)

            if card.reply_to_message_id:
                # 根据配置和模式决定是否使用话题回复
                # 优先使用显式传入的 reply_in_thread 参数
                effective_reply_in_thread = card.reply_in_thread
                if effective_reply_in_thread is None:
                    if card.mode == InteractionMode.SMART:
                        effective_reply_in_thread = self._settings.smart_reply_mode == "thread"
                    else:
                        effective_reply_in_thread = self._settings.default_reply_mode == "thread"
                request = (
                    ReplyMessageRequest.builder()
                    .message_id(card.reply_to_message_id)
                    .request_body(
                        ReplyMessageRequestBody.builder()
                        .msg_type("interactive")
                        .content(content)
                        .reply_in_thread(effective_reply_in_thread)
                        .build()
                    )
                    .build()
                )
                response = self._client.im.v1.message.reply(request)
            else:
                request = (
                    CreateMessageRequest.builder()
                    .receive_id_type("chat_id")
                    .request_body(
                        CreateMessageRequestBody.builder()
                        .receive_id(card.chat_id)
                        .msg_type("interactive")
                        .content(content)
                        .build()
                    )
                    .build()
                )
                response = self._client.im.v1.message.create(request)

            if not response.success():
                logger.error("发送流式卡片失败: code=%s, msg=%s", response.code, response.msg)
                return None

            card.message_id = response.data.message_id
            card.last_update_at = time.time()
            with self._lock:
                self._cards[card.message_id] = card
            return card.message_id

        except Exception as e:
            logger.error("发送流式卡片异常: %s", e, exc_info=True)
            return None

    def set_sticky_message(self, card_id: str, message: str, duration: float = 5.0):
        """Set a sticky message that persists for at least `duration` seconds."""
        with self._lock:
            card = self._cards.get(card_id)
            if not card:
                return
            card.sticky_message = message
            card.sticky_expires_at = time.time() + duration
            # Force update to show the message
            self.update_content(card, card.full_content, force=True)

    def update_content(self, card: StreamingCard, content: str, force: bool = False, is_typing: bool = False) -> bool:
        if not card.message_id:
            return False

        now = time.time()

        with card.lock:
            # Check sticky expiration
            if card.sticky_message and card.sticky_expires_at > 0 and now > card.sticky_expires_at:
                card.sticky_message = None
                card.sticky_expires_at = 0.0
                force = True  # Force update to remove sticky message

            current_len = len(content)

            # Calculate content arrival rate (based on FULL content length)
            delta_c = current_len - card.last_content_len
            self._flow_control.update_rate(card.flow_control_state, now, delta_c)

            # Update full content in memory.
            # Monotonic guarantee: streaming output should only grow. If a shorter
            # content arrives (out-of-order / stale caller), keep the longer one.
            if not force:
                try:
                    prev_len = len(card.full_content or "")
                    new_len = len(content or "")
                    if new_len < prev_len:
                        content = card.full_content
                except Exception:
                    logger.debug("content length fallback failed", exc_info=True)
            card.full_content = content
            card.is_typing = is_typing

            # Dual buffering strategy: Time OR Size
            content_len = len(content)
            size_delta = abs(content_len - card.last_content_len)
            time_delta = now - card.last_update_at

            should_update = (
                force
                or card.last_update_at == 0.0
                or size_delta >= card.size_threshold
                or time_delta >= card.flow_control_state.min_update_interval_s
                or (is_typing and time_delta >= 0.8)  # Slower update for just typing animation
            )

            if should_update:
                card.pending_update = True

            if not card.pending_update or card.is_inflight:
                return True

            card.is_inflight = True
            card.inflight_done.clear()

        # Dispatch async update
        self._executor.submit(self._do_patch_task, card)
        return True

    def _maybe_create_continuation(self, card: StreamingCard, content: str) -> bool:
        """Check if content exceeds threshold and create a continuation card.

        Returns True if a continuation was created (card object mutated in-place).

        Safety: creates new card FIRST, then closes old card. If new card creation
        fails, the old card remains active and continues receiving updates.
        """
        if not self._settings.card_continuation_enabled:
            return False

        max_cards = THRESHOLDS.get("CONTINUATION_MAX_CARDS", 10)
        if card.continuation_index >= max_cards:
            return False

        threshold = int(self._max_card_chars * self._settings.card_continuation_threshold_pct)
        if len(content) < threshold:
            return False

        from .styles import UI_TEXT
        footer = UI_TEXT.get("streaming_continuation_footer", "\n\n---\n⬇️ 后续内容见下方")
        suffix_template = UI_TEXT.get("streaming_continuation_title_suffix", " (续 #{n})")
        initial_text = UI_TEXT.get("streaming_continuation_initial", "🔄 继续输出...")

        old_message_id = card.message_id
        new_index = card.continuation_index + 1
        new_title = card.title + suffix_template.replace("{n}", str(new_index))

        # 1. Create new card FIRST (safe: old card still active if this fails)
        new_card_json = self._build_update_card_json(
            title=new_title,
            header_template=card.header_template,
            project_path=card.project_path,
            initial_content=initial_text,
            image_keys=None,
            buttons=[],
            streaming_mode=True,
            status_color=card.status_color,
        )
        try:
            reply_msg_id = card.reply_to_message_id or old_message_id
            msg_content = json.dumps(new_card_json, ensure_ascii=False)
            request = (
                ReplyMessageRequest.builder()
                .message_id(reply_msg_id)
                .request_body(
                    ReplyMessageRequestBody.builder()
                    .msg_type("interactive")
                    .content(msg_content)
                    .reply_in_thread(True)
                    .build()
                )
                .build()
            )
            response = self._client.im.v1.message.reply(request)
            if not response.success():
                logger.warning("续接：创建新卡片失败: code=%s, msg=%s", response.code, response.msg)
                return False
            new_message_id = response.data.message_id
        except Exception as e:
            logger.warning("续接：创建新卡片异常: %s", str(e))
            return False

        # 2. Close old card with stub message (best-effort: new card already exists)
        stub_text = UI_TEXT.get("continuation_stale_stub", "ℹ️ **此页已收起，请查看下方更新后的卡片。**")
        normalized = _normalize_streaming_markdown(stub_text, is_final=True, max_chars=0)
        buttons = self._build_buttons(card.mode, card.project_id, thread_root_id=card.thread_root_id)
        close_json = self._build_update_card_json(
            title=card.title,
            header_template=card.header_template,
            project_path=card.project_path,
            initial_content=normalized,
            image_keys=card.image_keys,
            buttons=buttons,
            streaming_mode=False,
            status_color=card.status_color,
            error_count=card.error_count,
            progress_text=card.progress_text,
        )
        try:
            req = (
                PatchMessageRequest.builder()
                .message_id(old_message_id)
                .request_body(
                    PatchMessageRequestBody.builder().content(json.dumps(close_json, ensure_ascii=False)).build()
                )
                .build()
            )
            resp = self._client.im.v1.message.patch(req)
            if not resp.success():
                logger.warning("续接：关闭旧卡片失败(非致命): code=%s, msg=%s", resp.code, resp.msg)
                # Non-fatal: new card already created, continue with it
        except Exception as e:
            logger.warning("续接：关闭旧卡片异常(非致命): %s", str(e))
            # Non-fatal: new card already created, continue with it

        # 3. Mutate card in-place to point to new card
        with card.lock:
            card.continuation_index = new_index
            card.title = new_title
            card.full_content = ""
            card.last_content = ""
            card.last_content_len = 0
            card.view_start = 0
            card.structured_sections = None
            card._collapsible_patch_failed = False
            old_mid = card.message_id
            card.message_id = new_message_id

        # 4. Update _cards dict
        with self._lock:
            self._cards.pop(old_mid, None)
            self._cards[new_message_id] = card

        # 5. Notify external listener to reset renderer state
        if card.on_continuation is not None:
            try:
                card.on_continuation()
            except Exception as exc:
                logger.warning("on_continuation callback error: %s", str(exc))

        logger.info("续接卡片创建成功: #%d, old=%s, new=%s", new_index, old_mid, new_message_id)
        return True

    def _do_patch_task(self, card: StreamingCard) -> None:
        try:
            while True:
                with card.lock:
                    if not card.pending_update:
                        card.is_inflight = False
                        card.inflight_done.set()
                        break
                    card.pending_update = False

                    content = card.full_content
                    is_typing = card.is_typing
                    structured = card.structured_sections  # snapshot
                    use_collapsible = (
                        structured is not None
                        and not card._collapsible_patch_failed
                        and self._settings.card_collapsible_enabled
                    )

                # Check continuation (outside lock — may do API calls)
                if self._maybe_create_continuation(card, content):
                    # Card was mutated in-place, content reset. Re-read and continue.
                    with card.lock:
                        content = card.full_content
                        card.pending_update = True  # ensure we process remaining content

                with card.lock:
                    display_content, has_prev, has_more = self._slice_window(card, content)

                    now = time.time()

                normalized = _normalize_streaming_markdown(
                    display_content,
                    is_final=False,
                    max_chars=0,
                )

                # Add typing indicator suffix if active
                if is_typing:
                    with card.lock:
                        if now - card.last_typing_update > 0.4:
                            card.typing_state = (card.typing_state + 1) % 4
                            card.last_typing_update = now
                        dots = "." * card.typing_state
                    if normalized.strip():
                        normalized += f"\n\n_对方正在思考{dots}_"
                    else:
                        normalized = f"_对方正在思考{dots}_"

                with card.lock:
                    if normalized == card.last_content and not has_more and not is_typing:
                        # nothing real changed
                        card.last_content_len = len(content)
                        continue

                buttons = self._build_buttons(card.mode, card.project_id, thread_root_id=card.thread_root_id)

                if has_prev:
                    buttons.append(
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "⬆️ 上一段"},
                            "type": "default",
                            "value": {"action": "load_prev", "message_id": card.message_id},
                        }
                    )

                if has_more:
                    buttons.append(
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "⬇️ 加载更多"},
                            "type": "primary",
                            "value": {"action": "load_more", "message_id": card.message_id},
                        }
                    )

                # Try structured (collapsible) rendering if available
                card_json = None
                tried_collapsible = False
                if use_collapsible and not has_prev and not has_more:
                    from ..acp.renderer import RenderedContent
                    rc = RenderedContent(sections=structured)
                    content_elements = rc.to_elements(collapsible=True)
                    max_elements = THRESHOLDS.get("COLLAPSIBLE_MAX_ELEMENTS", 20)
                    if len(content_elements) <= max_elements:
                        tried_collapsible = True
                        elements = self._build_structured_elements(
                            project_path=card.project_path,
                            content_elements=content_elements,
                            image_keys=card.image_keys,
                            buttons=buttons,
                            status_color=card.status_color,
                            error_count=card.error_count,
                            progress_text=card.progress_text,
                            sticky_message=card.sticky_message,
                        )
                        config: dict = {
                            "wide_screen_mode": True,
                            "update_multi": True,
                            "streaming_mode": True,
                            "streaming_config": _STREAMING_CONFIG,
                        }
                        card_json = {
                            "schema": "2.0",
                            "config": config,
                            "header": {
                                "title": {"tag": "plain_text", "content": card.title},
                                "template": card.header_template,
                            },
                            "body": {"elements": elements},
                        }

                # Fallback to flat markdown
                if card_json is None:
                    card_json = self._build_update_card_json(
                        title=card.title,
                        header_template=card.header_template,
                        project_path=card.project_path,
                        initial_content=normalized,
                        image_keys=card.image_keys,
                        buttons=buttons,
                        streaming_mode=True,
                        status_color=card.status_color,
                        error_count=card.error_count,
                        progress_text=card.progress_text,
                        sticky_message=card.sticky_message,
                    )

                req = (
                    PatchMessageRequest.builder()
                    .message_id(card.message_id)
                    .request_body(
                        PatchMessageRequestBody.builder().content(json.dumps(card_json, ensure_ascii=False)).build()
                    )
                    .build()
                )

                resp = self._client.im.v1.message.patch(req)

                with card.lock:
                    if not resp.success():
                        logger.warning("卡片消息更新失败: code=%s, msg=%s", resp.code, resp.msg)
                        # If collapsible failed, mark fallback and retry with flat markdown
                        if tried_collapsible:
                            card._collapsible_patch_failed = True
                            card.pending_update = True  # retry next iteration with flat
                            logger.info("Collapsible PATCH 失败，已切换到平坦 markdown 回退模式")
                    else:
                        card.last_content = normalized
                        card.last_content_len = len(content)
                        card.last_update_at = time.time()

        except Exception as e:
            logger.warning("卡片消息异步更新异常: %s", e, exc_info=True)
            with card.lock:
                card.is_inflight = False
                card.inflight_done.set()

    def close_streaming(self, card: StreamingCard, final_content: Optional[str] = None) -> bool:
        if not card.message_id:
            return False

        # Wait for any in-flight update to finish to avoid race conditions.
        # Use a longer timeout (5s) to accommodate slow Feishu API responses.
        if not card.inflight_done.wait(timeout=5.0):
            logger.warning("close_streaming: in-flight update did not complete within 5s, proceeding anyway")

        with card.lock:
            # Prevent new updates
            card.pending_update = False
            card.is_inflight = True
            card.inflight_done.clear()

        try:
            final_text = final_content if final_content else card.last_content
            if final_text is None:
                final_text = ""
            with card.lock:
                card.full_content = final_text
            display, has_prev, has_more = self._slice_window(card, final_text)
            normalized = _normalize_streaming_markdown(display, is_final=True, max_chars=0)
            buttons = self._build_buttons(card.mode, card.project_id, thread_root_id=card.thread_root_id)

            if has_prev:
                buttons.append(
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "⬆️ 上一段"},
                        "type": "default",
                        "value": {"action": "load_prev", "message_id": card.message_id},
                    }
                )

            if has_more:
                buttons.append(
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "⬇️ 加载更多"},
                        "type": "primary",
                        "value": {"action": "load_more", "message_id": card.message_id},
                    }
                )

            # Try structured (collapsible) rendering for the final card
            card_json = None
            with card.lock:
                structured = card.structured_sections
                use_collapsible = (
                    structured is not None
                    and not card._collapsible_patch_failed
                    and self._settings.card_collapsible_enabled
                )
            if use_collapsible and not has_prev and not has_more:
                try:
                    from ..acp.renderer import RenderedContent
                    rc = RenderedContent(sections=structured)
                    content_elements = rc.to_elements(collapsible=True)
                    max_elements = THRESHOLDS.get("COLLAPSIBLE_MAX_ELEMENTS", 20)
                    if len(content_elements) <= max_elements:
                        elements = self._build_structured_elements(
                            project_path=card.project_path,
                            content_elements=content_elements,
                            image_keys=card.image_keys,
                            buttons=buttons,
                            status_color=card.status_color,
                            error_count=card.error_count,
                            progress_text=card.progress_text,
                            sticky_message=card.sticky_message,
                        )
                        card_json = {
                            "schema": "2.0",
                            "config": {"wide_screen_mode": True, "update_multi": True},
                            "header": {
                                "title": {"tag": "plain_text", "content": card.title},
                                "template": card.header_template,
                            },
                            "body": {"elements": elements},
                        }
                except Exception:
                    logger.debug("close_streaming: collapsible build 失败，回退到平坦 markdown")

            if card_json is None:
                card_json = self._build_update_card_json(
                    title=card.title,
                    header_template=card.header_template,
                    project_path=card.project_path,
                    initial_content=normalized,
                    image_keys=card.image_keys,
                    buttons=buttons,
                    streaming_mode=False,
                    status_color=card.status_color,
                    error_count=card.error_count,
                    progress_text=card.progress_text,
                    sticky_message=card.sticky_message,
                )
            req = (
                PatchMessageRequest.builder()
                .message_id(card.message_id)
                .request_body(
                    PatchMessageRequestBody.builder().content(json.dumps(card_json, ensure_ascii=False)).build()
                )
                .build()
            )
            resp = self._client.im.v1.message.patch(req)
            if not resp.success():
                logger.warning("关闭流式失败: code=%s, msg=%s", resp.code, resp.msg)
                return False

            # If pagination is still needed, keep the card for a while so that
            # the user can continue paging after the task has finished.
            if not (has_prev or has_more):
                with self._lock:
                    self._cards.pop(card.message_id, None)
            with card.lock:
                card.is_inflight = False
                card.inflight_done.set()
            return True
        except Exception as e:
            logger.warning("关闭流式异常: %s", e, exc_info=True)
            with card.lock:
                card.is_inflight = False
                card.inflight_done.set()
            return False

    # ---- 查询/清理 ----

    def get_card(self, card_id: str) -> Optional[StreamingCard]:
        with self._lock:
            return self._cards.get(card_id)

    def cleanup_expired_cards(self, max_age_seconds: int = 3600):
        now = time.time()
        with self._lock:
            expired = [card_id for card_id, card in self._cards.items() if now - card.created_at > max_age_seconds]
            for card_id in expired:
                del self._cards[card_id]
                logger.info("清理过期卡片: %s", card_id)

    def increase_pagination(self, card_id: str) -> bool:
        """分页：优先扩大可见窗口；到达上限后改为滑动窗口向后翻页。"""
        with self._lock:
            card = self._cards.get(card_id)
            if not card:
                return False

            content_to_render = card.full_content or ""
            total = len(content_to_render)
            if total <= 0:
                return False

            # Try to increase window size first (up to max card chars)
            if card.visible_chars < min(total, self._max_card_chars):
                card.visible_chars = min(self._max_card_chars, card.visible_chars + card.pagination_step)
                card.view_start = 0
            else:
                # Slide forward by step
                window = min(int(card.visible_chars or 0) or self._max_card_chars, self._max_card_chars)
                max_start = max(0, total - window)
                card.view_start = min(max_start, int(card.view_start or 0) + int(card.pagination_step or 0))

        return self.update_content(card, content_to_render, force=True)

    def decrease_pagination(self, card_id: str) -> bool:
        """滑动窗口向前翻页（若处于滑窗模式）。"""
        with self._lock:
            card = self._cards.get(card_id)
            if not card:
                return False
            content_to_render = card.full_content or ""
            if not content_to_render:
                return False
            step = int(card.pagination_step or 0) or 0
            card.view_start = max(0, int(card.view_start or 0) - max(1, step))
        return self.update_content(card, content_to_render, force=True)
