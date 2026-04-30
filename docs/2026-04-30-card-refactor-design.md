# 卡片系统重构设计文档

> 日期: 2026-04-30
> 目标: 参照 pokoclaw 的三层解耦架构，对 GhostAP 卡片系统进行激进式重写，实现状态/渲染/投递完全分离，确保 AI 过程消息不丢失，同时提升视觉审美和可维护性。

---

## 一、设计原则

| 原则 | 说明 |
|------|------|
| **纯函数渲染** | render(state) → JSON，无副作用，可测试可推理 |
| **事件驱动状态** | 所有状态变更通过 CardEvent 派发，reducer 纯函数归约 |
| **统一投递** | 一个 CardDelivery 管理所有 create/update/element_content |
| **消息不丢** | Atom 分页 + 历史折叠 + Segment 续接，所有内容保留 |
| **渐进披露** | 默认精简，用户主动展开查看详情 |
| **语义色彩** | 颜色传达含义而非装饰 |
| **移动优先** | 按钮足够大、内容不横溢、自适应布局 |

---

## 二、架构总览

### 2.1 三层分离

```
事件源 (ACP Events / Engine Events / Shell Events)
  → Layer 1: CardState Reducer (纯函数, 不可变状态归约)
    → Layer 2: Pure Render (state → CardJSON, 无副作用)
      → Layer 3: CardDelivery (统一投递: create/update/element_content)
        → Feishu API (message.create / message.patch)
```

### 2.2 目录结构

```
src/card/
├── __init__.py              # 公共 API 导出
├── events.py                # CardEvent 类型定义
├── state/
│   ├── __init__.py
│   ├── models.py            # CardState + 子状态 frozen dataclass
│   ├── reducer.py           # reduce_card_state() 主 reducer
│   └── reducers/            # 按事件类型拆分的子 reducer
│       ├── text.py          # assistant_text 相关
│       ├── tool.py          # tool_call 相关
│       ├── plan.py          # plan 相关
│       ├── reasoning.py     # reasoning 相关
│       └── lifecycle.py     # 生命周期 (start/complete/fail/cancel)
├── render/
│   ├── __init__.py
│   ├── renderer.py          # render_card() 主入口
│   ├── atoms.py             # ContentBlock → 视觉元素转换
│   ├── pagination.py        # Atom 分页 (预算约束)
│   ├── header.py            # Header 渲染 (title + subtitle + template)
│   ├── footer.py            # Footer 状态行渲染
│   ├── tools.py             # 工具调用面板渲染
│   ├── reasoning.py         # 推理面板渲染
│   ├── plan.py              # 计划面板渲染
│   ├── buttons.py           # 按钮组渲染
│   └── budget.py            # RenderBudget 预算计算
├── delivery/
│   ├── __init__.py
│   ├── engine.py            # CardDelivery 统一投递引擎
│   ├── sequence.py          # 序列号管理 + reconcile
│   ├── throttle.py          # 节流调度 (复用 FlowControlStrategy)
│   └── binding.py           # message_id 绑定管理
├── session.py               # CardSession (Handler 的入口接口)
├── themes.py                # 主题定义 (ProjectTheme, ENGINE_STYLES)
├── ui_text.py               # UI 文案字典 (按域懒加载)
├── thresholds.py            # 阈值常量 (THRESHOLDS, TRUNCATION_LIMITS, RenderBudget)
├── buttons_config.py        # 按钮配置 (BUTTON_CONFIG, ButtonSpec)
├── terminal.py              # 终态标记 (TERMINAL_MARKERS, FOOTER_STATUS)
└── truncation.py            # 截断工具函数 (保留)
```

### 2.3 数据流 (Handler 视角)

```python
# Handler 只做两件事: 创建 session + 派发事件
class DeepHandler:
    async def handle_deep(self, chat_id, requirement, project):
        session = CardSession(
            chat_id=chat_id,
            metadata=CardMetadata(
                project_name=project.name,
                mode_name="Deep Agent",
                mode_emoji="🧠",
                tool_name="coco",
                model_name="gpt-4o",
                engine_type="deep",
            ),
        )
        session.dispatch(CardEvent.started())

        async for acp_event in engine.run(requirement):
            session.dispatch(CardEvent.from_acp(acp_event))

        session.close()
```

---

## 三、Layer 1: 状态模型

### 3.1 CardState (顶层状态)

```python
from dataclasses import dataclass
from typing import Literal

@dataclass(frozen=True)
class CardState:
    """不可变卡片状态，由 reducer 纯函数产出"""
    blocks: tuple[ContentBlock, ...]        # 内容原子序列 (有序)
    terminal: TerminalStatus                # running/completed/failed/cancelled/...
    header: HeaderState                     # 标题/图标/模板色
    footer: FooterState                     # 底部状态行
    buttons: tuple[ButtonSpec, ...]         # 按钮规格列表
    metadata: CardMetadata                  # 项目/工具/模型等元信息
    version: int                            # 状态版本号 (每次 reduce +1)
```

### 3.2 ContentBlock (内容原子)

```python
@dataclass(frozen=True)
class ContentBlock:
    """不可分割的内容原子"""
    kind: Literal["text", "tool_call", "reasoning", "plan"]
    block_id: str                           # 唯一标识
    content: str                            # 主内容 (markdown)
    element_id: str | None                  # 流式更新定位 ID
    status: BlockStatus                     # active/completed/failed
    # tool_call 专属
    tool_name: str | None
    tool_summary: str | None
    tool_input: str | None
    tool_output: str | None
    # reasoning 专属
    char_count: int                         # 用于折叠后显示字数

BlockStatus = Literal["active", "completed", "failed"]
```

### 3.3 HeaderState

```python
@dataclass(frozen=True)
class HeaderState:
    title: str              # "🧠 ProjectName · Deep Agent"
    subtitle: str | None    # "🔧 coco · gpt-4o · 正在执行中"
    template: str           # header 颜色: "blue"/"green"/"red"/...
```

### 3.4 FooterState

```python
@dataclass(frozen=True)
class FooterState:
    status: Literal["thinking", "tool_running", "waiting_approval", "idle"] | None
    status_text: str | None     # "💭 正在思考..." / "🔧 执行中: bash"
    progress: str | None        # "▰▰▰▱▱▱▱▱▱▱ 30% · 步骤 2/6"
```

### 3.5 CardMetadata

```python
@dataclass(frozen=True)
class CardMetadata:
    project_name: str | None
    mode_name: str              # "Coco" / "Claude" / "Deep Agent"
    mode_emoji: str             # 🤖 / 🔮 / 🧠 / ♾️ / 🎮
    tool_name: str | None       # "coco" / "claude-cli" / "cursor" / "aider"
    model_name: str | None      # "gpt-4o" / "claude-sonnet-4-20250514"
    engine_type: str | None     # "deep" / "loop" / "spec" / None
```

### 3.6 CardEvent 类型

```python
@dataclass(frozen=True)
class CardEvent:
    type: CardEventType
    payload: dict               # 事件特定数据

class CardEventType(str, Enum):
    # 生命周期
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"
    RESUMED = "resumed"
    # 内容
    TEXT_STARTED = "text_started"
    TEXT_DELTA = "text_delta"
    TEXT_DONE = "text_done"
    REASONING_STARTED = "reasoning_started"
    REASONING_DELTA = "reasoning_delta"
    REASONING_DONE = "reasoning_done"
    TOOL_STARTED = "tool_started"
    TOOL_DELTA = "tool_delta"
    TOOL_DONE = "tool_done"
    TOOL_FAILED = "tool_failed"
    PLAN_UPDATED = "plan_updated"
    # 元信息
    TOOL_MODEL_CHANGED = "tool_model_changed"
    PROGRESS_UPDATED = "progress_updated"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESOLVED = "approval_resolved"
```

### 3.7 Reducer

```python
def reduce_card_state(state: CardState | None, event: CardEvent) -> CardState:
    """纯函数: 接收旧状态+事件, 产出新状态. 无副作用."""
    if state is None:
        state = _create_initial_state(event)

    match event.type:
        case CardEventType.TEXT_STARTED:
            return _on_text_started(state, event)
        case CardEventType.TEXT_DELTA:
            return _on_text_delta(state, event)
        case CardEventType.TOOL_STARTED:
            return _on_tool_started(state, event)
        case CardEventType.COMPLETED:
            return _on_completed(state, event)
        # ... 其他事件
    return state
```

---

## 四、Layer 2: 纯渲染

### 4.1 渲染入口

```python
@dataclass
class RenderedCard:
    """渲染产物"""
    card_json: dict                         # 飞书 Schema 2.0 完整 JSON
    structure_signature: str                 # 结构签名 (用于变更检测)
    active_element: ActiveElement | None     # 可流式更新的元素
    page_index: int                          # 页码 (0-based)
    total_pages: int                         # 总页数

@dataclass
class ActiveElement:
    element_id: str
    text: str

def render_card(state: CardState, budget: RenderBudget) -> list[RenderedCard]:
    """
    纯函数: 将 CardState 渲染为一个或多个 RenderedCard.
    无副作用, 无 I/O. 可直接单元测试.
    """
    atoms = flatten_to_atoms(state.blocks)
    pages = paginate_atoms(atoms, budget)
    return [render_page(state, page, i, len(pages)) for i, page in enumerate(pages)]
```

### 4.2 RenderBudget (预算约束)

```python
@dataclass(frozen=True)
class RenderBudget:
    byte_budget: int = 27 * 1024    # 27KB JSON 大小限制
    node_budget: int = 180          # 飞书标签节点上限
    visible_chars: int = 25000      # 可见字符上限
    tool_history_fold_threshold: int = 3   # 工具历史折叠阈值
    reasoning_tail_chars: int = 500        # 推理尾部保留字符
```

### 4.3 Atom 分页算法

```python
def paginate_atoms(
    atoms: list[RenderAtom],
    budget: RenderBudget,
) -> list[list[RenderAtom]]:
    """
    贪心分页:
    1. 尝试将 atom 加入当前页
    2. 若超出预算, 尝试 split (段落→行→1600字符)
    3. split 失败则开新页
    4. 所有内容都保留, 不丢弃
    """
    pages: list[list[RenderAtom]] = [[]]
    current_size = _base_overhead()

    for atom in atoms:
        atom_size = estimate_atom_size(atom)
        if current_size + atom_size <= budget.byte_budget:
            pages[-1].append(atom)
            current_size += atom_size
        else:
            # 尝试切分
            parts = split_atom(atom, budget.byte_budget - current_size)
            if parts:
                pages[-1].append(parts[0])
                pages.append(parts[1:])
                current_size = sum(estimate_atom_size(p) for p in parts[1:])
            else:
                pages.append([atom])
                current_size = _base_overhead() + atom_size
    return pages
```

### 4.4 Structure Signature (变更检测)

```python
def compute_structure_signature(state: CardState) -> str:
    """
    计算结构签名. 仅当卡片结构真正变化时签名才变.
    活跃的 text delta 不改变签名 (通过 element_content 流式推送).
    """
    sig_parts = []
    for block in state.blocks:
        if block.kind == "text" and block.status == "active":
            sig_parts.append(f"text:active:{block.block_id}")
        else:
            sig_parts.append(f"{block.kind}:{block.status}:{block.block_id}:{hash(block.content)}")
    sig_parts.append(f"terminal:{state.terminal}")
    sig_parts.append(f"footer:{state.footer.status}")
    sig_parts.append(f"buttons:{len(state.buttons)}")
    return hashlib.md5("|".join(sig_parts).encode()).hexdigest()
```

---

## 五、Layer 3: 统一投递

### 5.1 CardDelivery (核心引擎)

```python
class CardDelivery:
    """
    统一投递引擎. 合并原 StreamingCardManager + SmartSender.
    管理所有卡片的 create/update/element_content 操作.
    """

    def __init__(self, feishu_client, flow_strategy: FlowControlStrategy):
        self._client = feishu_client
        self._flow = flow_strategy
        self._bindings: dict[str, DeliveryBinding] = {}  # session_id → binding
        self._sequence: dict[str, int] = {}              # card_id → sequence

    async def deliver(self, session_id: str, rendered: list[RenderedCard]) -> None:
        """
        投递渲染结果. 自动决策操作类型:
        - 无 binding → card.create (发送新卡片)
        - signature 变化 → card.update (更新结构)
        - 仅 active_element 变化 → element_content (流式推送)
        - stale 页面 → 回收
        """
        binding = self._bindings.get(session_id)

        if binding is None:
            await self._create_cards(session_id, rendered)
        else:
            await self._update_cards(session_id, binding, rendered)

    async def close(self, session_id: str) -> None:
        """终态化: flush pending, 关闭 streaming_mode"""
        ...
```

### 5.2 操作类型决策

```python
async def _update_cards(self, session_id, binding, rendered):
    for i, card in enumerate(rendered):
        existing = binding.pages.get(i)

        if existing is None:
            # 新页面 → create
            await self._create_page(session_id, card)

        elif existing.signature != card.structure_signature:
            # 结构变化 → update (含 sequence)
            await self._update_page(session_id, existing, card)

        elif card.active_element and card.active_element.text != existing.last_text:
            # 仅文本变化 → element_content (流式)
            await self._stream_element(session_id, existing, card.active_element)

    # 回收 stale 页面
    for i in range(len(rendered), len(binding.pages)):
        await self._mark_stale(session_id, binding.pages[i])
```

### 5.3 节流调度

```python
DELIVERY_INTERVAL_MS = 200  # 最小 flush 间隔

class DeliveryThrottle:
    """
    节流策略:
    - 终态事件: 立即 flush
    - 结构变化: 200ms 节流
    - 纯文本流: 复用 FlowControlStrategy (EMA 自适应)
    """

    def schedule(self, session_id: str, immediate: bool = False):
        if immediate:
            self._flush_now(session_id)
        else:
            self._schedule_delayed(session_id, DELIVERY_INTERVAL_MS)
```

### 5.4 Sequence + Reconcile

```python
async def _sequenced_mutation(self, card_id: str, invoke_fn) -> MutationOutcome:
    """
    乐观并发:
    - 每次操作递增 sequence
    - 飞书返回 300317 (序列号冲突) → reconcile
    - 5xx/timeout → 延迟 1000ms 后 reconcile
    """
    seq = self._next_sequence(card_id)
    try:
        result = await invoke_fn(sequence=seq)
        return MutationOutcome(kind="applied", response=result)
    except SequenceConflictError as e:
        self._raise_sequence_floor(card_id, e.next_floor)
        return MutationOutcome(kind="reconcile")
    except TransportError:
        await asyncio.sleep(1.0)
        return MutationOutcome(kind="reconcile")
```

### 5.5 Segment 续接

```python
class SegmentManager:
    """
    管理单次运行中的多个 segment (因审批/暂停中断).
    每个 segment 对应独立的卡片集合.
    """

    def allocate_segment(self, session_id: str) -> str:
        """分配新 segment, 返回 segment_id"""
        idx = self._next_index(session_id)
        return f"{session_id}:seg:{idx}"

    def finalize_segment(self, segment_id: str):
        """终态化当前 segment 的卡片"""
        ...
```

---

## 六、CardSession (Handler 接口)

```python
class CardSession:
    """
    卡片会话. Handler 与卡片系统的唯一交互点.
    内部编排: dispatch → reduce → render → deliver.
    """

    def __init__(
        self,
        chat_id: str,
        metadata: CardMetadata,
        delivery: CardDelivery,
        budget: RenderBudget | None = None,
    ):
        self._chat_id = chat_id
        self._state: CardState | None = None
        self._delivery = delivery
        self._budget = budget or RenderBudget()
        self._metadata = metadata
        self._last_signature: str | None = None

    def dispatch(self, event: CardEvent) -> None:
        """
        派发事件. 触发完整的 reduce → render → deliver 流水线.
        Handler 不再直接操作卡片 JSON.
        """
        # 1. Reduce
        self._state = reduce_card_state(self._state, event)

        # 2. Render
        rendered = render_card(self._state, self._budget)

        # 3. Deliver (异步, 内部节流)
        is_terminal = event.type in (
            CardEventType.COMPLETED, CardEventType.FAILED, CardEventType.CANCELLED
        )
        self._delivery.schedule_deliver(
            session_id=self._session_id,
            rendered=rendered,
            immediate=is_terminal,
        )

    def close(self) -> None:
        """终态化: flush 所有 pending 更新, 关闭 streaming_mode"""
        self._delivery.close(self._session_id)
```

---

## 七、UX 视觉设计

### 7.1 Header 设计

#### 7.1.1 信息架构

```
┌──────────────────────────────────────────────────────────┐
│ {mode_emoji} {project_name} · {mode_name}                │  ← title (plain_text)
│    🔧 {tool_name} · {model_name} [· {实时状态}]          │  ← subtitle (plain_text)
└──────────────────────────────────────────────────────────┘
```

#### 7.1.2 场景示例

| 场景 | title | subtitle |
|------|-------|----------|
| Coco 编程 | `🤖 MyProject · Coco` | `🔧 coco · gpt-4o` |
| Claude 编程 | `🔮 MyProject · Claude` | `🔧 claude-cli · claude-sonnet-4-20250514` |
| TTADK 编程 | `🎮 MyProject · TTADK` | `🔧 cursor · gemini-2.5-pro` |
| Gemini 编程 | `✨ MyProject · Gemini` | `🔧 gemini · gemini-2.5-pro` |
| Deep 引擎 | `🧠 MyProject · Deep Agent` | `🔧 coco · gpt-4o · 正在执行` |
| Loop 引擎 | `♾️ MyProject · Loop Engine` | `🔧 claude-cli · opus · 迭代 2/5` |
| Spec 引擎 | `🧠 MyProject · Spec Engine` | `🔧 coco · gpt-4o · Build 阶段` |
| 无项目 | `🤖 Coco 编程模式` | `🔧 coco · gpt-4o` |
| 模型未知 | `🤖 MyProject · Coco` | `🔧 coco · 模型加载中...` |
| 工具切换中 | `🎮 MyProject · TTADK` | `🔧 切换中...` |

#### 7.1.3 Header 模板色规则

**优先级: 终态色 > 模式色**

终态覆盖:
| 终态 | template |
|------|----------|
| running | 保持模式色 |
| completed | `green` |
| failed | `red` |
| cancelled | `grey` |
| paused | `orange` |
| awaiting_approval | `indigo` |

模式色 (running 状态时使用):
| 模式 | template |
|------|----------|
| Coco | `blue` |
| Claude | `purple` |
| Gemini | `turquoise` |
| TTADK | `orange` |
| Deep | `turquoise` |
| Loop | `indigo` |
| Spec | `green` |
| Smart | `turquoise` |

### 7.2 Body 内容区域

#### 7.2.1 整体布局结构 (固定顺序)

```
1. ⚠️ Banner (可选, Apple 语义配色)
2. ── 分隔线 ──
3. 📊 进度条 (可选, 引擎模式)
4. 📝 内容区: ContentBlock 序列渲染
   - text blocks → markdown 直出
   - tool_call blocks → collapsible_panel
   - reasoning blocks → collapsible_panel
   - plan blocks → collapsible_panel
5. ── 分隔线 ──
6. 📋 验收标准 (可选, Loop/Spec)
7. ── 分隔线 ──
8. {footer_status_text}          (text_size: notation)
9. ── 分隔线 ──
10. [按钮组]
11. ✅ 终态标记 (可选)
```

#### 7.2.2 Assistant Text

- Markdown 原生渲染，默认字号
- 活跃文本块设置 `element_id`，支持流式 `element_content` 推送
- streaming_mode 启用时飞书客户端自动显示打字光标

#### 7.2.3 Reasoning (思考过程)

```json
{
  "tag": "collapsible_panel",
  "expanded": true,
  "header": {
    "title": {"tag": "markdown", "content": "💭 **深度思考中...**"},
    "vertical_align": "center",
    "icon": {"tag": "standard_icon", "token": "down-small-ccm_outlined", "size": "16px 16px"},
    "icon_position": "follow_text",
    "icon_expanded_angle": -180
  },
  "border": {"color": "grey", "corner_radius": "5px"},
  "vertical_spacing": "8px",
  "padding": "8px 8px 8px 8px",
  "elements": [{"tag": "markdown", "content": "...", "text_size": "notation"}]
}
```

状态变化:
- Active: `💭 **深度思考中...**` → expanded=true
- Done: `💭 **思考完成** · {char_count}字` → expanded=false
- 内容: 保留尾部 500 字符，超出前面加 `...`

#### 7.2.4 Tool Call (工具调用)

**单个工具面板:**

```json
{
  "tag": "collapsible_panel",
  "expanded": false,
  "header": {
    "title": {"tag": "markdown", "content": "{status_icon} **{tool_name}** — {summary}"},
    "vertical_align": "center",
    "icon": {"tag": "standard_icon", "token": "down-small-ccm_outlined", "size": "16px 16px"},
    "icon_position": "follow_text",
    "icon_expanded_angle": -180
  },
  "border": {"color": "grey", "corner_radius": "5px"},
  "vertical_spacing": "8px",
  "padding": "8px 8px 8px 8px",
  "elements": [{"tag": "markdown", "content": "...", "text_size": "notation"}]
}
```

**状态图标:**

| 状态 | 图标 | 边框色 | 标题示例 |
|------|------|--------|---------|
| running | ⏳ | grey | `⏳ **bash** — ls -la /src` |
| completed | ✓ | grey | `✓ **bash** — ls -la /src` |
| failed | ✗ | red | `✗ **bash** — ls -la /src` |

**工具摘要规则:**
- `bash` → 命令文本 (单行化, 80 字符截断)
- `read`/`write`/`edit` → 文件路径
- `grep`/`search` → `query · path`
- `create_file` → 文件路径
- 通用 → 尝试提取 path/name/id/query 字段
- 多信息用 ` · ` 连接

**工具详情内容 (notation 字号):**

```markdown
**Input**
```json
{"path": "/src/main.py"}
```

**Output**
```
文件内容...
```
```

**bash 工具定制展示:**

```markdown
**Command**
```bash
npm run build
```

**Result** · exit_code: `0`

**Stdout**
```
Build successful
```
```

**工具历史折叠:**

当连续工具调用 ≥ 3 个且已完成时:

```json
{
  "tag": "collapsible_panel",
  "expanded": false,
  "header": {"tag": "markdown", "content": "📋 **{N} 个工具调用已完成**"},
  "border": {"color": "blue", "corner_radius": "5px"},
  "elements": [/* 内部嵌套每个工具的 collapsible_panel */]
}
```

规则:
- 最新 1 个工具始终独立展示 (可能正在运行)
- 已完成 ≥3 个 → 折叠到蓝色外层面板
- 已完成 ≤2 个 → 各自独立展示
- 折叠 ≠ 删除，展开即可查看全部

#### 7.2.5 Plan (计划)

```json
{
  "tag": "collapsible_panel",
  "expanded": true,
  "header": {"tag": "markdown", "content": "📋 **执行计划**"},
  "border": {"color": "blue", "corner_radius": "5px"},
  "elements": [{
    "tag": "markdown",
    "content": "1. ✅ 分析需求\n2. ✅ 创建文件\n3. ⏳ 编写测试\n4. ○ 运行验证"
  }]
}
```

步骤状态图标: `✅` 完成 / `⏳` 进行中 / `○` 待执行 / `✗` 跳过

### 7.3 Footer 状态行

```
── 分隔线 ──
💭 正在思考...                    ← text_size: notation
```

| 活动状态 | 文案 |
|---------|------|
| thinking | `💭 正在思考...` |
| tool_running | `🔧 执行中: {tool_name}` |
| waiting_approval | `🔐 等待授权处理` |
| progress | `⚡ 步骤 {n}/{total} · 已用时 {duration}` |

### 7.4 进度条

Markdown 文本模拟 (飞书无原生 progress bar):

```
▰▰▰▰▰▱▱▱▱▱ 50% · 步骤 3/6 · 预计还需 1min
```

- 10 格宽度
- `▰` 已完成, `▱` 未完成
- 附带百分比 + 步骤 + 预估时间

### 7.5 Banner 语义配色

| 类型 | background_style | 前缀 | 用途 |
|------|-----------------|------|------|
| success | `green` | ✅ | 完成通知 |
| warning | `orange` | ⚠️ | 接近限制、需注意 |
| error | `red` | ❌ | 错误、连接异常 |
| info | `wathet` | ℹ️ | 模式切换、状态通知 |

实现: `column_set` + `flex_mode: "stretch"` + `background_style`，内容加粗。

### 7.6 按钮系统

#### 按钮类型语义

| type | 颜色 | 用途 |
|------|------|------|
| `primary` | 蓝色 | 主操作 (继续, 确认) |
| `default` | 灰色 | 辅助操作 (展开, 退出, 暂停) |
| `danger` | 红色 | 危险操作 (停止) |

#### 按钮布局

- ≤2 按钮: 水平并排 (`column_set`)
- 3-4 按钮: 2 列 grid
- >4 按钮: flow 流式布局 (`flex_mode: "flow"`)
- 移动端: 强制垂直堆叠

#### 引擎控制按钮组

```
运行中:   [⏸ 暂停](default)  [⏹ 停止](danger)
暂停中:   [▶️ 继续](primary)  [⏹ 停止](danger)
等待授权: [✅ 批准](primary)  [❌ 拒绝](danger)
```

辅助按钮 (下方行):
```
[🔽 展开详情](default)  [📜 历史](default)  [🚪 退出](default)
```

### 7.7 终态标记

卡片最底部 (按钮之后):

```python
TERMINAL_MARKERS = {
    "completed":         "✅ **已完成**",
    "failed":            "❌ **执行失败**",
    "blocked":           "⏸ **任务已阻塞**",
    "cancelled":         "⏹ **已停止**",
    "awaiting_approval": "🔐 **等待授权**",
    "denied":            "❌ **授权已拒绝**",
    "continued":         "✅ **已获得授权**",
}
```

### 7.8 分页导航

**首页 footer (非末页时):**
```
📄 第 1/{total} 页 · 后续内容见下一张卡片
```

**非首页 header (body 顶部):**
```
📄 第 {n}/{total} 页 · 接上文
```

**Stale 页面:**
```
ℹ️ **此页已整理，请查看最新的运行卡片。**
```

### 7.9 文本层级体系

| 上下文 | text_size | 用途 |
|--------|-----------|------|
| Assistant 正文 | 默认 (不设) | 主要阅读内容 |
| Reasoning 内容 | `notation` | 辅助信息, 小字 |
| 工具详情 | `notation` | 代码/数据, 小字 |
| Footer 状态 | `notation` | 辅助状态, 小字 |
| 进度条 | 默认 | 需要醒目 |
| 终态标记 | 默认 | 需要醒目 |

### 7.10 Emoji 语义系统

| Emoji | 语义 | 使用场景 |
|-------|------|---------|
| 💭 | 思考/推理 | Reasoning 面板标题 |
| 🔧 | 工具/执行 | subtitle 前缀, footer 工具执行 |
| 🔐 | 授权/锁定 | 等待授权状态 |
| ✅ | 成功/完成 | 终态标记, 步骤完成 |
| ❌ | 失败/错误 | 终态标记, Banner |
| ⏹ | 停止/取消 | 终态标记, 停止按钮 |
| ⏸ | 暂停/阻塞 | 终态标记, 暂停按钮 |
| ⏳ | 运行中 | 工具执行中 |
| 📋 | 列表/计划 | 工具历史折叠, Plan 面板 |
| 📊 | 进度 | 进度条前缀 |
| 📄 | 页面 | 分页导航 |
| ⚡ | 快速/步骤 | Footer 进度信息 |
| ℹ️ | 提示 | info banner, stale 页面 |
| ⚠️ | 警告 | warning banner |
| ▶️ | 继续 | 继续按钮 |
| 🚪 | 退出 | 退出模式按钮 |

### 7.11 颜色系统

```
语义色板 (状态驱动):
┌──────────┬──────────────────────────────────┐
│ Blue     │ 进行中、活跃、信息                 │
│ Green    │ 成功、完成、确认                   │
│ Red      │ 失败、错误、危险操作               │
│ Orange   │ 警告、暂停、需关注                 │
│ Grey     │ 中性、已取消、默认边框             │
│ Indigo   │ 等待/授权（特殊状态）              │
│ Wathet   │ 信息提示背景（轻量级）             │
└──────────┴──────────────────────────────────┘

collapsible_panel 边框色:
┌──────────┬──────────────────────────────────┐
│ grey     │ 默认 (reasoning, 成功/运行中工具)  │
│ red      │ 失败的工具调用                     │
│ blue     │ 历史工具折叠, Plan 面板            │
└──────────┴──────────────────────────────────┘
```

---

## 八、"消息不丢" 保障机制

### 8.1 设计目标

用户希望看到 AI 过程的所有消息，但不能太乱/被截断/被顶掉。

### 8.2 多层保障

| 层级 | 机制 | 效果 |
|------|------|------|
| 原子不丢 | ContentBlock 是不可分割的最小单位 | 分页时宁可开新页也不截断 atom |
| 智能切分 | 超大 atom 按段落→行→1600字符逐级切分 | 内容全保留只是跨页 |
| 历史折叠 | ≥3 已完成工具折叠到蓝色面板 | 不混乱但展开即可查看 |
| Reasoning 保留 | 尾部 500 字符 + `...` 前缀 | 保留最新思考 |
| Segment 续接 | 超出单卡上限自动开新卡 | 旧卡保留不被覆盖 |
| Stale 提示 | 页面整合后留友好提示 | 用户不迷路 |
| 翻页按钮 | `⬆️ 上一段` / `⬇️ 加载更多` | 历史可回溯 |
| 签名检测 | 仅结构变化才 update | 避免无谓覆盖 |

### 8.3 信息密度控制

在"不丢"的前提下保持清晰:

1. **默认折叠**: 已完成的工具调用、思考过程默认折叠
2. **历史合并**: 连续多个已完成工具合并为一个折叠面板
3. **字号分层**: 辅助信息用 notation 小字，减少视觉噪音
4. **分页标记**: 清晰的页码和导航，不会"迷失在哪一页"
5. **活跃优先**: 当前正在执行的内容始终可见且展开

---

## 九、styles.py 拆分方案

### 现状问题

`styles.py` 超过 800 行，承担 7 种职责，是典型的 God Object。

### 拆分后结构

| 新文件 | 职责 | 原内容来源 |
|--------|------|-----------|
| `themes.py` | ProjectTheme + ENGINE_STYLES + PANEL_STYLES | styles.py 前 100 行 |
| `ui_text.py` | UI_TEXT 字典 (支持按域懒加载) | styles.py 700+ 条目 |
| `thresholds.py` | THRESHOLDS + TRUNCATION_LIMITS + RenderBudget | styles.py 阈值部分 |
| `buttons_config.py` | BUTTON_CONFIG + ButtonSpec | styles.py 按钮部分 |
| `terminal.py` | TERMINAL_MARKERS + FOOTER_STATUS | styles.py 终态部分 |

### 迁移策略

1. 新文件创建后，原 `styles.py` 改为薄 re-export 层 (兼容期)
2. 逐步修改 import 路径
3. 最终删除 `styles.py`

---

## 十、Handler 适配方案

### 10.1 现有 Handler 改造

现有 Handler 从"直接操作 CardBuilder"改为"通过 CardSession 派发事件":

```python
# Before (紧耦合)
class DeepHandler:
    async def run(self):
        card_json = CardBuilder.build_engine_card(project, state)
        await self.smart_sender.send(card_json)

# After (解耦)
class DeepHandler:
    async def run(self):
        session = CardSession(chat_id, metadata, delivery)
        session.dispatch(CardEvent.started())
        # ... engine events
        session.dispatch(CardEvent.from_acp(event))
        session.close()
```

### 10.2 事件适配器

为现有 ACP 事件提供转换层:

```python
class CardEvent:
    @classmethod
    def from_acp(cls, acp_event: ACPEvent) -> "CardEvent":
        """将 ACPEvent 转换为 CardEvent"""
        match acp_event.type:
            case ACPEventType.TEXT_CHUNK:
                return cls(type=CardEventType.TEXT_DELTA, payload={"text": acp_event.text})
            case ACPEventType.TOOL_START:
                return cls(type=CardEventType.TOOL_STARTED, payload={...})
            # ...
```

### 10.3 向后兼容

- `CardBuilder` Facade 保留但标记 deprecated
- `StreamingCardManager` 保留但内部重定向到 `CardSession` + `CardDelivery`
- `SmartSender` 保留但内部重定向

---

## 十一、测试策略

### 11.1 纯函数可测试性

三层分离的核心收益是可测试性:

```python
# Reducer 测试: 纯输入输出
def test_text_delta_appends():
    state = reduce_card_state(None, CardEvent.started())
    state = reduce_card_state(state, CardEvent(TEXT_STARTED, {"block_id": "b1"}))
    state = reduce_card_state(state, CardEvent(TEXT_DELTA, {"block_id": "b1", "text": "hello"}))
    assert state.blocks[0].content == "hello"

# Render 测试: 纯输入输出
def test_tool_history_folds():
    state = _state_with_n_completed_tools(5)
    rendered = render_card(state, RenderBudget())
    body = rendered[0].card_json["body"]["elements"]
    # 验证: 有蓝色折叠面板包含 4 个工具, 最新 1 个独立展示
    ...

# Delivery 测试: mock feishu_client
def test_structure_change_triggers_update():
    delivery = CardDelivery(mock_client, FlowControlStrategy())
    # 首次 → create
    await delivery.deliver("s1", [rendered_v1])
    assert mock_client.create_called
    # 结构变化 → update
    await delivery.deliver("s1", [rendered_v2_different_sig])
    assert mock_client.update_called
```

### 11.2 测试分层

| 层 | 测试类型 | 关注点 |
|-----|---------|--------|
| Reducer | 纯单元测试 | 事件序列 → 状态正确性 |
| Render | 纯单元测试 | 状态 → JSON 结构正确性, 分页正确性 |
| Delivery | 集成测试 (mock API) | 操作决策、节流、reconcile |
| CardSession | 集成测试 | 端到端事件→API调用 |

---

## 十二、迁移计划概述

| 阶段 | 内容 | 验证 |
|------|------|------|
| Phase 1 | 创建 state/ 模型和 reducer | 单元测试 reducer |
| Phase 2 | 创建 render/ 纯渲染函数 | 单元测试 render 产出 |
| Phase 3 | 创建 delivery/ 投递引擎 | Mock API 集成测试 |
| Phase 4 | 实现 CardSession 编排 | 端到端测试 |
| Phase 5 | 拆分 styles.py | import 检查 |
| Phase 6 | 适配 Handler (Deep/Loop/Spec) | 功能回归测试 |
| Phase 7 | 适配 StreamingCardManager 场景 | 编程模式回归 |
| Phase 8 | 移除旧代码 | 全量测试 |

---

## 十三、与 pokoclaw 架构对照

| 能力 | pokoclaw (TypeScript) | GhostAP 新架构 (Python) |
|------|----------------------|------------------------|
| 状态模型 | LarkRunState (mutable object) | CardState (frozen dataclass) |
| Reducer | reduceLarkRunState() | reduce_card_state() |
| 渲染 | renderLarkRunCard() → JSON | render_card() → list[RenderedCard] |
| 投递 | outbound.ts + cardkit-mutations.ts | CardDelivery + sequence.py |
| 流式 | cardElement.content | element_content (相同 API) |
| 签名 | structureSignature | structure_signature |
| 分页 | paginateRunCardAtoms (贪心) | paginate_atoms (贪心, 同策略) |
| 续接 | allocateRunSegmentObjectId | SegmentManager |
| 节流 | 200ms interval | DeliveryThrottle (200ms + EMA) |
| Reconcile | 300317 检测 + 1000ms 重试 | 同策略 |
| 工具折叠 | >2 合并蓝色面板 | ≥3 合并蓝色面板 (对齐) |
| Reasoning | 尾部 500 字符 | 尾部 500 字符 (对齐) |
| Stale 回收 | buildStaleRunCardCard() | _mark_stale() |
| Header subtitle | ❌ 未使用 | ✅ 展示工具+模型 |
| Standard icon | collapsible 箭头 | collapsible 箭头 (对齐) |

---

## 十四、关键设计决策记录

| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| 状态可变性 | mutable / frozen | frozen dataclass | Python 中 frozen 提供更好的不可变保证和 hash 支持 |
| 异步模型 | asyncio / threading | threading + asyncio bridge | 与现有架构兼容，delivery 内部使用 asyncio |
| 工具折叠阈值 | >2 / ≥3 | ≥3 | 2 个工具并列展示信息密度合理，不需要折叠 |
| Header subtitle | 无 / 有 | 有 | 用户明确需求: 随时知道"谁在帮我干活" |
| 工具状态图标 | emoji / 字符 | 轻量字符 (✓/✗/⏳) | 标题行用轻量字符保持紧凑，终态标记用大 emoji 保持醒目 |
| styles 拆分 | 保留/拆分 | 拆分 5 文件 + 兼容期 re-export | 消除 God Object，逐步迁移 |
