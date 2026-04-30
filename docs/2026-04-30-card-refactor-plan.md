# 卡片系统重构 — 详细实现计划

> 基于 `docs/2026-04-30-card-refactor-design.md` spec 文档
> 日期: 2026-04-30
> 策略: 激进式重写，三层解耦（State → Render → Delivery）

---

## 执行原则

1. **TDD 驱动**: 每个 Task 先写失败测试 → 验证失败 → 写实现 → 验证通过
2. **逐步验证**: 每完成一个 Task 立即 `uv run pytest -x -q` 验证
3. **向前兼容**: Phase 1-4 是新模块，不修改旧代码；Phase 5-7 做适配桥接；Phase 8 移除旧代码
4. **独立可测**: 每个 Phase 的测试独立运行，不依赖飞书 API

---

## Phase 1: State 模型和 Reducer

**目标**: 创建不可变状态模型 + 纯函数 reducer，事件驱动状态归约。

### Task 1.1: CardEvent 类型定义

**文件**: `src/card/events.py`

**内容**:
- `CardEventType` 枚举（21 种事件类型）
- `CardEvent` frozen dataclass（type + payload）
- `CardEvent.from_acp()` 类方法：ACPEvent → CardEvent 适配

**依赖**: `src/acp/models.py` (ACPEvent, ACPEventType, ToolCallInfo, PlanInfo)

**测试文件**: `tests/test_card_events.py`
- test_card_event_creation: 验证所有事件类型可创建
- test_from_acp_text_chunk: ACPEvent(TEXT_CHUNK) → CardEvent(TEXT_DELTA)
- test_from_acp_tool_start: ACPEvent(TOOL_CALL_START) → CardEvent(TOOL_STARTED)
- test_from_acp_tool_done: ACPEvent(TOOL_CALL_DONE) → CardEvent(TOOL_DONE)
- test_from_acp_plan_update: ACPEvent(PLAN_UPDATE) → CardEvent(PLAN_UPDATED)
- test_from_acp_thought_chunk: ACPEvent(THOUGHT_CHUNK) → CardEvent(REASONING_DELTA)
- test_card_event_is_frozen: 验证不可变性

**验收**: `uv run pytest tests/test_card_events.py -v` 全部通过

---

### Task 1.2: CardState 模型定义

**文件**: `src/card/state/__init__.py`, `src/card/state/models.py`

**内容**:
- `CardState` frozen dataclass（blocks, terminal, header, footer, buttons, metadata, version）
- `ContentBlock` frozen dataclass（kind, block_id, content, element_id, status, tool_name, tool_summary, tool_input, tool_output, char_count）
- `HeaderState` frozen dataclass（title, subtitle, template）
- `FooterState` frozen dataclass（status, status_text, progress）
- `CardMetadata` frozen dataclass（project_name, mode_name, mode_emoji, tool_name, model_name, engine_type）
- `ButtonSpec` frozen dataclass（text, action_id, type, confirm）
- `TerminalStatus` 类型别名 (Literal[...])
- `BlockStatus` 类型别名 (Literal["active", "completed", "failed"])

**依赖**: 无外部依赖，纯标准库

**测试文件**: `tests/test_card_state_models.py`
- test_card_state_frozen: 验证不可变
- test_content_block_kinds: 验证 text/tool_call/reasoning/plan 四种 kind
- test_header_state_defaults: 验证默认值
- test_metadata_all_fields: 验证元信息完整性
- test_card_state_version_increment: 验证 version 字段语义
- test_replace_helper: 验证 `dataclasses.replace()` 产出新实例

**验收**: `uv run pytest tests/test_card_state_models.py -v` 全部通过

---

### Task 1.3: 子 Reducer 实现

**文件**:
- `src/card/state/reducers/__init__.py`
- `src/card/state/reducers/text.py` — TEXT_STARTED/TEXT_DELTA/TEXT_DONE
- `src/card/state/reducers/tool.py` — TOOL_STARTED/TOOL_DELTA/TOOL_DONE/TOOL_FAILED
- `src/card/state/reducers/plan.py` — PLAN_UPDATED
- `src/card/state/reducers/reasoning.py` — REASONING_STARTED/REASONING_DELTA/REASONING_DONE
- `src/card/state/reducers/lifecycle.py` — STARTED/COMPLETED/FAILED/CANCELLED/PAUSED/RESUMED

**内容**:
每个子 reducer 是纯函数 `(state: CardState, event: CardEvent) -> CardState`：
- `text.py`: 追加文本块、增量更新 content、标记完成
- `tool.py`: 创建工具块、更新输出、标记完成/失败
- `reasoning.py`: 创建推理块、追加内容、计算 char_count
- `plan.py`: 替换 plan 块内容
- `lifecycle.py`: 设置 terminal 状态、更新 header template 色、设置按钮组

**依赖**: `src/card/state/models.py`, `src/card/events.py`

**测试文件**: `tests/test_card_reducers.py`
- test_text_started_creates_block: 验证新 text block 创建
- test_text_delta_appends_content: 验证增量追加
- test_text_done_marks_completed: 验证状态切换
- test_tool_started_creates_block: 验证工具块创建
- test_tool_done_sets_summary: 验证工具摘要
- test_tool_failed_sets_status: 验证失败状态
- test_reasoning_accumulates_chars: 验证字数计算
- test_plan_updated_replaces_content: 验证计划替换
- test_lifecycle_completed_sets_terminal: 验证终态
- test_lifecycle_completed_sets_green_header: 验证 header 颜色
- test_lifecycle_failed_sets_red_header: 验证失败颜色
- test_lifecycle_started_initializes_state: 验证初始化

**验收**: `uv run pytest tests/test_card_reducers.py -v` 全部通过

---

### Task 1.4: 主 Reducer 编排

**文件**: `src/card/state/reducer.py`

**内容**:
- `reduce_card_state(state: CardState | None, event: CardEvent) -> CardState`
- 匹配 event.type 分派到子 reducer
- 每次 reduce 自动 version +1
- state=None 时由 STARTED 事件初始化

**依赖**: `src/card/state/models.py`, `src/card/events.py`, `src/card/state/reducers/*`

**测试文件**: `tests/test_card_reducer_main.py`
- test_reduce_none_state_with_started: 验证初始化
- test_reduce_full_sequence: STARTED → TEXT_STARTED → TEXT_DELTA → TOOL_STARTED → TOOL_DONE → COMPLETED 完整序列
- test_version_increments: 验证每次 reduce version +1
- test_unknown_event_returns_same_state: 未知事件不改变状态
- test_idempotent_terminal: 终态后再收到事件不变
- test_multiple_text_blocks: 多个 text block 并存
- test_tool_model_changed_updates_metadata: 验证元信息更新

**验收**: `uv run pytest tests/test_card_reducer_main.py -v` 全部通过

---

## Phase 2: 纯渲染函数

**目标**: 将 CardState 转换为飞书 Schema 2.0 JSON，纯函数无副作用。

### Task 2.1: RenderBudget + Atom 抽象

**文件**:
- `src/card/render/__init__.py`
- `src/card/render/budget.py`
- `src/card/render/atoms.py`

**内容**:
- `RenderBudget` frozen dataclass（byte_budget=27*1024, node_budget=180, visible_chars=25000, tool_history_fold_threshold=3, reasoning_tail_chars=500）
- `RenderAtom` dataclass（kind, elements, byte_size, node_count, splittable, block_id）
- `flatten_to_atoms(blocks: tuple[ContentBlock, ...], budget: RenderBudget) -> list[RenderAtom]`: ContentBlock 序列 → RenderAtom 序列（含工具历史折叠逻辑）
- `estimate_atom_size(atom: RenderAtom) -> int`: 估算 JSON 字节大小

**依赖**: `src/card/state/models.py`

**测试文件**: `tests/test_card_render_atoms.py`
- test_text_block_to_atom: text ContentBlock → markdown atom
- test_tool_block_to_atom: tool ContentBlock → collapsible_panel atom
- test_reasoning_block_to_atom: reasoning → collapsible atom
- test_tool_history_fold: ≥3 完成工具 → 蓝色折叠面板
- test_tool_history_no_fold: ≤2 完成工具 → 独立展示
- test_active_tool_never_folded: 活跃工具不被折叠
- test_estimate_atom_size: 验证大小估算合理

**验收**: `uv run pytest tests/test_card_render_atoms.py -v` 全部通过

---

### Task 2.2: 分页算法

**文件**: `src/card/render/pagination.py`

**内容**:
- `paginate_atoms(atoms: list[RenderAtom], budget: RenderBudget) -> list[list[RenderAtom]]`
- `split_atom(atom: RenderAtom, remaining_bytes: int) -> list[RenderAtom] | None`
- 贪心策略：尝试放入 → 尝试切分 → 开新页
- 保证所有内容保留，不丢弃

**依赖**: `src/card/render/atoms.py`, `src/card/render/budget.py`

**测试文件**: `tests/test_card_pagination.py`
- test_single_page_within_budget: 内容在预算内 → 1 页
- test_multi_page_split: 超出预算 → 多页
- test_atom_split_by_paragraph: 按段落切分
- test_atom_split_by_line: 按行切分
- test_atom_split_by_chars: 按 1600 字符切分
- test_no_content_lost: 验证所有字符保留（字数总和相等）
- test_empty_atoms: 空列表 → [[]]

**验收**: `uv run pytest tests/test_card_pagination.py -v` 全部通过

---

### Task 2.3: Header / Footer / Buttons 渲染

**文件**:
- `src/card/render/header.py`
- `src/card/render/footer.py`
- `src/card/render/buttons.py`

**内容**:
- `render_header(state: CardState) -> dict`: 生成 Schema 2.0 header JSON（title + subtitle + template）
- `render_footer(state: CardState) -> list[dict]`: 生成 footer 元素（分隔线 + 状态文本）
- `render_buttons(state: CardState) -> list[dict]`: 生成按钮组元素（column_set 或 action 布局）

**依赖**: `src/card/state/models.py`

**测试文件**: `tests/test_card_render_components.py`
- test_header_with_project: 有项目名 → "🧠 ProjectName · Deep Agent"
- test_header_without_project: 无项目名 → "🤖 Coco 编程模式"
- test_header_subtitle: 验证 subtitle 格式 "🔧 coco · gpt-4o"
- test_header_subtitle_with_status: "🔧 coco · gpt-4o · 正在执行"
- test_header_template_running: running 时使用模式色
- test_header_template_terminal: 终态时使用终态色
- test_footer_thinking: status=thinking → "💭 正在思考..."
- test_footer_tool_running: status=tool_running → "🔧 执行中: {tool}"
- test_footer_none: status=None → 无 footer 元素
- test_buttons_layout_2: ≤2 按钮水平
- test_buttons_layout_flow: >4 按钮 flow

**验收**: `uv run pytest tests/test_card_render_components.py -v` 全部通过

---

### Task 2.4: 工具 / 推理 / 计划面板渲染

**文件**:
- `src/card/render/tools.py`
- `src/card/render/reasoning.py`
- `src/card/render/plan.py`

**内容**:
- `render_tool_panel(block: ContentBlock) -> dict`: 单工具 → collapsible_panel JSON
- `render_tool_history_panel(blocks: list[ContentBlock]) -> dict`: 多工具 → 蓝色折叠面板
- `render_reasoning_panel(block: ContentBlock) -> dict`: 推理 → collapsible_panel
- `render_plan_panel(block: ContentBlock) -> dict`: 计划 → 蓝色 collapsible_panel
- `generate_tool_summary(block: ContentBlock) -> str`: 工具摘要生成

**依赖**: `src/card/state/models.py`

**测试文件**: `tests/test_card_render_panels.py`
- test_tool_panel_running: ⏳ 图标 + grey 边框
- test_tool_panel_completed: ✓ 图标 + grey 边框 + expanded=false
- test_tool_panel_failed: ✗ 图标 + red 边框
- test_tool_summary_bash: bash → 命令文本截断
- test_tool_summary_read: read → 文件路径
- test_tool_summary_generic: 通用 → path/name/query 提取
- test_tool_history_panel_structure: 蓝色外层 + 内嵌子面板
- test_reasoning_active: expanded=true + "深度思考中..."
- test_reasoning_done: expanded=false + 字数 + 尾部 500 字符
- test_plan_panel: 步骤图标 (✅/⏳/○/✗)

**验收**: `uv run pytest tests/test_card_render_panels.py -v` 全部通过

---

### Task 2.5: 主渲染入口 + Structure Signature

**文件**: `src/card/render/renderer.py`

**内容**:
- `RenderedCard` dataclass（card_json, structure_signature, active_element, page_index, total_pages）
- `ActiveElement` dataclass（element_id, text）
- `render_card(state: CardState, budget: RenderBudget) -> list[RenderedCard]`
- `compute_structure_signature(state: CardState) -> str`
- 组装完整 Schema 2.0 JSON: config + header + body(elements) + streaming_mode

**依赖**: `src/card/render/*`, `src/card/state/models.py`

**测试文件**: `tests/test_card_renderer.py`
- test_render_minimal_state: 最小状态 → 有效 JSON
- test_render_schema_structure: 验证 config/header/body 三段结构
- test_streaming_mode_when_active: 活跃文本时 streaming_mode=true
- test_no_streaming_mode_when_terminal: 终态无 streaming
- test_signature_stable_for_text_delta: 文本增量不改变签名
- test_signature_changes_for_structure: 新 block 改变签名
- test_multi_page_render: 超大内容 → 多页 RenderedCard
- test_active_element_tracking: 活跃元素正确标识

**验收**: `uv run pytest tests/test_card_renderer.py -v` 全部通过

---

## Phase 3: 统一投递引擎

**目标**: 合并 StreamingCardManager + SmartSender 为统一的 CardDelivery。

### Task 3.1: Sequence 管理 + Binding

**文件**:
- `src/card/delivery/__init__.py`
- `src/card/delivery/sequence.py`
- `src/card/delivery/binding.py`

**内容**:
- `SequenceManager`: 管理 card_id → sequence 映射，支持 floor 提升
- `DeliveryBinding` dataclass: session_id → pages 映射（page_index → PageBinding）
- `PageBinding` dataclass: message_id, card_id, signature, last_text
- `BindingStore`: 管理所有 session 的 binding

**依赖**: 无外部依赖

**测试文件**: `tests/test_card_delivery_sequence.py`
- test_sequence_increments: 验证递增
- test_sequence_floor_raise: floor 提升后跳跃
- test_binding_create: 首次绑定
- test_binding_update_signature: 更新签名
- test_binding_page_management: 多页管理

**验收**: `uv run pytest tests/test_card_delivery_sequence.py -v` 全部通过

---

### Task 3.2: 节流调度

**文件**: `src/card/delivery/throttle.py`

**内容**:
- `DeliveryThrottle`: 节流调度器
  - `schedule(session_id, rendered, immediate)`: 调度投递
  - 终态 → 立即 flush
  - 结构变化 → 200ms 节流
  - 纯文本流 → 复用 FlowControlStrategy (EMA)
- 内部使用 `threading.Timer` 或 `sched` 实现延迟 flush
- `flush_now(session_id)`: 立即 flush 所有 pending

**依赖**: `src/card/flow_control.py` (FlowControlStrategy)

**测试文件**: `tests/test_card_delivery_throttle.py`
- test_immediate_flush: immediate=True → 立即调用
- test_throttle_200ms: 200ms 内多次 schedule → 只 flush 最新
- test_terminal_event_immediate: 终态立即
- test_pending_cancelled_on_new: 新调度取消旧 pending

**验收**: `uv run pytest tests/test_card_delivery_throttle.py -v` 全部通过

---

### Task 3.3: CardDelivery 投递引擎

**文件**: `src/card/delivery/engine.py`

**内容**:
- `CardDelivery` 类: 统一投递引擎
  - `deliver(session_id, chat_id, rendered, immediate)`: 核心投递方法
  - `close(session_id)`: 终态化
  - 决策逻辑: 无 binding → create; signature 变 → update; 仅 text → element_content
  - 序列号冲突 (300317) → reconcile
  - 5xx/timeout → delay 1s → reconcile
- `MutationOutcome` dataclass: applied / reconcile / skipped

**依赖**:
- `src/card/delivery/sequence.py`
- `src/card/delivery/binding.py`
- `src/card/delivery/throttle.py`
- `src/card/render/renderer.py` (RenderedCard)
- 飞书 API client (通过接口注入)

**测试文件**: `tests/test_card_delivery_engine.py`
- test_first_deliver_creates_card: 首次 → create API
- test_signature_change_triggers_update: 结构变 → update API
- test_text_only_triggers_element_content: 纯文本 → element_content API
- test_no_change_skips: 无变化 → 不调用
- test_sequence_conflict_reconcile: 300317 → reconcile
- test_multi_page_delivery: 多页正确投递
- test_close_flushes_pending: close 时 flush
- test_close_disables_streaming: close 后 streaming_mode=false

**验收**: `uv run pytest tests/test_card_delivery_engine.py -v` 全部通过

---

## Phase 4: CardSession 编排

**目标**: 实现 Handler 的唯一交互点，编排 dispatch → reduce → render → deliver。

### Task 4.1: CardSession 实现

**文件**: `src/card/session.py`

**内容**:
- `CardSession` 类:
  - `__init__(chat_id, metadata, delivery, budget)`: 初始化
  - `dispatch(event: CardEvent)`: 核心方法 — reduce → render → schedule deliver
  - `close()`: 终态化
  - `state` property: 只读访问当前状态
  - 线程安全: 内部 Lock
- `CardSessionFactory`: 工厂（注入 delivery 实例）

**依赖**:
- `src/card/events.py`
- `src/card/state/reducer.py`
- `src/card/render/renderer.py`
- `src/card/delivery/engine.py`

**测试文件**: `tests/test_card_session.py`
- test_dispatch_started_creates_card: STARTED 事件 → delivery.create 被调用
- test_dispatch_text_delta_streams: TEXT_DELTA → delivery.element_content
- test_dispatch_tool_started_updates: TOOL_STARTED → delivery.update
- test_dispatch_completed_closes: COMPLETED → delivery.close
- test_full_lifecycle: 完整生命周期事件序列
- test_thread_safety: 多线程 dispatch 不 crash
- test_close_idempotent: 多次 close 安全

**验收**: `uv run pytest tests/test_card_session.py -v` 全部通过

---

### Task 4.2: 端到端集成测试

**文件**: `tests/test_card_e2e.py`

**内容**:
模拟完整的 Handler → CardSession → 飞书 API 流程:
- test_deep_engine_flow: started → text → tool → text → completed
- test_loop_engine_flow: started → iteration(text+tool) × N → completed
- test_multi_page_flow: 大量内容触发分页
- test_tool_history_fold_in_render: 多工具折叠验证
- test_header_subtitle_updates: 模型切换 → subtitle 更新

**验收**: `uv run pytest tests/test_card_e2e.py -v` 全部通过

---

## Phase 5: 拆分 styles.py

**目标**: 将 822 行的 God Object 拆分为 5 个职责单一的模块。

### Task 5.1: 创建拆分后的模块

**文件**:
- `src/card/themes.py` — ProjectTheme + THEMES + DARK_THEME_NAMES + ENGINE_STYLES + PANEL_STYLES
- `src/card/ui_text.py` — UI_TEXT 字典 + 按域组织 + 合并逻辑
- `src/card/thresholds.py` — THRESHOLDS + TRUNCATION_LIMITS + RenderBudget (兼容导出)
- `src/card/buttons_config.py` — BUTTON_CONFIG + ButtonSpec
- `src/card/terminal.py` — TERMINAL_MARKERS + FOOTER_STATUS + STATUS_DISPLAY_MAP

**步骤**:
1. 创建 5 个新文件，从 styles.py 搬迁对应内容
2. 修改 `styles.py` 为薄 re-export 层（兼容期）:
   ```python
   # styles.py — 兼容层, 逐步迁移后删除
   from .themes import *
   from .ui_text import UI_TEXT
   from .thresholds import *
   from .buttons_config import *
   from .terminal import *
   ```
3. 验证所有现有测试通过（import 路径不变）

**测试文件**: `tests/test_card_styles_split.py`
- test_themes_accessible: 验证从新路径导入正确
- test_ui_text_complete: 验证 UI_TEXT 条目数不变
- test_thresholds_values: 验证阈值不变
- test_buttons_config: 验证按钮配置完整
- test_terminal_markers: 验证终态标记完整
- test_backward_compat: 验证从 styles.py 导入仍然工作

**验收**: `uv run pytest tests/ -v -k "card"` 所有现有卡片测试 + 新测试通过

---

## Phase 6: Handler 适配 (Deep/Loop/Spec)

**目标**: 将引擎 Handler 从直接操作 CardBuilder 改为通过 CardSession 派发事件。

### Task 6.1: Deep Handler 适配

**修改文件**:
- `src/feishu/handlers/deep.py`
- `src/feishu/renderers/deep.py` → `src/feishu/renderers/deep_renderer.py`

**改造内容**:
- `DeepHandler.start_deep_engine()`:
  - 创建 `CardSession(chat_id, metadata, delivery)`
  - `session.dispatch(CardEvent.started())`
  - 引擎回调改为 `session.dispatch(CardEvent.from_acp(event))`
  - 完成时 `session.close()`
- `DeepRenderer.create_deep_callbacks()`:
  - 回调内部改为 `session.dispatch()` 而非 `CardBuilder.build_engine_card()`
  - 保留 `SmartSender` 的 throttle 语义（由 CardDelivery 接管）

**测试文件**: `tests/test_deep_handler_card.py`
- test_deep_start_dispatches_started: 验证 STARTED 事件
- test_deep_acp_event_dispatches: 验证 ACP 事件转发
- test_deep_complete_dispatches_completed: 验证完成事件
- test_deep_error_dispatches_failed: 验证失败事件
- test_deep_metadata_correct: 验证 tool_name/model_name

**验收**: `uv run pytest tests/test_deep_handler_card.py -v` 全部通过

---

### Task 6.2: Loop Handler 适配

**修改文件**:
- `src/feishu/handlers/loop.py`
- `src/feishu/renderers/loop.py` → `src/feishu/renderers/loop_renderer.py`

**改造内容**:
- Loop 的多段特性 → Segment 续接:
  - 每个 iteration 开始分配新 segment
  - iteration 完成时 finalize 当前 segment
- 视图状态机保留，但渲染路径改为 CardSession
- Progress bar → CardEvent.progress_updated()

**测试文件**: `tests/test_loop_handler_card.py`
- test_loop_iteration_segments: 验证多 segment 分配
- test_loop_progress_updates: 验证进度事件
- test_loop_review_done: 验证 review 结果渲染
- test_loop_multi_iteration: 3 次迭代完整流程

**验收**: `uv run pytest tests/test_loop_handler_card.py -v` 全部通过

---

### Task 6.3: Spec Handler 适配

**修改文件**:
- `src/feishu/handlers/spec.py`
- `src/feishu/renderers/spec.py` → `src/feishu/renderers/spec_renderer.py`

**改造内容**:
- Spec 的 5 阶段 (Spec→Plan→Task→Build→Review) → 进度事件
- Review pipeline 结果 → 专用 CardEvent
- 验收标准区域 → plan block 渲染

**测试文件**: `tests/test_spec_handler_card.py`
- test_spec_phase_transitions: 验证阶段切换事件
- test_spec_build_progress: 验证 Build 阶段进度
- test_spec_review_result: 验证 Review 结果渲染

**验收**: `uv run pytest tests/test_spec_handler_card.py -v` 全部通过

---

## Phase 7: Programming Mode 适配

**目标**: 将 StreamingCardManager 场景统一到 CardSession 架构。

### Task 7.1: Programming Handler 适配

**修改文件**:
- `src/feishu/handlers/programming.py`

**改造内容**:
- `handle_response()`:
  - 创建 `CardSession` 替代 `StreamingCardManager.create_streaming_card()`
  - ACP 事件回调改为 `session.dispatch(CardEvent.from_acp(event))`
  - session.close() 替代 streaming_card.finalize()
- 保留 `build_project_response_card()` 用于非流式场景（进入/退出模式）
- 全编程模式兼容: Coco/Claude/Aiden/Codex/Gemini/TTADK

**测试文件**: `tests/test_programming_card_session.py`
- test_coco_streaming_uses_session: Coco 流式走 CardSession
- test_claude_streaming_uses_session: Claude 流式走 CardSession
- test_ttadk_streaming_uses_session: TTADK 流式走 CardSession
- test_session_metadata_per_mode: 各模式 metadata 正确
- test_header_subtitle_shows_tool_model: 验证 subtitle 展示

**验收**: `uv run pytest tests/test_programming_card_session.py -v` 全部通过

---

### Task 7.2: StreamingCardManager 桥接 (过渡期)

**修改文件**: `src/card/streaming.py`

**改造内容**:
- `StreamingCardManager` 标记 `@deprecated`
- 内部代理到 `CardSession` + `CardDelivery`（或保留直到 Phase 8 移除）
- 确保外部未迁移的调用方仍然工作

**验收**: 现有所有 `test_card*.py` 测试通过

---

## Phase 8: 移除旧代码

**目标**: 清理过渡层，移除不再需要的旧模块。

### Task 8.1: 移除 styles.py re-export 层

**操作**:
1. 全局替换 `from src.card.styles import` → 对应的新模块路径
2. 删除 `src/card/styles.py`（仅在所有 import 迁移后）
3. 同步处理 `src/card/styles_lock.py`（合并到 `ui_text.py`）

**验证**: `uv run pytest tests/ -v` 全量通过 + `grep -r "from.*card.styles" src/` 无结果

---

### Task 8.2: 移除旧渲染层

**操作** (视 Phase 6-7 稳定后执行):
1. 移除 `src/feishu/renderers/base.py` 中的 `SmartSender`（功能已由 CardDelivery 替代）
2. 移除 `src/acp/renderer.py` 中的 `RenderedContent.to_elements()`（功能已由 render/tools.py 替代）
3. 精简 `CardBuilder` Facade 中已废弃的方法
4. 移除 `src/card/streaming.py`（功能已由 CardSession + CardDelivery 替代）

**验证**: `uv run pytest tests/ -v` 全量通过

---

### Task 8.3: 最终验证

**操作**:
1. `uv run pytest tests/ -v` — 全量测试通过
2. `uv run python -m src.main --validate` — 配置校验通过
3. 验证无循环依赖: `import src.card` 正常
4. 验证新旧模块无残留引用

---

## 依赖图 (Phase 执行顺序)

```
Phase 1 (State) ──→ Phase 2 (Render) ──→ Phase 3 (Delivery) ──→ Phase 4 (Session)
                                                                       │
Phase 5 (styles split) ── 独立，可与 Phase 1-4 并行 ─────────────────────┤
                                                                       │
                                                           Phase 6 (Engine Handlers)
                                                                       │
                                                           Phase 7 (Programming Mode)
                                                                       │
                                                           Phase 8 (Cleanup)
```

- Phase 1→2→3→4 严格顺序（每层依赖前一层）
- Phase 5 独立，可与 Phase 1-4 并行执行
- Phase 6-7 依赖 Phase 4 完成
- Phase 8 依赖 Phase 6-7 稳定

---

## 工作量估算

| Phase | Tasks | 新文件数 | 测试用例数 | 复杂度 |
|-------|-------|---------|-----------|--------|
| 1 | 4 | 9 | ~35 | ⭐⭐ |
| 2 | 5 | 10 | ~40 | ⭐⭐⭐ |
| 3 | 3 | 5 | ~20 | ⭐⭐⭐ |
| 4 | 2 | 2 | ~15 | ⭐⭐ |
| 5 | 1 | 5 | ~10 | ⭐ |
| 6 | 3 | 3 | ~15 | ⭐⭐⭐ |
| 7 | 2 | 1 | ~10 | ⭐⭐⭐ |
| 8 | 3 | 0 (删除) | 0 (回归) | ⭐⭐ |
| **总计** | **23** | **~35** | **~145** | - |

---

## 风险与缓解

| 风险 | 缓解 |
|------|------|
| 飞书 Schema 2.0 collapsible_panel 兼容性 | Task 2.4 中验证 JSON 结构，保留降级路径 |
| Sequence 冲突频繁 | Task 3.3 实现 reconcile + exponential backoff |
| 多线程并发状态冲突 | CardSession 内部 Lock + CardState frozen 不可变 |
| 旧测试在 Phase 6-7 可能 break | Phase 6-7 保持向后兼容，Phase 8 统一清理 |
| 大量文件新建导致 merge 冲突 | 新目录（state/render/delivery）不与旧代码交叉 |

---

## Commit 策略

每个 Phase 完成后独立 commit:
- `feat(card): add state models and reducer (Phase 1)`
- `feat(card): add pure render functions (Phase 2)`
- `feat(card): add unified delivery engine (Phase 3)`
- `feat(card): add CardSession orchestration (Phase 4)`
- `refactor(card): split styles.py into focused modules (Phase 5)`
- `refactor(card): adapt engine handlers to CardSession (Phase 6)`
- `refactor(card): adapt programming mode to CardSession (Phase 7)`
- `refactor(card): remove deprecated modules (Phase 8)`
