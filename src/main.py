import argparse
import logging
import os
import signal
import sys
import threading
from typing import Optional

try:
    from config import get_settings, ConfigurationError
    from feishu.message_formatter import FeishuMessageFormatter as fmt
    from feishu.ws_client import EmojiReaction, FeishuWSClient
    from utils.errors import get_error_detail
except ImportError:
    from .config import get_settings, ConfigurationError
    from .feishu.message_formatter import FeishuMessageFormatter as fmt
    from .feishu.ws_client import EmojiReaction, FeishuWSClient
    from .utils.errors import get_error_detail

logger = logging.getLogger(__name__)


class Application:
    def __init__(self):
        self.settings = get_settings()
        self.feishu_client: Optional[FeishuWSClient] = None
        self._shutdown_once = threading.Event()

    def _install_signal_handlers(self):
        """Ensure SIGTERM triggers graceful cleanup; ignore SIGHUP to survive
        terminal/SSH disconnects that previously killed the service mid-run.

        restart.sh uses SIGTERM; without a handler Python may exit immediately
        and skip finally blocks, leaving child agent processes orphaned.
        """

        def _handle_sigterm(signum, frame):  # pragma: no cover
            if self._shutdown_once.is_set():
                return
            self._shutdown_once.set()
            try:
                sig_name = signal.Signals(signum).name
            except Exception:
                logger.debug("_handle_sigterm: signal name lookup failed for %s", signum, exc_info=True)
                sig_name = str(signum)
            logger.warning("收到终止信号 %s，开始优雅停机", sig_name)
            raise KeyboardInterrupt

        try:
            signal.signal(signal.SIGTERM, _handle_sigterm)
        except Exception:
            # Some environments (non-main thread / restricted) may fail; best-effort.
            logger.debug("_install_signal_handlers: SIGTERM handler install failed", exc_info=True)

        # 忽略 SIGHUP：避免 SSH 会话/终端关闭意外终止长时间运行的引擎任务。
        # restart.sh 使用 SIGTERM 停机，不依赖 SIGHUP。
        try:
            if hasattr(signal, "SIGHUP"):
                signal.signal(signal.SIGHUP, signal.SIG_IGN)
        except Exception:
            logger.debug("_install_signal_handlers: SIGHUP ignore failed", exc_info=True)

    def handle_message(self, message_id: str, chat_id: str, command: str, working_dir: Optional[str] = None):
        """Legacy callback — executes shell command directly via SandboxExecutor."""
        try:
            self.feishu_client._system_handler.execute_shell_and_reply(
                message_id,
                chat_id,
                command,
                working_dir,
            )
        except Exception as e:
            logger.error("处理命令异常: %s", get_error_detail(e))
            try:
                self.feishu_client.add_reaction(message_id, EmojiReaction.on_error())
                self.feishu_client.reply(message_id, fmt.format_error(get_error_detail(e)), chat_id=chat_id)
            except Exception:
                logger.debug("failed to reply error message", exc_info=True)

    @staticmethod
    def _shutdown_lock_managers() -> None:
        """Best-effort shutdown of lock-manager singletons (stops daemon threads)."""
        try:
            from chat_lock import shutdown_if_active as _chat_shutdown
        except ImportError:
            from .chat_lock import shutdown_if_active as _chat_shutdown
        try:
            from repo_lock import shutdown_if_active as _repo_shutdown
        except ImportError:
            from .repo_lock import shutdown_if_active as _repo_shutdown

        _chat_shutdown()
        _repo_shutdown()

    def run(self):
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )

        logger.info("=" * 50)
        logger.info("GhostAP - 飞书机器人Shell沙箱服务")
        logger.info("=" * 50)

        if not self.settings.validate_feishu_config():
            logger.error("配置错误: APP_ID 和 APP_SECRET 未配置或不完整，请参考 .env.example 模板配置 .env 文件")
            sys.exit(1)

        logger.info("APP_ID: %s...", self.settings.app_id[:8])
        logger.info("命令超时: %d秒", self.settings.sandbox_timeout)
        if self.settings.default_acp_tool:
            logger.info("默认 ACP 工具: %s", self.settings.default_acp_tool)
        else:
            logger.info("默认模式: 纯 Shell")

        # TTADK 常用工具模型预热（后台 best-effort）
        try:
            if getattr(self.settings, "ttadk_preheat_enabled", True) and getattr(
                self.settings, "ttadk_preheat_on_startup", True
            ):
                from ttadk import get_ttadk_manager

                get_ttadk_manager().kickoff_preheat_common_models(cwd=os.getcwd())
        except Exception:
            logger.debug("Application.run: TTADK preheat failed", exc_info=True)

        self._install_signal_handlers()

        self.feishu_client = FeishuWSClient(message_callback=self.handle_message)

        logger.info("启动飞书长连接服务...")
        logger.info("支持的功能: Shell模式 | Coco模式 | 目录切换")

        try:
            self.feishu_client.start()
        except KeyboardInterrupt:
            logger.info("服务已停止")
        except Exception as e:
            logger.error("服务异常: %s", get_error_detail(e))
            sys.exit(1)
        finally:
            # Shut down lock-manager cleanup daemons before closing the WS client
            # so that background threads do not fire callbacks on a half-torn-down
            # Feishu client.
            for _shutdown_fn in (
                self._shutdown_lock_managers,
            ):
                try:
                    _shutdown_fn()
                except Exception:
                    logger.debug("lock manager shutdown error", exc_info=True)
            try:
                if self.feishu_client:
                    self.feishu_client.close()
            except Exception:
                logger.debug("failed to close feishu client", exc_info=True)


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="GhostAP - 飞书机器人服务")
    parser.add_argument(
        "--validate", "--check-config",
        action="store_true",
        dest="validate",
        help="仅校验配置后退出，不启动服务",
    )
    args, _ = parser.parse_known_args(argv)

    if args.validate:
        try:
            settings = get_settings()
            if not settings.validate_feishu_config():
                sys.stderr.write(
                    f"{'=' * 40}\n[配置校验失败]\n"
                    "飞书应用配置不完整: APP_ID 和 APP_SECRET 不能为空\n"
                    f"{'=' * 40}\n"
                )
                sys.exit(1)
            print("配置校验通过")
            sys.exit(0)
        except ConfigurationError as e:
            sys.stderr.write(f"{'=' * 40}\n[配置校验失败]\n{e}\n{'=' * 40}\n")
            sys.exit(1)

    try:
        app = Application()
    except ConfigurationError as e:
        sys.stderr.write(f"{'=' * 40}\n[配置校验失败]\n{e}\n{'=' * 40}\n")
        sys.exit(1)
    app.run()


if __name__ == "__main__":
    main()
