import logging
import sys
from typing import Optional
try:
    from config import get_settings
    from feishu.ws_client import FeishuWSClient, EmojiReaction
    from feishu.message_formatter import FeishuMessageFormatter as fmt
except ImportError:
    from .config import get_settings
    from .feishu.ws_client import FeishuWSClient, EmojiReaction
    from .feishu.message_formatter import FeishuMessageFormatter as fmt

logger = logging.getLogger(__name__)


class Application:
    def __init__(self):
        self.settings = get_settings()
        self.feishu_client: Optional[FeishuWSClient] = None

    def handle_message(self, message_id: str, chat_id: str, command: str, working_dir: Optional[str] = None):
        """Legacy callback — executes shell command directly via SandboxExecutor."""
        try:
            self.feishu_client._system_handler.execute_shell_and_reply(
                message_id, chat_id, command, working_dir,
            )
        except Exception as e:
            logger.error("处理命令异常: %s", e)
            try:
                self.feishu_client.add_reaction(message_id, EmojiReaction.on_error())
                self.feishu_client.reply(message_id, fmt.format_error(str(e)), chat_id=chat_id)
            except Exception:
                pass

    def run(self):
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )

        logger.info("=" * 50)
        logger.info("GhostAP - 飞书机器人Shell沙箱服务")
        logger.info("=" * 50)

        if not self.settings.validate_feishu_config():
            logger.error("配置错误: APP_ID 和 APP_SECRET 未配置或不完整，请在 .env 文件或环境变量中配置")
            sys.exit(1)

        logger.info("APP_ID: %s...", self.settings.app_id[:8])
        logger.info("命令超时: %d秒", self.settings.sandbox_timeout)
        logger.info("意图识别: ARK (%s)", self.settings.ark_model)

        self.feishu_client = FeishuWSClient(message_callback=self.handle_message)

        logger.info("启动飞书长连接服务...")
        logger.info("支持的功能: Shell模式 | Coco模式 | 目录切换")

        try:
            self.feishu_client.start()
        except KeyboardInterrupt:
            logger.info("服务已停止")
        except Exception as e:
            logger.error("服务异常: %s", e)
            sys.exit(1)
        finally:
            try:
                if self.feishu_client:
                    self.feishu_client.close()
            except Exception:
                pass


def main():
    app = Application()
    app.run()


if __name__ == "__main__":
    main()
