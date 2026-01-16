import json
import hashlib
import base64
from typing import Optional, Callable, Awaitable
from dataclasses import dataclass
from Crypto.Cipher import AES
from ..config import get_settings


@dataclass
class MessageEvent:
    event_id: str
    message_id: str
    chat_id: str
    chat_type: str
    sender_id: str
    sender_type: str
    content: str
    message_type: str
    create_time: str

    @classmethod
    def from_event_data(cls, data: dict) -> "MessageEvent":
        event = data.get("event", {})
        message = event.get("message", {})
        sender = event.get("sender", {})

        content_str = message.get("content", "{}")
        try:
            content_dict = json.loads(content_str)
            content = content_dict.get("text", content_str)
        except json.JSONDecodeError:
            content = content_str

        return cls(
            event_id=data.get("header", {}).get("event_id", ""),
            message_id=message.get("message_id", ""),
            chat_id=message.get("chat_id", ""),
            chat_type=message.get("chat_type", ""),
            sender_id=sender.get("sender_id", {}).get("open_id", ""),
            sender_type=sender.get("sender_type", ""),
            content=content,
            message_type=message.get("message_type", ""),
            create_time=message.get("create_time", ""),
        )


class FeishuEventHandler:
    def __init__(self):
        self.settings = get_settings()
        self._processed_events: set[str] = set()
        self._max_event_cache = 1000

    def _decrypt_data(self, encrypt: str) -> dict:
        if not self.settings.encrypt_key:
            raise ValueError("未配置encrypt_key，无法解密消息")

        key = hashlib.sha256(self.settings.encrypt_key.encode()).digest()
        cipher_text = base64.b64decode(encrypt)

        cipher = AES.new(key, AES.MODE_CBC, cipher_text[:AES.block_size])
        decrypted = cipher.decrypt(cipher_text[AES.block_size:])

        padding_len = decrypted[-1]
        decrypted = decrypted[:-padding_len]

        return json.loads(decrypted.decode())

    def handle_challenge(self, data: dict) -> Optional[dict]:
        if "challenge" in data:
            return {"challenge": data["challenge"]}
        return None

    def is_event_processed(self, event_id: str) -> bool:
        if event_id in self._processed_events:
            return True

        self._processed_events.add(event_id)
        if len(self._processed_events) > self._max_event_cache:
            to_remove = list(self._processed_events)[:self._max_event_cache // 2]
            for item in to_remove:
                self._processed_events.discard(item)

        return False

    def parse_event(self, raw_data: dict) -> Optional[MessageEvent]:
        if "encrypt" in raw_data:
            try:
                raw_data = self._decrypt_data(raw_data["encrypt"])
            except Exception as e:
                print(f"解密消息失败: {e}")
                return None

        header = raw_data.get("header", {})
        event_type = header.get("event_type", "")

        if event_type != "im.message.receive_v1":
            return None

        event_id = header.get("event_id", "")
        if self.is_event_processed(event_id):
            return None

        return MessageEvent.from_event_data(raw_data)

    def extract_command(self, content: str) -> Optional[str]:
        content = content.strip()

        if content.startswith("@"):
            parts = content.split(None, 1)
            if len(parts) > 1:
                content = parts[1].strip()
            else:
                return None

        prefixes = ["/shell ", "/sh ", "/exec ", "$ "]
        for prefix in prefixes:
            if content.lower().startswith(prefix.lower()):
                return content[len(prefix):].strip()

        if content.startswith("/"):
            return None

        return content
