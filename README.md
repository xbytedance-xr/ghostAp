# GhostAP

飞书 AI 开发助手 —— 通过飞书对话实现安全的远程 Shell 执行、多模型 AI 编程（Coco / Claude / Aiden / Codex / Gemini / TTADK）、以及自主任务编排（Deep / Spec / Worktree 引擎）。无需公网 IP，WebSocket 长连接即插即用。

## 功能概览

GhostAP 提供 **8 种交互模式** + **3 种编排引擎**，覆盖从简单命令到复杂开发任务的全场景：

```
交互模式（用户驱动）                 编排引擎（系统驱动）
┌─────────┐ ┌──────────┐           ┌─────────────────┐
│  Smart  │ │  Shell   │           │  Deep Engine    │
│ 智能路由 │ │ 命令直达  │           │  一次规划 · 顺序执行 │
└─────────┘ └──────────┘           └─────────────────┘
┌─────────┐ ┌──────────┐           ┌─────────────────┐
│  Coco   │ │  Claude  │           │  Spec Engine    │
│ 多轮编程 │ │ 多轮编程  │           │  结构化产物 · 闭环迭代 │
└─────────┘ └──────────┘           └─────────────────┘
┌─────────┐ ┌──────────┐           ┌─────────────────┐
│  Aiden  │ │  Codex   │           │  Worktree Engine│
│ 多轮编程 │ │ 多轮编程  │           │  并行工具 · 分支隔离 │
└─────────┘ └──────────┘           └─────────────────┘
┌─────────┐ ┌──────────┐
│ Gemini  │ │  TTADK   │
│ 多轮编程 │ │ 多工具编程 │
└─────────┘ └──────────┘
```

### 交互模式

| 模式 | 说明 | 进入方式 | 退出方式 |
|------|------|----------|----------|
| **Smart** | 默认模式，LLM 意图识别自动路由 | 默认 | - |
| **Coco** | 与 Coco AI（字节 ARK）多轮编程对话 | `/coco` | `/exit` |
| **Claude** | 与 Claude Code 多轮编程对话 | `/claude` | `/exit` |
| **Aiden** | 与 Aiden 多轮编程对话 | `/aiden` | `/exit` |
| **Codex** | 与 Codex 多轮编程对话 | `/codex` | `/exit` |
| **Gemini** | 与 Gemini 多轮编程对话 | `/gemini` | `/exit` |
| **TTADK** | 与多工具 AI（支持 Coco/Claude/Cursor/Gemini 等）多轮编程对话 | `/ttadk` | `/exit` |
| **Shell** | 所有消息直接作为 Shell 命令执行 | `/shell` | `/exit` |

### 编排引擎

| 引擎 | 说明 | 启动方式 | 停止方式 |
|------|------|----------|----------|
| **Deep** | 单次输入完整需求，Agent 自主规划并执行 | `/deep <需求>` | `/stop_deep` |
| **Spec** | 按 `Spec→Plan→Task→Build→Review` 产出结构化产物并迭代收敛 | `/spec <需求>` | `/stop_spec` |
| **Worktree** | Git worktree 并行多工具执行，分支隔离 | `/worktree <需求>` | `/stop_worktree` |

#### Deep / Spec / Worktree 对比

| 维度 | Deep Engine | Spec Engine | Worktree Engine |
|------|------------|-------------|-----------------|
| 执行策略 | 一次规划，顺序执行 | `Spec→Plan→Task→Build→Review` 循环 | 并行多工具分支执行 |
| 适用场景 | 目标明确的单次冲刺 | 需要"可复盘产物"驱动持续推进 | 多工具协作、并行开发 |
| 进度追踪 | 计划条目 + 工具调用 | 阶段产物（Spec/Plan/Task）+ 验收标准 + 审查 | 工具选择 + 分支进度 |
| 终止条件 | 执行完毕/手动停止 | 标准满足+审查通过/收敛/手动停止/待澄清 | 所有工具完成/手动停止 |

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
            ENTER_COCO    → ACPSessionManager("coco")（ACP 直连）
            ENTER_CLAUDE  → ACPSessionManager("claude")（CLI backend）
            ENTER_AIDEN   → ACPSessionManager("aiden")（ACP 直连）
            ENTER_CODEX   → ACPSessionManager("codex")（ACP 直连）
            ENTER_GEMINI  → ACPSessionManager("gemini")（ACP 直连）
            ENTER_TTADK   → TTADKManager + ACPSessionManager("ttadk_*")（强制 CLI bridge）
            DEEP_COMMAND  → DeepEngine（ACP 单次深度执行）
            SPEC_COMMAND  → SpecEngine（结构化产物闭环）
            WORKTREE_CMD  → WorktreeEngine（并行多工具执行）
            项目/模式命令  → ProjectHandler / SystemHandler
      → ACPEventRenderer（结构化事件 → 飞书 Markdown）
      → CardDelivery（卡片统一投递引擎）
      → 飞书 API 回复 + EmojiReaction 反馈
```

### ACP 协议层

GhostAP 通过 **ACP（Agent Client Protocol）** 与 ACP-capable 后端通信，基于 JSON-RPC 2.0 over stdio，实现结构化的工具调用追踪、执行计划感知和实时进度展示：

```
┌──────────────────┐      JSON-RPC 2.0       ┌──────────────────┐
│   GhostAPClient  │ ◄─── stdio stream ────► │ ACP-capable Agent│
│   (ACP Client)   │                          │  Agent Process   │
└────────┬─────────┘                          └──────────────────┘
         │
         │  ACPEvent 流
         ▼
┌──────────────────┐      Feishu Markdown     ┌──────────────────┐
│ ACPEventRenderer │ ──────────────────────► │ CardDelivery     │
│ (事件→渲染)       │                          │ (卡片统一投递)    │
└──────────────────┘                          └──────────────────┘
```

**ACP 事件类型**:
- `TEXT_CHUNK` / `THOUGHT_CHUNK` — 文本/思考流
- `TOOL_CALL_START` / `TOOL_CALL_UPDATE` / `TOOL_CALL_DONE` — 工具调用全生命周期
- `PLAN_UPDATE` — 执行计划实时更新

### 核心模块

| 模块 | 职责 |
|------|------|
| `src/acp/` | ACP 协议层：Client、Session、SyncAdapter、Manager、Renderer、Provider、Telemetry |
| `src/ttadk/` | TTADK 多工具编程：TTADKManager、工具/模型管理、启动策略（official_cli/interactive/local_config/probe） |
| `src/feishu/` | 飞书集成：WebSocket 客户端 + 12 个 Handler + Renderer 层 + 消息缓存 + 路由 + 控制平面 |
| `src/deep_engine/` | Deep 引擎：单次规划执行、进度追踪、报告生成 |
| `src/spec_engine/` | Spec 引擎：结构化闭环（Spec→Plan→Task→Build→Review）、审查管线、重试系统、持久化 |
| `src/worktree_engine/` | Worktree 引擎：Git worktree 并行多工具执行、工具发现、选择控制器 |
| `src/card/` | 卡片渲染：CardBuilder（schema 2.0）+ 流式更新 + 统一布局 + 锁定卡片 + 主题系统（18 主题） |
| `src/project/` | 多项目管理：上下文隔离、会话快照、跨模式桥接（UnifiedContext） |
| `src/agent/` | 意图识别：ReAct LLM 推理，~30 种意图类型 |
| `src/tasking/` | 任务调度：per-chat 串行 + 全局并发控制 + 优先级队列 + ServiceRegistry |
| `src/chat_lock.py` | Chat 级锁定：管理员限制聊天访问，结构化锁定码 |
| `src/repo_lock.py` | Repo 级互斥：防止跨聊天并发 git 操作，可重入、P2P 特权、空闲超时释放 |
| `src/coco_model/` | Coco 模型管理：模型缓存、YAML 配置、默认模型列表 |
| `src/thread/` | 线程上下文管理：TTL 淘汰、别名支持、后台清理 |
| `src/utils/` | 基础设施：熔断器、GC 监控、Hook 系统、锁排序、限流、DI 注册表、优雅关停 |
| `src/sandbox/` | Shell 安全沙箱：20+ 危险模式正则 + 黑名单/白名单 |
| `src/mode/` | 模式状态机：SMART ↔ 编程/Shell 模式切换 |

### 设计模式

- **ACP 协议通信**: JSON-RPC 2.0 over stdio，`GhostAPClient` 实现 ACP `Client` 接口
- **策略层 × 传输层解耦**: Normal/Deep/Spec/Worktree 与 ACP/CLI 传输独立演进
- **传输策略矩阵**: `coco/aiden/codex/gemini` 优先 ACP，`claude` 固定 CLI，`ttadk_*` 强制 CLI
- **事件驱动渲染**: `ACPEventRenderer` 处理事件流 → 飞书 Markdown，Handler 注册 `on_event` 回调驱动实时卡片
- **状态机**: `InteractionMode`（交互模式切换）、`EngineRunState`（引擎生命周期）
- **同步-异步桥接**: `SyncACPSession` 通过 daemon thread + `run_coroutine_threadsafe()` 桥接 async ACP 到同步线程
- **per-chat 任务调度**: `TaskScheduler` 每个 chat 串行执行，全局并发上限，长时任务独立队列
- **会话隔离**: `ACPSessionManager` 按 `(chat_id, project_id)` 隔离 AI 会话
- **多层锁定**: 6 级锁排序层级（ENGINE_MANAGER → ENGINE_INSTANCE → PROJECT_MANAGER → CHAT_LOCK_CTX → CHAT_LOCK_MGR → REPO_LOCK），运行时死锁检测
- **DI 容器**: `ServiceRegistry` 支持单例/瞬态/工厂模式，层级作用域，线程安全
- **审查管线**: Spec 审查解耦为策略选择 → 管线组装 → 并行 Worker → 重试 → 输出解析 6 个独立模块
- **熔断器**: 滑动窗口失败计数，CLOSED → OPEN → HALF_OPEN 三态转换
- **Hook 系统**: 7 种事件（pre/post shell、session start/end、engine start/stop、iteration done）

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
SANDBOX_USE_WHITELIST=false           # 启用白名单模式（true/false）
SANDBOX_COMMAND_WHITELIST=            # 白名单命令列表（逗号分隔，如 ls,cd,pwd,git）

# AI 会话
COCO_EXECUTION_TIMEOUT=7200           # Coco 单次执行超时
CLAUDE_EXECUTION_TIMEOUT=7200         # Claude 单次执行超时

# TTADK 多工具模式
TTADK_DEFAULT_TOOL=coco                # 默认工具（coco/claude/cursor/gemini/codex/tmates/trae/opencode）
TTADK_DEFAULT_MODEL=                   # 默认模型（可选：gpt-5.2/gpt-4.1/claude-3-opus/claude-3.5-sonnet/claude-3.7-sonnet/doubao-1.5-pro/gemini-2.0-pro/gemini-2.5-pro）

# ACP 协议
ACP_PERMISSION_AUTO_APPROVE=true      # 自动批准 Agent 操作
ACP_STREAM_BUFFER_LIMIT=10485760      # stdio 缓冲区（默认 10MB）

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
| `/aiden` | 进入 Aiden 编程模式 |
| `/codex` | 进入 Codex 编程模式 |
| `/gemini` | 进入 Gemini 编程模式 |
| `/ttadk` | 进入 TTADK 多工具编程模式 |
| `/ttadk_tool <tool>` | 切换 TTADK 使用的工具 |
| `/ttadk_model <model>` | 切换 TTADK 使用的模型 |
| `/ttadk_info` | 查看 TTADK 当前工具和模型 |
| `/shell` | 进入 Shell 直通模式 |
| `/exit` | 退出当前模式，回到智能模式 |

### Deep 引擎

| 命令 | 作用 |
|------|------|
| `/deep <需求>` | 启动 Deep 任务 |
| `/deep_status` | 查看执行进度 |
| `/deep_update <补充>` | 注入上下文补充 |
| `/stop_deep` | 停止当前任务 |

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
- **命令白名单** — 可选更严格的白名单模式，通过 `SANDBOX_USE_WHITELIST` 和 `SANDBOX_COMMAND_WHITELIST` 配置
- **执行超时** — 默认 30 秒
- **输出截断** — 默认 4000 字符

#### 白名单模式使用说明

为了提供更高的安全性，沙箱支持白名单模式。在白名单模式下，只有明确配置的命令才能执行。

**配置方式：**

```env
# 启用白名单模式
SANDBOX_USE_WHITELIST=true

# 配置允许的命令列表（逗号分隔）
SANDBOX_COMMAND_WHITELIST=ls,cd,pwd,echo,cat,git
```

**白名单模式特点：**
- 仅允许配置的命令执行
- 禁止包含 `;`、`&&`、`||`、`|`、\`、`$()` 等控制字符
- 禁止包含括号字符 `()`、`{}`
- 按命令名精确匹配（不区分大小写）
- 当启用白名单时，黑名单检查将被跳过

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
│   ├── engine_base.py               # 引擎基类：EngineRunState、ReviewPerspective
│   ├── agent_session.py             # 会话后端抽象（ACP / CLI）
│   ├── chat_lock.py                 # Chat 级锁定管理
│   ├── repo_lock.py                 # Repo 级互斥锁（可重入、P2P 特权）
│   │
│   ├── acp/                         # ACP 协议层
│   │   ├── models.py                #   事件模型：ACPEvent, ToolCallInfo, PlanInfo
│   │   ├── client.py                #   GhostAPClient（ACP Client 实现）
│   │   ├── session.py               #   ACPSession（async 生命周期管理）
│   │   ├── sync_adapter.py          #   SyncACPSession（async→sync 桥接）
│   │   ├── manager.py               #   ACPSessionManager（per-chat 会话管理）
│   │   ├── renderer.py              #   ACPEventRenderer（事件→飞书 Markdown）
│   │   ├── provider.py              #   ACPProvider 协议 + ToolRegistry
│   │   ├── providers/               #   Provider 实现（可用性检测、帮助加载）
│   │   ├── diagnostics.py           #   会话诊断
│   │   ├── session_factory.py       #   会话工厂
│   │   └── telemetry.py             #   遥测采集
│   │
│   ├── ttadk/                       # TTADK 多工具编程
│   │   ├── models.py                #   TTADKTool, TTADKModel 数据模型
│   │   ├── manager.py               #   TTADKManager（工具/模型切换管理）
│   │   ├── startup.py               #   启动编排
│   │   ├── strategies/              #   启动策略（official_cli/interactive/local_config/probe）
│   │   ├── cache.py                 #   缓存管理
│   │   ├── model_fetcher.py         #   模型列表拉取
│   │   └── env_sandbox.py           #   环境隔离沙箱
│   │
│   ├── deep_engine/                 # Deep 编排引擎
│   │   ├── models.py                #   DeepProject, EngineRunState
│   │   ├── engine.py                #   DeepEngine, DeepEngineManager
│   │   ├── progress.py              #   DeepProgress（计划/工具调用追踪）
│   │   └── reporter.py              #   进度格式化
│   │
│   ├── spec_engine/                 # Spec 结构化闭环引擎（26 个模块）
│   │   ├── engine.py                #   SpecEngine, SpecEngineManager
│   │   ├── manager.py               #   引擎管理（多 agent 类型支持）
│   │   ├── models.py                #   SpecProject, SpecCycle, SpecArtifact
│   │   ├── review.py                #   ReviewOrchestrator（审查编排中枢）
│   │   ├── review_pipeline.py       #   并行审查管线（L1 Lint → Worker）
│   │   ├── review_strategy.py       #   审查策略（NoReview / MultiPerspective）
│   │   ├── review_retry.py          #   管线内重试逻辑
│   │   ├── review_parsing.py        #   LLM 审查输出解析
│   │   ├── review_types.py          #   共享类型（打破循环依赖）
│   │   ├── perspective_worker.py    #   单视角审查 Worker
│   │   ├── cycle_budget.py          #   审查轮次预算（wall-clock cap）
│   │   ├── retry_status.py          #   RetryStatus/RetryEvent 结构化重试状态
│   │   ├── constants.py             #   SPEC_UI_TEXT（引擎层 UI 文本常量）
│   │   ├── persistence.py           #   状态/产物/历史持久化
│   │   ├── tracker.py               #   PhaseTracker（ACP 事件处理）
│   │   └── reporter.py              #   进度报告格式化
│   │
│   ├── worktree_engine/             # Worktree 并行执行引擎
│   │   ├── manager.py               #   WorktreeManager
│   │   ├── dispatcher.py            #   任务分发
│   │   ├── git_service.py           #   Git worktree 操作
│   │   ├── selection.py             #   工具选择逻辑
│   │   ├── selection_controller.py  #   选择控制器
│   │   ├── tool_discovery.py        #   工具可用性发现
│   │   └── reporter.py              #   执行报告
│   │
│   ├── feishu/                      # 飞书集成
│   │   ├── ws_client.py             #   WebSocket 客户端（消息调度中枢）
│   │   ├── ws_health.py             #   WebSocket 健康看门狗
│   │   ├── control_plane.py         #   控制平面（待退出处理、事件队列）
│   │   ├── router.py                #   路由分发表
│   │   ├── handler_context.py       #   HandlerContext（Handler 共享状态）
│   │   ├── handlers/                #   消息处理器（12 个）
│   │   │   ├── base.py              #     BaseHandler 抽象基类
│   │   │   ├── engine_base.py       #     引擎 Handler 基类
│   │   │   ├── programming.py       #     编程模式 Handler
│   │   │   ├── deep.py              #     Deep 引擎 Handler
│   │   │   ├── spec.py              #     Spec 引擎 Handler
│   │   │   ├── worktree.py          #     Worktree Handler
│   │   │   ├── project.py           #     项目管理 Handler
│   │   │   ├── system.py            #     系统命令 Handler
│   │   │   ├── diagnostics.py       #     诊断调试 Handler
│   │   │   └── lock_helper.py       #     锁操作辅助
│   │   ├── renderers/               #   渲染器层
│   │   │   ├── base.py              #     BaseRenderer 抽象
│   │   │   ├── deep_renderer.py     #     Deep 渲染器
│   │   │   └── spec_renderer.py     #     Spec 渲染器（含重试回调）
│   │   ├── user_cache.py            #   用户名 LRU 缓存（500 容量、1h TTL）
│   │   ├── message_cache.py         #   消息去重（TTL + 后台清理）
│   │   ├── chat_lock_gate.py        #   锁定拦截门
│   │   ├── action_registry.py       #   卡片操作注册
│   │   ├── session_hub.py           #   会话中枢
│   │   └── image_handler.py         #   图片消息处理
│   │
│   ├── card/                        # 卡片渲染
│   │   ├── builder.py               #   CardBuilder（schema 2.0 交互卡片）
│   │   ├── delivery/                #   CardDelivery 统一投递引擎
│   │   ├── styles.py                #   主题系统（18 主题）+ UI_TEXT + ENGINE_STYLES
│   │   ├── styles_lock.py           #   锁定相关 UI 文本
│   │   ├── truncation.py            #   内容截断策略
│   │   ├── flow_control.py          #   流控
│   │   ├── models.py                #   卡片数据模型
│   │   ├── builders/                #   卡片构建器（12 个）
│   │   │   ├── layout.py            #     UnifiedCardLayout 统一布局
│   │   │   ├── core.py              #     核心卡片
│   │   │   ├── deep.py              #     Deep 引擎卡片
│   │   │   ├── system.py            #     系统卡片
│   │   │   ├── project.py           #     项目卡片
│   │   │   ├── worktree.py          #     Worktree 卡片
│   │   │   ├── diagnostics.py       #     诊断卡片
│   │   │   ├── lock.py              #     锁定总入口
│   │   │   ├── lock_chat.py         #     Chat 锁定卡片
│   │   │   ├── lock_repo.py         #     Repo 锁定卡片
│   │   │   └── lock_common.py       #     锁定共享工具
│   │   └── shared.py                #   共享枚举和工具
│   │
│   ├── project/                     # 多项目管理
│   │   ├── manager.py               #   ProjectManager 生命周期
│   │   ├── context.py               #   ProjectContext 会话历史
│   │   ├── unified_context.py       #   UnifiedContext 跨模式桥接（版本快照）
│   │   └── mapper.py                #   消息→项目映射
│   │
│   ├── coco_model/                  # Coco 模型管理
│   │   ├── manager.py               #   CocoModelManager（5min TTL 缓存）
│   │   └── models.py                #   模型数据结构
│   │
│   ├── thread/                      # 线程上下文管理
│   │   ├── manager.py               #   ThreadContextManager（TTL 淘汰）
│   │   └── models.py                #   上下文数据模型
│   │
│   ├── agent/                       # AI Agent
│   │   └── intent_recognizer.py     #   ReAct 意图识别（~30 种意图）
│   │
│   ├── sandbox/                     # Shell 安全
│   │   └── executor.py              #   SandboxExecutor
│   │
│   ├── tasking/                     # 任务调度
│   │   ├── scheduler.py             #   TaskScheduler
│   │   └── registry.py              #   ServiceRegistry DI 容器
│   │
│   ├── mode/                        # 模式管理
│   │   └── manager.py               #   ModeManager 状态机
│   │
│   └── utils/                       # 基础设施（30 个模块）
│       ├── circuit_breaker.py       #   熔断器（CLOSED/OPEN/HALF_OPEN）
│       ├── gc_monitor.py            #   GC / 内存监控
│       ├── hooks.py                 #   事件 Hook 系统（7 种事件）
│       ├── lock_order.py            #   6 级锁排序 + 运行时违规检测
│       ├── rate_limit.py            #   Token Bucket 限流器
│       ├── registry.py              #   ServiceRegistry DI 容器
│       ├── shutdown.py              #   优雅关停（信号处理 + 清理编排）
│       ├── cleanup.py               #   异步清理函数注册
│       ├── review_helpers.py        #   审查辅助工具
│       ├── errors.py                #   错误格式化
│       ├── text.py                  #   文本处理
│       └── ...                      #   其他工具（签名、路径、重试、遥测等）
│
├── tests/                           # 测试套件（3,857 个测试）
│
├── docs/                            # 架构文档
├── .Memory/                         # 项目记忆（开发决策日志）
├── AGENTS.md                        # AI Agent 项目指令
├── CLAUDE.md                        # Claude Code 项目指令
├── pyproject.toml                   # 项目配置 & 依赖
└── .env.example                     # 环境变量模板
```

## 技术栈

| 组件 | 技术 |
|------|------|
| 语言 | Python 3.11+ |
| 飞书 SDK | lark-oapi（WebSocket 长连接） |
| AI 后端 | Coco（ARK 方舟大模型）、Claude Code、TTADK（多工具：Coco/Claude/Cursor/Gemini 等） |
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
uv run python -m pytest tests/test_deep_engine.py::TestDeepEngine::test_execute -v
```

**测试覆盖**：6,100+ 个测试，覆盖 ACP 协议、Deep/Spec/Worktree 引擎、审查管线、锁定系统、Handler 路由、卡片渲染、安全沙箱、任务调度等全部核心模块。

## 连接优势

使用飞书 SDK 的 **WebSocket 长连接模式**：
- 无需公网 IP 或域名
- 无需内网穿透（ngrok、frp）
- 本地可访问公网即可接收消息
- 自动加密传输

## 代码统计

| 类型 | 规模 |
|------|------|
| 源代码 | ~68,000 行 |
| 测试代码 | ~69,000 行 |
| 测试用例 | 3,857 个 |
| 核心模块 | 20+ 个 |

## License

MIT License
