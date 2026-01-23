import json
import time
import threading
from typing import Optional, Callable
from dataclasses import dataclass, field
import lark_oapi as lark
from lark_oapi.api.cardkit.v1 import *
from lark_oapi.api.im.v1 import *


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
        reply_to_message_id: Optional[str] = None,
    ) -> Optional[StreamingCard]:
        mode_icon = "🤖" if is_coco_mode else "🧠"
        if project_name:
            title = f"{mode_icon} {project_name}"
        else:
            mode_name = "编程模式" if is_coco_mode else "智能模式"
            title = f"{mode_icon} {mode_name}"

        path_display = project_path or "~"

        buttons = self._build_buttons(is_coco_mode, project_id)

        card_json = {
            "schema": "2.0",
            "config": {
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
                "template": "blue"
            },
            "body": {
                "elements": [
                    {
                        "tag": "markdown",
                        "content": f"📁 `{path_display}`",
                        "element_id": "path_md"
                    },
                    {"tag": "hr"},
                    {
                        "tag": "markdown",
                        "content": initial_content,
                        "element_id": element_id
                    },
                    {"tag": "hr"},
                    {
                        "tag": "column_set",
                        "flex_mode": "stretch",
                        "background_style": "default",
                        "columns": [
                            {
                                "tag": "column",
                                "width": "weighted",
                                "weight": 1,
                                "elements": [buttons[0]] if buttons else []
                            },
                            {
                                "tag": "column",
                                "width": "weighted",
                                "weight": 1,
                                "elements": [buttons[1]] if len(buttons) > 1 else []
                            }
                        ]
                    }
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

    def _build_buttons(self, is_coco_mode: bool, project_id: Optional[str] = None) -> list[dict]:
        if is_coco_mode:
            return [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "🚪 退出Coco"},
                    "type": "default",
                    "behaviors": [{"type": "callback", "value": {"action": "exit_coco", "project_id": project_id}}]
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "🔄 切换项目"},
                    "type": "default",
                    "behaviors": [{"type": "callback", "value": {"action": "switch_project"}}]
                }
            ]
        else:
            return [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "🤖 编程模式"},
                    "type": "primary",
                    "behaviors": [{"type": "callback", "value": {"action": "enter_coco", "project_id": project_id}}]
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "📋 选择项目"},
                    "type": "default",
                    "behaviors": [{"type": "callback", "value": {"action": "show_board"}}]
                }
            ]

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
