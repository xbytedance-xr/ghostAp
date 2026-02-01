import logging
import sys
from typing import Optional
try:
    from config import get_settings
    from feishu.ws_client import FeishuWSClient, EmojiReaction
    from feishu.message_formatter import FeishuMessageFormatter as fmt
    from sandbox.executor import SandboxExecutor
except ImportError:
    from .config import get_settings
    from .feishu.ws_client import FeishuWSClient, EmojiReaction
    from .feishu.message_formatter import FeishuMessageFormatter as fmt
    from .sandbox.executor import SandboxExecutor

logger = logging.getLogger(__name__)


class Application:
    def __init__(self):
        self.settings = get_settings()
        self.feishu_client: Optional[FeishuWSClient] = None
        self.sandbox_executor: Optional[SandboxExecutor] = None

    def handle_message(self, message_id: str, chat_id: str, command: str, working_dir: Optional[str] = None):
        try:
            is_safe, reason = self.sandbox_executor.is_command_safe(command)
            if not is_safe:
                self.feishu_client.add_reaction(message_id, EmojiReaction.on_blocked())
                self.feishu_client.reply(message_id, fmt.format_safety_block(command, reason), chat_id=chat_id)
                return

            self.feishu_client.add_reaction(message_id, EmojiReaction.on_processing())
            
            result = self.sandbox_executor.execute(command, cwd=working_dir)
            
            if result.success:
                self.feishu_client.add_reaction(message_id, EmojiReaction.on_shell_executed())
            else:
                self.feishu_client.add_reaction(message_id, EmojiReaction.on_error())
            
            response = fmt.format_command_result(
                command=command,
                working_dir=working_dir,
                stdout=result.stdout,
                stderr=result.stderr,
                return_code=result.return_code,
                success=result.success,
                error_message=result.error_message
            )
            self.feishu_client.reply(message_id, response, chat_id=chat_id)

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

        self.sandbox_executor = SandboxExecutor()
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
