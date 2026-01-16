import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from .config import get_settings
from .feishu.client import FeishuClient
from .feishu.handler import FeishuEventHandler, MessageEvent
from .sandbox.executor import SandboxExecutor
from .agent.shell_agent import ShellAgent


feishu_client: FeishuClient = None
event_handler: FeishuEventHandler = None
sandbox_executor: SandboxExecutor = None
shell_agent: ShellAgent = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global feishu_client, event_handler, sandbox_executor, shell_agent

    settings = get_settings()
    if not settings.validate_feishu_config():
        print("⚠️ 警告: 飞书配置不完整，请检查 APP_ID 和 APP_SECRET")

    feishu_client = FeishuClient()
    event_handler = FeishuEventHandler()
    sandbox_executor = SandboxExecutor()
    shell_agent = ShellAgent()

    print(f"🚀 GhostAP 服务启动")
    print(f"📡 监听地址: {settings.server_host}:{settings.server_port}")
    print(f"🤖 Ollama模型: {settings.ollama_model}")

    yield

    await feishu_client.close()
    print("👋 GhostAP 服务已停止")


app = FastAPI(
    title="GhostAP",
    description="飞书机器人Shell沙箱服务",
    version="0.1.0",
    lifespan=lifespan,
)


async def process_message(event: MessageEvent):
    try:
        command = event_handler.extract_command(event.content)
        if not command:
            await feishu_client.reply_message(
                event.message_id,
                "💡 使用说明:\n"
                "- 直接发送shell命令即可执行\n"
                "- 或使用 /shell <命令> 格式\n"
                "- 示例: ls -la 或 /shell whoami"
            )
            return

        safety_result = await shell_agent.check_command_safety(command)

        if not safety_result.is_safe:
            await feishu_client.reply_message(
                event.message_id,
                f"🚫 命令被AI安全检查拦截\n"
                f"风险等级: {safety_result.risk_level}\n"
                f"原因: {safety_result.reason}"
            )
            return

        if safety_result.risk_level in ["high", "critical"]:
            warning = f"⚠️ 警告: 该命令风险等级为 {safety_result.risk_level}\n原因: {safety_result.reason}\n\n"
        else:
            warning = ""

        result = await sandbox_executor.execute_async(command)

        response = f"🖥️ 执行命令: `{command}`\n\n{warning}{result.to_message()}"

        await feishu_client.reply_message(event.message_id, response)

    except Exception as e:
        print(f"处理消息异常: {e}")
        try:
            await feishu_client.reply_message(
                event.message_id,
                f"❌ 处理消息时发生错误: {str(e)}"
            )
        except Exception:
            pass


@app.post("/webhook/event")
async def handle_feishu_event(request: Request, background_tasks: BackgroundTasks):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    challenge_response = event_handler.handle_challenge(data)
    if challenge_response:
        return JSONResponse(challenge_response)

    event = event_handler.parse_event(data)
    if event:
        background_tasks.add_task(process_message, event)

    return JSONResponse({"code": 0, "msg": "success"})


@app.get("/health")
async def health_check():
    settings = get_settings()
    return {
        "status": "healthy",
        "feishu_configured": settings.validate_feishu_config(),
        "ollama_model": settings.ollama_model,
    }


@app.get("/")
async def root():
    return {
        "name": "GhostAP",
        "description": "飞书机器人Shell沙箱服务",
        "version": "0.1.0",
        "endpoints": {
            "webhook": "/webhook/event",
            "health": "/health",
        }
    }
