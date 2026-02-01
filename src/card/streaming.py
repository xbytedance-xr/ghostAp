import json
import time
import threading
from typing import Optional, Callable
from dataclasses import dataclass, field
import lark_oapi as lark
from lark_oapi.api.cardkit.v1 import *
from lark_oapi.api.im.v1 import *

from ..config import get_settings


@dataclass
class StreamingCard:
    card_id: str
    element_id: str
    chat_id: str
    message_id: Optional[str] = None
    sequence: int = 1
    created_at: float = field(default_factory=time.time)
    last_content: str = ""
    project_id: Optional[str] = None
    reply_to_message_id: Optional[str] = None


class StreamingCardManager:
    def __init__(self, client: lark.Client):
        self._client = client
        self._cards: dict[str, StreamingCard] = {}
        self._lock = threading.Lock()

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
        settings = get_settings()
        if is_claude_mode:
            mode_icon = "🔮"
        elif is_coco_mode:
            mode_icon = "🤖"
        else:
            mode_icon = "🧠"

        if project_name:
            if is_claude_mode:
                title = f"🔮 {project_name} · Claude"
            elif is_coco_mode:
                title = f"🤖 {project_name} · Coco"
            else:
                title = f"🧠 {project_name}"
        else:
            if is_claude_mode:
                mode_name = "Claude 编程模式"
            elif is_coco_mode:
                mode_name = "编程模式"
            else:
                mode_name = "智能模式"
            title = f"{mode_icon} {mode_name}"

        path_display = project_path or "~"

        buttons = self._build_buttons(is_coco_mode, project_id, is_claude_mode)

        # 头部颜色：Coco/Claude 快速区分
        if is_claude_mode:
            header_template = "purple"
        elif is_coco_mode:
            header_template = "blue"
        else:
            header_template = "turquoise"

        button_elements = self._build_button_elements(buttons, layout=(settings.card_button_layout or "responsive"))

        card_json = {
            "schema": "2.0",
            "config": {
                "wide_screen_mode": True,
                "update_multi": True,
                "streaming_mode": True,
                "streaming_config": {
                    "print_frequency_ms": {
                        "default": 30,
                        "android": 30,
                        "ios": 30,
                        "pc": 30
                    },
                    "print_step": {
                        "default": 3,
                        "android": 3,
                        "ios": 3,
                        "pc": 3
                    },
                    "print_strategy": "fast"
                }
            },
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": header_template
            },
            "body": {
                "elements": [
                    {
                        "tag": "markdown",
                        "content": f"📁 `{path_display}`",
                        "element_id": "path_md"
                    },
                    {"tag": "hr"},
                    *([
                        *[
                            {
                                "tag": "img",
                                "img_key": key,
                                "alt": {"tag": "plain_text", "content": f"图片 {i + 1}"}
                            }
                            for i, key in enumerate(image_keys)
                        ],
                        {"tag": "hr"},
                    ] if image_keys else []),
                    {
                        "tag": "markdown",
                        "content": initial_content,
                        "element_id": element_id
                    },
                    *([{"tag": "hr"}, *button_elements] if button_elements else [])
                ]
            }
        }

        try:
            print(f"📤 创建流式卡片: chat_id={chat_id}, project={project_name}")
            request = CreateCardRequest.builder() \
                .request_body(CreateCardRequestBody.builder()
                    .type("card_json")
                    .data(json.dumps(card_json, ensure_ascii=False))
                    .build()) \
                .build()

            response = self._client.cardkit.v1.card.create(request)

            if not response.success():
                print(f"❌ 创建卡片实体失败: code={response.code}, msg={response.msg}")
                return None

            card_id = response.data.card_id
            print(f"✅ 创建流式卡片成功: card_id={card_id}")

            streaming_card = StreamingCard(
                card_id=card_id,
                element_id=element_id,
                chat_id=chat_id,
                last_content=initial_content,
                project_id=project_id,
                reply_to_message_id=reply_to_message_id
            )

            with self._lock:
                self._cards[card_id] = streaming_card

            return streaming_card

        except Exception as e:
            print(f"❌ 创建卡片实体异常: {e}")
            import traceback
            traceback.print_exc()
            return None

    def _build_buttons(self, is_coco_mode: bool, project_id: Optional[str] = None, is_claude_mode: bool = False) -> list[dict]:
        if is_claude_mode:
            return [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "🚪 退出Claude"},
                    "type": "default",
                    "size": "small",
                    "behaviors": [{"type": "callback", "value": {"action": "exit_claude", "project_id": project_id}}]
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "🔄 切换项目"},
                    "type": "default",
                    "size": "small",
                    "behaviors": [{"type": "callback", "value": {"action": "switch_project"}}]
                }
            ]
        elif is_coco_mode:
            return [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "🚪 退出Coco"},
                    "type": "default",
                    "size": "small",
                    "behaviors": [{"type": "callback", "value": {"action": "exit_coco", "project_id": project_id}}]
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "🔄 切换项目"},
                    "type": "default",
                    "size": "small",
                    "behaviors": [{"type": "callback", "value": {"action": "switch_project"}}]
                }
            ]
        else:
            return [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "🤖 Coco模式"},
                    "type": "primary",
                    "size": "small",
                    "behaviors": [{"type": "callback", "value": {"action": "enter_coco", "project_id": project_id}}]
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "🔮 Claude模式"},
                    "type": "default",
                    "size": "small",
                    "behaviors": [{"type": "callback", "value": {"action": "enter_claude", "project_id": project_id}}]
                }
            ]

    def _build_button_elements(self, buttons: list[dict], layout: str = "responsive") -> list[dict]:
        """为流式卡片生成按钮区。

        - desktop: 使用 action（桌面端更紧凑）
        - mobile: 强制两列 column_set
        - responsive: <=2 个按钮用 action，否则用两列
        """
        if not buttons:
            return []
        layout = (layout or "responsive").strip().lower()

        def as_action(btns: list[dict]) -> list[dict]:
            return [{"tag": "action", "actions": btns}]

        def as_grid_two(btns: list[dict]) -> list[dict]:
            # 目前 streaming 场景按钮数固定 <=2，直接生成两列
            col_1 = btns[0] if len(btns) > 0 else None
            col_2 = btns[1] if len(btns) > 1 else None
            return [{
                "tag": "column_set",
                "flex_mode": "stretch",
                "background_style": "default",
                "columns": [
                    {"tag": "column", "width": "weighted", "weight": 1, "elements": [col_1] if col_1 else []},
                    {"tag": "column", "width": "weighted", "weight": 1, "elements": [col_2] if col_2 else []},
                ]
            }]

        if layout == "desktop":
            return as_action(buttons)
        if layout == "mobile":
            return as_grid_two(buttons)

        # responsive
        if len(buttons) <= 2:
            return as_action(buttons)
        return as_grid_two(buttons)

    def send_streaming_card(self, card: StreamingCard) -> Optional[str]:
        content = json.dumps({
            "type": "card",
            "data": {"card_id": card.card_id}
        })

        try:
            if card.reply_to_message_id:
                print(f"📤 回复流式卡片: card_id={card.card_id}, reply_to={card.reply_to_message_id}")
                from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody
                request = ReplyMessageRequest.builder() \
                    .message_id(card.reply_to_message_id) \
                    .request_body(ReplyMessageRequestBody.builder()
                        .msg_type("interactive")
                        .content(content)
                        .build()) \
                    .build()
                response = self._client.im.v1.message.reply(request)
            else:
                print(f"📤 发送流式卡片: card_id={card.card_id}, chat_id={card.chat_id}")
                request = CreateMessageRequest.builder() \
                    .receive_id_type("chat_id") \
                    .request_body(CreateMessageRequestBody.builder()
                        .receive_id(card.chat_id)
                        .msg_type("interactive")
                        .content(content)
                        .build()) \
                    .build()
                response = self._client.im.v1.message.create(request)

            if not response.success():
                print(f"❌ 发送流式卡片失败: code={response.code}, msg={response.msg}")
                return None

            card.message_id = response.data.message_id
            print(f"✅ 发送流式卡片成功: message_id={card.message_id}")
            return card.message_id

        except Exception as e:
            print(f"❌ 发送流式卡片异常: {e}")
            import traceback
            traceback.print_exc()
            return None

    def update_content(self, card: StreamingCard, content: str) -> bool:
        if content == card.last_content:
            return True

        try:
            request = ContentCardElementRequest.builder() \
                .card_id(card.card_id) \
                .element_id(card.element_id) \
                .request_body(ContentCardElementRequestBody.builder()
                    .content(content)
                    .sequence(card.sequence)
                    .build()) \
                .build()

            response = self._client.cardkit.v1.card_element.content(request)

            if not response.success():
                print(f"⚠️ 流式更新失败: code={response.code}, msg={response.msg}, seq={card.sequence}")
                return False

            card.sequence += 1
            card.last_content = content
            return True

        except Exception as e:
            print(f"⚠️ 流式更新异常: {e}")
            return False

    def close_streaming(self, card: StreamingCard, final_content: Optional[str] = None) -> bool:
        if final_content and final_content != card.last_content:
            self.update_content(card, final_content)

        settings = json.dumps({
            "config": {"streaming_mode": False}
        })

        try:
            print(f"📤 关闭流式模式: card_id={card.card_id}")
            request = SettingsCardRequest.builder() \
                .card_id(card.card_id) \
                .request_body(SettingsCardRequestBody.builder()
                    .settings(settings)
                    .sequence(card.sequence)
                    .build()) \
                .build()

            response = self._client.cardkit.v1.card.settings(request)

            if not response.success():
                print(f"⚠️ 关闭流式模式失败: code={response.code}, msg={response.msg}")
                return False

            card.sequence += 1
            print(f"✅ 关闭流式模式成功")

            with self._lock:
                if card.card_id in self._cards:
                    del self._cards[card.card_id]

            return True

        except Exception as e:
            print(f"⚠️ 关闭流式模式异常: {e}")
            return False

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
                print(f"🧹 清理过期卡片: {card_id}")
