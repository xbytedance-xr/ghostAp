# ACP 重构架构设计文档

> 架构师视角 — 将 GhostAP 的 Coco/Claude 调用从 subprocess CLI 模式重构为 ACP (Agent Client Protocol) 模式

## 一、现状分析

### 1.1 当前架构（subprocess 模式）

```
Feishu Message → Handler → SessionManager → BaseSession → subprocess.Popen("coco -p"/"claude -p")
                                                          ↓
                                              select() + os.read() 逐行读取 stdout
                                                          ↓
                                              on_chunk(text) → StreamingCard 更新
```

**核心问题：**
- **无结构化信息**：只能获取纯文本流，无法知道 agent 正在执行什么工具、读写哪些文件
- **无执行计划可见性**：agent 的 plan/task 进度对外不可见
- **无权限控制**：使用 `--dangerously-skip-permissions` 绕过所有检查
- **会话管理脆弱**：Claude 的 session ID 过期需要特殊恢复逻辑（`_reset_stale_session`）
- **模块膨胀**：Deep Engine 6 个文件（含 parser/planner/executor），Loop Engine 14 个文件

### 1.2 目标架构（ACP 模式）

```
Feishu Message → Handler → ACPSessionManager → SyncACPSession → ACPSession
                                                                    ↓
                                                         spawn_agent_process()
                                                                    ↓
                                                         JSON-RPC 2.0 over stdio
                                                                    ↓
                                                  GhostAPClient ← session/update notifications
                                                                    ↓
                                                    ACPEvent → ACPEventRenderer → StreamingCard
```

### 1.3 现有资产

- **`acp` 分支**已有完整实现（commit `0251d46`），但未合并到 `multicoco`
- **ACP SDK** `agent-client-protocol==0.8.0` 已安装
- **Coco** (`v0.111.3`) 原生支持 `coco acp serve`
- **Claude** (`v2.1.38`) 不直接支持 `acp serve`，但 ACP SDK 的 `spawn_agent_process("claude", ...)` 内部处理了协议适配

---

## 二、ACP 协议要点

### 2.1 核心概念

| 概念 | 说明 |
|------|------|
| **Transport** | JSON-RPC 2.0 over stdio（UTF-8，换行分隔，不含嵌套换行） |
| **Client** | 代码编辑器/宿主程序（GhostAP），实现 `Client` 接口接收回调 |
| **Agent** | AI 代理进程（Coco/Claude），由 Client 作为子进程启动 |
| **Session** | 独立对话线程，每个连接支持多个并发 session |

### 2.2 协议生命周期

```
Client                              Agent
  |-- initialize ------------------>|    版本协商 + 能力声明
  |<--- response (capabilities) ----|
  |                                 |
  |-- session/new (cwd) ----------->|    创建会话
  |<--- response (sessionId) -------|
  |                                 |
  |-- session/prompt (text) ------->|    发送提示
  |<--- session/update (message) ---|    ← 文本流
  |<--- session/update (tool_call) -|    ← 工具调用开始
  |<--- request_permission -------->|    ← 权限请求（可选）
  |--- AllowedOutcome ------------->|
  |<--- session/update (tool_upd) --|    ← 工具进度/完成
  |<--- session/update (plan) ------|    ← 执行计划更新
  |<--- prompt response (stop) -----|    ← 完成
  |                                 |
  |-- session/cancel --------------->|    取消（可选）
  |-- (close stdin) --------------->|    关闭连接
```

### 2.3 session/update 事件类型

| 类型 | ACP Schema 类型 | 说明 |
|------|----------------|------|
| `message` | `AgentMessageChunk` | 文本输出（增量） |
| `thought` | `AgentThoughtChunk` | 思考过程（增量） |
| `tool_call` | `ToolCallStart` | 工具调用开始（id, title, kind, status=pending） |
| `tool_call_update` | `ToolCallProgress` | 工具状态更新（in_progress/completed/failed） |
| `plan` | `AgentPlanUpdate` | 执行计划（entries: [{content, priority, status}]） |

### 2.4 工具调用种类（kind）

`read` / `edit` / `delete` / `move` / `search` / `execute` / `think` / `fetch` / `other`

### 2.5 停止原因（stop_reason）

`end_turn` / `max_tokens` / `max_turn_requests` / `refusal` / `cancelled`

---

## 三、新模块设计：`src/acp/`

### 3.1 模块结构

```
src/acp/
├── __init__.py        # 公共导出
├── models.py          # 数据模型（ACPEvent, ToolCallInfo, PlanInfo, etc.）
├── client.py          # GhostAPClient — 实现 ACP Client 接口
├── session.py         # ACPSession — 异步会话生命周期
├── sync_adapter.py    # SyncACPSession — 同步线程桥接
├── manager.py         # ACPSessionManager — 每聊天会话管理
└── renderer.py        # ACPEventRenderer — ACP事件→飞书Markdown
```

### 3.2 数据模型 (`models.py`)

```python
class ACPEventType(Enum):
    TEXT_CHUNK = "text_chunk"           # ← AgentMessageChunk
    THOUGHT_CHUNK = "thought_chunk"     # ← AgentThoughtChunk
    TOOL_CALL_START = "tool_call_start" # ← ToolCallStart
    TOOL_CALL_UPDATE = "tool_call_update" # ← ToolCallProgress (in_progress)
    TOOL_CALL_DONE = "tool_call_done"   # ← ToolCallProgress (completed/failed)
    PLAN_UPDATE = "plan_update"         # ← AgentPlanUpdate

@dataclass
class ToolCallInfo:
    id: str
    title: str
    kind: str        # read/edit/delete/execute/think/search/fetch/other
    status: str      # pending/in_progress/completed/failed
    content: str = ""
    locations: list[str] = field(default_factory=list)

@dataclass
class PlanEntryInfo:
    content: str
    priority: str = "medium"   # high/medium/low
    status: str = "pending"    # pending/in_progress/completed

@dataclass
class PlanInfo:
    entries: list[PlanEntryInfo]

@dataclass
class ACPEvent:
    event_type: ACPEventType
    text: Optional[str] = None
    tool_call: Optional[ToolCallInfo] = None
    plan: Optional[PlanInfo] = None
    timestamp: float = field(default_factory=time.time)

@dataclass
class ACPSessionState:
    session_id: str
    agent_type: str    # "coco" / "claude"
    cwd: str
    created_at: float
    message_count: int = 0
    is_active: bool = True
    last_active: float
    # Supports to_dict() / from_dict() for persistence

@dataclass
class PromptResult:
    stop_reason: str   # end_turn/max_tokens/cancelled/...
    text: str = ""
    tool_calls: list[ToolCallInfo]
    plan: Optional[PlanInfo] = None
    modified_files: set[str]
```

### 3.3 Client 实现 (`client.py`)

```python
class GhostAPClient(Client):
    """实现 acp.interfaces.Client，将 session/update 转换为 ACPEvent。"""

    def __init__(self, on_event: Callable[[ACPEvent], None], auto_approve: bool = True):
        ...

    # Core: session_update → dispatch by type → emit ACPEvent
    async def session_update(self, session_id, update, **kw) -> None:
        if isinstance(update, AgentMessageChunk):  → ACPEvent(TEXT_CHUNK)
        if isinstance(update, AgentThoughtChunk):  → ACPEvent(THOUGHT_CHUNK)
        if isinstance(update, ToolCallStart):      → ACPEvent(TOOL_CALL_START)
        if isinstance(update, ToolCallProgress):   → ACPEvent(TOOL_CALL_UPDATE or TOOL_CALL_DONE)
        if isinstance(update, AgentPlanUpdate):    → ACPEvent(PLAN_UPDATE)

    # Permission: auto-approve (选择 allow_once)
    async def request_permission(self, options, session_id, tool_call, **kw):
        → AllowedOutcome(option_id=allow_once_id, outcome="selected")

    # File/Terminal stubs (agent 自己管理文件系统)
    async def read_text_file(...)  → ReadTextFileResponse(content="")
    async def write_text_file(...) → WriteTextFileResponse()
    async def create_terminal(...) → CreateTerminalResponse(terminal_id="stub")
    ...
```

### 3.4 异步会话 (`session.py`)

```python
class ACPSession:
    """管理单个 ACP agent 进程的完整生命周期。"""

    def __init__(self, agent_cmd: str, agent_args: list[str], cwd: str):
        ...

    async def start(self) -> str:
        # 1. spawn_agent_process(GhostAPClient, cmd, *args, cwd=...)
        # 2. conn.initialize(protocol_version=1)
        # 3. conn.new_session(cwd=cwd) → session_id

    async def load_session(self, session_id: str) -> None:
        # conn.load_session(cwd, session_id) — 恢复已有会话

    async def prompt(self, text: str, on_event=None) -> PromptResult:
        # conn.prompt(session_id, [text_block(text)]) — 发送并等待
        # 事件通过 GhostAPClient → _dispatch_event → on_event 回调

    async def cancel(self) -> None:
        # conn.cancel(session_id) — 取消进行中的 prompt

    async def close(self) -> None:
        # ctx_manager.__aexit__() — 关闭连接，终止子进程
```

### 3.5 同步适配器 (`sync_adapter.py`)

```python
class SyncACPSession:
    """将 async ACPSession 桥接到同步线程世界。"""

    # 架构：daemon thread + asyncio event loop
    # Main thread (sync)  ←→  Background thread (async event loop)

    # Agent 命令参数预设
    _AGENT_ARGS = {
        "coco": ["--no-interactive"],     # coco 的 ACP 模式标志
        "claude": ["--no-interactive"],   # claude 的非交互标志
    }

    def start(self) -> str:
        # 1. 创建 asyncio.new_event_loop()
        # 2. 启动 daemon thread 运行 loop.run_forever()
        # 3. run_coroutine_threadsafe(ACPSession.start()) → session_id

    def send_prompt(self, text, on_event=None, timeout=None) -> PromptResult:
        # run_coroutine_threadsafe(session.prompt(text)) → blocking wait

    def cancel(self) -> None:
        # run_coroutine_threadsafe(session.cancel()) — fire-and-forget

    def close(self) -> None:
        # 关闭 session → 停止 event loop → join thread

    def to_snapshot(self) -> dict:
        # 可持久化的会话快照

    # 兼容旧 BaseSession 接口的属性
    session_id, created_at, last_active, message_count, last_query, is_resumed
```

### 3.6 会话管理器 (`manager.py`)

```python
class ACPSessionManager:
    """统一的每聊天 ACP 会话管理器，替代 CocoSessionManager/ClaudeSessionManager。"""

    def __init__(self, agent_type: str, session_timeout: int = 86400):
        # agent_type: "coco" / "claude"
        # _sessions: dict[str, SyncACPSession]  — chat_id → session

    def start_session(self, chat_id, cwd="", session_id=None) -> SyncACPSession:
        # 关闭旧 session → 创建新 SyncACPSession → start() → 可选 load_session()

    def resume_session(self, chat_id, session_id, cwd="") -> SyncACPSession:
        # = start_session(chat_id, cwd, session_id)

    def get_session(self, chat_id) -> Optional[SyncACPSession]:
        # 带超时检查的获取

    def end_session(self, chat_id) -> Optional[dict]:
        # close() → 返回 snapshot

    def has_active_session(self, chat_id) -> bool
    def get_session_info(self, chat_id) -> Optional[str]
    def cleanup_all(self) -> None
```

### 3.7 事件渲染器 (`renderer.py`)

```python
class ACPEventRenderer:
    """将 ACP 事件流转换为飞书 Markdown。维护状态以构建完整视图。"""

    # 内部状态
    _text_buffer: str              # 累积文本
    _active_tools: dict[str, ToolCallInfo]  # 进行中的工具
    _completed_tools: list[ToolCallInfo]    # 已完成的工具
    _plan: Optional[PlanInfo]      # 最新执行计划
    _modified_files: set[str]      # 修改过的文件路径

    def process_event(self, event: ACPEvent) -> str:
        # 处理事件 → 更新内部状态 → 返回完整渲染内容
        # TEXT_CHUNK → 追加到 text_buffer
        # TOOL_CALL_START → 加入 active_tools
        # TOOL_CALL_DONE → 移入 completed_tools + 内联摘要
        # PLAN_UPDATE → 替换 plan

    def get_final_content(self) -> str:
        # 清除 active_tools → 返回最终渲染

    # 渲染结构：
    # **📋 执行计划**
    # ✅ Step 1
    # 🔄 Step 2 (进行中)
    # ⏳ Step 3 (待执行)
    #
    # 🔍 Searching... (当前工具)
    #
    # Agent 文本输出...
    # 📖 Read file `src/main.py` ✅
```

---

## 四、引擎层重构

### 4.1 Deep Engine（6→3 文件）

**删除**：`parser.py`、`planner.py`、`executor.py`（agent 通过 ACP 自主规划执行）

**保留/新增**：
- `engine.py` — 单 prompt 驱动，通过 ACP 事件跟踪进度
- `models.py` — 保留 `EngineRunState`、`DeepProject` 等模型
- `progress.py`（新）— `DeepProgress` 从 ACP 事件提取计划/工具/文件进度

```python
# engine.py 核心流程
class DeepEngine:
    def plan_and_execute(self, requirement: str, callbacks: DeepEngineCallbacks):
        session = SyncACPSession(agent_type=self._agent_type, cwd=self.root_path)
        session.start()

        prompt = f"请完整实现以下需求：\n{requirement}\n请自行规划和执行..."

        def on_event(event: ACPEvent):
            renderer.process_event(event)
            if event.event_type == PLAN_UPDATE: progress.update_plan(event.plan)
            if event.event_type == TOOL_CALL_DONE: progress.record_tool(event.tool_call)
            callbacks.on_event(event)

        result = session.send_prompt(prompt, on_event=on_event, timeout=timeout)
        session.close()

@dataclass
class DeepEngineCallbacks:
    on_planning_start: Callable[[], None]
    on_planning_done: Callable[[DeepProject], None]
    on_event: Callable[[ACPEvent], None]     # 原始 ACP 事件转发
    on_text: Callable[[str], None]            # 文本流转发
    on_project_done: Callable[[DeepProject], None]
    on_error: Callable[[str], None]
```

### 4.2 Loop Engine（14→4 文件）

**删除**：`analyzer.py`、`roles.py`、`controller.py`、`adapter.py`、`tool_integrator.py`、`optimizer.py`、`context_manager.py`、`product_analyzer.py`、`iteration_flow.py`、`task_manager.py`、`termination_detector.py`、`termination.py`

**保留/新增**：
- `engine.py` — 单 ACP 会话多轮 prompt，收敛检测，标准评估
- `models.py` — 保留核心模型，`IterationRecord` 字段微调
- `tracker.py`（新）— `IterationTracker` 从 ACP 事件提取单次迭代信息
- `reporter.py` — 简化签名，移除 adapter 依赖

```python
# engine.py 核心流程
class LoopEngine:
    def execute(self, requirement: str, callbacks: LoopEngineCallbacks):
        session = SyncACPSession(agent_type=self._agent_type, cwd=self.root_path)
        session.start()

        for iteration in range(1, max_iterations + 1):
            iter_tracker = IterationTracker()

            def on_event(event):
                iter_tracker.process(event)
                renderer.process_event(event)
                callbacks.on_iteration_event(iteration, event)

            prompt = self._build_iteration_prompt(iteration, requirement, history)
            result = session.send_prompt(prompt, on_event=on_event, timeout=timeout)

            # 标准评估（同一 session 中发送评估 prompt）
            eval_result = self._evaluate_criteria(session, criteria)

            # 收敛检测
            if self._detect_convergence(recent_outputs):
                break

        session.close()

@dataclass
class LoopEngineCallbacks:
    on_analyzing_start: Callable[[], None]
    on_analyzing_done: Callable[[LoopProject], None]
    on_iteration_start: Callable[[int, int], None]
    on_iteration_event: Callable[[int, ACPEvent], None]
    on_iteration_done: Callable[[int, IterationRecord], None]
    on_project_done: Callable[[LoopProject], None]
    on_error: Callable[[str], None]
```

---

## 五、集成层变更

### 5.1 handler_context.py

```python
@dataclass
class HandlerContext:
    # 旧：coco_manager: CocoSessionManager
    # 旧：claude_manager: ClaudeSessionManager
    # 新：
    coco_manager: ACPSessionManager     # ACPSessionManager("coco")
    claude_manager: ACPSessionManager   # ACPSessionManager("claude")
    ...
```

### 5.2 ws_client.py

```python
class FeishuWSClient:
    def __init__(self, ...):
        # 旧：self._coco_manager = CocoSessionManager()
        # 旧：self._claude_manager = ClaudeSessionManager()
        # 新：
        self._coco_manager = ACPSessionManager("coco", session_timeout=settings.coco_session_timeout)
        self._claude_manager = ACPSessionManager("claude", session_timeout=settings.claude_session_timeout)
```

### 5.3 handlers/programming.py

```python
class ProgrammingModeHandler:
    def handle_response(self, ...):
        # 旧：session.send_prompt_streaming(text, on_chunk=lambda chunk: ...)
        # 新：
        renderer = ACPEventRenderer()
        def on_event(event: ACPEvent):
            rendered = renderer.process_event(event)
            if rendered and streaming_card:
                streaming_manager.update_content(streaming_card, rendered)

        result = session.send_prompt(text, on_event=on_event, timeout=timeout)
        final_content = renderer.get_final_content()
```

### 5.4 handlers/deep.py

```python
class DeepHandler:
    def _create_deep_callbacks(self, ...):
        renderer = ACPEventRenderer()

        def on_event(event: ACPEvent):
            rendered = renderer.process_event(event)
            if event.event_type == ACPEventType.PLAN_UPDATE:
                # 发送计划卡片
                ...

        return DeepEngineCallbacks(on_event=on_event, ...)
```

### 5.5 handlers/loop.py

```python
class LoopHandler:
    def _create_loop_callbacks(self, ...):
        # 类似 DeepHandler，但以迭代为粒度
        def on_iteration_event(iteration, event):
            # 处理 ACP 事件并更新卡片
            ...

        return LoopEngineCallbacks(on_iteration_event=on_iteration_event, ...)
```

### 5.6 config.py

新增/确认字段：
```python
class Settings(BaseSettings):
    # 已有
    coco_session_timeout: int = 86400
    claude_session_timeout: int = 86400
    # 引擎使用已有 timeout 字段即可
```

---

## 六、不受影响的模块

以下模块 **无需修改**：

| 模块 | 原因 |
|------|------|
| `src/agent/intent_recognizer.py` | 独立使用 LangChain/ARK，不涉及 session |
| `src/sandbox/executor.py` | Shell 命令直接执行，不涉及 AI session |
| `src/mode/manager.py` | 纯状态机，不依赖 session 实现 |
| `src/project/` | 项目管理层，session 快照格式兼容 |
| `src/card/builder.py` | 卡片构建层，输入是 Markdown 字符串 |
| `src/card/streaming.py` | 流式卡片更新，输入是内容字符串 |
| `src/tasking/scheduler.py` | 任务调度层，与 session 实现无关 |
| `src/feishu/message_cache.py` | 消息缓存，无 session 依赖 |

---

## 七、删除的模块

| 文件 | 原因 |
|------|------|
| `src/session/__init__.py` | 整个 session 目录被 `src/acp/` 替代 |
| `src/session/base.py` | subprocess 基类，被 `ACPSession` + `SyncACPSession` 替代 |
| `src/session/coco.py` | Coco subprocess 实现，被 `ACPSessionManager("coco")` 替代 |
| `src/session/claude.py` | Claude subprocess 实现，被 `ACPSessionManager("claude")` 替代 |
| `src/session/manager.py` | 旧会话管理器，被 `ACPSessionManager` 替代 |
| `src/deep_engine/parser.py` | 需求解析器（agent 自行解析） |
| `src/deep_engine/planner.py` | 任务规划器（agent 自行规划） |
| `src/deep_engine/executor.py` | 任务执行器（agent 自行执行） |
| `src/loop_engine/analyzer.py` | 需求分析器（简化为内联解析） |
| `src/loop_engine/roles.py` | 角色系统（agent 自行决策） |
| `src/loop_engine/termination.py` | 终止判定器（简化为内联收敛检测） |

---

## 八、实施计划（8 阶段）

### Phase 1: ACP 基础层
**创建 `src/acp/` 全部 6 个文件**
- `models.py` → 数据模型
- `client.py` → GhostAPClient 实现
- `session.py` → 异步会话
- `sync_adapter.py` → 同步桥接
- `manager.py` → 会话管理
- `renderer.py` → 事件渲染
- **测试**：`test_acp_models.py`、`test_acp_client.py`、`test_acp_renderer.py`

### Phase 2: 编程模式迁移
**改造 `handlers/programming.py`、`handler_context.py`**
- `ProgrammingModeHandler` 使用 `ACPSessionManager` + `ACPEventRenderer`
- `HandlerContext` 类型更新
- **测试**：验证 Coco/Claude 编程模式事件流

### Phase 3: ws_client.py 集成
**改造 `ws_client.py`**
- 替换 session manager 实例化
- 确保所有路由正常
- **测试**：`test_ws_client_patch.py` 更新

### Phase 4: Deep Engine 重构
**改造 `src/deep_engine/`**
- 删除 `parser.py`、`planner.py`、`executor.py`
- 新增 `progress.py`
- 重写 `engine.py`
- **测试**：`test_deep_engine.py`

### Phase 5: Loop Engine 重构
**改造 `src/loop_engine/`**
- 删除 `analyzer.py`、`roles.py`、`termination.py`
- 新增 `tracker.py`
- 重写 `engine.py`、简化 `models.py`、`reporter.py`
- **测试**：`test_loop_engine.py`、`test_loop_models.py`

### Phase 6: Handler 层适配
**改造 `handlers/deep.py`、`handlers/loop.py`**
- 使用新的 Callbacks 模式
- ACP 事件驱动卡片更新
- **测试**：`test_handlers.py` 更新

### Phase 7: 清理与删除
- 删除 `src/session/` 目录
- 删除 engine 中废弃模块
- 更新 `__init__.py` 导出
- `config.py` 清理

### Phase 8: 全量测试与修复
- 运行全量测试 `uv run pytest tests/ -v`
- 修复 import 引用、类型不匹配
- 验证 SMART 模式不受影响

---

## 九、关键设计决策

### 9.1 为什么使用 `spawn_agent_process` 而非直接 `coco acp serve`？

ACP SDK 的 `spawn_agent_process(client, "coco", *args)` 内部处理了：
- 子进程启动
- JSON-RPC 消息帧封装
- 协议版本协商
- 双向消息路由
- 进程生命周期管理

直接调用 `coco acp serve` 需要自己实现所有 JSON-RPC 处理。SDK 封装更安全更可靠。

### 9.2 为什么 Claude 不需要 `acp serve` 子命令？

Claude Code CLI v2.1.38 原生支持 ACP 协议的 stdio transport。`spawn_agent_process("claude", "--no-interactive")` 直接通过 stdin/stdout 进行 JSON-RPC 通信。`--no-interactive` 标志禁用终端 UI，使其适合作为 ACP agent 运行。

### 9.3 为什么 file/terminal 操作使用 stub 实现？

在 GhostAP 的使用场景中，agent 运行在与 GhostAP 相同的机器上，直接访问文件系统。Client 端的 file/terminal 接口主要用于 IDE 场景（编辑器暂存区、编辑器终端面板）。GhostAP 不需要拦截这些操作，因此使用空实现。

### 9.4 同步-异步桥接为什么用 daemon thread？

GhostAP 的核心是同步线程模型（Feishu WebSocket → TaskScheduler → Handler）。ACP SDK 是 async。使用 daemon thread + `asyncio.new_event_loop()` 是最简单的桥接方式：
- daemon thread 随主进程退出而终止
- `run_coroutine_threadsafe` 提供线程安全的异步调用
- 每个 `SyncACPSession` 有独立的 event loop，隔离性好

### 9.5 权限控制策略

当前策略：**全部自动审批**（选择 `allow_once` 选项）。这等价于旧的 `--dangerously-skip-permissions`。

未来可扩展为：
- 配置 `auto_approve` 白名单（按 tool kind）
- 危险操作（`delete`、`execute`）需要人工审批
- 通过飞书卡片按钮实现交互式审批

---

## 十、风险评估

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| Claude CLI 不支持 ACP | 高 | `acp` 分支已验证可行；`--no-interactive` 标志兼容 |
| ACP SDK 版本不稳定 | 中 | 锁定 `agent-client-protocol==0.8.0`；所有 schema 类型在 client.py 中适配 |
| Event loop 泄漏 | 中 | `SyncACPSession.close()` 显式停止 loop + join thread |
| 旧 session 快照不兼容 | 低 | `to_snapshot()` 返回兼容格式；项目 context 只存 session_id |
| SMART 模式被误影响 | 低 | SMART 模式使用 IntentRecognizer → 不涉及 session 层 |

---

## 十一、测试策略

### 新增测试文件
| 文件 | 覆盖范围 |
|------|----------|
| `test_acp_models.py` | 6 个模型类的构造、序列化、边界条件 |
| `test_acp_client.py` | GhostAPClient 事件分发、权限处理、stub 实现 |
| `test_acp_renderer.py` | ACPEventRenderer 渲染逻辑、状态累积、Markdown 格式 |
| `test_deep_engine.py` | DeepEngine ACP 集成、回调、进度跟踪 |
| `test_loop_engine.py` | LoopEngine 多轮 prompt、收敛检测、标准评估 |

### 更新测试文件
| 文件 | 变更 |
|------|------|
| `test_ws_client_patch.py` | session manager mock 类型更新 |
| `test_handlers.py` | 编程模式 handler mock 更新 |
| `test_loop_models.py` | 字段名变更（iteration_id → iteration） |

### 删除测试文件
对应已删除模块的测试文件（15+ 个）。
