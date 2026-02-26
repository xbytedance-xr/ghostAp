# Loop Engine 架构文档

> 最后更新：2026-02-26 | 基于 ACP 重构后的实际实现

## 1. 概述

### 1.1 什么是 Loop Engine

Loop Engine 是 GhostAP 的 **迭代式自主开发引擎**。用户提交产品需求后，引擎将需求拆解为可验证的验收标准，然后通过 ACP 多轮 prompt 驱动 Agent 反复迭代，直到所有标准满足或触发终止条件。

核心理念：**验收标准驱动 + 迭代闭环 + 多视角审查**

### 1.2 与 Deep Engine 的对比

| 维度 | Deep Engine | Loop Engine |
|------|------------|-------------|
| 执行策略 | 单次 prompt，Agent 自主规划执行 | 多轮 prompt，每轮后评估验收标准 |
| 需求解析 | 原样传给 Agent | LLM 拆解为结构化验收标准 |
| 进度追踪 | 被动追踪 Agent 计划 + 工具调用 | 主动评估验收标准完成率 |
| 终止条件 | Agent prompt 结束 | 标准全满足 / 收敛检测 / 最大迭代 |
| 质量保证 | 无 | Ralph Loop 多视角审查 |
| 适用场景 | 明确可拆分的多步任务 | 模糊/探索性/需反复验证的需求 |

### 1.3 与交互模式的关系

```
交互模式（用户驱动）                 编排引擎（系统驱动）
┌─────────┐ ┌──────────┐           ┌─────────────────┐
│  Smart  │ │  Shell   │           │  Deep Engine    │
└─────────┘ └──────────┘           └─────────────────┘
┌─────────┐ ┌──────────┐           ┌─────────────────┐
│  Coco   │ │  Claude  │           │  Loop Engine    │ ← 本文
└─────────┘ └──────────┘           └─────────────────┘
```

Loop Engine 不是新的交互模式，而是与 Deep Engine 平级的 **编排引擎**。它通过 ACP 协议复用 Coco/Claude Agent 后端。

---

## 2. 执行流程

### 2.1 完整流程

```
用户: /loop <需求>
  │
  ▼
┌─────────────────────────────────────────────────────┐
│ Phase 1: 需求解析                                    │
│                                                     │
│  ① 尝试从文本中提取列表标记（- / * / [ ]）            │
│  ② 无显式列表 → LLM 拆解口语化输入为验收标准          │
│  ③ LLM 也失败 → 兜底为单条标准                       │
│                                                     │
│  输出: LoopRequirement {                            │
│    goal: "原始需求"                                  │
│    acceptance_criteria: ["标准1", "标准2", ...]       │
│  }                                                  │
└────────────────────┬────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────┐
│ Phase 2: 创建 ACP Session + 首轮 Prompt              │
│                                                     │
│  create_engine_session(agent_type, cwd)              │
│  → 发送包含完整需求 + 验收标准的初始 prompt            │
└────────────────────┬────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────┐
│ Phase 3: 迭代主循环                                  │
│                                                     │
│  for iteration in 1..max_iterations:                │
│    ① send_prompt → Agent 执行                       │
│    ② IterationTracker 追踪 ACP 事件                  │
│    ③ 多视角审查 (Ralph Loop, 可选)                    │
│    ④ LLM 评估验收标准完成情况                         │
│    ⑤ 终止判断:                                      │
│       - 全部标准满足 + 审查通过 → break              │
│       - 收敛检测触发 → break                         │
│       - 用户停止 → break                            │
│       - 否则 → 构建下轮 prompt 继续                   │
└────────────────────┬────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────┐
│ Phase 4: 输出最终报告                                │
│                                                     │
│  on_project_done(LoopProject)                       │
│  → LoopReporter 格式化迭代历史 + 验收标准状态          │
└─────────────────────────────────────────────────────┘
```

### 2.2 单轮迭代详情

```
  send_prompt(prompt)
       │
       ▼ ACP 事件流
  ┌──────────────────────────────┐
  │ IterationTracker.process()   │
  │   TEXT_CHUNK → 累积文本       │
  │   TOOL_CALL_* → 记录工具调用  │
  │   PLAN_UPDATE → 记录计划      │
  │                              │
  │ ACPEventRenderer.process()   │
  │   → 飞书 Markdown 渲染       │
  │                              │
  │ callbacks.on_iteration_event │
  │   → StreamingCard 实时更新    │
  └──────────────────────────────┘
       │
       ▼ prompt 结束
  ┌──────────────────────────────┐
  │ 多视角审查 (可选)             │
  │   send_prompt(审查prompt)    │
  │   → 解析四视角 PASS/FAIL     │
  │   → 提取改进建议             │
  └──────────────────────────────┘
       │
       ▼
  ┌──────────────────────────────┐
  │ 验收标准评估                  │
  │   send_prompt(评估prompt)    │
  │   → 解析 CRITERIA_N: PASS    │
  │   → CriteriaTracker 更新     │
  └──────────────────────────────┘
```

---

## 3. 模块结构

ACP 重构后，Loop Engine 从早期的 14 个文件简化为 4 个文件：

```
src/loop_engine/
├── models.py        # 数据模型 (625 行)
├── engine.py        # 核心引擎 (1110 行)
├── tracker.py       # ACP 事件追踪 (197 行)
└── reporter.py      # 报告格式化 (200 行)
```

### 3.1 models.py — 数据模型

**枚举类型**:

| 枚举 | 值 | 说明 |
|------|-----|------|
| `LoopProjectStatus` | IDLE/ANALYZING/RUNNING/PAUSED/COMPLETED/ABORTED | 项目状态机 |
| `IterationStatus` | RUNNING/SUCCESS/FAILED | 迭代状态 |
| `LoopRole` | ARCHITECT/DEVELOPER/REVIEWER/TESTER/DEBUGGER/INTEGRATOR | 角色类型 |
| `ReviewPerspective` | ARCHITECT/PRODUCT/USER/TESTER | 审查视角 |
| `TerminationSignal` | CONTINUE/COMPLETE/CONVERGED/MAX_ITER/FATAL/USER_STOP | 终止信号 |

**核心数据结构**:

```python
LoopProject                    # 顶层项目容器
├── requirement: LoopRequirement  # 结构化需求 (goal, criteria, constraints)
├── iterations: list[IterationRecord]  # 迭代历史
├── criteria_tracker: CriteriaTracker  # 验收标准追踪
└── status: LoopProjectStatus

IterationRecord               # 单轮迭代记录
├── iteration: int
├── role: Optional[LoopRole]
├── focus: str                # 本轮工作重点（从输出提取）
├── output: str               # Agent 完整输出
├── duration: float
├── criteria_progress: dict[int, bool]
├── review_result: Optional[ReviewResult]
└── status: IterationStatus

CriteriaTracker               # 验收标准追踪器
├── criteria: list[str]       # 标准列表
├── satisfied: dict[int, bool]  # 每条标准的满足状态
├── satisfied_at_iteration: dict[int, int]  # 首次满足的迭代号
└── is_all_satisfied: bool    # 是否全部满足

ReviewResult                  # 多视角审查结果
├── reviews: list[PerspectiveReview]  # 四个视角的审查
├── all_passed: bool
├── total_suggestions: int
└── failed_perspectives: list[PerspectiveReview]
```

**LoopContextManager** — 线程安全的上下文管理器，使用三级压缩策略（远期 1-line / 近期 brief / 最新 full）管理迭代历史。

### 3.2 engine.py — 核心引擎

**LoopEngine** — 核心执行引擎，一个 chat 一个实例：

| 方法 | 说明 |
|------|------|
| `execute(text, callbacks)` | 主入口：解析需求 → 创建 session → 迭代循环 |
| `resume(callbacks)` | 恢复暂停的执行 |
| `stop()` / `pause()` | 终止/暂停 |
| `inject_guidance(msg)` | 注入用户引导（下轮 prompt 消费） |
| `save_state(path)` | 持久化项目状态 |

核心私有方法：

| 方法 | 说明 |
|------|------|
| `_parse_requirement(text)` | 需求解析：列表标记 → LLM 拆解 → 兜底 |
| `_decompose_criteria_with_llm(text)` | LLM 将口语化需求拆解为验收标准 |
| `_build_initial_prompt(req)` | 构建首轮完整 prompt |
| `_build_iteration_prompt(n, req)` | 构建后续迭代 prompt（含进度/引导/审查反馈） |
| `_evaluate_criteria(criteria, n)` | 在 ACP session 中评估验收标准（CRITERIA_N: PASS/FAIL） |
| `_conduct_review(n, cb)` | 多视角审查（发 prompt → 解析四视角结果） |
| `_parse_review_output(text, n)` | 三级解析：正则快速 → 容错正则 → LLM 兜底 |
| `_detect_convergence()` | 收敛检测（连续 N 轮输出极短） |

**LoopEngineManager** — per-chat 引擎管理器（线程安全，二级索引加速查找）。

**LoopEngineCallbacks** — 事件回调接口：

```python
@dataclass
class LoopEngineCallbacks:
    on_analyzing_start: Callable[[str], None]              # 需求解析开始
    on_analyzing_done: Callable[[LoopProject], None]       # 需求解析完成
    on_iteration_start: Callable[[int, int], None]         # 迭代开始 (current, max)
    on_iteration_event: Callable[[int, ACPEvent], None]    # ACP 事件流
    on_iteration_done: Callable[[int, IterationRecord], None]  # 迭代完成
    on_review_done: Callable[[int, ReviewResult], None]    # 审查完成
    on_project_done: Callable[[LoopProject], None]         # 项目完成
    on_error: Callable[[str], None]                        # 错误
```

### 3.3 tracker.py — ACP 事件追踪

`IterationTracker` 处理单轮迭代的 ACP 事件：

```python
class IterationTracker:
    text_buffer: str           # 累积的文本输出
    tool_calls: list[dict]     # 工具调用记录
    modified_files: set[str]   # 修改的文件
    plan_entries: list[dict]   # 计划条目
```

### 3.4 reporter.py — 报告格式化

`LoopReporter` 将 LoopProject 格式化为飞书 Markdown：
- 迭代进度（当前轮 / 最大轮）
- 验收标准状态（每条标准的 PASS/FAIL + 首次满足轮次）
- 迭代历史摘要
- 审查结果展示

---

## 4. 多视角审查（Ralph Loop）

### 4.1 设计动机

Agent 自评"全部标准满足"可能存在盲区 —— 功能实现了但代码质量差、缺少错误处理、用户体验不佳等。Ralph Loop 在功能迭代完成后追加多视角审查，发现 Agent 自身忽略的问题。

### 4.2 四个审查视角

| 视角 | 关注点 |
|------|--------|
| **架构师** (ARCHITECT) | 代码结构、设计模式、可维护性、性能、安全性 |
| **产品经理** (PRODUCT) | 需求完整度、用户价值、边界场景、功能一致性 |
| **用户** (USER) | 易用性、文档、错误提示、交互体验、可理解性 |
| **测试** (TESTER) | 测试覆盖、边界条件、异常处理、回归风险、可测试性 |

### 4.3 执行机制

1. 每轮迭代执行完成后，在同一个 ACP session 中发送审查 prompt
2. Agent 从四个视角审查当前实现，输出 PASS/FAIL + 改进建议
3. 解析审查输出（三级容错）
4. 如果有 FAIL 视角，审查建议注入下轮迭代 prompt
5. 验收标准满足后，最多追加 `loop_review_extra_iterations`（默认 3）轮审查修复迭代

### 4.4 审查输出解析（三级容错）

```
Level 1: 正则快速匹配
  [ARCHITECT]\nPASS\n... → 直接提取

Level 2: 容错正则
  支持 5 种英文格式 + 中文表头：
  - [ARCHITECT]: PASS
  - **[ARCHITECT]** FAIL
  - **ARCHITECT**: PASS
  - ### ARCHITECT PASS
  - ARCHITECT: PASS
  - 架构师: PASS / 🏗️ 架构师 PASS

Level 3: LLM 兜底
  正则全部失败 → 发送给 LLM 提取结构化 JSON
  → PerspectiveReview 列表
```

### 4.5 配置

```env
LOOP_REVIEW_ENABLED=true           # 启用/禁用审查（默认启用）
LOOP_REVIEW_EXTRA_ITERATIONS=3     # 审查修复额外迭代上限
```

---

## 5. 终止策略

### 5.1 终止条件（优先级从高到低）

| 优先级 | 条件 | 信号 | 说明 |
|--------|------|------|------|
| 1 | 用户 `/stop_loop` | USER_STOP | 当前迭代完成后停止 |
| 2 | 迭代 >= max_iterations | MAX_ITER | 默认 100 轮 |
| 3 | 全部标准满足 + 审查通过 | COMPLETE | 正常完成 |
| 4 | 标准满足但审查修复超限 | COMPLETE | 审查额外迭代 > 3 |
| 5 | 收敛检测触发 | CONVERGED | 连续 N 轮极短输出 |
| 6 | 默认 | CONTINUE | 继续下一轮 |

### 5.2 收敛检测

当最近 `loop_convergence_window`（默认 3）轮迭代的输出长度都小于 50 字符时，判定为收敛（Agent 已无法产生有效工作）。

### 5.3 验收标准评估

每轮迭代后在同一 ACP session 中发送评估 prompt，要求 Agent 对每条标准输出 `CRITERIA_N: PASS/FAIL`。使用预编译正则（支持 100 条标准）解析结果，更新 `CriteriaTracker`。

标准满足是累积的 —— 一旦某条标准在某轮被标记为 PASS，后续不会回退。

---

## 6. 状态机

```
                  /loop <需求>
                       │
                       ▼
                 ┌───────────┐
                 │   IDLE    │
                 └─────┬─────┘
                       │ _parse_requirement()
                       ▼
                 ┌───────────┐
                 │ ANALYZING │── 解析失败 ──→ ABORTED
                 └─────┬─────┘
                       │ 解析成功
                       ▼
                 ┌───────────┐
            ┌───│  RUNNING   │◄──────────────────────┐
            │   └──┬──┬──┬──┘                        │
            │      │  │  │                           │
 /stop_loop │      │  │  │  迭代 + 评估               │
            │      │  │  │                           │
            │      │  │  ▼                           │
            │      │  │ CONTINUE ────────────────────┘
            │      │  │ COMPLETE ──→ COMPLETED
            │      │  │ CONVERGED ─→ COMPLETED
            │      │  │ MAX_ITER ──→ COMPLETED
            │      │  │
            │      │  │ /loop_pause
            │      │  ▼
            │      │ ┌───────────┐
            │      │ │  PAUSED   │──/loop_resume──→ RUNNING
            │      │ └───────────┘
            │      │
            ▼      │ Exception
       ┌─────────┐ │
       │ PAUSED  │ ▼
       └─────────┘┌───────────┐
                  │ ABORTED   │
                  └───────────┘
```

---

## 7. 用户命令

| 命令 | 说明 | 状态要求 |
|------|------|----------|
| `/loop <需求>` | 启动 Loop 迭代开发 | IDLE |
| `/loop_status` | 查看迭代进度和验收标准 | 任意 |
| `/loop_guide <引导>` | 注入方向引导（下轮消费） | RUNNING |
| `/loop_pause` | 暂停迭代 | RUNNING |
| `/loop_resume` | 恢复迭代 | PAUSED |
| `/stop_loop` | 停止 Loop | RUNNING/PAUSED |

---

## 8. 与 ACP 协议的集成

### 8.1 Session 管理

Loop Engine 通过 `create_engine_session(agent_type, cwd)` 创建 ACP session，支持 Coco 和 Claude 两种后端。单个 Loop 执行全程使用同一个 ACP session（多轮 prompt），保持上下文连续性。

### 8.2 事件流

ACP session 产生的事件流通过 `IterationTracker` 和 `ACPEventRenderer` 双重处理：

```python
def on_event(event: ACPEvent):
    iter_tracker.process(event)       # 追踪工具/计划/文件
    self._renderer.process_event(event)  # 渲染 Markdown
    callbacks.on_iteration_event(iteration, event)  # 转发给 Handler → 卡片更新
```

### 8.3 多轮 Prompt 策略

| 轮次 | Prompt 内容 |
|------|-------------|
| 第 1 轮 | 完整需求 + 验收标准 + 工作目录 + 审查说明 |
| 第 N 轮 | 验收标准进度（已满足/未满足）+ 用户引导 + 上轮审查反馈 |
| 审查轮 | 四视角审查 prompt → PASS/FAIL + 建议 |
| 评估轮 | 逐条验收标准评估 → CRITERIA_N: PASS/FAIL |

---

## 9. 配置项

| 配置 | 默认值 | 说明 |
|------|--------|------|
| `LOOP_MAX_ITERATIONS` | 100 | 最大迭代次数 |
| `LOOP_EXECUTION_TIMEOUT` | 7200 | 单轮执行超时（秒） |
| `LOOP_CONVERGENCE_WINDOW` | 3 | 收敛检测窗口大小 |
| `LOOP_MAX_CONTEXT_TOKENS` | 8000 | 上下文 token 预算 |
| `LOOP_DEFAULT_MAX_RETRIES` | 2 | 单轮失败重试次数 |
| `LOOP_REVIEW_ENABLED` | true | 启用多视角审查 |
| `LOOP_REVIEW_EXTRA_ITERATIONS` | 3 | 审查修复额外迭代上限 |

---

## 10. 测试

Loop Engine 的测试位于 `tests/test_loop_engine.py`，覆盖：

- 需求解析（显式列表 / LLM 拆解 / 兜底）
- 迭代执行（正常完成 / 收敛终止 / 用户停止）
- 验收标准评估（PASS/FAIL 解析 / 累积更新）
- 多视角审查（正则解析 / 容错解析 / LLM 兜底 / 中文支持）
- 暂停/恢复
- LoopEngineManager 并发管理
- CriteriaTracker 边界条件
- LoopContextManager 三级压缩
