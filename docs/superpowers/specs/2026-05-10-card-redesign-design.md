# 飞书卡片重设计 · 统一编程模式 v2

- **Status**: Draft (pending user review)
- **Date**: 2026-05-10
- **Author**: jiataorui (协作: Claude Opus 4.7)
- **Mockups**: `ux/unified_card_v2_single.html` · `ux/unified_card_v2_split_parallel.html`
- **Replaces (extends)**: `docs/superpowers/specs/2026-05-09-unified-programming-card-design.md`

## 1. 动机

2026-05-09 统一了 SectionLayout 四区骨架，但仍存在以下信息缺口与体验问题：

1. **Header 信息不全**：当前 phase_banner 只展示 `工具 · 状态 · 时长`，缺项目名/卡片序号/项目目录。多项目并发时无法快速分辨上下文。
2. **任务列表信息密度不平衡**：当前 plan block 以"当前任务 + 进度计数"为主，缺乏"已完成 / 未处理"分组列表。用户在长流程中无法一眼看出还剩哪些任务。
3. **Reasoning 与工具调用的循环结构未显化**：activity_summary 是跨 turn 聚合的统计面板，丢失"思考 → 工具批 → 再思考 → 再工具批"这种局部循环节奏。
4. **当前调用工具的 footer 提示缺失**：用户在长 body 区滚动时无法快速看出"现在到底在做什么"。
5. **Subagent 上下文不可见**：worktree / spec review 等并行 subagent 卡片无统一的"父子关系 + model + tool"标识。
6. **切卡断裂感**：现有 `card_split` 已重注 sticky 头，但缺乏"累计时间 / 任务转移指针 / 旧卡冻结视觉"等连贯性信号。

## 2. 设计目标

| # | 目标 | 度量 |
|---|------|------|
| G1 | 单卡承载 8 项要素：项目名/工具/序号/目录/时长/任务列表/流程/footer hint | 单卡可视化校验，全部存在 |
| G2 | 任务列表三段固定常开 (进行中/已完成/未处理)，不被 pagination 截断 | sticky_head 必含 task_list；node ≤ 25 |
| G3 | 工具调用默认折叠，仅运行中工具展开（保留 shimmer/markdown live 提示） | 单 turn 渲染回归测试 |
| G4 | 切卡保持连贯：旧卡冻结视觉 + 新卡 header 含累计时间 + flow 续接桥语 | card_split 测试覆盖 3 个新断言 |
| G5 | 多 subagent 每 agent 一卡，并行 streaming 不互相干扰，footer 标 model/tool | 并行 streaming 集成测试 |
| G6 | 模块级淡背景色区分，飞书 Schema 2.0 兼容（column_set 级 `background_style`） | renderer 静态检查无 `text_color` 与 `div`+padding 违规 |

## 3. 卡片骨架（v2）

延用 SectionLayout SSOT 四区，但每区内容重新组织：

```
┌─────────────────────────────────────┐
│ sticky_head                         │ 每页重注，节点预算 ≤ 25
│ ├ HEADER (项目·工具·#seq + 目录/时长) │
│ └ TASK_BLOCK (三段常开)              │
├─────────────────────────────────────┤
│ status (仅首页)                      │ Deep/Loop/Spec 进度条 / acceptance criteria
├─────────────────────────────────────┤
│ body (主内容、跨页分页)               │
│ └ FLOW (reasoning ↔ tool 循环)       │
├─────────────────────────────────────┤
│ appendix (仅末页)                    │
│ └ FOOTER (当前工具小字 + subagent 标)│
└─────────────────────────────────────┘
```

变化点：
- **task_list 从 status 区上移进 sticky_head**：保证翻页/续卡始终可见。
- **footer 从 body 末尾下移到 appendix**：仅末页带，避免每页重复。
- **activity_summary 移除作为独立面板**，其语义被 turn-local reasoning 块吸收（详见 §4.3）。

### 3.1 节点预算（Schema 2.0）

| 区 | 节点上限 | Fallback |
|----|---------|---------|
| sticky_head | 25 | 任务列表超出按"已完成"组先折叠为 `✅ 已完成 (N) ▼` 单行 |
| status | 8 | progress_bar 简化文本 |
| body | 整卡 ≤ 180 - sticky_head - status - appendix | 触发 `card_split` |
| appendix | 6 | footer 单行 markdown |

## 4. 各区细节

### 4.1 HEADER (sticky)

布局两行：

- **第一行**（粗体 14px）：`📁 {project_name} · 🤖 {tool_display} · #{card_seq}`
  - 右对齐副标题：`{model_id}` (11px, muted)
- **第二行**（11.5px, muted）：
  - 左：`{relative_path}` (monospace)，工作目录相对 `~` 的短路径
  - 右：`{live_dot} {elapsed}` 运行中显示绿点动画 + `Xm Ys`，已封存显示 `⏸ {final}`

背景：`linear-gradient(135deg, #eef3ff 0%, #f5f0ff 100%)`，下边框 `#c8d3f0` 1px。
飞书实现：column_set + `background_style.color = "#eef3ff"`（飞书不支持渐变；折中用单色）。

#### 数据来源

| 字段 | 来源 |
|------|------|
| project_name | `ProjectContext.name`，无 project 时回落到 chat title |
| tool_display | `ACPSession.tool_id` 或 `TTADKManager.current_tool_display` |
| card_seq | 新增 `CardSession.sequence` 单调递增计数（同 chat + 同 session 内） |
| model_id | 当前 ACP session 的 model 字段 |
| relative_path | `os.path.relpath(working_dir, Path.home())` 折叠成 `~/...` |
| elapsed | `time.time() - session.started_at`，每秒驱动 markdown 文本刷新一次（throttle 1s） |

### 4.2 TASK_BLOCK (sticky · 三段常开)

```
▶ 进行中 (1)
  ▶ 修复路由层异常分支         ← 黄底高亮 + 跳动 ▶
✅ 已完成 (2)
  ✔ 探索代码库结构              ← 灰色删除线
  ✔ 定位 router 入口
⏳ 未处理 (3)
  ○ 补充单元测试
  ○ 跑集成测试
  ○ 更新 CHANGELOG
```

- 三段始终展示，分组小标题 11px UPPERCASE muted。
- 进行中任务用 column_set 加 `background_style.color = "rgba(255,200,40,0.18)"` 实现黄底（飞书 column 级支持）。
- ▶ 跳动通过 markdown 文本 `▶` ↔ `▷` 帧切换实现（节流 1Hz，与 elapsed tick 复用调度）。
- 字段来源：`PlanInfo.entries` (ACP plan)；非 ACP 工具回落到 task_registry。

#### 节点预算降级

任务总数 N > 12 时按以下顺序折叠：
1. 已完成组合并为单行 `✅ 已完成 (N) ▼`（折叠 details）
2. 未处理组只展示前 5 + `…还有 K 个`
3. 进行中组永不折叠（最多 3 个并发任务）

### 4.3 FLOW · 思考-工具循环 (body)

每个 **turn** 是一个 reasoning 块加 0..N 个工具折叠。结构：

```
┌─ reasoning (灰底 + 左侧灰边竖条) ─┐
│ 读完入口后判断错误处理在 dispatch │
│ 层，准备 grep 匹配并打补丁。       │
└──────────────────────────────────┘
[🔍 Grep "def route" · 8 hits · 0.4s]   ← 折叠
[📖 Read dispatch.py · 220 lines]       ← 折叠
[📝 Edit dispatch.py · 进行中…]          ← 仅 running 时展开 + shimmer
```

#### 折叠规则（确认与现 v1 不同）

> 用户原话："默认折叠一起" → 在 v1 mockup 单卡场景里 confirmed 为：**仅运行中的工具展开**，已完成全部折叠。这是对 2026-05-09 v1 "latest active 展开" 的进一步收紧（单 turn 内多个 active 时也只 1 个展开）。

| 工具状态 | 折叠 | 视觉 |
|---------|------|------|
| running (唯一展开) | open | shimmer 渐变背景（markdown 文本切换 `🟦 → ⬜ → 🟦` 模拟，或 emoji `⏳`/`⌛` 帧切换） |
| completed | collapsed | summary 显示工具名 + 结果摘要 + 耗时 |
| failed | collapsed (但 summary 红字) | summary 含 `❌` |
| cancelled | collapsed | summary 含 `⊘` |

#### Reasoning 块语义

每个 turn 顶部一段。来源：ACP `text` 事件累积，遇到 `ToolCall` 事件时 finalize 当前块、起新 turn。规则：

- 一个 turn ≤ 1 个 reasoning 块；text 事件追加到当前块。
- 收到第一个 `ToolCall` 时，当前 reasoning 块定型为只读 atom。
- 工具组结束（即下一次 text 事件到来）时，开新 turn。

数据流：`ACPEventRenderer._consume_event` 已经在分发 text/tool；新增 `_open_turn() / _close_turn()` 状态机。

### 4.4 STATUS（仅首页）

仅 Deep/Loop/Spec 引擎使用：进度条 / acceptance criteria 面板。普通编程模式空区。沿用现有实现，不变。

### 4.5 FOOTER（appendix · 末页）

两行小字（11px）：

```
⚙ Edit · 写入 src/router/dispatch.py
🧬 sub · model: claude-haiku-4-5 · tool: Aiden       ← 仅 subagent 卡显示
```

- 第一行 `now_tool_hint`：当前工具一句话描述。映射表 `tool_id → human_brief`（见 §4.5.1）。
- 第二行 `subagent_badge`：仅当 `CardSession.is_subagent=True` 时渲染。橙色 column_set + `background_style.color = "#fff3e6"`。

#### 4.5.1 工具简介映射

```python
TOOL_BRIEF = {
    "Read":  lambda p: f"读取 {p.get('path','...')}",
    "Edit":  lambda p: f"写入 {p.get('path','...')}",
    "Grep":  lambda p: f"搜索 “{p.get('pattern','...')}”",
    "Glob":  lambda p: f"列出 {p.get('pattern','...')}",
    "Bash":  lambda p: f"执行 {_short_cmd(p.get('command',''))}",
    "Write": lambda p: f"创建 {p.get('path','...')}",
    "Task":  lambda p: f"派发 {p.get('subagent_type','agent')}",
    # 其余工具落 fallback: f"{tool_id}"
}
```

存放位置：`src/card/render/footer.py` 已有 footer 生成；新增模块级常量。

## 5. 切卡连贯性 (`card_split`)

现有 `card_split` 在 task_done / round_changed / cycle_changed / pagination overflow 时触发。本次新增以下连贯信号：

### 5.1 旧卡冻结态

切卡瞬间对前一张卡执行一次"冻结渲染"：
- header 第一行 badge 后追加 `已封存`（灰色 chip）
- header 第二行去掉 live_dot，elapsed 显示为最终值前缀 `⏸`
- header 背景从蓝紫渐变改为 `#eef0f2`（灰）
- footer 改写为 `本卡已停止更新 · 续接 #{N+1} ↓`

### 5.2 新卡 header

- `card_seq` = 旧卡 + 1
- elapsed 行展示 `{current_card_elapsed} · 累计 {total_session_elapsed}`
  - `total_session_elapsed` 来自 `CardSession.session_started_at`（跨切卡保持）

### 5.3 新卡 flow 桥语

新卡 body 第一个 reasoning 块前缀注入 `续接：`（如果该 reasoning 是续接 turn 而非全新 turn）。判定：切卡发生在 turn 中段（reasoning 已开始未完成）→ 标记续接。

### 5.4 实现入口

修改文件：
- `src/card/render/header.py` · 新增 `_render_frozen_header(state) -> Atom`
- `src/card/state/reducers/programming.py` · `card_split` action 写入 `previous_card_final_state` 给上一张卡
- `src/card/orchestrator.py` · 切卡时把旧卡最后一帧改为 frozen 渲染再上传 patch

## 6. 多 subagent 并行

### 6.1 拓扑

```
main session #5  ──dispatch──▶  subagent #5.a (architect-review)
                            └─▶ subagent #5.b (security-review)
```

- 每个 subagent 启动时新建独立 `CardSession`，`parent_card_seq=5`，`card_seq` 用点号分支编号 `5.a / 5.b / 5.c …`
- 三张卡独立 `card_id`，独立 element_id 命名空间，**各自 streaming 互不干扰**
- 主卡 flow 区出现一个 `🧬 Dispatch · N subagents` 折叠 atom，列出子卡指针

### 6.2 视觉区分

| 卡型 | 边框 | header 背景 | badge |
|------|------|------------|-------|
| main | `#3370ff` 2px | 蓝紫渐变 | `#5` |
| subagent | `#ffb84d` 1px | 橙色渐变 `#fff7e6` | `#5.a` (橙色 chip) |

subagent header 第二行左侧改为 `↳ from #{parent_seq}`（替换 relative_path，后者在 subagent 上下文意义不大）。

### 6.3 并发 streaming 保护

现有 `_StreamThrottle` 是 per-card 的；并行场景下确认每个 `CardSession` 拥有独立 throttle 实例：
- 修改 `src/card/orchestrator.py` 的 stream_bridge 注册逻辑，每个 subagent dispatch 时分配独立 bridge。
- 主卡 dispatch atom 在收到子卡 finalize 事件时更新摘要（`#5.a ✔ 1m54s`）。

### 6.4 完成态

subagent 完成不合并到主卡，三张卡独立保留。主卡的 dispatch atom summary 反映汇总：
```
🧬 Dispatch · 2 subagents (✔ 2 / ⏳ 0)
→ #5.a · architect-review · ✔ 1m54s
→ #5.b · security-review · ✔ 2m08s
```

## 7. 动效策略 (飞书可承载部分)

飞书 Schema 2.0 不支持 CSS 动画。可用手段：

| 视觉目标 | 实现 | 节流 |
|---------|------|------|
| live_dot 跳动 | element_content patch 替换 emoji `🟢` ↔ `⚪` | 1Hz |
| 当前任务 ▶ 缩放 | text patch `▶` ↔ `▷` | 1Hz |
| running 工具 shimmer | summary 文本尾追加 `…` 帧切 (`. → .. → …`) | 1Hz |
| elapsed tick | text patch `Xm Ys` 每秒+1 | 1Hz |

**统一调度**：新建 `src/card/render/live_ticker.py`，单线程驱动所有 1Hz 帧切（共享 element_content patch 队列）。仅在 `CardSession.is_running` 时启动；冻结即停。

> 不实现：颜色渐变循环、缩放动画、平滑过渡。飞书均不支持。

## 8. 数据模型变更

### 8.1 `CardSession` 新增字段

```python
@dataclass
class CardSession:
    # 既有...
    sequence: int = 1                    # 同会话内的卡片序号
    session_started_at: float            # 跨切卡保留的最初启动时间
    is_subagent: bool = False
    parent_card_seq: str | None = None   # "5" or None
    final_state_for_freeze: CardState | None = None  # 切卡时由旧卡填入
```

### 8.2 `CardEvent` 增量

无新事件类型；复用现有 `card_split` + `text` + `tool_call`。新增可选字段：

```python
@dataclass
class CardSplitEvent:
    reason: Literal["task_done","round_changed","cycle_changed","budget_overflow"]
    bridge_phrase: str | None = None    # 注入到新卡第一个 reasoning 前缀
```

### 8.3 任务列表数据契约

任务三段视图来源：

```python
def group_tasks(plan: PlanInfo) -> tuple[list[Task], list[Task], list[Task]]:
    """returns (in_progress, completed, pending)."""
```

新建于 `src/card/render/task_list.py`（已存在文件，新增函数）。

## 9. 实现迁移点（按文件）

| 文件 | 改动类型 | 内容 |
|------|---------|------|
| `src/card/render/header.py` | 改 | header 改两行布局；新增 frozen 渲染 |
| `src/card/render/task_list.py` | 改 | 三段分组渲染 + 折叠降级 |
| `src/card/render/tools.py` | 改 | 折叠规则收紧到"仅 running 展开" |
| `src/card/render/footer.py` | 改 | now_tool_hint + subagent_badge |
| `src/card/render/live_ticker.py` | **新** | 1Hz 帧切调度 |
| `src/card/render/renderer.py` | 改 | 任务列表上移到 sticky_head；activity_summary 移除 |
| `src/card/state/models.py` | 改 | CardSession 新增 4 字段 |
| `src/card/state/reducers/programming.py` | 改 | card_split 注入 frozen state；turn 状态机 |
| `src/card/orchestrator.py` | 改 | 切卡 patch 旧卡最后帧；subagent 独立 throttle |
| `src/acp/renderer.py` | 改 | 新增 `_open_turn / _close_turn` 状态机驱动 reasoning ↔ tool 循环 |
| `tests/test_card_redesign_v2/` | **新** | 见 §10 |

## 10. 测试策略

### 10.1 渲染层（单元）

- `test_header_two_row_layout`：项目/工具/序号/目录/时长五要素全部出现
- `test_task_list_three_groups_always_open`：三组小标题 + 折叠降级回归 (12+ tasks)
- `test_tool_collapse_rule_only_running_open`：3 工具 turn (1 running, 1 done, 1 failed) → 仅 running open
- `test_footer_now_tool_hint`：5 种工具 ID 映射验证
- `test_subagent_badge_visibility`：is_subagent=True/False 切换

### 10.2 切卡（集成）

- `test_card_split_freezes_previous_with_pointer`：旧卡 badge 含 `已封存` + footer 含续接指针
- `test_card_split_new_card_shows_cumulative_time`：新卡 elapsed 含 `累计 X`
- `test_card_split_bridge_phrase_in_new_reasoning`：mid-turn split → 新卡第一行 `续接：`

### 10.3 并行（集成）

- `test_parallel_subagents_independent_streams`：dispatch 2 subagent，3 cards 各自 patch 不互相覆盖
- `test_subagent_card_orange_theme`：边框/背景/badge 颜色断言
- `test_main_card_dispatch_atom_summary_updates`：子卡 finalize → 主卡 dispatch atom 摘要更新

### 10.4 节点预算回归

- 30 tasks + 100 tools + 5 split + 2 subagents 跨页节点数全部 ≤ 180

### 10.5 静态门禁

- 复用现有 `test_card_renderer.py` 的 `div`+`background_style` 不允许出现规则
- 新增 `text_color` 属性递归不允许（已有先例 2026-05-07）

## 11. 兼容性

- **向后兼容**：旧 `CardSession` 反序列化时 `sequence` 默认 1、`session_started_at` 默认 `started_at`、`is_subagent` 默认 False
- **存量测试**：2026-05-09 v1 的 SectionLayout 契约不变（sticky_head/status/body/appendix）；store reducers 仅扩字段，不改语义。预期影响测试数量约 30~50 个，需更新 fixtures。
- **Memory.md / .Memory/**：本次改动需要在 `.Memory/2026-05-10.md` 加条记录，并更新 `Abstract.md`。

## 12. 不在范围

- 飞书卡片 dark mode（飞书自身已处理 light/dark 主题）
- worktree 选择卡 / 模型选择卡（保留现有实现，不动）
- Deep/Loop/Spec status panel 内部布局（仅承接新 sticky_head，不动 status）
- 真正的 CSS 动画 / SVG（飞书不支持）

## 13. 风险

| 风险 | 缓解 |
|------|------|
| 1Hz tick 全局调度成为瓶颈 | 仅在 `is_running` 时启动；空 session 不消耗；接入现有 `TimerScheduler` |
| sticky_head 频繁重注的 patch 体积 | 现有 `paginate_layout` 已 sticky-aware；patch 仅 diff sticky 内的变化 atoms |
| subagent 与 worktree subagent 概念重叠 | worktree 是用户显式多路；subagent 是 ACP `Task` 工具触发的隐式分发。卡片层仅看 `CardSession.is_subagent` flag，不耦合上层语义 |
| frozen 旧卡 patch 失败 | 旧卡 patch 失败仅影响视觉；新卡照常工作；记录 metric `card_freeze_patch_failed` |

## 14. 开放问题

- [ ] subagent 的"父卡 dispatch atom 摘要"是否需要点击跳转到子卡？飞书卡片不支持卡片间跳转，只能用文本编号引导。当前结论：仅文本引导。
- [ ] 累计时间是否包含上一段被人工 STOP 的时间？建议：包含（贴合用户感知），按 `session_started_at` 计算。
- [ ] live_ticker 1Hz 是否过于频繁导致飞书 patch 限流？预备策略：检测到限流时自动降级到 2Hz / 5Hz。

---

**附录 A · 视觉资源**

- `ux/unified_card_v2_single.html` — 单卡完整形态
- `ux/unified_card_v2_split_parallel.html` — 切卡 + 并行 subagent
