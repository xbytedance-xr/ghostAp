import json
import logging
import time
import threading
from dataclasses import dataclass, field
from typing import Optional

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
    UpdateMessageRequest,
    UpdateMessageRequestBody,
)

from ..config import get_settings
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

    message_id: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    last_content: str = ""
    last_update_at: float = 0.0
    min_update_interval_s: float = 0.6


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
        # key 使用 message_id（发送成功后才会写入）
        self._cards: dict[str, StreamingCard] = {}
        self._lock = threading.Lock()

        settings = get_settings()
        # 卡片消息每次更新都要全量携带 card JSON，建议保守一些
        self._max_card_chars = min(getattr(settings, "coco_max_output_length", 30000), 8000)

    # ---- 卡片 JSON 构建（共用） ----

    def _build_card_json(
        self,
        title: str,
        header_template: str,
        project_path: Optional[str] = None,
        initial_content: str = "正在思考...",
        element_id: str = "content_md",
        image_keys: Optional[list[str]] = None,
        buttons: Optional[list[dict]] = None,
        streaming_mode: bool = True,
    ) -> dict:
        """构建 CardKit schema 2.0 卡片 JSON 结构。

        流式和非流式共用同一结构，区别仅在 config.streaming_mode。
        """
        settings = get_settings()
        path_display = project_path or "~"
        button_elements = self._build_button_elements(
            buttons or [], layout=(settings.card_button_layout or "responsive")
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
                "elements": [
                    {"tag": "markdown", "content": f"📁 `{path_display}`", "element_id": "path_md", "text_size": "notation"},
                    {"tag": "hr"},
                    *([
                        *[
                            {
                                "tag": "img",
                                "img_key": key,
                                "alt": {"tag": "plain_text", "content": f"图片 {i + 1}"},
                            }
                            for i, key in enumerate(image_keys)
                        ],
                        {"tag": "hr"},
                    ] if image_keys else []),
                    {"tag": "markdown", "content": initial_content, "element_id": element_id, "text_size": "normal"},
                    *([{"tag": "hr"}, *button_elements] if button_elements else []),
                ]
            },
        }

    def _resolve_title_and_template(
        self,
        project_name: Optional[str],
        is_coco_mode: bool,
        is_claude_mode: bool,
    ) -> tuple[str, str]:
        """根据模式和项目名生成标题与头部颜色模板。"""
        return resolve_title_and_template(project_name, is_coco_mode, is_claude_mode)

    # ---- 创建流式卡片 ----

    def create_streaming_card(
        self,
        chat_id: str,
        project_name: Optional[str] = None,
        project_path: Optional[str] = None,
        project_id: Optional[str] = None,
        initial_content: str = "正在思考...",
        element_id: str = "content_md",
        is_coco_mode: bool = True,
        is_claude_mode: bool = False,
        reply_to_message_id: Optional[str] = None,
        image_keys: Optional[list[str]] = None,
    ) -> Optional[StreamingCard]:
        title, header_template = self._resolve_title_and_template(
            project_name, is_coco_mode, is_claude_mode
        )

        # “创建”阶段不做任何远端调用：先把卡片所需的元信息封装起来
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
            last_content=initial_content,
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
        reply_to_message_id: Optional[str] = None,
        image_keys: Optional[list[str]] = None,
    ) -> Optional[str]:
        """非流式发送：直接发送 schema 2.0 card JSON（不依赖 CardKit 卡片实体）。"""
        title, header_template = self._resolve_title_and_template(
            project_name, is_coco_mode, is_claude_mode
        )
        buttons = self._build_buttons(is_coco_mode, project_id, is_claude_mode)
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
                msg_request = (
                    ReplyMessageRequest.builder()
                    .message_id(reply_to_message_id)
                    .request_body(
                        ReplyMessageRequestBody.builder()
                        .msg_type("interactive")
                        .content(msg_content)
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
            logger.error("发送卡片异常: %s", e)
            return None

    # ---- 按钮构建 ----

    def _build_buttons(self, is_coco_mode: bool, project_id: Optional[str] = None, is_claude_mode: bool = False) -> list[dict]:
        return build_mode_buttons(is_coco_mode, project_id, is_claude_mode)

    def _build_button_elements(self, buttons: list[dict], layout: str = "responsive") -> list[dict]:
        """为卡片生成按钮区。Delegates to shared.build_responsive_layout."""
        if not buttons:
            return []
        return build_responsive_layout(buttons)

    # ---- 发送/更新/关闭 ----

    def send_streaming_card(self, card: StreamingCard) -> Optional[str]:
        try:
            buttons = self._build_buttons(card.is_coco_mode, card.project_id, card.is_claude_mode)
            initial = _normalize_streaming_markdown(
                card.last_content,
                is_final=False,
                max_chars=self._max_card_chars,
            )
            card_json = self._build_card_json(
                title=card.title,
                header_template=card.header_template,
                project_path=card.project_path,
                initial_content=initial,
                image_keys=card.image_keys,
                buttons=buttons,
                streaming_mode=True,
            )
            content = json.dumps(card_json, ensure_ascii=False)

            if card.reply_to_message_id:
                request = (
                    ReplyMessageRequest.builder()
                    .message_id(card.reply_to_message_id)
                    .request_body(
                        ReplyMessageRequestBody.builder()
                        .msg_type("interactive")
                        .content(content)
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

    def update_content(self, card: StreamingCard, content: str) -> bool:
        if not card.message_id:
            return False

        now = time.time()
        if card.last_update_at and now - card.last_update_at < card.min_update_interval_s:
            # 节流：过于频繁的 update 会触发飞书限流/失败
            return True

        normalized = _normalize_streaming_markdown(
            content,
            is_final=False,
            max_chars=self._max_card_chars,
        )

        if normalized == card.last_content:
            return True

        try:
            buttons = self._build_buttons(card.is_coco_mode, card.project_id, card.is_claude_mode)
            card_json = self._build_card_json(
                title=card.title,
                header_template=card.header_template,
                project_path=card.project_path,
                initial_content=normalized,
                image_keys=card.image_keys,
                buttons=buttons,
                streaming_mode=True,
            )
            req = (
                UpdateMessageRequest.builder()
                .message_id(card.message_id)
                .request_body(
                    UpdateMessageRequestBody.builder()
                    .msg_type("interactive")
                    .content(json.dumps(card_json, ensure_ascii=False))
                    .build()
                )
                .build()
            )
            resp = self._client.im.v1.message.update(req)
            if not resp.success():
                logger.warning("卡片消息更新失败: code=%s, msg=%s", resp.code, resp.msg)
                return False

            card.last_content = normalized
            card.last_update_at = now
            return True
        except Exception as e:
            logger.warning("卡片消息更新异常: %s", e)
            return False

    def close_streaming(self, card: StreamingCard, final_content: Optional[str] = None) -> bool:
        if not card.message_id:
            return False

        try:
            final_text = final_content if final_content is not None else card.last_content
            normalized = _normalize_streaming_markdown(
                final_text,
                is_final=True,
                max_chars=self._max_card_chars,
            )
            buttons = self._build_buttons(card.is_coco_mode, card.project_id, card.is_claude_mode)
            card_json = self._build_card_json(
                title=card.title,
                header_template=card.header_template,
                project_path=card.project_path,
                initial_content=normalized,
                image_keys=card.image_keys,
                buttons=buttons,
                streaming_mode=False,
            )
            req = (
                UpdateMessageRequest.builder()
                .message_id(card.message_id)
                .request_body(
                    UpdateMessageRequestBody.builder()
                    .msg_type("interactive")
                    .content(json.dumps(card_json, ensure_ascii=False))
                    .build()
                )
                .build()
            )
            resp = self._client.im.v1.message.update(req)
            if not resp.success():
                logger.warning("关闭流式失败: code=%s, msg=%s", resp.code, resp.msg)
                return False

            with self._lock:
                self._cards.pop(card.message_id, None)
            return True
        except Exception as e:
            logger.warning("关闭流式异常: %s", e)
            return False

    # ---- 查询/清理 ----

    def get_card(self, card_id: str) -> Optional[StreamingCard]:
        with self._lock:
            return self._cards.get(card_id)

    def cleanup_expired_cards(self, max_age_seconds: int = 3600):
        now = time.time()
        with self._lock:
            expired = [
                card_id for card_id, card in self._cards.items()
                if now - card.created_at > max_age_seconds
            ]
            for card_id in expired:
                del self._cards[card_id]
                logger.info("清理过期卡片: %s", card_id)
