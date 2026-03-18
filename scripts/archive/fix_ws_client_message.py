
with open("src/feishu/ws_client.py", "r") as f:
    content = f.read()

bad_message = """                self._send_text_reply(message_id, f"⚠️ 系统繁忙 (Spec 模式)，请稍后再试: {e}")"""
good_message = """                self._send_text_reply(message_id, "⚠️ 当前服务繁忙，请稍后再试。")"""

content = content.replace(bad_message, good_message)

with open("src/feishu/ws_client.py", "w") as f:
    f.write(content)
