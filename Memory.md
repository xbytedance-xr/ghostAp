# GhostAP 项目进展记录

## 项目概述
GhostAP 是一个飞书机器人Shell沙箱服务，通过飞书机器人对话来安全执行本地shell命令，并支持 Coco AI 远程开发模式。

## 最新更新
**更新时间**: 2026-01-18

### 已完成功能
1. ✅ 项目初始化 - pyproject.toml、目录结构
2. ✅ 配置管理模块 - 支持环境变量和.env文件
3. ✅ 沙箱命令执行器 - 危险命令检测、超时控制、输出截断
4. ✅ 飞书长连接客户端 - WebSocket方式接收消息，无需公网IP
5. ✅ AI Agent - 使用 ARK 方舟大模型进行命令安全检查
6. ✅ Coco 远程对话模式 - 通过飞书与 Coco 进行远程开发
7. ✅ ReAct 智能意图识别 - 推理式意图理解，支持任务拆解
8. ✅ 消息过期丢弃 - 超过30秒的旧消息自动丢弃
9. ✅ 表情回复 - 消息状态反馈（OK、Typing、Done等）
10. ✅ 安全工具链 - SafeShellTool、FileEditorTool、ToolManager
11. ✅ 代码清理 - 移除废弃的 HTTP 模式代码
12. ✅ 单元测试 - 76 个测试全部通过

### 支持的功能

#### 📟 Shell 模式（默认）
直接发送 shell 命令执行

#### 🤖 Coco 模式
与 Coco AI 进行远程开发对话：
- 说「帮我写代码」或 `/coco` - 进入 Coco 模式
- 说「退出」或 `/end_coco` - 退出 Coco 模式
- `/coco_info` - 查看会话信息
- 进入 Coco 模式后，消息直接转发给 Coco，不经过 ReAct

#### 📁 目录切换
- 说「切换到xxx目录」- 智能切换工作目录
- 支持自然语言描述：「切换到用户目录下的workspace」

#### 🧠 ReAct 智能意图识别
使用 ARK 方舟大模型进行推理式意图理解：
- Thought（思考）→ Action（行动）→ Observation（观察）→ Reflection（反思）
- 支持复合意图拆解为多步骤任务
- 例如：「切换到项目目录然后帮我写代码」→ 拆解为 2 个步骤执行

#### 🛠️ 安全工具链
- **SafeShellTool**: 安全 Shell 执行，20+ 危险模式检测，风险等级评估
- **FileEditorTool**: 文件编辑，支持 JSON/YAML/Markdown 等格式
- **ToolManager**: 统一管理工具和 AI Agent

#### 😀 表情回复
- 收到消息：OK 表情
- Coco 处理中：Typing 表情
- 完成：Done 表情
- 多任务执行：Rocket 表情

### 技术栈
- Python 3.11+
- lark-oapi (飞书SDK，长连接模式)
- LangChain + LangGraph (AI Agent + ReAct 意图识别)
- ARK 方舟大模型（字节跳动）
- pydantic-settings (配置管理)
- coco CLI (远程开发)

### 安全机制
1. 正则表达式检测危险命令模式（20+）
2. 命令黑名单配置
3. 白名单模式（可选）
4. 风险等级评估（5级）
5. 命令执行超时控制
6. 输出长度限制
7. 消息过期丢弃（30秒）
8. 文件路径安全检查
9. 文件扩展名过滤
10. 删除保护

### 连接方式
使用飞书SDK的**长连接模式（WebSocket）**：
- ✅ 无需公网IP或域名
- ✅ 无需内网穿透
- ✅ 本地只要能访问公网就能接收消息
- ✅ 自动加密传输

### 代码统计
| 类型 | 行数 |
|------|------|
| 源代码 | 2,663 行 |
| 测试代码 | 519 行 |
| **总计** | **3,182 行** |

## 历史记录

### 2026-01-18
- 新增安全工具链（SafeShellTool、FileEditorTool、ToolManager）
- 完成 Claude Code SDK vs LangChain 技术调研
- 移除废弃的 HTTP 模式代码（server.py、client.py、handler.py）
- 清理无用配置（server_host、server_port）
- 新增 58 个工具链测试用例
- 重构 main.py 为 Application 类
- 更新项目文档

### 2026-01-09 23:12
- 引入 ReAct 推理模式进行意图识别
- 新增 TaskStep 类支持多任务拆解
- 新增消息过期丢弃机制（30秒阈值）
- 新增多任务执行逻辑（展示计划、逐步执行、进度反馈）
- Coco 模式下消息直接转发，不经过 ReAct

### 2026-01-09 22:55
- 新增表情回复功能
- 注册 im.message.reaction.created_v1 事件处理器消除错误日志

### 2026-01-09 22:29
- 新增智能意图识别功能
- 新增 src/agent/intent_recognizer.py 意图识别器
- 支持自然语言切换 Coco 模式
- 支持自然语言切换工作目录
- 每个聊天维护独立的工作目录状态

### 2026-01-09 22:22
- 修复消息重复处理问题
- 添加消息去重机制（message_id 缓存）

### 2026-01-09 22:13
- 新增 Coco 远程对话模式
- 新增 src/coco/session.py 会话管理器
- 支持 /coco、/end_coco、/coco_info 命令
- 会话隔离、上下文保持、超时控制

### 2026-01-09 22:00
- 修复飞书消息接收问题
- 从HTTP Webhook模式改为WebSocket长连接模式
- 新增 ws_client.py 长连接客户端
- 长连接测试成功

### 2026-01-09 21:34
- 项目创建并完成所有核心功能
- 通过所有单元测试
