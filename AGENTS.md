# GhostAP 技术架构文档

> 最后更新：2026-02-26

## 1. 系统架构总览

GhostAP 采用分层架构，从上到下为：**接入层 → 调度层 → 业务层 → 协议层 → 执行层**。

```
┌──────────────────────────────────────────────────────────────────────────┐
│                          接入层 (Feishu)                                 │
│  FeishuWSClient → MessageCache(去重) → _handle_message(校验/过期)        │
└───────────────────────────────┬──────────────────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────────────────┐
│                          调度层 (Tasking)                                │
│  TaskScheduler: per-chat 串行队列 + 全局并发控制 + 优先级                  │
│  IntentRecognizer: ReAct LLM 意图分类 (~30 种)                          │
└───────────────────────────────┬──────────────────────────────────────────┘
                                │ 路由分发
        ┌───────────┬───────────┼───────────┬───────────┐
        ▼           ▼           ▼           ▼           ▼
┌─────────────┐┌─────────┐┌─────────┐┌──────────┐┌──────────┐
│ Programming ││  Deep   ││  Loop   ││ Project  ││ System   │
│  Handler    ││ Handler ││ Handler ││ Handler  ││ Handler  │
│(Coco/Claude)││         ││         ││          ││(Shell等) │
└──────┬──────┘└────┬────┘└────┬────┘└──────────┘└──────────┘
       │            │          │
┌──────▼────────────▼──────────▼──────────────────────────────────────────┐
│                         协议层 (ACP)                                    │
│  ACPSessionManager → SyncACPSession → ACPSession → GhostAPClient      │
│                                                     (JSON-RPC 2.0)     │
└───────────────────────────────┬──────────────────────────────────────────┘
                                │ stdio
┌───────────────────────────────▼──────────────────────────────────────────┐
│                         执行层 (Agent Process)                           │
│  Coco Agent (ARK 方舟大模型)  /  Claude Code Agent                       │
└──────────────────────────────────────────────────────────────────────────┘
```

## 2. 模块详解

### 2.1 ACP 协议层 (`src/acp/`)

ACP（Agent Client Protocol）是 GhostAP 与 AI 后端通信的核心协议，基于 JSON-RPC 2.0 over stdio。

**设计目标**：替代早期的 subprocess CLI 调用（`coco -p`、`claude -p`），实现结构化的工具调用追踪、执行计划感知和权限控制。

```
文件                  职责
models.py            事件模型定义
                     - ACPEventType: TEXT_CHUNK, THOUGHT_CHUNK, TOOL_CALL_START/UPDATE/DONE, PLAN_UPDATE
                     - ACPEvent: 统一事件结构 (type, content, tool_call, plan)
                     - ToolCallInfo: 工具调用详情 (call_id, name, status, error)
                     - PlanInfo/PlanEntryInfo: 执行计划条目
                     - ACPSessionState: session 状态枚举
                     - PromptResult: prompt 执行结果

client.py            GhostAPClient(Client)
                     - session_update() 按 update type 分发 → ACPEvent
                     - request_permission() 自动批准（AllowedOutcome）
                     - 文件/终端操作 stub（ReadTextFileResponse 等）

session.py           ACPSession（async 生命周期）
                     - start(): spawn_agent_process() + 初始化
                     - prompt(): text_block() 发送消息
                     - cancel() / close(): 优雅终止

sync_adapter.py      SyncACPSession（async→sync 桥接）
                     - daemon thread 运行 asyncio event loop
                     - run_coroutine_threadsafe() 暴露同步 API

manager.py           ACPSessionManager
                     - 按 (chat_id, project_id) 隔离会话
                     - 超时清理、并发安全（threading.Lock）
                     - inject_context / resume 支持

renderer.py          ACPEventRenderer（事件→飞书 Markdown）
                     - 累积 TEXT + TOOL_CALL + PLAN_UPDATE
                     - render_tool_calls(): 工具状态行
                     - render_plan_view(): 计划视图（独立于正文）
                     - on_event 回调驱动实时卡片
```

**关键 SDK 模式**:
```python
from acp import spawn_agent_process, text_block
from acp.interfaces import Client, Agent
from acp.schema import ToolCallStart, ToolCallProgress, AgentMessageChunk
from acp.schema import AllowedOutcome, ReadTextFileResponse
```

### 2.2 Deep 引擎 (`src/deep_engine/`)

**定位**：单次输入完整需求 → Agent 自主规划并执行。适合明确可拆分的多步任务。

**执行流程**：
1. 用户发送 `/deep <需求>`
2. 构建包含完整需求的 prompt
3. 通过 ACP 单次 prompt 发送给 Agent
4. Agent 自主制定计划并执行（GhostAP 被动追踪）
5. 通过 ACP 事件流实时更新进度卡片

```
文件                  职责
models.py            DeepProject（项目状态）、DeepTask、EngineRunState
engine.py            DeepEngine: 核心引擎
                     - execute(): ACP prompt → 事件流处理
                     - _process_events(): 消费 ACPEvent 更新 DeepProgress
                     DeepEngineManager: per-chat 引擎管理
                     DeepEngineCallbacks: 事件转发接口
progress.py          DeepProgress: 追踪计划条目、工具调用、修改文件
reporter.py          ProgressReporter: 进度格式化为 Markdown
```

**状态机**: `IDLE → RUNNING → STOPPING → IDLE`（EngineRunState）

### 2.3 Loop 引擎 (`src/loop_engine/`)

**定位**：迭代闭环开发引擎。将产品诉求转化为可验证的验收标准，通过多轮迭代持续推进直到全部标准满足。

**核心特性**：
- **验收标准驱动**：LLM 将口语化需求拆解为结构化验收标准
- **迭代闭环**：每轮迭代后评估标准完成情况，动态决策下一步
- **收敛检测**：连续 N 轮无新标准满足时自动终止，避免无限循环
- **多视角审查（Ralph Loop）**：功能迭代完成后，从架构师/产品/用户/测试四视角审查

**执行流程**：
```
/loop <需求>
  → LLM 拆解为验收标准
    → 迭代主循环:
       ① ACP prompt 执行一轮迭代
       ② IterationTracker 追踪事件
       ③ LLM 评估验收标准完成情况
       ④ 收敛检测 → CONTINUE / COMPLETE / CONVERGED / MAX_ITER
    → 全部标准满足后进入 Ralph Loop（可选）:
       ⑤ 架构师审查 → 产品审查 → 用户体验审查 → 测试审查
       ⑥ 审查意见驱动额外迭代
    → 输出最终报告
```

```
文件                  职责
models.py            LoopProject, IterationRecord, CriteriaTracker
                     LoopRole (架构师/开发者/审查/测试/调试/集成)
engine.py            LoopEngine: 迭代主循环
                     - execute(): while RUNNING { prompt → track → evaluate }
                     - _evaluate_criteria(): LLM 评估标准完成
                     - _detect_convergence(): 收敛检测
                     - _run_review(): Ralph Loop 多视角审查
                     LoopEngineManager: per-chat 引擎管理
tracker.py           IterationTracker: 处理 ACP 事件 → 迭代记录
reporter.py          LoopReporter: 迭代进度 + 验收标准格式化
```

### 2.4 Spec 引擎 (`src/spec_engine/`)

**定位**：结构化开发方法论引擎。每个周期按照 `Spec → Plan → Task → Build → Review` 流程推进，通过多视角审查（Review）反馈驱动下一轮迭代。

**核心特性**：
- **全生命周期管理**：涵盖需求拆解、任务规划、编码实现、代码审查全流程。
- **多视角审查**：集成 Ralph Loop 的多视角审查机制（架构师/产品/用户/测试）。
- **迭代闭环**：审查意见直接作为下一轮迭代的输入，直到所有验收标准满足且审查通过。

```
文件                  职责
models.py            SpecProject, SpecCycle, SpecTask, SpecPhase
                     - SpecPhase: IDLE -> SPEC -> PLAN -> TASK -> BUILD -> REVIEW
engine.py            SpecEngine: 核心循环
                     - execute(): 驱动 SpecPhase 状态流转
                     - _run_spec_phase(), _run_plan_phase()...
                     - _run_review_phase(): 执行多视角审查
tracker.py           SpecTracker: 追踪 ACP 事件，更新 SpecTask 状态
reporter.py          SpecReporter: 生成结构化进度报告
```

### 2.5 会话后端抽象 (`src/agent_session.py`)

提供统一的 `SyncSession` 接口，支持两种后端：

| 后端 | 类 | 协议 | 特点 |
|------|-----|------|------|
| ACP | `SyncACPSession` | JSON-RPC 2.0 over stdio | 结构化事件（工具/计划/文本） |
| CLI | `SyncClaudeCLISession` | `claude -p` 子进程 | 文本流，支持 `--resume` |

```python
# 工厂方法
session = create_sync_session("coco", cwd="/path/to/project")  # → SyncACPSession
session = create_sync_session("claude", cwd="/path/to/project")  # → SyncClaudeCLISession

# 引擎用（自动启动 + 重试）
session = create_engine_session("coco", cwd="/path/to/project")
```

### 2.6 飞书集成 (`src/feishu/`)

**ws_client.py** — 消息调度中枢。通过 `_FORWARDING_MAP` + `__getattr__` 将方法调用委托到对应 Handler。

**Handler 架构**（7 个 Handler）：

| Handler | 文件 | 职责 |
|---------|------|------|
| `CocoModeHandler` | programming.py | Coco 多轮编程会话 |
| `ClaudeModeHandler` | programming.py | Claude 多轮编程会话 |
| `DeepHandler` | deep.py | Deep 引擎命令路由 |
| `LoopHandler` | loop.py | Loop 引擎命令路由 |
| `ProjectHandler` | project.py | 项目创建/切换/查看 |
| `SystemHandler` | system.py | 帮助/Shell/模式切换 |
| `DiagnosticsHandler` | diagnostics.py | 调试信息 |

**handler_context.py** — HandlerContext，所有 Handler 共享的上下文：
- `coco_manager` / `claude_manager`：ACPSessionManager 实例
- `deep_manager` / `loop_manager`：EngineManager 实例
- `project_manager`：ProjectManager
- `mode_manager`：ModeManager
- `scheduler`：TaskScheduler
- 飞书 API client

### 2.7 卡片渲染 (`src/card/`)

- **CardBuilder** — 构建飞书 Interactive Card（schema 2.0），支持按钮、菜单、Markdown 区块。引擎感知的 header 颜色（Coco=蓝、Claude=紫）。
- **StreamingCardManager** — 通过 Feishu Patch API 实现卡片实时更新。支持 desktop/mobile/responsive 三种按钮布局策略。

### 2.8 任务调度 (`src/tasking/scheduler.py`)

- 线程池 + per-chat 串行队列 + 全局并发上限（默认 10）
- `TaskSpec` 元数据：chat_id, project_id, priority, queue_key
- 长时任务（Deep/Loop）使用独立 queue_key 避免阻塞系统命令
- 支持取消令牌（CancellationToken）和进度追踪

### 2.9 意图识别 (`src/agent/intent_recognizer.py`)

基于 LangChain + ARK 的 ReAct Agent，支持 ~30 种意图类型：

```
编程模式:  ENTER_COCO, EXIT_COCO, ENTER_CLAUDE, EXIT_CLAUDE, COCO_MESSAGE, CLAUDE_MESSAGE
Shell:    SHELL_COMMAND, CHANGE_DIR
项目:     CREATE_PROJECT, SWITCH_PROJECT, LIST_PROJECTS, CLOSE_PROJECT, PROJECT_STATUS
Deep:     ENTER_DEEP, DEEP_STATUS, STOP_DEEP, DEEP_UPDATE
Loop:     ENTER_LOOP, LOOP_STATUS, STOP_LOOP, LOOP_PAUSE, LOOP_RESUME, LOOP_GUIDE
系统:     SHOW_HELP, EXIT_MODE, UNKNOWN
```

### 2.10 其他模块

| 模块 | 说明 |
|------|------|
| `src/project/` | 多项目管理（ProjectManager + ProjectContext + UnifiedContext + MessageProjectMapper） |
| `src/mode/manager.py` | ModeManager 状态机：SMART ↔ COCO / CLAUDE / SHELL |
| `src/sandbox/executor.py` | SandboxExecutor：20+ 危险正则 + 黑名单 + 超时 + 截断 |
| `src/utils/` | 错误格式化（fmt_*）、文本处理 |

## 3. 关键设计决策

### 3.1 ACP 替代 subprocess CLI

**问题**：早期通过 `coco -p` / `claude -p` 子进程调用，只有纯文本流，无法感知 Agent 内部的工具调用、文件修改、执行计划。

**方案**：迁移到 ACP（JSON-RPC 2.0 over stdio），获得结构化事件流。

**影响**：删除 `src/session/` 整个目录，重写 deep_engine（6→4文件）和 loop_engine（14→4文件），代码量减少 60%+。

### 3.2 同步-异步桥接

**问题**：ACP SDK 是异步的（asyncio），但 GhostAP 的 Handler/Engine 运行在同步线程中。

**方案**：`SyncACPSession` 在 daemon thread 中运行 asyncio event loop，通过 `run_coroutine_threadsafe()` 暴露同步 API。

### 3.3 per-chat 会话隔离

**问题**：同一用户在同一 chat 中切换项目时，AI 会话上下文可能串台。

**方案**：`ACPSessionManager` 按 `(chat_id, project_id)` 双键隔离会话，确保项目间上下文完全独立。

### 3.4 Loop 引擎收敛保护

**问题**：Agent 可能在某些验收标准上反复尝试但无进展，导致无限循环。

**方案**：三级终止保护：
1. 收敛检测（连续 N 轮无新标准满足）
2. 最大迭代上限（默认 100）
3. 用户手动停止（`/stop_loop`）

### 3.5 多视角审查（Ralph Loop）

**问题**：Agent 自评"全部标准满足"可能存在盲区。

**方案**：功能迭代完成后，追加 3 轮多视角审查迭代（架构师→产品→用户体验→测试），审查意见驱动修复。可通过 `LOOP_REVIEW_ENABLED` 开关控制。

### 3.6 ACP 缓冲区溢出

**问题**：asyncio.StreamReader 默认 64KB 缓冲区，Agent 长时间执行产生的大量输出导致 "chunk is longer than limit" 崩溃。

**方案**：`ACP_STREAM_BUFFER_LIMIT` 配置化，默认 10MB。

### 3.7 TTADK 集成与诊断

**问题**：TikTok 内部开发需要特定的工具链 (TTADK) 和模型访问权限，且这些工具通常依赖交互式 Shell 环境。

**方案**：
1.  **PTY 模拟**：在 ACP 中集成 PTY 支持，允许 Agent 像在真实终端中一样执行交互式命令（如 `ttadk auth`）。
2.  **诊断增强**：引入 `DiagnosticsHandler` 和详细的错误追踪（Snippet Capture），在启动失败或 Review 失败时提供具体的上下文信息。
3.  **模型解析**：实现智能的模型名称解析策略（`src/ttadk/manager.py`），支持别名、前缀匹配和模糊搜索，确保障碍更少的模型调用。

## 4. 线程安全

| 资源 | 保护方式 |
|------|----------|
| ACPSessionManager 会话字典 | `threading.Lock` |
| EngineManager 引擎注册 | `threading.Lock` |
| MessageCache 消息去重 | `threading.Lock` + 后台清理线程 |
| ProjectManager 项目数据 | 文件锁（fcntl/msvcrt） |
| TaskScheduler 队列 | `threading.Lock` + `queue.PriorityQueue` |
| ModeManager 模式状态 | `threading.Lock` |
| StreamingCardManager 卡片 | `threading.Lock` + 自动清理 |

## 5. 配置一览

所有配置通过 `.env` 文件 + `pydantic-settings` 管理，参见 `src/config.py`。

关键分组：

| 分组 | 配置项 | 默认值 |
|------|--------|--------|
| **飞书** | `APP_ID`, `APP_SECRET` | （必填） |
| **ARK** | `ARK_API_KEY`, `ARK_MODEL`, `ARK_BASE_URL` | （必填） |
| **沙箱** | `SANDBOX_TIMEOUT`, `SANDBOX_MAX_OUTPUT_LENGTH` | 30s, 4000 |
| **Coco** | `COCO_EXECUTION_TIMEOUT`, `COCO_SESSION_TIMEOUT` | 7200s, 86400s |
| **Claude** | `CLAUDE_EXECUTION_TIMEOUT`, `CLAUDE_CLI_SKIP_PERMISSIONS` | 7200s, true |
| **ACP** | `ACP_PERMISSION_AUTO_APPROVE`, `ACP_STREAM_BUFFER_LIMIT` | true, 10MB |
| **Loop** | `LOOP_MAX_ITERATIONS`, `LOOP_REVIEW_ENABLED` | 100, true |
| **调度** | `TASK_SCHEDULER_MAX_CONCURRENT` | 10 |
| **卡片** | `CARD_BUTTON_LAYOUT`, `STREAMING_ENABLED` | responsive, true |

## 6. 测试

815 个测试，20 个测试文件，覆盖全部核心模块。

```bash
uv run python -m pytest tests/ -v          # 全部
uv run python -m pytest tests/ -x -q       # 快速（失败即停）
```

| 测试文件 | 覆盖模块 |
|----------|----------|
| test_acp_client.py | GhostAPClient 事件分发 |
| test_acp_models.py | ACPEvent 模型 |
| test_acp_renderer.py | ACPEventRenderer Markdown 渲染 |
| test_acp_stdio_integration.py | ACP 端到端通信 |
| test_deep_engine.py | DeepEngine 执行/进度 |
| test_loop_engine.py | LoopEngine 迭代/收敛/审查 |
| test_handlers.py | Handler 意图路由 |
| test_card.py | CardBuilder 卡片构建 |
| test_streaming.py | StreamingCardManager |
| test_task_scheduler*.py | TaskScheduler 并发/稳定性 |
| test_intent.py | IntentRecognizer 分类 |
| test_sandbox.py | SandboxExecutor 安全 |
| test_project.py | ProjectManager |
| test_mode_manager.py | ModeManager 状态切换 |
| test_message_cache.py | MessageCache 去重 |
| test_unified_context.py | UnifiedContext 跨模式 |

## 7. 开发演进

项目从 2026-01 启动至今的关键里程碑：

| 时间 | 里程碑 |
|------|--------|
| 2026-01-09 | 项目创建：飞书 WebSocket + Coco 会话 + ReAct 意图识别 |
| 2026-01-18 | 多项目管理 + 安全工具链 + 三模式架构 |
| 2026-01-22 | 流式卡片 + Card schema 2.0 |
| 2026-01-29 | Claude 编程模式 + Deep Engine |
| 2026-02-01 | 项目大扫除（Session 基类 + 卡片统一 + 配置整理） |
| 2026-02-02 | ws_client God Class 拆分（3444→1170 行，6 Handler） |
| 2026-02-09 | Loop Engine 完整实现（12 模块，1550 测试） |
| 2026-02-10 | **ACP 协议重构**（subprocess→JSON-RPC 2.0，删除 src/session/） |
| 2026-02-11 | Ralph Loop 多视角审查 + 架构优化 14 项 + 性能优化 10 项 |
| 2026-02-12 | ACP 缓冲区溢出修复 + Shell 卡片渲染 |
| 2026-02-24 | Loop 验收标准 LLM 拆解 + Deep 卡片修复 |
| 2026-02-26 | Loop 多视角审查输出解析三级容错 |
| 2026-03-02 | **Spec 引擎预览** (Spec-driven development) |
| 2026-03-09 | **TTADK 深度集成** (PTY, Diagnostics, Model Resolver) |
