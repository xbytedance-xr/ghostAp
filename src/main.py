import sys
from typing import Optional
from .config import get_settings
from .feishu.ws_client import FeishuWSClient, EmojiType
from .feishu.message_formatter import FeishuMessageFormatter as fmt
from .sandbox.executor import SandboxExecutor
from .agent.shell_agent import ShellAgent

feishu_client: FeishuWSClient = None
sandbox_executor: SandboxExecutor = None
shell_agent: ShellAgent = None


def handle_message(message_id: str, chat_id: str, command: str, working_dir: Optional[str] = None):
    global feishu_client, sandbox_executor, shell_agent

    try:
        is_safe, reason = sandbox_executor.is_command_safe(command)
        if not is_safe:
            feishu_client.add_reaction(message_id, EmojiType.CROSS_MARK)
            feishu_client.reply(message_id, fmt.format_safety_block(command, reason))
            return

        result = sandbox_executor.execute(command, cwd=working_dir)
        
        if result.success:
            feishu_client.add_reaction(message_id, EmojiType.DONE)
        else:
            feishu_client.add_reaction(message_id, EmojiType.CROSS_MARK)
        
        response = fmt.format_command_result(
            command=command,
            working_dir=working_dir,
            stdout=result.stdout,
            stderr=result.stderr,
            return_code=result.return_code,
            success=result.success,
            error_message=result.error_message
        )
        feishu_client.reply(message_id, response)

    except Exception as e:
        print(f"处理命令异常: {e}")
        try:
            feishu_client.add_reaction(message_id, EmojiType.CROSS_MARK)
            feishu_client.reply(message_id, fmt.format_error(str(e)))
        except Exception:
            pass


def main():
    global feishu_client, sandbox_executor, shell_agent

    settings = get_settings()

    print("=" * 50)
    print("🔮 GhostAP - 飞书机器人Shell沙箱服务")
    print("=" * 50)

    if not settings.validate_feishu_config():
        print("\n❌ 配置错误:")
        print("   - APP_ID 和 APP_SECRET 未配置或不完整")
        print("   - 请在 .env 文件或环境变量中配置")
        print("   - 参考 .env.example 文件\n")
        sys.exit(1)

    print(f"📱 APP_ID: {settings.app_id[:8]}...")
    print(f"⏱️  命令超时: {settings.sandbox_timeout}秒")
    print(f"🧠 意图识别: Ollama ({settings.ollama_model})")
    print()

    sandbox_executor = SandboxExecutor()
    shell_agent = ShellAgent()
    feishu_client = FeishuWSClient(message_callback=handle_message)

    print("🚀 启动飞书长连接服务...")
    print()
    print("💡 支持的功能:")
    print("   📟 Shell 模式 - 直接发送命令执行")
    print("   🤖 Coco 模式  - 说「帮我写代码」进入AI对话")
    print("   📁 目录切换   - 说「切换到xxx目录」")
    print()

    try:
        feishu_client.start()
    except KeyboardInterrupt:
        print("\n👋 服务已停止")
    except Exception as e:
        print(f"\n❌ 服务异常: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
