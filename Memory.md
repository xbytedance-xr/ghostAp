# GhostAP 项目进展记录

## 项目概述
GhostAP 是一个飞书机器人Shell沙箱服务，通过飞书机器人对话来安全执行本地shell命令，并支持 Coco AI 和 Claude AI 远程开发模式。

## 最新更新
**更新时间**: 2026-02-02 21:30:00

### 项目级任务隔离与系统命令快速通道（2026-02-02 21:30:00）

#### 问题背景
在编码模式下，系统命令（如 `/help`, `/projects`）被阻塞，无法立即执行。不同项目的任务也无法并发执行。

#### 解决方案
重新设计任务调度系统，实现以项目维度管理对话任务：

1. **TaskSpec 增强**
   - 新增 `is_system_command: bool` 字段
   - 新增 `get_effective_queue_key()` 方法，自动计算 queue_key
   - 路由规则：
     - 系统命令: `{chat_id}:SYSTEM` (高并发)
     - 项目任务: `{chat_id}:{project_id}` (串行)
     - 无项目任务: `{chat_id}:DEFAULT` (串行)

2. **TaskScheduler 改进**
   - 参数重命名: `per_chat_concurrency` → `per_key_concurrency`
   - 新增 `system_concurrency` 参数（默认 10），系统队列并发数
   - 系统命令使用独立快速通道，不受 `per_key_concurrency` 限制

3. **ModeManager 项目级模式管理**
   - 支持 `project_id` 参数，每个项目可以有独立的编码模式
   - 模式解析顺序：项目模式 > chat 模式 > SMART（默认）
   - 新增 `clear_project_mode()`, `get_project_mode()` 方法

4. **ws_client.py 集成**
   - 新增 `_is_system_command_message()` 方法识别系统命令
   - 新增 `_is_system_card_action()` 方法识别系统卡片动作
   - 系统命令使用 `TaskPriority.HIGH` + `is_system_command=True`

#### 系统命令定义
立即执行（系统命令）：`/help`, `/帮助`, `/projects`, `/status`, `/coco_info`, `/claude_info`, `/deep_status`, `/deep_board`

受项目隔离（普通任务）：`/coco`, `/claude`, `/exit`, `/deep <requirement>`, 普通消息

#### 测试
- 新增 6 个 TaskScheduler 测试（queue_key 计算、系统队列高并发、项目并发隔离）
- 新增 7 个 ModeManager 项目级模式测试
- 全部 744 个测试通过

#### 修改文件
- `src/tasking/scheduler.py`: TaskSpec 增强、TaskScheduler 改进
- `src/tasking/__init__.py`: 导出新常量
- `src/mode/manager.py`: 项目级模式管理
- `src/feishu/ws_client.py`: 系统命令识别与路由
- `tests/test_task_scheduler.py`: 新增测试
- `tests/test_task_scheduler_stability.py`: 参数名更新
- `tests/test_mode_manager.py`: 新增项目级模式测试

---

### 清理无关入口文件（2026-02-02 19:20:00）
- 删除根目录误入的爬虫项目入口文件 `main.py`，避免与真实入口混淆
- 验证：`uv run pytest tests/test_project.py -q`

**更新时间**: 2026-02-02 19:10:00

### 项目持久化原子写与损坏恢复（2026-02-02 19:10:00）
- 为 `projects.json` 持久化引入跨进程文件锁与原子写入，降低中断/并发导致的文件损坏风险
- 加入损坏文件备份策略：读取失败时备份为 `projects.json.corrupt.<ts>` 并继续启动
- 新增单元测试覆盖损坏文件自动恢复场景
- 测试: `uv run pytest tests/test_project.py -k corrupted`

**更新时间**: 2026-02-02 18:30:00
**更新时间**: 2026-02-02 18:30:00

### ws_client.py God Class 拆分重构（2026-02-02 18:30:00）

将 `src/feishu/ws_client.py`（3,444 行 / 104 方法）拆分为 Dispatcher + Handlers 架构。ws_client.py 从 3,444 行缩减至约 1,170 行，业务逻辑移至 6 个专职 Handler。

#### 架构设计

- **HandlerContext** (`src/feishu/handler_context.py`): 依赖注入容器 dataclass，聚合 16+ 共享依赖
- **BaseHandler** (`src/feishu/handlers/base.py`): 公共基类，提供消息发送/回复/反应/流式卡片/工作目录/上下文桥接等工具方法
- **ProgrammingModeHandler** (`src/feishu/handlers/programming.py`): Coco/Claude 共享模板方法基类
  - **CocoModeHandler**: ~40 行配置子类
  - **ClaudeModeHandler**: ~40 行配置子类
- **DeepHandler** (`src/feishu/handlers/deep.py`): Deep Engine 全生命周期（start/status/pause/resume/stop/update）
- **ProjectHandler** (`src/feishu/handlers/project.py`): 项目 CRUD + 上下文保存/恢复 + 项目切换
- **SystemHandler** (`src/feishu/handlers/system.py`): 帮助/退出/Shell/目录/拦截命令路由
- **DiagnosticsHandler** (`src/feishu/handlers/diagnostics.py`): 任务看板/上下文 Diff/消息追踪

#### ws_client.py 改造

- `__init__` 中创建 HandlerContext，实例化 6 个 Handler，建立跨 Handler 引用（互斥模式、拦截命令路由）
- 保留 ~60 个向后兼容转发 stub（如 `_enter_coco_mode` → `self._coco_handler.enter_mode(...)`）
- 核心路由方法保留在 ws_client 中（_handle_message, _process_message_async, _process_with_intent, _process_card_action_async, _execute_single_task 等）

#### 测试

- 修复 5 个因 mock 目标从 ws_client stub 迁移到 handler 级别的测试（test_claude.py, test_ws_client_patch.py）
- 新增 `tests/test_handlers.py`: 85 个测试覆盖 BaseHandler、SystemHandler（路由/退出）、CocoModeHandler、ClaudeModeHandler（模板方法/进入/退出/卡片）、ProjectHandler、DeepHandler、DiagnosticsHandler
- 新增 `tests/test_mode_manager.py`: 16 个测试覆盖 ModeManager 状态机（基础切换、谓词、显示名、隔离性、线程安全）
- 全部 730 个测试通过（原 629 + 新增 101）

#### 文件清单

| 文件 | 操作 | 行数 |
|------|------|------|
| `src/feishu/handler_context.py` | 新增 | ~62 |
| `src/feishu/handlers/__init__.py` | 新增 | ~20 |
| `src/feishu/handlers/base.py` | 新增 | ~438 |
| `src/feishu/handlers/programming.py` | 新增 | ~507 |
| `src/feishu/handlers/deep.py` | 新增 | ~469 |
| `src/feishu/handlers/project.py` | 新增 | ~245 |
| `src/feishu/handlers/system.py` | 新增 | ~291 |
| `src/feishu/handlers/diagnostics.py` | 新增 | ~473 |
| `src/feishu/ws_client.py` | 修改 | 3444→~1170 |
| `tests/test_handlers.py` | 新增 | ~740 |
| `tests/test_mode_manager.py` | 新增 | ~135 |
| `tests/test_claude.py` | 修改 | mock 目标更新 |
| `tests/test_ws_client_patch.py` | 修改 | mock 目标更新 |

---

### Deep Engine 实时上下文调整优化（2026-02-02 08:00:00）

为 Deep Engine 引入执行上下文累积器、LLM 自适应 prompt 调整、智能失败重规划以及 `/deep_update` 用户注入命令，解决三个核心缺陷：任务 prompt 静态固化、失败重试盲目、无用户介入通道。

#### 新增 `src/deep_engine/models.py` — ContextEntry + ExecutionContext
- **ContextEntry**: 上下文条目数据类，支持 task_result / user_injection / deviation / adaptation 四种类型
- **ExecutionContext**: 线程安全的上下文累积器（`threading.Lock` 保护）
  - `add_result()`: 记录任务执行结果（不触发 adaptation flag）
  - `inject_user_context()`: 注入用户上下文（触发 adaptation flag）
  - `has_meaningful_context()`: O(1) 布尔 flag 判断
  - `consume_new_context_flag()`: 消费 flag 避免重复触发
  - `build_context_prompt(max_entries=10)`: 构建上下文摘要给 LLM
  - 完整 `to_dict()` / `from_dict()` 序列化支持
- **DeepTask** 新增 `original_prompt` 和 `adapted_prompt` 可选字段用于审计跟踪

#### 新增 `src/deep_engine/planner.py` — adapt_task_prompt()
- 使用专用 system prompt 指导 LLM 做保守调整
- LLM 返回 JSON: `{should_adapt, reason, adapted_prompt}`
- 异常时 fallback 到原始 prompt，不影响执行

#### 修改 `src/deep_engine/engine.py` — 核心执行循环改造
- 新增 `_execution_context: ExecutionContext` 字段
- 新增 `inject_context(message)` 公共方法（线程安全，供 ws_client 调用）
- 新增 `on_context_adapted` 回调（task, reason, prompt_preview）
- **execute() 循环改造**:
  - ① 上下文感知 prompt 适配（仅当 `has_meaningful_context()` 时触发 LLM）
  - ② 执行任务
  - ③ 记录结果到上下文
  - ④ 智能失败处理：使用 `replan_task()` 替代盲目重试

#### 修改 `src/deep_engine/reporter.py` — 新增展示方法
- `format_context_injected()` / `get_context_injected_title()`
- `format_task_adapted()` / `get_task_adapted_title()`

#### 修改 `src/agent/intent_recognizer.py` — 新增意图类型
- `IntentType.DEEP_UPDATE = "deep_update"`
- `INTENT_MAP` / `EXACT_COMMANDS` / `_quick_match()` 中添加 `/deep_update` 映射

#### 修改 `src/feishu/ws_client.py` — /deep_update 命令路由
- `_handle_deep_command()`: 新增 `/deep_update` 路由
- 新增 `_update_deep_context()`: 找到 active engine → 调用 `inject_context()` → 回复确认卡片
- `_create_deep_callbacks()`: 新增 `on_context_adapted` 回调处理

#### 修改 `src/deep_engine/__init__.py` — 导出更新
- 新增 `ContextEntry`、`ExecutionContext` 到导出列表

#### 新增测试 `tests/test_deep_engine.py` — 35 个新测试
- `TestContextEntry` (2): 基础创建、序列化
- `TestExecutionContext` (12): CRUD、flag 机制、上下文构建、max_entries 截断、线程安全、序列化
- `TestDeepTaskAdaptedFields` (4): 默认值、赋值、序列化、反序列化兼容
- `TestTaskPlannerAdapt` (4): LLM mock 测试 adapt/no-adapt/error/unparseable 四种路径
- `TestDeepEngineContextInjection` (5): inject_context、adaptation 触发、无上下文不触发、replan_on_failure
- `TestProgressReporterContextMethods` (5): 新展示方法
- `TestIntentRecognizerDeepUpdate` (3): /deep_update 命令识别和路由
- `TestWsClientDeepUpdate` (1): _is_deep_command 识别 /deep_update

#### 测试结果: 617 全部通过 ✅

---

### 修复流式卡片更新失败 + Claude 会话闲置优化（2026-02-02 06:30:00）

修复流式卡片 `update_content()` 和 `close_streaming()` 更新失败的问题（`code=99992402, msg=field validation failed`），并优化 Claude 会话闲置后的恢复体验。

#### 问题根因

1. **卡片更新使用了错误的 API 端点**：代码使用 `UpdateMessageRequest`（对应 `PUT /open-apis/im/v1/messages/:message_id`），该端点用于更新**文本/富文本消息**，不支持卡片消息更新。飞书卡片消息更新应使用 `PatchMessageRequest`（对应 `PATCH /open-apis/im/v1/messages/:message_id`），该端点专门用于更新应用发送的消息卡片。

2. **Claude 会话闲置过久后恢复失败**：Claude CLI 内部的会话有独立的过期机制。当 GhostAP 尝试 `--resume` 一个已在 Claude CLI 中过期的会话时，会触发 "No conversation found with session ID" 错误。虽然现有的错误恢复机制有效（自动创建新会话），但失败的恢复尝试增加了不必要的延迟。

#### 修改 `src/card/streaming.py` — 卡片更新 API 修复

- **替换导入**：`UpdateMessageRequest` / `UpdateMessageRequestBody` → `PatchMessageRequest` / `PatchMessageRequestBody`
- **`update_content()`**：`client.im.v1.message.update(req)` → `client.im.v1.message.patch(req)`
- **`close_streaming()`**：`client.im.v1.message.update(req)` → `client.im.v1.message.patch(req)`

#### 修改 `src/claude/session.py` — Claude 会话闲置优化

- **新增** `_CLI_SESSION_MAX_IDLE = 1800`：Claude CLI 会话最大闲置时间阈值（30分钟）
- **新增** `_reset_stale_session()`：在 `send_prompt` / `send_prompt_streaming` 调用前检查会话是否闲置过久，若超过阈值则主动创建新会话，避免触发失败的 `--resume` 尝试
- **重写** `send_prompt()` / `send_prompt_streaming()`：在调用基类方法前执行闲置检查

#### 修改 `tests/test_streaming.py`

- 所有 `mock_client.im.v1.message.update` 替换为 `mock_client.im.v1.message.patch`

#### 测试结果: 582 全部通过 ✅

---

**更新时间**: 2026-02-01 23:00:00

### 项目大扫除 — 6 阶段重构（2026-02-01 23:00:00）

对项目进行全面重构，消除重复代码、统一架构、升级卡片 schema、清理配置命名和日志。全量 580 测试通过。

#### Phase 0: 基础清理
- **新增** `src/utils/text.py`: 提取 `clean_terminal_output()` 和 `truncate_output()` 共享函数
- **修改** `src/coco/session.py`, `src/claude/session.py`: 移除未使用 import（threading, Generator, re），委托给共享 utils
- **修改** `src/sandbox/executor.py`: 移除未使用 shlex import，委托截断逻辑给 `truncate_output()`

#### Phase 1: 统一 SessionSnapshot + 上下文清理
- **修改** `src/project/context.py`: 合并 `CocoSessionSnapshot` / `ClaudeSessionSnapshot` 为单一 `SessionSnapshot`（保留别名向后兼容）；移除 `THEME_COLORS` / `EMOJI_PREFIXES` 常量
- **修改** `src/project/manager.py`: 改从 `card/themes.py` 的 `THEMES` 字典派生主题颜色

#### Phase 2: 会话基类提取（核心重构）
- **新增** `src/session/base.py`: `BaseSession` 抽象基类，包含 `send_prompt()`, `send_prompt_streaming()`, `resume()`, `to_snapshot()`, `from_snapshot()` 等 80%+ 共享代码
- **新增** `src/session/manager.py`: `BaseSessionManager[T]` 泛型基类，包含 `start_session()`, `resume_session()`, `get_session()`, `end_session()` 等共享逻辑
- **修改** `src/coco/session.py`: CocoSession 精简为薄子类（~60 行 vs 原 ~300 行）
- **修改** `src/claude/session.py`: ClaudeSession 精简为薄子类，保留错误恢复钩子
- **修改** `src/deep_engine/executor.py`: `AISession = BaseSession`（替代 Union 类型）

#### Phase 3: 卡片统一升级 schema 2.0
- **新增** `src/card/shared.py`: 共享卡片元素构建器（`build_mode_buttons()`, `build_responsive_layout()`, `resolve_title_and_template()`, `apply_compact_style()`）
- **修改** `src/card/builder.py`: 升级到 schema 2.0（`_wrap_card()` 方法），合并重复方法（resume 卡片、response 卡片），委托给 shared.py
- **修改** `src/card/streaming.py`: 委托按钮/标题/布局给 shared.py 共享函数
- **修改** `tests/test_card.py`: 更新全部卡片结构断言从 v1 到 v2 格式

#### Phase 4: 配置命名优化
- **修改** `src/config.py`: 新增 `claude_execution_timeout`, `claude_session_timeout`, `claude_max_output_length` 独立配置项
- **修改** `src/claude/session.py`: `_get_execution_timeout()` / `_get_max_output_length()` 使用 Claude 专属配置；`ClaudeSessionManager` 使用 `claude_session_timeout`

#### Phase 5: 统一日志
- 将全部 `print()` 调用替换为 `logging`（logger.info/warning/error/debug）
- 涉及模块: `main.py`, `ws_client.py`, `streaming.py`, `executor.py`, `session.py`, `intent_recognizer.py`, `engine.py`, `planner.py`, `parser.py`, `manager.py`, `message_cache.py`
- 在 `main.py` 的 `run()` 中配置 `logging.basicConfig()`

#### 预期成果
| 指标 | 重构前 | 重构后 |
|------|--------|--------|
| session 代码行数 | ~650 行（两文件重复） | ~300 行（基类+两个薄子类） |
| 卡片按钮重复 | 2 套独立实现 | 1 套共享 + schema 2.0 统一 |
| 配置命名 | Claude 用 coco 的配置名 | 各有独立配置项 |
| 日志方式 | 91 处 print() | 统一 logging |
| SessionSnapshot 类 | 2 个一样的类 | 1 个 + 别名 |

---

### 编程模式命令拦截 + 即时反馈 + 卡片渲染优化（2026-02-02 03:00:00）

修复编程模式（Coco/Claude）中系统命令被错误发送给 AI 的问题，优化消息处理的即时反馈，改善卡片 Markdown 渲染。

#### 问题
1. **命令拦截缺失**: 在 Claude/Coco 模式中，`/帮助`、`/help`、`/projects`、`/status` 等系统命令未被拦截，直接发送给 AI 处理，导致无响应或错误响应
2. **无即时反馈**: 非流式路径中用户发送消息后看不到任何卡片，直到 AI 完整响应返回才展示
3. **卡片样式简陋**: 卡片内 Markdown 元素缺少 `text_size` 属性，渲染效果不佳

#### 修改 `src/feishu/ws_client.py`
- **新增** `_is_interceptable_command()`: 判断是否为需要在编程模式中拦截的系统命令（`/help`、`/帮助`、`/coco_info`、`/claude_info`、`/projects`、`/status`、`/switch`、`/new`、`/close`）
- **新增** `_handle_intercepted_command()`: 统一路由拦截的系统命令到对应处理方法
- **修改** `_process_with_intent()`: 在 Coco/Claude 模式路径中，退出命令和 Deep 命令之后、发送给 AI 之前，增加 `_is_interceptable_command()` 检查
- **重构** `_handle_claude_response()` 和 `_handle_coco_response()`: 无论流式/非流式，统一先创建流式卡片展示"正在思考"，再等待 AI 响应。消除非流式路径中"空白等待"问题
- **改善** `_show_full_help()`: 帮助卡片增加 `text_size` 属性，状态栏使用 `notation` 大小更紧凑

#### 修改 `src/card/streaming.py`
- **改善** `_build_card_json()`: 路径元素增加 `text_size: "notation"`（更紧凑），内容元素增加 `text_size: "normal"`（更可读）

#### 新增测试 `tests/test_claude.py` — 9 个新测试
- `TestCommandInterceptionInProgrammingMode`: 系统命令拦截测试类
  - `_is_interceptable_command()`: 5 个测试（help/info/project 命令 + 普通文本 + exit 命令区分）
  - `_handle_intercepted_command()`: 4 个测试（help/claude_info/projects/switch 路由）
- 更新 `tests/test_streaming.py`: `test_build_card_json_streaming_mode` 增加 `text_size` 断言

#### 全部 580 个测试通过 ✅

---

### 统一编程模式回复为 CardKit 流式卡片（2026-02-02 01:30:00）

将所有编程模式（Coco/Claude）的消息回复统一为 CardKit 流式卡片（schema 2.0），移除旧的 `CardBuilder` 静态卡片路径，消除两套并行的卡片渲染实现。

#### 背景
- **旧架构**: 非流式路径（`_handle_xxx_normal`）使用 `CardBuilder` 构建旧格式卡片；流式路径（`_handle_xxx_streaming`）使用 `StreamingCardManager` 的 CardKit API
- **问题**: 两套代码维护成本高，非流式卡片虽已升级到 schema 2.0 但仍与流式卡片走不同的构建路径

#### 修改 `src/card/streaming.py` — 重构 StreamingCardManager
- **提取共享方法** `_build_card_json()`: 统一流式/非流式卡片的 JSON 结构构建
  - `streaming_mode=True`: 包含 `streaming_config`（print frequency/step/strategy）
  - `streaming_mode=False`: 不含 streaming 配置，直接渲染完整内容
- **提取** `_resolve_title_and_template()`: 根据 mode（Coco/Claude/Smart）和项目名解析标题与头部颜色模板
- **新增** `create_and_send_card()`: 非流式 CardKit 卡片一次性发送方法
  - 创建 streaming_mode=false 的 CardKit 卡片 → 写入完整内容 → 发送消息 → 返回 message_id
  - 支持 reply（引用回复）和 create（直接发送）两种模式
- **重构** `create_streaming_card()`: 复用 `_build_card_json()` 和 `_resolve_title_and_template()`

#### 修改 `src/feishu/ws_client.py` — 合并消息处理路径
- **删除** 4 个旧方法: `_handle_coco_normal`、`_handle_coco_streaming`、`_handle_claude_normal`、`_handle_claude_streaming`
- **新增** 2 个统一方法:
  - `_handle_coco_response`: 合并 Coco 模式的流式/非流式处理
  - `_handle_claude_response`: 合并 Claude 模式的流式/非流式处理
- **统一逻辑**: 方法内部根据 `self._enable_streaming` 分支：
  - `True`: 创建流式卡片 → 实时 update_content → close_streaming（打字机效果）
  - `False`: 同步获取完整响应 → `create_and_send_card()` 一次性发送（CardKit schema 2.0）
- **Fallback**: 流式创建失败时，自动降级为非流式 `create_and_send_card()`，不再回退到旧 `CardBuilder`

#### 新增测试 `tests/test_streaming.py` — 14 个新测试
- `_resolve_title_and_template()`: 5 个测试（Coco/Claude/Smart + 有无项目名）
- `_build_card_json()`: 3 个测试（streaming/non-streaming/带图片）
- `create_and_send_card()`: 5 个测试（reply 模式、create 模式、卡片创建失败、消息发送失败、Claude 模板）
- `_build_button_elements()`: 1 个测试（Claude 模式按钮）

#### 保留的兼容性
- `CardBuilder` 类保留用于非编程模式场景（项目创建、状态看板、错误卡片、Deep Engine 进度、通知等）
- `streaming_enabled` 配置项保留，语义不变（true=打字机效果，false=一次性渲染）

#### 测试结果: 571 全部通过（14 新增）

---

### 高优先级代码质量修复（2026-02-02 00:30:00）

#### 1. 修复 `build_bridge_summary()` FILE_CHANGE 死代码
- **问题**: `FILE_CHANGE` 不在 `bridgeable_types` 中，但 `build_bridge_summary()` 有处理 `FILE_CHANGE` 的分支代码，导致 `files_modified` 列表永远为空
- **方案**: 将 `FILE_CHANGE` 加入 `bridgeable_types`，使文件变更信息可正确传递到桥接摘要。跨模式切换时知道哪些文件被修改过是有价值的上下文信息
- **文件**: `src/project/unified_context.py:484`

#### 2. 修复 `max_entries=0` 边界行为
- **问题**: `add_entry()` 中 `self._entries[-0:]` 等价于 `self._entries[:]`，当 `max_entries=0` 时实际不触发淘汰（依赖 Python 切片的偶然行为）
- **方案**: 增加 `if self.max_entries > 0` 的显式保护判断，`max_entries=0` 明确语义为"不限制条目数量"
- **文件**: `src/project/unified_context.py:263`

#### 3. 添加关键操作日志
- 使用 `logging` 模块为上下文管理和模式切换的关键路径添加日志
- **unified_context.py**: 条目淘汰(DEBUG)、版本创建(DEBUG)、桥接摘要构建(INFO)、桥接摘要消费(INFO)、上下文创建/移除(INFO)
- **ws_client.py**: 项目上下文保留(INFO)、恢复(INFO/DEBUG)、模式切换记录(INFO)、Bridge 注入(INFO)

#### 测试更新
- `test_bridge_summary_skips_file_changes` → 重命名为 `test_bridge_summary_collects_file_changes`，验证 FILE_CHANGE 被正确收集
- `test_max_entries_zero_keeps_all` → 重命名为 `test_max_entries_zero_means_unlimited`，反映明确的语义
- 全部 557 个测试通过 ✅

---

**更新时间**: 2026-02-01 23:30:00

### 卡片 Markdown 渲染效果测试用例（2026-02-01 23:30:00）
为修复后的卡片 Markdown 渲染编写了 56 个测试用例（从 34 → 90），覆盖三个维度。

#### 新增测试类和覆盖范围

**1. TestCardSchema20Structure（10 个测试）— Card JSON 2.0 结构验证**
- 验证所有 10 种卡片类型（coco/smart/project/status_board_empty/status_board/notification/coco_resume/project_created/error/deep）均使用：
  - `"schema": "2.0"` 声明
  - `"body": {"elements": [...]}` 而非顶层 `"elements"`
  - 无任何 `lark_md` 残留
  - 无 `div` + `lark_md` 组合

**2. TestMarkdownContentRendering（22 个测试）— 常见/复杂 Markdown 语法渲染**
- 常见语法（10 个）：标题(#/##/###)、无序列表、有序列表、粗体/斜体、行内代码、代码块、链接、引用、水平线、删除线
- 复杂内容（7 个）：
  - 模拟 AI 回复（标题+列表+代码块+引用+链接混合）
  - 嵌套列表+代码块
  - 多语言代码块（Python/JS/Bash）
  - 表格语法
  - 标题前置（with_title 参数）
- 各卡片类型 pipeline 验证（5 个）：Deep 卡片、通知卡片建议、状态看板项目信息、错误卡片、Coco/Smart 卡片的 Markdown 完整传递

**3. TestMarkdownEdgeCases（24 个测试）— 边界情况**
- 空内容/空白：空字符串、纯空白、有标题无内容
- 特殊字符：HTML 标签、JSON 内容、Unicode/emoji、反斜杠/转义、Markdown 特殊符号
- 超长内容：5000 字符长文本、200 行内容、100 行代码块
- 嵌套边界：未闭合代码块、嵌套 backticks、纯 Markdown 符号
- 目录元素：使用 markdown 标签、无项目默认 ~、working_dir、路径含空格
- 完整卡片边界：空内容卡片、纯换行卡片、JSON 序列化完整性（引号/转义/换行/制表符）、无建议的通知卡片、空看板提示、Deep 进度条

#### 测试结果: 557 全部通过（90 卡片 + 199 统一上下文 + 268 其他）

### 补充项目级上下文管理的综合测试用例（2026-02-01 22:30:00）
为统一上下文管理系统编写了 61 个新测试用例（从 138 → 199），覆盖 4 大维度的测试需求。

#### 新增测试类和覆盖范围

**1. TestCRUDAdvanced（17 个测试）— 补充 CRUD 高级场景**
- FILE_CHANGE / AI_SUMMARY 条目类型的创建与查询
- 同时更新 content 和 metadata、单独更新保留另一字段
- 组合条件查询（type + mode + since + limit 同时使用）
- 删除无匹配模式返回 0、清空后重新添加、删除中间条目保持顺序
- ProjectContextManager 的 entries+conversation 混合更新、type+limit 组合查询

**2. TestCrossModeContextSharing（12 个测试）— 跨模式上下文共享（此前完全空白）**
- 5 种模式（SMART/COCO/CLAUDE/SHELL/DEEP_ENGINE）条目共存验证
- 完整工作流：SMART→COCO→CLAUDE→SHELL→DEEP_ENGINE 全链路
- 桥接摘要携带多模式历史、桥接链跨多次模式切换
- 删除单一模式不影响其他模式、跨模式按时间排序
- 版本快照捕获多模式状态、FILE_CHANGE 不在 bridgeable_types 中的行为验证
- ProjectContextManager 端到端跨模式工作流+桥接

**3. TestProjectSwitchAdvanced（3 个测试）— 项目切换补充场景**
- 多项目快速切换 A→B→C→A 数据累积与版本链
- 切换时存在未完成模式切换的快照保存
- A↔B 反复切换版本累积正确性

**4. TestEdgeCases（29 个测试）— 边界情况**
- 空字符串、10 万字符长文本、Unicode/特殊字符、metadata 中换行符
- max_entries=1/0 边界行为、max_versions=1 淘汰验证
- 版本 entry_count stale 后的 diff 行为、clear 后 diff 返回空
- 空上下文/无可桥接条目的桥接摘要、内容截断（300 字符）、摘要行数上限（8 行）
- from_dict 缺少字段时默认值、所有 6 种条目类型的序列化/反序列化 roundtrip
- 并发版本创建、并发读写不崩溃
- 删除+添加后索引一致性、按模式清除后索引正确
- ProjectContextManager 传入 None project_id 的防御性校验（5 个接口）
- Store 交叉操作隔离性

#### 发现的实现行为说明
- `build_bridge_summary` 的 `bridgeable_types` 不包含 `FILE_CHANGE`，导致 `files_modified` 始终为空列表（代码中 `elif FILE_CHANGE` 分支为死代码）
- `max_entries=0` 时 `[-0:]` 等同于 `[:]`，不触发淘汰，实际保留全部条目

#### 测试结果: 501 全部通过（199 统一上下文 + 302 其他）

### 修复卡片 Markdown 未渲染问题（2026-02-01 21:00:00）
修复飞书卡片回复内容 Markdown 未正确渲染的问题。根因分析发现两个核心问题并全部修复。

#### 问题根因
1. **`_build_content_element()` 使用 `div` + `lark_md` 渲染非代码块内容**：`lark_md` 是飞书卡片 JSON 1.0 的文本级标签，仅支持有限的 Markdown 子集（粗体、斜体、链接），不支持代码块、表格、列表等完整 Markdown 语法。AI 回复中的列表、标题等格式在无代码块时全部退化为纯文本。
2. **非流式卡片使用 JSON 1.0 结构**：流式卡片 (`streaming.py`) 已使用 `schema: "2.0"` + `body.elements` 结构，支持完整 Markdown 渲染；非流式卡片 (`builder.py`) 无 schema 声明，默认 JSON 1.0 行为，Markdown 渲染能力受限。

#### 修改 `src/card/builder.py` — 统一升级到卡片 JSON 2.0
- **`_build_content_element()`**: 移除 `_has_code_block()` 分支判断，统一使用 `{"tag": "markdown"}` 元素替代 `div` + `lark_md`
- **`_build_directory_element()`**: 同样从 `div` + `lark_md` 改为 `markdown` 标签
- **所有 11 个卡片构建方法**: 添加 `"schema": "2.0"` 声明，将 `"elements"` 移入 `"body": {"elements": [...]}` 结构，与流式卡片保持一致
- **`build_status_board_card()`**: 修复空项目卡片和项目列表卡片中的 4 处 `lark_md` 用法
- **`build_notification_card()`**: 修复建议文本的 `lark_md` 用法
- 删除不再需要的 `_has_code_block()` 静态方法

#### 修改 `tests/test_card.py` — 适配新的卡片结构
- 所有 `card["elements"]` 引用更新为 `card["body"]["elements"]`
- 所有 `card.get("elements", [])` 引用更新为 `card.get("body", {}).get("elements", [])`

#### 测试结果: 440 全部通过

### 项目切换上下文保留与恢复（2026-02-01 19:30:00）
实现项目切换时的上下文完整保留与安全恢复，确保切换项目不会丢失任何 AI 会话上下文。

#### 修改 `src/feishu/ws_client.py` — 项目切换上下文安全
- 新增 `_preserve_project_context()`: 离开项目前保存当前模式的 AI 会话快照到统一上下文
- 新增 `_restore_project_context()`: 切换到目标项目时加载已有上下文，返回恢复状态信息
- 重构 `_switch_project()`:
  - 切换前先调用 `_preserve_project_context` 保存会话快照
  - 安全退出当前模式（Coco/Claude），释放 session 资源
  - 为旧项目创建离开版本书签
  - 为新项目自动创建/加载统一上下文
  - UI 反馈中包含上下文恢复信息（条目数、上次模式等）
- 新增 `_inject_bridge_context()`: 在消息处理时检查并消费桥接摘要，注入到 AI prompt 前面
- 在 `_handle_coco_message` 和 `_handle_claude_message` 中注入桥接上下文
- 在 `_handle_card_resume_coco` 和 `_handle_card_resume_claude` 中记录模式切换到统一上下文

#### 新增测试: 24 个测试覆盖项目切换上下文流程
- `TestProjectSwitchContextPreservation` (4 tests): 旧项目上下文保留、快照保存、版本创建、增量 diff
- `TestProjectSwitchContextRestoration` (4 tests): 新项目自动创建、已有上下文加载、恢复信息格式
- `TestProjectSwitchBridgeSummary` (6 tests): 桥接摘要构建、一次性消费、prompt 注入格式、跨切换保留
- `TestProjectSwitchEdgeCases` (6 tests): 同项目切换、无活跃项目、空上下文、快速多次切换、滚动窗口、版本链
- `TestProjectSwitchEndToEnd` (4 tests): 完整切换流程、无会话切换、Deep Engine 结果保留、桥接包含 Deep 结果

#### 测试结果: 440 passed

---

### 项目级统一上下文管理系统（2026-02-01 18:00:00）
实现项目级统一上下文管理系统，解决各编程模式（Coco/Claude/Shell/Deep Engine）上下文彼此隔离的问题。

#### 新增 `src/project/unified_context.py`
- **数据结构**: `ContextEntry`（统一上下文条目）、`ContextVersion`（版本书签）、`ContextBridgeSummary`（跨模式桥接摘要）
- **枚举**: `ContextEntryType`（6种条目类型）、`ContextSourceMode`（5种来源模式）
- **UnifiedContext**: 单项目上下文容器，滚动窗口(200条)、版本控制(50个)、O(1)条目查找、桥接摘要生成
- **UnifiedContextStore**: 内存存储管理器，按 project_id 隔离，线程安全
- **ContextResult**: 标准化响应格式
- **ProjectContextManager**: 5个标准CRUD操作接口（create/get/update/delete/exists）

#### 修改 `src/feishu/ws_client.py` — 集成统一上下文
- `__init__`: 新增 `self._context_manager = ProjectContextManager()`
- `_enter_coco_mode` / `_enter_claude_mode`: 记录模式切换 + 创建版本 + 构建桥接摘要
- `_exit_coco_mode` / `_exit_claude_mode`: 保存会话快照到统一上下文
- `_handle_coco_normal/streaming` / `_handle_claude_normal/streaming`: 对话写入统一上下文
- Shell 命令处理: 写入统一上下文
- `_switch_project`: 切换前为旧项目创建版本快照
- Deep Engine `on_project_done`: 写入结果 + 创建版本
- 新增辅助方法: `_mode_to_context_source()`、`_record_mode_transition()`

#### 修复 `src/project/context.py` — conversation_history 序列化
- `to_snapshot()`: 新增 `conversation_history` 字段序列化
- `from_snapshot()`: 新增 `conversation_history` 字段反序列化
- 修复服务重启后对话历史丢失的问题

#### 新增 `tests/test_unified_context.py` — 114 个测试
- 覆盖所有数据结构、CRUD、滚动窗口、版本控制、桥接摘要、序列化、线程安全等

#### 测试结果: 416 passed

---

### 任务调度器 + Deep Engine 多后端 + 卡片 UI 优化（2026-02-01 12:00:00）
引入全新的线程级任务调度器替换原有 ThreadPoolExecutor，Deep Engine 支持 Coco/Claude 双后端，卡片 UI 按引擎类型区分视觉样式。

#### 新增 `src/tasking/` 模块 — TaskScheduler
- **TaskScheduler**: 轻量线程级调度器，替换 `ThreadPoolExecutor`
  - per-chat 有序执行（`per_chat_concurrency` 默认 1，同一 chat 消息串行处理）
  - 全局并发限制（`max_concurrent` 默认 10）
  - 支持 `queue_key` 路由：长耗时任务（如 Deep Engine）使用独立队列，不阻塞同 chat 的控制指令
  - 任务优先级（HIGH / NORMAL / LOW），HIGH 任务插队到队列头部
  - `CancellationToken` 协作式取消 + `TaskCanceledError`
  - `TaskContext.progress()` 进度上报
  - `TaskHandle` 支持 cancel / wait / get_state
  - 事件监听器 `add_listener(callback)` 用于外部扩展
- **数据模型**: `TaskSpec`、`TaskResult`、`TaskEvent`、`TaskHandle`、`TaskRunState`
- **配置项**: `task_scheduler_max_concurrent`、`task_scheduler_per_key_concurrency`（`config.py`）

#### Deep Engine 支持 Claude 后端
- `DeepEngine` 新增 `engine_name` 参数，`_coco_session` 泛化为 `_ai_session`（`Union[CocoSession, ClaudeSession]`）
- `TaskExecutor` 的 `coco_session` 泛化为 `session: AISession`
- `DeepEngineManager` 维护 `_coco_session_manager` 和 `_claude_session_manager` 两个实例
  - `get_or_create()` 根据 `engine_name` 自动选择后端
  - 已有 engine 切换后端时自动清理重建（非运行中时）
- `ws_client.py` 新增 `_get_engine_name()` 根据当前交互模式返回 "Coco" 或 "Claude"
- Deep Engine 启动/恢复/状态查询全部传递正确的 engine_name

#### ws_client.py 调度迁移
- `_handle_message` 和 `_handle_card_action` 改用 `TaskScheduler.submit(TaskSpec, fn)` 调度
- Deep Engine 任务使用 `queue_key=f"{chat_id}:deep"`，避免阻塞同 chat 的普通消息处理
- Deep Engine 恢复任务使用 `TaskPriority.HIGH` 优先调度
- 新增 `close()` 方法：停止 MessageCache 清理线程 + DeepEngineManager 清理 + TaskScheduler 停止
- `main.py` 的 `Application.run()` 新增 `finally` 块调用 `feishu_client.close()` 做优雅退出

#### 卡片 UI 优化
- **Deep 卡片头部颜色按引擎区分**: Coco → blue、Claude → purple、默认 → turquoise（`_pick_engine_template()`）
- **流式卡片头部同步适配**: Coco=blue、Claude=purple
- **移除重复进度条**: `reporter.py` 不再在正文内嵌 progress_bar；`builder.py` 去重检测，正文已含进度条时不再额外渲染
- **按钮布局统一**: Deep 卡片按钮改用 `_build_buttons_responsive()` 响应式布局
- **流式卡片按钮布局**: 新增 `_build_button_elements()` 支持 desktop / mobile / responsive 三种策略，由 `card_button_layout` 配置控制
- **按钮样式**: Deep 卡片按钮统一应用 `_apply_compact_button_style`
- **流式卡片**: 新增 `wide_screen_mode: True` 配置

#### 改动文件
- 新增 `src/tasking/__init__.py` — 模块导出
- 新增 `src/tasking/scheduler.py` — TaskScheduler 实现（~450 行）
- 修改 `src/config.py` — 新增调度器配置项
- 修改 `src/main.py` — 优雅退出
- 修改 `src/feishu/ws_client.py` — 调度迁移 + close() + engine_name 传递
- 修改 `src/deep_engine/engine.py` — 多后端支持
- 修改 `src/deep_engine/executor.py` — AISession 泛化
- 修改 `src/deep_engine/reporter.py` — 移除内嵌进度条
- 修改 `src/card/builder.py` — 引擎颜色区分 + 按钮布局 + 进度条去重
- 修改 `src/card/streaming.py` — 头部颜色 + 按钮布局策略
- 新增 `tests/test_task_scheduler.py` — 调度器单元测试（90 行）
- 新增 `tests/test_task_scheduler_stability.py` — 调度器稳定性测试（195 行）
- 修改 `tests/test_card.py` — 新增进度条去重、布局、引擎颜色测试
- 修改 `tests/test_streaming.py` — 适配 get_settings mock、新增 Claude/mobile 布局测试
- 修改 `tests/test_ws_client_patch.py` — 适配 TaskScheduler mock + 新增配置项

#### 测试
- 总测试数 302 个（收集成功）

---

### Claude 编程模式全面修复（2026-01-29 21:00:00）
修复 Claude 编程模式无法使用的问题（报错 `Invalid session ID. Must be a valid UUID`），并全面适配卡片按钮、项目管理和会话快照。

#### 根因修复
- **Session ID 格式**: 从 `feishu_claude_{chat_id}_{timestamp}` 改为 `uuid.uuid4()` 生成的合法 UUID
- **命令构造冲突**: 第一条消息用 `--session-id`，后续消息用 `--resume`，不再同时传两个参数

#### 卡片按钮适配
- Claude 模式下显示「退出Claude」+「切换项目」按钮
- Smart 模式下显示「Coco模式」+「Claude模式」两个入口按钮
- 流式卡片同步适配，传 `is_claude_mode=True`
- 项目创建卡片新增「开始 Claude」按钮

#### 项目管理兼容
- `ProjectContext` 新增 `claude_mode`、`claude_session_snapshot` 字段
- 新增 `ClaudeSessionSnapshot` 数据类
- 新增 `set_claude_mode()`、`update_claude_snapshot()` 方法
- 快照序列化/反序列化包含 Claude 字段，兼容旧数据
- 退出 Claude 模式时保存会话快照，下次可恢复
- 切换项目时显示 Claude 恢复卡片

#### 卡片动作处理
- 新增 `enter_claude`、`exit_claude`、`resume_claude`、`new_claude` 四个卡片动作
- 进入 Claude 时自动退出 Coco（互斥保护，反之亦然）
- 回复编程消息自动识别 Claude/Coco 模式并自动进入
- 项目状态面板显示 Claude 模式信息

#### 改动文件
- 修改 `src/claude/session.py` — UUID 生成 + 命令构造修复
- 修改 `src/project/context.py` — 新增 Claude 字段和方法
- 修改 `src/project/__init__.py` — 导出 ClaudeSessionSnapshot
- 修改 `src/card/builder.py` — Claude 模式按钮、标题、恢复卡片
- 修改 `src/card/streaming.py` — 流式卡片 Claude 适配
- 修改 `src/feishu/ws_client.py` — 卡片动作、状态管理、快照保存
- 修改 `tests/test_claude.py` — 新增 15 个测试（UUID、快照、按钮）
- 修改 `tests/test_streaming.py` — 适配按钮文案变更

#### 测试
- 总测试数 214 个全部通过

---

### Claude 编程模式初始实现（2026-01-29 19:30:00）
新增 Claude 编程模式，与 Coco 模式平行，用户可以选择使用不同的 AI 进行编程。

#### 新增功能
- **Claude 会话管理器**: `src/claude/session.py` - 类似 CocoSession 的实现
- **模式扩展**: ModeManager 支持 SMART、COCO、CLAUDE 三种模式
- **意图识别扩展**: 新增 ENTER_CLAUDE、EXIT_CLAUDE、CLAUDE_MESSAGE、SHOW_HELP 意图
- **帮助命令**: `/help` 和 `/帮助` 显示完整使用说明

#### 命令说明
| 命令 | 说明 |
|------|------|
| `/coco` | 进入 Coco 编程模式（默认） |
| `/claude` | 进入 Claude 编程模式 |
| `/exit` | 退出当前编程模式 |
| `/coco_info` | 查看 Coco 会话信息 |
| `/claude_info` | 查看 Claude 会话信息 |
| `/help` 或 `/帮助` | 显示完整帮助信息 |

#### 改动文件
- 新增 `src/claude/__init__.py`
- 新增 `src/claude/session.py`
- 修改 `src/mode/manager.py` - 添加 CLAUDE 模式支持
- 修改 `src/agent/intent_recognizer.py` - 添加 Claude 相关意图
- 修改 `src/feishu/ws_client.py` - 添加 Claude 模式处理逻辑
- 新增 `tests/test_claude.py` - 24 个测试用例

#### 测试
- 新增 24 个 Claude 相关测试用例
- 总测试数 199 个全部通过

---

### Bug 修复（2026-01-29 17:00:00）
- **问题**: 在编程模式（Coco 模式）下发送 `/deep` 命令时，消息被直接转发给 Coco，导致报错 "slash command '/deep ...' not found"
- **原因**: `_process_with_intent` 方法在 Coco 模式下只检查退出命令，其他消息都直接发给 Coco
- **解决**: 添加 `_is_deep_command()` 和 `_handle_deep_command()` 方法，在 Coco 模式下优先拦截 Deep 相关命令
- **测试**: 新增 4 个测试用例，总测试数 175 个全部通过

### 已完成功能
1. ✅ 项目初始化 - pyproject.toml、目录结构
2. ✅ 配置管理模块 - 支持环境变量和.env文件
3. ✅ 沙箱命令执行器 - 危险命令检测、超时控制、输出截断
4. ✅ 飞书长连接客户端 - WebSocket方式接收消息，无需公网IP
5. ✅ AI Agent - 使用 ARK 方舟大模型进行意图识别
6. ✅ Coco 远程对话模式 - 通过飞书与 Coco 进行远程开发
7. ✅ ReAct 智能意图识别 - 推理式意图理解，支持任务拆解
8. ✅ 消息过期丢弃 - 超过30秒的旧消息自动丢弃
9. ✅ 表情回复 - 消息状态反馈（OK、GET、Typing、Done等）
10. ✅ 多项目并行开发架构 - 支持单对话框管理多个项目
11. ✅ 两种交互模式 - 智能模式、编程模式
12. ✅ 消息卡片优化 - 支持代码块渲染（markdown 组件）
13. ✅ 流式卡片输出 - 打字机效果的实时输出
14. ✅ 单元测试 - 171 个测试全部通过
15. ✅ **Deep Engine** - 复杂任务自动拆解与执行引擎（新增）

### Deep Engine 模块（2026-01-29 新增）

Deep Engine 是一个复杂任务编排引擎，能够将用户的复杂需求自动拆解为多个子任务，并依次调用 Coco 执行，实时反馈进度。

#### 架构设计
```
deep_engine/
├── __init__.py          # 模块导出
├── models.py            # 数据模型（DeepTask, DeepProject, ParsedRequirement 等）
├── parser.py            # RequirementParser - 需求解析器（使用 LLM）
├── planner.py           # TaskPlanner - 任务规划器（生成任务列表）
├── executor.py          # TaskExecutor - 任务执行器（调用 Coco）
├── engine.py            # DeepEngine - 顶层编排器
└── reporter.py          # ProgressReporter - 进度报告器
```

#### 核心组件
| 组件 | 职责 |
|------|------|
| **RequirementParser** | 使用 LLM 解析用户需求，提取 goals、constraints、tech_stack |
| **TaskPlanner** | 将需求拆解为可执行的任务列表，支持任务依赖 |
| **TaskExecutor** | 调用 Coco 执行单个任务，支持流式输出和失败重试 |
| **DeepEngine** | 顶层编排器，协调规划和执行流程 |
| **ProgressReporter** | 生成用户友好的进度消息 |

#### 使用方式
| 命令 | 说明 |
|------|------|
| `/deep <需求描述>` | 启动 Deep Engine 执行复杂任务 |
| `/deep_status` | 查看当前 Deep 任务进度 |
| `/stop_deep` | 停止正在执行的 Deep 任务 |

#### 执行流程
1. 用户发送 `/deep 帮我写一个爬虫...`
2. RequirementParser 解析需求，提取目标和约束
3. TaskPlanner 生成任务列表（如：创建项目、实现抓取、解析数据、保存文件）
4. DeepEngine 依次执行每个任务
5. 每个任务完成后，发送进度消息给用户
6. 全部完成后，发送汇总报告

#### 进度反馈示例
```
🧠 Deep Engine 启动
📝 正在分析需求...

✅ 任务规划完成
📂 项目: my_crawler
📊 共 5 个任务

🔄 执行任务 [1/5]
[█░░░░░░░░░] 0%
📌 创建项目结构

✅ 任务完成 [1/5]
[██░░░░░░░░] 20%
⏱️ 耗时: 3.5s

...

🎉 全部任务完成！
[██████████] 100% (5/5)
⏱️ 总耗时: 45.2s
```

### 三种交互模式

| 模式 | 图标 | 说明 | 进入方式 | 退出方式 |
|------|------|------|----------|----------|
| **智能模式** | 🧠 | 默认模式，根据意图自动选择 Shell 或编程 | 默认 / 退出编程模式后 | - |
| **Coco 编程模式** | 🤖 | 所有消息都发给 Coco，支持流式输出 | `/coco` / "进入编程模式" | `/exit` / "退出模式" |
| **Claude 编程模式** | 🔮 | 所有消息都发给 Claude，支持流式输出 | `/claude` / "进入claude模式" | `/exit` / "退出模式" |

#### 模式切换命令
| 命令 | 作用 |
|------|------|
| `/coco` | 进入 Coco 编程模式 |
| `/claude` | 进入 Claude 编程模式 |
| `/exit` 或 "退出模式" | 退出编程模式，回到智能模式 |
| `/help` 或 `/帮助` | 显示完整帮助信息 |

#### 表情回复规则
| 模式 | 首次回复 | 处理中 |
|------|----------|--------|
| 智能模式 | OK 👌 | Typing ⌨️ |
| 编程模式 | GET 🤙 | Typing ⌨️ |

#### 自动进入编程模式
当用户**回复机器人的编程消息**时：
1. 自动识别消息关联的项目
2. 自动切换到该项目
3. 如果该项目之前在编程模式，自动进入编程模式
4. 用户的消息直接作为编程指令处理

### 目录概念

| 概念 | 图标 | 说明 |
|------|------|------|
| **工作目录** | 📁 | 全局唯一的当前目录，跟随 `cd` 命令变化 |
| **项目目录** | 📂 | 项目代码所在目录，创建时绑定，不会改变 |

- **Coco 编程**使用项目目录 (`root_path`)
- **Shell 命令**使用工作目录 (全局 `_working_dirs`)

### 支持的功能

#### 🧠 智能模式（默认）
根据用户意图自动选择执行方式：
- Shell 命令 → 执行命令
- 编程需求 → 进入编程模式
- 目录切换 → 切换工作目录
- 项目管理 → 创建/切换/查看项目

#### 🤖 编程模式
与 Coco AI 进行远程开发对话：
- 说「进入编程模式」或 `/coco` - 进入编程模式
- 说「退出模式」或 `/exit` - 退出编程模式
- `/coco_info` - 查看会话信息
- 回复编程消息自动进入编程模式

#### 💻 Shell 模式
所有消息直接作为 Shell 命令执行：
- 说「进入shell模式」或 `/shell` - 进入 Shell 模式
- 说「退出模式」或 `/exit` - 退出 Shell 模式

#### 📁 目录切换
- 说「切换到xxx目录」- 智能切换工作目录
- 支持自然语言描述：「切换到用户目录下的workspace」

#### 📂 多项目并行开发
单对话框管理多个开发项目，**全部通过自然语言交互**：

**自然语言支持：**
- 「创建项目」→ 使用当前目录名作为项目名
- 「创建项目 myapp」→ 创建名为 myapp 的项目
- 「切换到 test 项目」→ 切换项目
- 「看看有哪些项目」→ 显示项目列表
- 「项目状态」→ 查看当前项目状态

**命令支持：**
- `/projects` - 查看所有项目状态面板
- `/new <名称> [目录]` - 创建新项目
- `/switch <名称>` - 切换当前项目
- `/close <名称>` - 关闭项目
- `/status` - 查看当前项目详情

**特性：**
- 引用消息自动关联对应项目
- 交互式卡片快捷操作
- Coco 响应显示项目目录和工作目录
- 全局激活项目，切换后工作目录自动跟随

####  表情回复
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
3. 命令执行超时控制
4. 输出长度限制
5. 消息过期丢弃（30秒）

### 连接方式
使用飞书SDK的**长连接模式（WebSocket）**：
- ✅ 无需公网IP或域名
- ✅ 无需内网穿透
- ✅ 本地只要能访问公网就能接收消息
- ✅ 自动加密传输

### 代码统计
| 类型 | 行数 |
|------|------|
| 源代码 | ~4,000 行 |
| 测试代码 | ~1,100 行 |
| **总计** | **~5,100 行** |

## 历史记录

### 2026-01-29 15:30:00（代码重构与优化）
- **修复 Bug**：移除 `reply` 方法中的重复发送代码（调试遗留）
- **代码拆分**：
  - 新增 `src/feishu/emoji.py` - 提取 `EmojiType` 和 `EmojiReaction` 类
  - 新增 `src/feishu/message_cache.py` - 独立的消息缓存管理器
- **消息缓存优化**：
  - 使用 `MessageCache` 类替代原有的 `OrderedDict` 实现
  - 支持后台定时清理线程（每 60 秒清理过期消息）
  - 快速清理限制每次最多清理 100 条，避免阻塞
  - 线程安全，支持并发访问
- **ws_client.py 精简**：从 1288 行减少到 1131 行
- **新增测试**：`tests/test_message_cache.py`（10 个测试）
- **测试**：130 个单元测试全部通过

### 2026-01-23 10:25:00（添加流式输出开关配置）
- **新增配置项**：`STREAMING_ENABLED`（默认 `true`）
- **改动文件**：
  - `config.py`：新增 `streaming_enabled: bool = True`
  - `.env.example`：新增 `STREAMING_ENABLED=true`
  - `ws_client.py`：从配置读取 `_enable_streaming`
- **使用方式**：在 `.env` 中设置 `STREAMING_ENABLED=false` 即可关闭流式输出
- **效果**：关闭后使用普通卡片回复，不使用流式打字机效果

### 2026-01-23 10:15:00（优化卡片按钮布局适配移动端）
- **改动**：将按钮布局从 `action` 改为 `column_set`
- **优化点**：
  - 使用 `column_set` 两列布局，按钮等宽排列
  - `flex_mode: stretch` 确保按钮填满宽度
  - 按钮前增加分隔线 `hr`，视觉更清晰
- **影响文件**：`streaming.py`、`builder.py`
- **测试**：28 个单元测试全部通过

### 2026-01-23 08:45:00（流式卡片引用回复）
- **新增**：流式卡片支持引用回复用户消息
- **改动**：
  - `StreamingCard` 新增 `reply_to_message_id` 字段
  - `create_streaming_card` 新增 `reply_to_message_id` 参数
  - `send_streaming_card` 支持 `reply` API（引用回复）和 `create` API（直接发送）
  - `_handle_coco_streaming` 传递 `message_id` 作为引用目标
- **效果**：流式卡片回复会显示在用户消息下方，形成引用关系
- **测试**：28 个单元测试全部通过

### 2026-01-23 08:25:00（统一卡片按钮格式与状态区分）
- **按钮格式统一**：将 `value` 改为 `behaviors` 格式，兼容 Card JSON 2.0
- **编程模式按钮**：🚪 退出Coco、🔄 切换项目
- **非编程模式按钮**：🤖 编程模式、📋 选择项目
- **改动文件**：`streaming.py`、`builder.py`
- **测试**：28 个单元测试全部通过

### 2026-01-23 08:10:00（修复流式卡片 Card JSON 2.0 兼容问题）
- **问题**：`code=200861, msg=cards of schema V2 no longer support this capability; ErrorValue: unsupported tag action`
- **根因**：Card JSON 2.0 的 `body.elements` 不支持 `action` 标签
- **修复**：将按钮从 `body.elements` 移到顶层 `actions` 字段
- **测试**：14 个单元测试全部通过

### 2026-01-23 07:53:32（重新实现流式卡片输出）
- **重写 StreamingCardManager**
  - 增加 `project_id` 字段支持按钮回调关联项目
  - 优化打字机效果配置：`print_frequency_ms=30`, `print_step=3`, `print_strategy=fast`
  - 增加详细日志：创建/发送/更新/关闭各阶段
  - 新增 `cleanup_expired_cards` 方法清理过期卡片
  - 按钮 value 统一使用 `behaviors` 对象结构
- **优化 ws_client 流式处理**
  - 传递 `project_id` 到流式卡片
  - 增加更新计数与最终长度日志
  - 关闭流式时传递 `final_content` 确保最终内容完整
  - 缩短 `chunk_interval` 到 0.3 秒
- **新增单元测试**：`tests/test_streaming.py`（14 个测试全部通过）

### 2026-01-22 22:23:12（卡片回调日志加强 + value 结构调整 + SDK 升级）
- **日志增强**
  - 卡片回调记录 value 预览、解析失败提示与处理耗时
- **卡片按钮 value 改为对象**
  - 统一按钮回传 value 为 dict，避免 SDK 字符串解析歧义
- **依赖升级**
  - lark-oapi 升级至 >=1.5.2
- **测试**：tests/test_card.py 通过

### 2026-01-22 22:10:14（移除卡片回调 Patch + 增强日志）
- **移除 WS client patch**
  - 不再 monkey patch SDK 的 CARD 消息处理逻辑
- **增强卡片回调日志**
  - 记录 event_id、open_message_id、action 元信息与 value 类型
  - 记录 operator 与 value 解析后的 key 集合
- **测试**：tests/test_ws_client_patch.py 通过

### 2026-01-22 22:00:23（卡片回调 200671 复发修复）
- **修复卡片回调空响应再次触发 200671**
  - 避免对空对象/空 JSON（"{}"、"null"）写入 resp.data，保持标准空响应
  - 保留非空响应的 Base64 data 序列化
  - 测试更新：新增非空响应 data 设置用例，调整空响应断言
- **测试**：tests/test_ws_client_patch.py 通过

### 2026-01-22（代码清理）
- **移除未使用的模块和依赖**
  - 删除 `src/notification/` 模块（未被使用）
  - 删除 `src/tools/` 模块（未被使用）
  - 删除 `src/agent/shell_agent.py`（未被使用）
  - 删除 `docs/TOOL_CHAIN_REPORT.md`（过时文档）
  - 删除 `test_marshal.py`（临时测试脚本）
  - 删除 `tests/test_tools.py`、`tests/test_notification.py`（对应模块已删除）
- **清理依赖**
  - 移除 `fastapi`、`uvicorn`、`httpx`、`pycryptodome`（未使用）
- **清理配置**
  - 移除 `verification_token`、`encrypt_key` 配置项（HTTP 模式遗留）
  - 移除 `reload_settings()` 函数（未使用）
  - 更新 `.env.example` 移除废弃配置
- **清理代码**
  - 移除 `message_formatter.py` 中未使用的方法
  - 移除 `mode/manager.py` 中未使用的方法
  - 移除 `streaming.py` 中未使用的方法
  - 移除 `sandbox/executor.py` 中未使用的方法
- **测试**：107 个测试全部通过

### 2026-01-22（卡片回调 200671 修复）
- **修复卡片按钮点击报错 200671**
  - 根本原因：`_handle_card_action` 返回空的 `P2CardActionTriggerResponse` 对象，经 Monkey Patch 序列化为 `data: "e30="` (Base64 of `{}`)，飞书服务端认为格式无效。
  - 修复方案：修改 `_handle_card_action` 返回 `None`，Patch 逻辑检测到 `None` 时不设置 `data` 字段，返回标准空响应。
  - 验证：新增单元测试 `tests/test_ws_client_patch.py` 验证 `None` 返回值及 Patch 序列化行为。

### 2026-01-22（卡片回调修复 + 流式卡片）
- **修复卡片按钮点击报错 200340 和 200671**
  - 200340 根本原因：飞书开放平台未订阅 `card.action.trigger` 回调
  - 200671 根本原因：SDK bug - `MessageType.CARD` 类型消息未被处理
  - 代码修复：
    1. `_handle_card_action` 返回 `P2CardActionTriggerResponse`
    2. 添加 `_patch_ws_client_for_card_callback` monkey patch 修复 SDK bug
  - SDK bug 详情：`lark_oapi/ws/client.py` 第 264-265 行对 `MessageType.CARD` 直接 return，未调用 `do_without_validation`
- 实现飞书流式卡片输出（打字机效果）
- 新增 src/card/streaming.py - StreamingCardManager
- 修复卡片 JSON 2.0 结构（schema: "2.0"）
- 修复 CardKit API 参数（type: "card_json"）
- 简化模式系统为两种：智能模式 + 编程模式
- 移除 Shell 模式（Shell 命令在智能模式下直接执行）
- 优化表情回复：智能模式 OK、编程模式 GET
- 修复 Coco 输出截断问题（30000 字符限制）
- 修复 Coco 模式退出命令识别（/exit、/end_coco）

### 2026-01-18（三种模式重构）
- 新增 src/mode/ 模块（manager.py）- 模式管理器
- 实现三种交互模式：智能模式、编程模式、Shell模式
- 新增 ENTER_SHELL、EXIT_SHELL、EXIT_MODE 意图类型
- 支持回复编程消息自动进入编程模式
- 优化消息卡片支持代码块渲染（markdown 组件）
- 重构工作目录为全局唯一，项目目录固定不变
- 优化创建项目逻辑：无名称时使用目录名
- 测试总数：175 个全部通过

### 2026-01-18（意图识别整合）
- 将项目管理整合到 ReAct 意图识别系统
- 扩展 IntentType 新增 5 种项目管理意图
- 支持自然语言创建/切换/关闭项目
- 支持「在当前目录创建项目然后开始编程」多步骤任务
- Coco 响应添加项目名和工作目录信息
- 移除硬编码的项目命令处理，统一走意图识别

### 2026-01-18（多项目架构）
- 新增多项目并行开发架构
- 新增 src/project/ 模块（context.py、manager.py、mapper.py）
- 新增 src/card/ 模块（builder.py、themes.py）- 飞书交互式卡片
- 新增 src/notification/ 模块（hub.py）- 异步通知系统
- 增强 src/coco/session.py 支持会话恢复（--resume）
- 增强 src/feishu/ws_client.py 集成多项目管理
- 新增 47 个多项目相关测试用例
- 测试总数：123 个全部通过

### 2026-01-18（工具链）
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
