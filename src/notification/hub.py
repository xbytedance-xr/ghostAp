import json
import threading
from enum import Enum
from typing import Optional, Callable, Any
from dataclasses import dataclass, field
from datetime import datetime

from ..project.context import ProjectContext
from ..card.builder import CardBuilder


class NotificationType(Enum):
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_BLOCKED = "task_blocked"
    PROJECT_SWITCHED = "project_switched"
    SESSION_EXPIRED = "session_expired"
    COCO_COMPLETED = "coco_completed"
    SYSTEM_ALERT = "system_alert"


@dataclass
class Notification:
    notification_id: str
    notification_type: NotificationType
    project: Optional[ProjectContext]
    title: str
    content: str
    suggestions: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    chat_id: Optional[str] = None
    extra_data: dict = field(default_factory=dict)


class NotificationHub:
    def __init__(self, send_message_callback: Optional[Callable[[str, str, str], Optional[str]]] = None):
        self._send_message = send_message_callback
        self._notification_history: list[Notification] = []
        self._max_history = 50
        self._lock = threading.Lock()
        self._notification_counter = 0

    def set_send_callback(self, callback: Callable[[str, str, str], Optional[str]]):
        self._send_message = callback

    def _generate_id(self) -> str:
        with self._lock:
            self._notification_counter += 1
            return f"notif_{self._notification_counter}_{int(datetime.now().timestamp())}"

    def notify(
        self,
        chat_id: str,
        notification_type: NotificationType,
        title: str,
        content: str,
        project: Optional[ProjectContext] = None,
        suggestions: Optional[list[str]] = None,
        extra_data: Optional[dict] = None,
    ) -> Optional[str]:
        notification = Notification(
            notification_id=self._generate_id(),
            notification_type=notification_type,
            project=project,
            title=title,
            content=content,
            suggestions=suggestions or [],
            chat_id=chat_id,
            extra_data=extra_data or {},
        )

        with self._lock:
            self._notification_history.append(notification)
            if len(self._notification_history) > self._max_history:
                self._notification_history = self._notification_history[-self._max_history:]

        return self._send_notification(notification)

    def _send_notification(self, notification: Notification) -> Optional[str]:
        if not self._send_message or not notification.chat_id:
            return None

        type_map = {
            NotificationType.TASK_COMPLETED: "task_complete",
            NotificationType.TASK_FAILED: "error",
            NotificationType.TASK_BLOCKED: "warning",
            NotificationType.PROJECT_SWITCHED: "info",
            NotificationType.SESSION_EXPIRED: "warning",
            NotificationType.COCO_COMPLETED: "success",
            NotificationType.SYSTEM_ALERT: "warning",
        }

        if notification.project:
            msg_type, card_content = CardBuilder.build_notification_card(
                project=notification.project,
                notification_type=type_map.get(notification.notification_type, "info"),
                title=notification.title,
                content=notification.content,
                suggestions=notification.suggestions,
            )
        else:
            card = {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {"tag": "plain_text", "content": f"📢 {notification.title}"},
                    "template": "blue"
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {"tag": "lark_md", "content": notification.content}
                    }
                ]
            }
            if notification.suggestions:
                suggestion_text = "💡 **建议:**\n" + "\n".join(f"• {s}" for s in notification.suggestions)
                card["elements"].append({
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": suggestion_text}
                })
            msg_type = "interactive"
            card_content = json.dumps(card, ensure_ascii=False)

        return self._send_message(notification.chat_id, card_content, msg_type)

    def notify_task_completed(
        self,
        chat_id: str,
        project: ProjectContext,
        task_name: str,
        result_summary: str,
        duration_seconds: Optional[int] = None,
        suggestions: Optional[list[str]] = None,
    ) -> Optional[str]:
        duration_text = ""
        if duration_seconds is not None:
            if duration_seconds < 60:
                duration_text = f"\n• 耗时: {duration_seconds} 秒"
            else:
                minutes = duration_seconds // 60
                seconds = duration_seconds % 60
                duration_text = f"\n• 耗时: {minutes}分{seconds}秒"

        content = f"🎉 **{task_name}** 执行完成\n\n{result_summary}{duration_text}"

        return self.notify(
            chat_id=chat_id,
            notification_type=NotificationType.TASK_COMPLETED,
            title="任务完成",
            content=content,
            project=project,
            suggestions=suggestions or [
                f"运行 `ls -la` 查看文件",
                "继续下一步开发",
            ],
        )

    def notify_task_failed(
        self,
        chat_id: str,
        project: ProjectContext,
        task_name: str,
        error_message: str,
        suggestions: Optional[list[str]] = None,
    ) -> Optional[str]:
        content = f"❌ **{task_name}** 执行失败\n\n```\n{error_message}\n```"

        return self.notify(
            chat_id=chat_id,
            notification_type=NotificationType.TASK_FAILED,
            title="任务失败",
            content=content,
            project=project,
            suggestions=suggestions or [
                "检查错误信息",
                "使用 Coco 帮助分析问题",
            ],
        )

    def notify_coco_completed(
        self,
        chat_id: str,
        project: ProjectContext,
        query: str,
        response_summary: str,
    ) -> Optional[str]:
        content = f"🤖 Coco 已完成响应\n\n**你的问题:** {query[:100]}{'...' if len(query) > 100 else ''}\n\n**响应摘要:** {response_summary[:200]}{'...' if len(response_summary) > 200 else ''}"

        return self.notify(
            chat_id=chat_id,
            notification_type=NotificationType.COCO_COMPLETED,
            title="Coco 响应完成",
            content=content,
            project=project,
        )

    def notify_project_switched(
        self,
        chat_id: str,
        from_project: Optional[ProjectContext],
        to_project: ProjectContext,
    ) -> Optional[str]:
        from_name = from_project.project_name if from_project else "无"
        content = f"🔄 项目已切换\n\n• 从: **{from_name}**\n• 到: **{to_project.project_name}**\n• 📂 项目目录: `{to_project.root_path}`"

        return self.notify(
            chat_id=chat_id,
            notification_type=NotificationType.PROJECT_SWITCHED,
            title="项目已切换",
            content=content,
            project=to_project,
        )

    def get_recent_notifications(self, count: int = 10) -> list[Notification]:
        with self._lock:
            return self._notification_history[-count:]

    def clear_history(self):
        with self._lock:
            self._notification_history.clear()
