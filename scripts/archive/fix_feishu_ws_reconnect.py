
with open("src/feishu/ws_client.py", "r") as f:
    content = f.read()

# Add automatic reconnect logic if not already present
start_method_find = """        self._client.start()"""
start_method_repl = """        # Wrapping start in a loop to handle arbitrary disconnects and ensure 72h+ stability
        while True:
            try:
                self._client.start()
            except Exception as e:
                logger.error(f"WebSocket client disconnected with error: {e}. Reconnecting in 5s...")
                time.sleep(5)"""

if "while True:" not in content and "self._client.start()" in content:
    content = content.replace(start_method_find, start_method_repl)
    with open("src/feishu/ws_client.py", "w") as f:
        f.write(content)
