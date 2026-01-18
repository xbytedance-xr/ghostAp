# 🛡️ GhostAP

飞书机器人 Shell 沙箱服务 —— 通过飞书机器人对话安全执行本地 Shell 命令，并支持 Coco AI 远程开发模式。

## ✨ 功能特性

### 📟 Shell 模式（默认）
直接发送 Shell 命令执行，例如：
- `ls -la` - 列出文件
- `whoami` - 查看当前用户
- `cat file.txt` - 查看文件内容

### 🤖 Coco 模式
与 Coco AI 进行远程开发对话：
- 说「帮我写代码」或 `/coco` - 进入 Coco 模式
- 说「退出」或 `/end_coco` - 退出 Coco 模式
- `/coco_info` - 查看会话信息

### 🧠 智能意图识别
基于 ReAct 推理模式的智能意图理解：
- **自然语言交互**：「帮我看下当前目录有哪些文件」→ 自动执行 `ls`
- **目录切换**：「切换到用户目录下的 workspace」→ 智能解析路径
- **任务拆解**：「切换到项目目录然后帮我写代码」→ 拆解为多步骤执行

### 🛠️ 安全工具链
基于 LangChain 的安全工具链系统：
- **SafeShellTool** - 安全的 Shell 命令执行，内置危险命令拦截
- **FileEditorTool** - 文件读写编辑，支持 JSON/YAML/Markdown 等格式
- **ToolManager** - 统一管理工具和 AI Agent

### 😀 表情状态反馈
- ✅ 收到消息：OK 表情
- ⌨️ Coco 处理中：Typing 表情
- ✔️ 完成：Done 表情
- 🚀 多任务执行：Rocket 表情

## 🏗️ 技术架构

```
┌─────────────────┐     WebSocket      ┌─────────────────┐
│   飞书客户端     │ ◄──────────────────► │   GhostAP 服务   │
└─────────────────┘    (长连接模式)      └────────┬────────┘
                                                  │
                    ┌─────────────────────────────┼─────────────────────────────┐
                    │                             │                             │
              ┌─────▼─────┐               ┌───────▼───────┐             ┌───────▼───────┐
              │  ReAct    │               │    Shell      │             │    Coco       │
              │ 意图识别   │               │   沙箱执行     │             │   远程开发     │
              └─────┬─────┘               └───────────────┘             └───────────────┘
                    │
              ┌─────▼─────┐
              │    ARK    │
              │  方舟大模型 │
              └───────────┘
```

### 技术栈
- **语言**: Python 3.11+
- **飞书 SDK**: lark-oapi（长连接 WebSocket 模式）
- **AI 框架**: LangChain + LangGraph（ReAct Agent）
- **大模型**: ARK 方舟大模型（字节跳动）
- **配置管理**: pydantic-settings
- **远程开发**: Coco CLI
- **包管理**: uv

## 🚀 快速开始

### 1. 环境准备

```bash
# 克隆项目
git clone <repo-url>
cd ghostAp

# 安装依赖（使用 uv）
uv sync --group dev
```

### 2. 配置飞书应用

1. 登录 [飞书开放平台](https://open.feishu.cn/)
2. 创建企业自建应用，获取 `APP_ID` 和 `APP_SECRET`
3. 进入 **事件与回调 > 事件配置**
4. 选择订阅方式为 **使用长连接接收事件**
5. 添加事件：`im.message.receive_v1`
6. 添加权限：`im:message:receive_v1`, `im:message:send_v1`

### 3. 配置环境变量

```bash
# 复制配置模板
cp .env.example .env

# 编辑配置文件
vim .env
```

必填配置项：
```env
APP_ID=your_app_id
APP_SECRET=your_app_secret

# ARK 方舟大模型配置
ARK_API_KEY=your_ark_api_key
ARK_MODEL=your_model_endpoint
ARK_BASE_URL=https://ark-cn-beijing.bytedance.net/api/v3
```

可选配置项：
```env
# 沙箱配置
SANDBOX_TIMEOUT=30
SANDBOX_MAX_OUTPUT_LENGTH=4000

# Coco 配置
COCO_EXECUTION_TIMEOUT=7200
COCO_SESSION_TIMEOUT=86400
```

### 4. 启动服务

```bash
uv run python -m src.main
```

启动成功后，即可在飞书中与机器人对话！

## 📖 使用说明

### Shell 命令执行
直接发送命令即可：
```
ls -la
pwd
cat README.md
```

### 自然语言交互
支持自然语言描述：
```
帮我看下当前目录有哪些文件
切换到上级目录
查看系统信息
```

### Coco 远程开发
```
/coco          # 进入 Coco 模式
帮我写一个 Python 脚本...
/end_coco      # 退出 Coco 模式
/coco_info     # 查看会话状态
```

## 🔒 安全机制

### Shell 安全
1. **危险命令检测** - 正则表达式匹配 20+ 危险模式（如 `rm -rf /`、`mkfs`、`dd`）
2. **命令黑名单** - 可配置的禁止命令列表
3. **白名单模式** - 可选的严格模式，仅允许指定命令
4. **风险等级评估** - SAFE/LOW/MEDIUM/HIGH/CRITICAL 五级评估
5. **执行超时控制** - 默认 30 秒超时
6. **输出长度限制** - 默认 4000 字符，防止刷屏攻击

### 文件安全
1. **路径黑名单** - 禁止访问系统目录（/etc, /usr, /bin 等）
2. **扩展名过滤** - 禁止操作可执行文件（.exe, .dll, .so 等）
3. **删除保护** - 默认禁止删除操作
4. **大小限制** - 默认 10MB 文件大小限制

### 消息安全
1. **消息过期丢弃** - 超过 30 秒的旧消息自动忽略
2. **消息去重** - 防止重复处理

## 📁 项目结构

```
ghostAp/
├── src/                          # 源代码目录
│   ├── main.py                   # 主入口
│   ├── config.py                 # 配置管理
│   ├── feishu/                   # 飞书集成模块
│   │   ├── ws_client.py          # 长连接客户端
│   │   └── message_formatter.py  # 消息格式化
│   ├── sandbox/                  # 沙箱执行模块
│   │   └── executor.py           # 命令执行器
│   ├── tools/                    # 安全工具链模块 (NEW)
│   │   ├── shell_tool.py         # 安全 Shell 工具
│   │   ├── file_tool.py          # 文件编辑工具
│   │   └── tool_manager.py       # 工具管理器
│   ├── coco/                     # Coco 远程开发模块
│   │   └── session.py            # 会话管理
│   └── agent/                    # AI Agent 模块
│       ├── shell_agent.py        # 安全检查
│       └── intent_recognizer.py  # 意图识别
├── tests/                        # 测试目录
│   ├── test_tools.py             # 工具链测试 (58 用例)
│   └── test_sandbox.py           # 沙箱测试
├── docs/                         # 文档目录
│   └── TOOL_CHAIN_REPORT.md      # 工具链技术报告
├── .env.example                  # 配置示例
└── pyproject.toml                # 项目配置
```

## 🧪 运行测试

```bash
uv run python -m pytest tests/ -v
```

当前测试覆盖：
- ✅ 76 个测试用例全部通过
- ✅ Shell 安全检测测试
- ✅ 文件操作测试
- ✅ 安全策略测试

## 🌟 连接方式优势

使用飞书 SDK 的**长连接模式（WebSocket）**：
- ✅ 无需公网 IP 或域名
- ✅ 无需内网穿透
- ✅ 本地只要能访问公网就能接收消息
- ✅ 自动加密传输

## 📊 代码统计

| 类型 | 行数 |
|------|------|
| 源代码 | 2,663 行 |
| 测试代码 | 519 行 |
| **总计** | **3,182 行** |

## ⚠️ 注意事项

- 建议仅在受信任的环境中使用
- 请勿在生产服务器上运行
- 定期检查命令执行日志
- 建议配置 ARK 大模型启用 AI 安全检查

## 📄 License

MIT License
