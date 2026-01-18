import sys
from typing import Optional
try:
    from config import get_settings
    from feishu.ws_client import FeishuWSClient, EmojiReaction
    from feishu.message_formatter import FeishuMessageFormatter as fmt
    from sandbox.executor import SandboxExecutor
    from agent.shell_agent import ShellAgent
except ImportError:
    from .config import get_settings
    from .feishu.ws_client import FeishuWSClient, EmojiReaction
    from .feishu.message_formatter import FeishuMessageFormatter as fmt
    from .sandbox.executor import SandboxExecutor
    from .agent.shell_agent import ShellAgent


class Application:
    def __init__(self):
        self.settings = get_settings()
        self.feishu_client: Optional[FeishuWSClient] = None
        self.sandbox_executor: Optional[SandboxExecutor] = None
        self.shell_agent: Optional[ShellAgent] = None

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
            print(f"处理命令异常: {e}")
            try:
                self.feishu_client.add_reaction(message_id, EmojiReaction.on_error())
                self.feishu_client.reply(message_id, fmt.format_error(str(e)), chat_id=chat_id)
            except Exception:
                pass

    def run(self):
        print("=" * 50)
        print("🔮 GhostAP - 飞书机器人Shell沙箱服务")
        print("=" * 50)

        if not self.settings.validate_feishu_config():
            print("\n❌ 配置错误:")
            print("   - APP_ID 和 APP_SECRET 未配置或不完整")
            print("   - 请在 .env 文件或环境变量中配置")
            print("   - 参考 .env.example 文件\n")
            sys.exit(1)

        print(f"📱 APP_ID: {self.settings.app_id[:8]}...")
        print(f"⏱️  命令超时: {self.settings.sandbox_timeout}秒")
        print(f"🧠 意图识别: ARK ({self.settings.ark_model})")
        print()

        self.sandbox_executor = SandboxExecutor()
        self.shell_agent = ShellAgent()
        self.feishu_client = FeishuWSClient(message_callback=self.handle_message)

        print("🚀 启动飞书长连接服务...")
        print()
        print("💡 支持的功能:")
        print("   📟 Shell 模式 - 直接发送命令执行")
        print("   🤖 Coco 模式  - 说「帮我写代码」进入AI对话")
        print("   📁 目录切换   - 说「切换到xxx目录」")
        print()

        try:
            self.feishu_client.start()
        except KeyboardInterrupt:
            print("\n👋 服务已停止")
        except Exception as e:
            print(f"\n❌ 服务异常: {e}")
            sys.exit(1)


def main():
    app = Application()
    app.run()


if __name__ == "__main__":
    main()
