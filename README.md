# GhostAP

飞书 AI 开发助手 —— 通过飞书对话实现安全的远程 Shell 执行、多模型 AI 编程（Coco / Claude）、以及自主任务编排（Deep / Loop 引擎）。无需公网 IP，WebSocket 长连接即插即用。

## 功能概览

GhostAP 提供 **4 种交互模式** + **3 种编排引擎**，覆盖从简单命令到复杂开发任务的全场景：

```
交互模式（用户驱动）                 编排引擎（系统驱动）
┌─────────┐ ┌──────────┐           ┌─────────────────┐
│  Smart  │ │  Shell   │           │  Deep Engine    │
│ 智能路由 │ │ 命令直达  │           │  一次规划 · 顺序执行 │
└─────────┘ └──────────┘           └─────────────────┘
┌─────────┐ ┌──────────┐           ┌─────────────────┐
│  Coco   │ │  Claude  │           │  Loop Engine    │
│ 多轮编程 │ │ 多轮编程  │           │  迭代闭环 · 动态决策 │
└─────────┘ └──────────┘           └─────────────────┘
                                  ┌─────────────────┐
                                  │  Spec Engine    │
                                  │  结构化产物 · 闭环迭代 │
                                  └─────────────────┘
```

### 交互模式

| 模式 | 说明 | 进入方式 | 退出方式 |
|------|------|----------|----------|
| **Smart** | 默认模式，LLM 意图识别自动路由 | 默认 | - |
| **Coco** | 与 Coco AI（字节 ARK）多轮编程对话 | `/coco` | `/exit` |
| **Claude** | 与 Claude Code 多轮编程对话 | `/claude` | `/exit` |
| **Shell** | 所有消息直接作为 Shell 命令执行 | `/shell` | `/exit` |

### 编排引擎

| 引擎 | 说明 | 启动方式 | 停止方式 |
|------|------|----------|----------|
| **Deep** | 单次输入完整需求，Agent 自主规划并执行 | `/deep <需求>` | `/stop_deep` |
| **Loop** | 迭代闭环开发，验收标准驱动，收敛后自动停止 | `/loop <需求>` | `/stop_loop` |
| **Spec** | 按 `Spec→Plan→Task→Build→Review` 产出结构化产物并迭代收敛 | `/spec <需求>` | `/stop_spec` |

#### Deep / Loop / Spec 对比

| 维度 | Deep Engine | Loop Engine | Spec Engine |
|------|------------|-------------|-------------|
| 执行策略 | 一次规划，顺序执行 | 多轮迭代，动态决策 | `Spec→Plan→Task→Build→Review` 循环 |
| 适用场景 | 目标明确的单次冲刺 | 需求偏探索、需要反复验证 | 需要“可复盘产物”驱动持续推进 |
| 进度追踪 | 计划条目 + 工具调用 | 验收标准 + 迭代记录 | 阶段产物（Spec/Plan/Task）+ 验收标准 + 审查 |
| 终止条件 | 执行完毕/手动停止 | 标准满足/收敛/手动停止 | 标准满足+审查通过/收敛/手动停止/待澄清 |

### 多项目管理

单对话框并行管理多个开发项目，每个项目有独立的工作目录、AI 会话和上下文：

```
创建项目 myapp              → 创建项目
/switch myapp               → 切换项目
/projects                   → 项目面板
```

## 技术架构

### 消息处理流

```
飞书 WebSocket 消息
  → FeishuWSClient._handle_message（校验、去重、过期检查）
    → TaskScheduler.submit（per-chat 有序、全局并发控制）
      → IntentRecognizer（ReAct LLM 意图分类，~30 种意图）
        → 路由分发:
            SHELL_COMMAND → SandboxExecutor（安全检查 → 执行）
            ENTER_COCO    → ACPSessionManager("coco")（多轮 AI 会话）
            ENTER_CLAUDE  → ACPSessionManager("claude")（Claude Code 会话）
            DEEP_COMMAND  → DeepEngine（ACP 单次深度执行）
            LOOP_COMMAND  → LoopEngine（ACP 迭代闭环）
            SPEC_COMMAND  → SpecEngine（结构化产物闭环）
            项目/模式命令  → ProjectHandler / SystemHandler
      → ACPEventRenderer（结构化事件 → 飞书 Markdown）
      → StreamingCardManager（实时流式卡片更新）
      → 飞书 API 回复 + EmojiReaction 反馈
```

### ACP 协议层

GhostAP 通过 **ACP（Agent Client Protocol）** 与 AI 后端通信，基于 JSON-RPC 2.0 over stdio，实现结构化的工具调用追踪、执行计划感知和实时进度展示：

```
┌──────────────────┐      JSON-RPC 2.0       ┌──────────────────┐
│   GhostAPClient  │ ◄─── stdio stream ────► │  Coco / Claude   │
│   (ACP Client)   │                          │  Agent Process   │
└────────┬─────────┘                          └──────────────────┘
         │
         │  ACPEvent 流
         ▼
┌──────────────────┐      Feishu Markdown     ┌──────────────────┐
│ ACPEventRenderer │ ──────────────────────► │ StreamingCard    │
│ (事件→渲染)       │                          │ (实时卡片更新)    │
└──────────────────┘                          └──────────────────┘
```

**ACP 事件类型**:
- `TEXT_CHUNK` / `THOUGHT_CHUNK` — 文本/思考流
- `TOOL_CALL_START` / `TOOL_CALL_UPDATE` / `TOOL_CALL_DONE` — 工具调用全生命周期
- `PLAN_UPDATE` — 执行计划实时更新

### 核心模块

| 模块 | 代码量 | 职责 |
|------|--------|------|
| `src/acp/` | 1,844 行 | ACP 协议层：Client、Session、SyncAdapter、Manager、Renderer |
| `src/feishu/` | 4,988 行 | 飞书集成：WebSocket 客户端 + 7 个 Handler + 消息缓存 |
| `src/deep_engine/` | 1,395 行 | Deep 引擎：单次规划执行、进度追踪、报告生成 |
| `src/loop_engine/` | 2,132 行 | Loop 引擎：迭代闭环、验收标准、收敛检测、多视角审查 |
| `src/card/` | 1,477 行 | 卡片渲染：CardBuilder（schema 2.0）+ 流式更新 |
| `src/project/` | 1,896 行 | 多项目管理：上下文隔离、会话快照、跨模式桥接 |
| `src/agent/` | 692 行 | 意图识别：ReAct LLM 推理，~30 种意图类型 |
| `src/tasking/` | 608 行 | 任务调度：per-chat 串行 + 全局并发控制 + 优先级队列 |
| `src/agent_session.py` | 292 行 | 会话后端抽象：ACP / Claude CLI 双后端支持 |
| `src/sandbox/` | 162 行 | Shell 安全沙箱：20+ 危险模式正则 + 黑名单 |
| `src/mode/` | 142 行 | 模式状态机：SMART / COCO / CLAUDE / SHELL 切换 |
| `src/utils/` | 135 行 | 工具函数：错误格式化、文本处理 |

### 设计模式

- **ACP 协议通信**: JSON-RPC 2.0 over stdio，`GhostAPClient` 实现 ACP `Client` 接口
- **事件驱动渲染**: `ACPEventRenderer` 处理事件流 → 飞书 Markdown，Handler 注册 `on_event` 回调驱动实时卡片
- **状态机**: `InteractionMode`（交互模式切换）、`EngineRunState`（引擎生命周期）
- **同步-异步桥接**: `SyncACPSession` 通过 daemon thread + `run_coroutine_threadsafe()` 桥接 async ACP 到同步线程
- **per-chat 任务调度**: `TaskScheduler` 每个 chat 串行执行，全局并发上限，长时任务独立队列
- **会话隔离**: `ACPSessionManager` 按 `(chat_id, project_id)` 隔离 AI 会话

## 快速开始

### 1. 环境准备

```bash
git clone <repo-url>
cd ghostAp

# 安装依赖（必须使用 uv）
uv sync --group dev
```

### 2. 配置飞书应用

1. 登录 [飞书开放平台](https://open.feishu.cn/)
2. 创建企业自建应用，获取 `APP_ID` 和 `APP_SECRET`
3. 进入 **事件与回调 > 事件配置**
4. 选择 **使用长连接接收事件**
5. 添加事件：`im.message.receive_v1`
6. 添加权限：`im:message:receive_v1`, `im:message:send_v1`, `im:message:patch_v1`（流式卡片需要 patch 权限）

### 3. 配置环境变量

```bash
cp .env.example .env
vim .env
```

必填配置：

```env
APP_ID=your_app_id
APP_SECRET=your_app_secret

# ARK 方舟大模型（Coco 后端）
ARK_API_KEY=your_ark_api_key
ARK_MODEL=your_model_endpoint
ARK_BASE_URL=https://ark-cn-beijing.bytedance.net/api/v3
```

可选配置（均有合理默认值）：

```env
# Shell 沙箱
SANDBOX_TIMEOUT=30                    # 命令超时（秒）
SANDBOX_MAX_OUTPUT_LENGTH=4000        # 输出截断长度

# AI 会话
COCO_EXECUTION_TIMEOUT=7200           # Coco 单次执行超时
CLAUDE_EXECUTION_TIMEOUT=7200         # Claude 单次执行超时

# ACP 协议
ACP_PERMISSION_AUTO_APPROVE=true      # 自动批准 Agent 操作
ACP_STREAM_BUFFER_LIMIT=10485760      # stdio 缓冲区（默认 10MB）

# Loop 引擎
LOOP_MAX_ITERATIONS=100               # 最大迭代次数
LOOP_REVIEW_ENABLED=true              # 启用多视角审查（Ralph Loop）

# Spec 引擎
SPEC_MAX_CYCLES=10                     # 最大循环次数
SPEC_CONVERGENCE_WINDOW=2              # 收敛窗口（连续 N 轮无有效改进则终止）
SPEC_REVIEW_ENABLED=true               # 启用多视角审查（架构师/产品/用户/测试）
SPEC_EXECUTION_TIMEOUT=7200            # 单次 Spec 执行超时（秒）

# 卡片
CARD_BUTTON_LAYOUT=responsive         # 按钮布局：desktop / mobile / responsive
STREAMING_ENABLED=true                # 启用流式卡片

# 任务调度
TASK_SCHEDULER_MAX_CONCURRENT=10      # 全局最大并发数
```

### 4. 启动服务

```bash
uv run python -m src.main
```

## 命令参考

### 模式控制

| 命令 | 作用 |
|------|------|
| `/coco` | 进入 Coco 编程模式 |
| `/claude` | 进入 Claude 编程模式 |
| `/shell` | 进入 Shell 直通模式 |
| `/exit` | 退出当前模式，回到智能模式 |

### Deep 引擎

| 命令 | 作用 |
|------|------|
| `/deep <需求>` | 启动 Deep 任务 |
| `/deep_status` | 查看执行进度 |
| `/deep_update <补充>` | 注入上下文补充 |
| `/stop_deep` | 停止当前任务 |

### Loop 引擎

| 命令 | 作用 |
|------|------|
| `/loop <需求>` | 启动 Loop 迭代开发 |
| `/loop_status` | 查看迭代进度和验收标准 |
| `/loop_guide <引导>` | 注入迭代方向引导 |
| `/loop_pause` | 暂停迭代 |
| `/loop_resume` | 恢复迭代 |
| `/stop_loop` | 停止 Loop |

### Spec 引擎

| 命令 | 作用 |
|------|------|
| `/spec <需求>` | 启动 Spec 结构化闭环 |
| `/spec_status` | 查看进度与当前阶段 |
| `/spec_guide <引导>` | 补充约束/偏好/回答澄清问题（下轮或恢复时生效） |
| `/spec_pause` | 暂停 Spec |
| `/spec_resume` | 恢复 Spec（常用于“待澄清”后继续） |
| `/spec_recover` | 恢复异常中断的任务（需指定 Task ID） |
| `/stop_spec` | 停止 Spec |

### 项目管理

| 命令 | 作用 |
|------|------|
| `/projects` | 查看项目面板 |
| `/new <名称> [目录]` | 创建新项目 |
| `/switch <名称>` | 切换项目 |
| `/close <名称>` | 关闭项目 |
| `/status` | 当前项目详情 |

### 其他

| 命令 | 作用 |
|------|------|
| `/help` | 查看帮助信息 |
| `/coco_info` / `/claude_info` | 查看 AI 会话状态 |

## 安全机制

### Shell 沙箱

- **20+ 危险模式正则** — `rm -rf /`、`mkfs`、`dd`、`shutdown` 等
- **命令黑名单** — 可通过 `SANDBOX_COMMAND_BLACKLIST` 配置
- **执行超时** — 默认 30 秒
- **输出截断** — 默认 4000 字符

### ACP 权限控制

- Agent 工具调用通过 `GhostAPClient.request_permission()` 拦截
- 可配置自动批准（`ACP_PERMISSION_AUTO_APPROVE=true`）或逐项审核

### 消息安全

- **消息过期** — 超过 30 秒的消息自动忽略
- **消息去重** — `MessageCache` 防止重复处理

## 项目结构

```
ghostAp/
├── src/
│   ├── main.py                      # 入口：Application 类
│   ├── config.py                    # Settings 单例（pydantic-settings）
│   ├── agent_session.py             # 会话后端抽象（ACP / CLI）
│   │
│   ├── acp/                         # ACP 协议层
│   │   ├── models.py                #   事件模型：ACPEvent, ToolCallInfo, PlanInfo
│   │   ├── client.py                #   GhostAPClient（ACP Client 实现）
│   │   ├── session.py               #   ACPSession（async 生命周期管理）
│   │   ├── sync_adapter.py          #   SyncACPSession（async→sync 桥接）
│   │   ├── manager.py               #   ACPSessionManager（per-chat 会话管理）
│   │   └── renderer.py              #   ACPEventRenderer（事件→飞书 Markdown）
│   │
│   ├── deep_engine/                 # Deep 编排引擎
│   │   ├── models.py                #   DeepProject, EngineRunState
│   │   ├── engine.py                #   DeepEngine, DeepEngineManager
│   │   ├── progress.py              #   DeepProgress（计划/工具调用追踪）
│   │   └── reporter.py              #   进度格式化
│   │
│   ├── loop_engine/                 # Loop 迭代引擎
│   │   ├── models.py                #   LoopProject, IterationRecord, CriteriaTracker
│   │   ├── engine.py                #   LoopEngine, LoopEngineManager
│   │   ├── tracker.py               #   IterationTracker（ACP 事件处理）
│   │   └── reporter.py              #   迭代报告格式化
│   │
│   ├── spec_engine/                 # Spec 结构化闭环引擎
│   │   ├── models.py                #   SpecProject, SpecCycle, SpecArtifact/PlanArtifact
│   │   ├── engine.py                #   SpecEngine, SpecEngineManager
│   │   ├── tracker.py               #   PhaseTracker（ACP 事件处理）
│   │   └── reporter.py              #   进度报告格式化
│   │
│   ├── feishu/                      # 飞书集成
│   │   ├── ws_client.py             #   WebSocket 客户端（消息调度中枢）
│   │   ├── handler_context.py       #   HandlerContext（Handler 共享状态）
│   │   ├── handlers/                #   消息处理器
│   │   │   ├── base.py              #     BaseHandler 抽象基类
│   │   │   ├── programming.py       #     Coco/Claude 模式 Handler
│   │   │   ├── deep.py              #     Deep 引擎 Handler
│   │   │   ├── loop.py              #     Loop 引擎 Handler
│   │   │   ├── spec.py              #     Spec 引擎 Handler
│   │   │   ├── project.py           #     项目管理 Handler
│   │   │   ├── system.py            #     系统命令 Handler
│   │   │   └── diagnostics.py       #     诊断调试 Handler
│   │   ├── message_cache.py         #   消息去重（TTL + 后台清理）
│   │   ├── message_formatter.py     #   消息格式化工具
│   │   ├── emoji.py                 #   EmojiReaction 表情反馈
│   │   └── image_handler.py         #   图片消息处理
│   │
│   ├── card/                        # 卡片渲染
│   │   ├── builder.py               #   CardBuilder（schema 2.0 交互卡片）
│   │   ├── streaming.py             #   StreamingCardManager（实时更新）
│   │   └── shared.py                #   共享枚举和工具
│   │
│   ├── project/                     # 多项目管理
│   │   ├── manager.py               #   ProjectManager 生命周期
│   │   ├── context.py               #   ProjectContext 会话历史
│   │   ├── unified_context.py       #   UnifiedContext 跨模式桥接
│   │   └── mapper.py                #   消息→项目映射
│   │
│   ├── agent/                       # AI Agent
│   │   └── intent_recognizer.py     #   ReAct 意图识别（~30 种意图）
│   │
│   ├── sandbox/                     # Shell 安全
│   │   └── executor.py              #   SandboxExecutor
│   │
│   ├── tasking/                     # 任务调度
│   │   └── scheduler.py             #   TaskScheduler
│   │
│   ├── mode/                        # 模式管理
│   │   └── manager.py               #   ModeManager 状态机
│   │
│   └── utils/                       # 工具函数
│       ├── errors.py                #   错误格式化
│       └── text.py                  #   文本处理
│
├── tests/                           # 测试套件（815 个测试）
│   ├── test_acp_*.py                #   ACP 协议测试（4 个文件）
│   ├── test_deep_engine.py          #   Deep 引擎测试
│   ├── test_loop_engine.py          #   Loop 引擎测试
│   ├── test_handlers.py             #   Handler 路由测试
│   ├── test_card.py                 #   卡片渲染测试
│   ├── test_streaming.py            #   流式更新测试
│   ├── test_intent.py               #   意图识别测试
│   ├── test_sandbox.py              #   沙箱安全测试
│   ├── test_project.py              #   项目管理测试
│   ├── test_task_scheduler*.py      #   调度器测试（2 个文件）
│   └── ...                          #   其他模块测试
│
├── docs/                            # 架构文档
├── .Memory/                         # 项目记忆（开发决策日志）
├── CLAUDE.md                        # Claude Code 项目指令
├── pyproject.toml                   # 项目配置 & 依赖
└── .env.example                     # 环境变量模板
```

## 技术栈

| 组件 | 技术 |
|------|------|
| 语言 | Python 3.11+ |
| 飞书 SDK | lark-oapi（WebSocket 长连接） |
| AI 后端 | Coco（ARK 方舟大模型）、Claude Code |
| Agent 协议 | ACP（Agent Client Protocol, JSON-RPC 2.0） |
| 意图识别 | LangChain + LangGraph（ReAct Agent） |
| 配置管理 | pydantic-settings + .env |
| 包管理 | uv |
| 测试 | pytest + pytest-asyncio |

## 开发

```bash
# 安装依赖
uv sync --group dev

# 运行全部测试
uv run python -m pytest tests/ -v

# 运行单个测试文件
uv run python -m pytest tests/test_deep_engine.py -v

# 运行单个测试用例
uv run python -m pytest tests/test_loop_engine.py::TestLoopEngine::test_execute -v
```

**测试覆盖**：815 个测试，覆盖 ACP 协议、Deep/Loop 引擎、Handler 路由、卡片渲染、安全沙箱、任务调度等全部核心模块。

## 连接优势

使用飞书 SDK 的 **WebSocket 长连接模式**：
- 无需公网 IP 或域名
- 无需内网穿透（ngrok、frp）
- 本地可访问公网即可接收消息
- 自动加密传输

## 代码统计

| 类型 | 规模 |
|------|------|
| 源代码 | ~15,500 行 |
| 测试代码 | ~9,900 行 |
| 测试用例 | 815 个 |
| 核心模块 | 12 个 |

## License

MIT License
