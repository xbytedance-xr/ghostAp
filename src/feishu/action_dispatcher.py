import logging
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class ActionDispatcher:
    """
    负责将飞书卡片的回调动作（Action）分发给对应的处理器（Handler）。
    支持精确匹配（exact match）和前缀匹配（prefix match）。
    """

    def __init__(self):
        self._action_registry_exact: Dict[str, Callable] = {}
        self._action_registry_prefix: List[Tuple[str, Callable]] = []

    def register(self, handler: Callable, exact: Optional[str] = None, prefix: Optional[str] = None):
        """Register a card action handler."""
        if exact:
            self._action_registry_exact[exact] = handler
        if prefix:
            self._action_registry_prefix.append((prefix, handler))

    def dispatch(
        self, action_type: str, open_message_id: str, open_chat_id: str, project_id: Optional[str], value: dict
    ) -> bool:
        """
        分发卡片动作。

        Args:
            action_type: 动作类型字符串 (e.g. "loop_pause")
            open_message_id: 消息ID
            open_chat_id: 会话ID
            project_id: 项目ID (可能为空)
            value: 动作携带的完整数据字典

        Returns:
            bool: 是否找到并执行了对应的 handler
        """
        # 1. Exact match
        if action_type in self._action_registry_exact:
            try:
                self._action_registry_exact[action_type](open_message_id, open_chat_id, project_id, value)
                return True
            except TypeError as e:
                # 兼容旧的 handler 签名 (可能只接受 3 个参数，或者其他变体)
                # 但目前约定是 (mid, cid, pid, value)
                logger.error(f"Action handler for '{action_type}' signature mismatch: {str(e) or repr(e)}")
                raise e

        # 2. Prefix match
        for prefix, handler in self._action_registry_prefix:
            if action_type.startswith(prefix):
                try:
                    handler(open_message_id, open_chat_id, project_id, value, type=action_type)
                    return True
                except TypeError as e:
                    logger.error(f"Prefix handler for '{prefix}' signature mismatch: {str(e) or repr(e)}")
                    raise e

        return False
