import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

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

logger = logging.getLogger(__name__)


@dataclass
class StreamingCard:
    chat_id: str
    title: str
    header_template: str
    project_path: Optional[str] = None
    project_id: Optional[str] = None
    reply_to_message_id: Optional[str] = None
    image_keys: Optional[list[str]] = None
    is_coco_mode: bool = True
    is_claude_mode: bool = False
    is_ttadk_mode: bool = False
    is_smart_mode: bool = False
    reply_in_thread: Optional[bool] = None  # 显式指定时优先使用

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
    visible_chars: int = 20000  # Current visibility limit (default ~20KB)
    pagination_step: int = 5000  # How much to add on "Load More"

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


def _normalize_streaming_markdown(content: str, *, is_final: bool, max_chars: int) -> str:
    """让卡片在“增量渲染”时更稳定。

    主要处理：
    - 限制卡片内容长度，避免超过飞书消息大小限制导致更新失败
    - 流式过程中如果出现未闭合的 ``` 代码块，自动补齐闭合，避免渲染错乱
    """
    if content is None:
        content = ""

    # 飞书消息 update 需要整包更新 card JSON，内容太大会导致请求失败/被截断
    if max_chars > 0 and len(content) > max_chars:
        tail = content[-max_chars:]
        content = f"…（内容过长，已截断，显示最后 {max_chars} 字符）\n\n{tail}"

    # 流式过程中，Markdown 代码块很容易出现“开了没关”的中间态，导致整卡渲染崩
    fence_count = content.count("```")
    if fence_count % 2 == 1:
        # 最终态也补齐，保证收尾可读
        content = content + "\n```"

    return content


class StreamingCardManager:
    def __init__(self, client: lark.Client):
        self._client = client
        self._settings = get_settings()
        # key 使用 message_id（发送成功后才会写入）
        self._cards: dict[str, StreamingCard] = {}
        self._lock = threading.Lock()

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

        self._max_card_chars = 28000
        self._last_cleanup: float = 0.0
        self._cleanup_interval: float = 300.0  # auto-cleanup every 5 minutes

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
        """构建卡片内容元素列表。

        注意：飞书消息 PATCH 更新（`im/v1/message.update`）对卡片字段校验更严格，
        legacy 卡片不支持 schema 2.0 的部分字段（如 `schema`/`body`/`text_size`/`element_id`）。
        """
        path_display = project_path or "~"
        button_elements = self._build_button_elements(buttons or [])

        def _maybe_attach(target: dict, **kv):
            if legacy:
                return target
            for k, v in kv.items():
                if v is not None:
                    target[k] = v
            return target

        # Status Bar
        status_icon = {"green": "🟢", "red": "🔴", "blue": "🔵", "grey": "⚪"}.get(status_color, "🔵")
        status_info = f"{status_icon} **状态**: {status_color.upper()}"
        if error_count > 0:
            status_info += f" | ❌ 错误: {error_count}"
        if progress_text:
            status_info += f" | {progress_text}"

        elements = [
            _maybe_attach(
                {"tag": "markdown", "content": f"📁 `{path_display}`\n{status_info}"},
                element_id="path_md",
                text_size="notation",
            ),
        ]

        if sticky_message:
            elements.append(
                {
                    "tag": "note",
                    "elements": [{"tag": "plain_text", "content": f"⚠️ {sticky_message}"}],
                }
            )

        elements.append({"tag": "hr"})

        if image_keys:
            for i, key in enumerate(image_keys):
                elements.append(
                    {
                        "tag": "img",
                        "img_key": key,
                        "alt": {"tag": "plain_text", "content": f"图片 {i + 1}"},
                    }
                )
            elements.append({"tag": "hr"})

        elements.append(
            _maybe_attach(
                {"tag": "markdown", "content": initial_content},
                element_id=element_id,
                text_size="normal",
            )
        )

        if button_elements:
            elements.append({"tag": "hr"})
            elements.extend(button_elements)

        return elements

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
            config["streaming_config"] = {
                "print_frequency_ms": {"default": 30, "android": 30, "ios": 30, "pc": 30},
                "print_step": {"default": 3, "android": 3, "ios": 3, "pc": 3},
                "print_strategy": "fast",
            }

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
        """构建 legacy 格式卡片 JSON。

        该格式同时用于：
        - 创建“可被 PATCH 更新”的流式消息卡片（create/reply）
        - 后续通过 `UpdateMessageRequest` 进行更新/关闭
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
            config["streaming_config"] = {
                "print_frequency_ms": {"default": 30, "android": 30, "ios": 30, "pc": 30},
                "print_step": {"default": 3, "android": 3, "ios": 3, "pc": 3},
                "print_strategy": "fast",
            }

        return {
            "config": config,
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": header_template,
            },
            "elements": elements,
        }

    def _resolve_title_and_template(
        self,
        project_name: Optional[str],
        is_coco_mode: bool,
        is_claude_mode: bool,
        is_ttadk_mode: bool = False,
    ) -> tuple[str, str]:
        """根据模式和项目名生成标题与头部颜色模板。"""
        return resolve_title_and_template(project_name, is_coco_mode, is_claude_mode, is_ttadk_mode=is_ttadk_mode)

    # ---- 创建流式卡片 ----

    def create_streaming_card(
        self,
        chat_id: str,
        project_name: Optional[str] = None,
        project_path: Optional[str] = None,
        project_id: Optional[str] = None,
        initial_content: str = "🔄 正在思考...",
        element_id: str = "content_md",
        is_coco_mode: bool = True,
        is_claude_mode: bool = False,
        is_ttadk_mode: bool = False,
        is_smart_mode: bool = False,
        reply_to_message_id: Optional[str] = None,
        image_keys: Optional[list[str]] = None,
        reply_in_thread: Optional[bool] = None,
    ) -> Optional[StreamingCard]:
        title, header_template = self._resolve_title_and_template(project_name, is_coco_mode, is_claude_mode, is_ttadk_mode=is_ttadk_mode)

        # "创建"阶段不做任何远端调用：先把卡片所需的元信息封装起来
        return StreamingCard(
            chat_id=chat_id,
            title=title,
            header_template=header_template,
            project_path=project_path,
            project_id=project_id,
            reply_to_message_id=reply_to_message_id,
            image_keys=image_keys,
            is_coco_mode=is_coco_mode,
            is_claude_mode=is_claude_mode,
            is_ttadk_mode=is_ttadk_mode,
            is_smart_mode=is_smart_mode,
            reply_in_thread=reply_in_thread,
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
        is_coco_mode: bool = True,
        is_claude_mode: bool = False,
        is_ttadk_mode: bool = False,
        is_smart_mode: bool = False,
        reply_to_message_id: Optional[str] = None,
        image_keys: Optional[list[str]] = None,
        reply_in_thread: Optional[bool] = None,
    ) -> Optional[str]:
        """非流式发送：直接发送 schema 2.0 card JSON（不依赖 CardKit 卡片实体）。"""
        title, header_template = self._resolve_title_and_template(project_name, is_coco_mode, is_claude_mode, is_ttadk_mode=is_ttadk_mode)
        buttons = self._build_buttons(is_coco_mode, project_id, is_claude_mode, is_ttadk_mode)
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
                    if is_smart_mode:
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
        self, is_coco_mode: bool, project_id: Optional[str] = None, is_claude_mode: bool = False, is_ttadk_mode: bool = False
    ) -> list[dict]:
        return build_mode_buttons(is_coco_mode, project_id, is_claude_mode, is_ttadk_mode)

    def _build_button_elements(self, buttons: list[dict], layout: str = "responsive") -> list[dict]:
        """为卡片生成按钮区。Delegates to shared.build_responsive_layout."""
        if not buttons:
            return []
        return build_responsive_layout(buttons)

    # ---- 发送/更新/关闭 ----

    def _maybe_cleanup(self) -> None:
        """Amortized auto-cleanup: evict expired cards periodically."""
        now = time.time()
        if now - self._last_cleanup < self._cleanup_interval:
            return
        self._last_cleanup = now
        self.cleanup_expired_cards()

    def send_streaming_card(self, card: StreamingCard) -> Optional[str]:
        self._maybe_cleanup()
        try:
            buttons = self._build_buttons(card.is_coco_mode, card.project_id, card.is_claude_mode, card.is_ttadk_mode)
            initial = _normalize_streaming_markdown(
                card.last_content,
                is_final=False,
                max_chars=self._max_card_chars,
            )
            # 关键：流式卡片后续需要用 message.update(PATCH) 更新。
            # PATCH 更新对卡片字段校验严格，schema 2.0 格式会触发 field validation failed。
            # 因此创建阶段也必须使用 legacy 卡片结构。
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
                    if card.is_smart_mode:
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

        # --- Adaptive Flow Control Calculation ---
        now = time.time()

        # Check sticky expiration
        if card.sticky_message and card.sticky_expires_at > 0 and now > card.sticky_expires_at:
            card.sticky_message = None
            card.sticky_expires_at = 0.0
            force = True  # Force update to remove sticky message

        current_len = len(content)

        # Calculate content arrival rate (based on FULL content length)
        # We track rate based on data arrival, not rendering frequency
        delta_c = current_len - card.last_content_len
        self._flow_control.update_rate(card.flow_control_state, now, delta_c)

        # Update full content in memory
        card.full_content = content
        card.is_typing = is_typing

        # Calculate display content based on visible_chars
        display_content = content
        has_more = False

        if len(content) > card.visible_chars:
            display_content = content[: card.visible_chars]
            has_more = True

        normalized = _normalize_streaming_markdown(
            display_content,
            is_final=False,
            max_chars=0,  # Disable truncation in normalize since we handled it
        )

        # Add typing indicator suffix if active
        if is_typing:
            if now - card.last_typing_update > 0.4:  # Update dots every 400ms
                card.typing_state = (card.typing_state + 1) % 4
                card.last_typing_update = now

            dots = "." * card.typing_state
            if normalized.strip():
                normalized += f"\n\n_对方正在思考{dots}_"
            else:
                normalized = f"_对方正在思考{dots}_"

        if normalized == card.last_content and not has_more and not is_typing:
            return True

        # Dual buffering strategy: Time OR Size
        # Update if enough time has passed OR enough content has accumulated
        # We compare against the FULL content length for activity, but send truncated.
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

        if not should_update:
            return True

        try:
            buttons = self._build_buttons(card.is_coco_mode, card.project_id, card.is_claude_mode, card.is_ttadk_mode)

            # Inject Load More button if needed
            if has_more:
                buttons.append(
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "⬇️ 加载更多"},
                        "type": "primary",
                        "value": {"action": "load_more", "message_id": card.message_id},
                    }
                )

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

            if not resp.success():
                # Rate limit handling could go here
                logger.warning("卡片消息更新失败: code=%s, msg=%s", resp.code, resp.msg)
                return False

            card.last_content = normalized
            card.last_content_len = len(content)  # Track FULL content length
            card.last_update_at = now
            return True
        except Exception as e:
            logger.warning("卡片消息更新异常: %s", e, exc_info=True)
            return False

    def close_streaming(self, card: StreamingCard, final_content: Optional[str] = None) -> bool:
        if not card.message_id:
            return False

        try:
            final_text = final_content if final_content else card.last_content
            normalized = _normalize_streaming_markdown(
                final_text,
                is_final=True,
                max_chars=0,  # final card: no truncation
            )
            buttons = self._build_buttons(card.is_coco_mode, card.project_id, card.is_claude_mode, card.is_ttadk_mode)
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

            with self._lock:
                self._cards.pop(card.message_id, None)
            return True
        except Exception as e:
            logger.warning("关闭流式异常: %s", e, exc_info=True)
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
        """Increase the visible character limit for a card and trigger update."""
        with self._lock:
            card = self._cards.get(card_id)
            if not card:
                return False

            # Increase limit
            card.visible_chars += card.pagination_step

            # Force update via normal flow
            # We use the cached full_content
            content_to_render = card.full_content

        return self.update_content(card, content_to_render, force=True)
