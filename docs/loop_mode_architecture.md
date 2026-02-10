# Loop Mode 架构设计文档

## 1. 概述

### 1.1 什么是 Loop Mode

Loop Mode 是 GhostAP 的**迭代式自主开发模式**。与 Deep Engine 的"一次规划、顺序执行"不同，Loop Mode 采用**感知-思考-行动-评估**的闭环迭代策略，在每轮迭代中动态决策下一步操作，直到产品诉求被完整满足或触发终止条件。

### 1.2 与现有模块的定位关系

```
┌─────────────────────────────────────────────────────────────┐
│                       GhostAP 模式层级                       │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  交互模式 (InteractionMode)      编排模式 (Orchestration)    │
│  ┌───────┐ ┌────────┐           ┌───────────┐              │
│  │ SMART │ │ SHELL  │           │Deep Engine │              │
│  └───────┘ └────────┘           │(一次规划)  │              │
│  ┌───────┐ ┌────────┐           └───────────┘              │
│  │ COCO  │ │ CLAUDE │           ┌───────────┐              │
│  └───────┘ └────────┘           │Loop Engine │ ← NEW       │
│                                 │(迭代闭环)  │              │
│                                 └───────────┘              │
│                                                             │
│  交互模式: 用户驱动, 每条消息独立处理                         │
│  编排模式: 系统驱动, 自主完成复杂任务                         │
└─────────────────────────────────────────────────────────────┘
```

**关键设计决策**: Loop Mode 不是一个新的 `InteractionMode`，而是与 Deep Engine 平级的**编排引擎**。它复用现有的 AI 后端（Coco/Claude），通过迭代闭环实现更智能的任务执行。

### 1.3 Deep Engine vs Loop Engine 对比

| 维度 | Deep Engine | Loop Engine |
|------|------------|-------------|
| 执行策略 | 预规划 → 顺序执行 | 迭代闭环 → 动态决策 |
| 任务分解 | 一次性 LLM 规划 | 每轮迭代动态评估 |
| 上下文利用 | 仅在 adapt 时被动使用 | 每轮主动评估+累积 |
| 错误处理 | replan 重试 (max 3次) | 自适应策略调整 |
| 适用场景 | 明确可拆分的多步任务 | 模糊/探索性/需反复验证的需求 |
| 用户干预 | `/deep_update` 注入上下文 | `/loop_guide` 引导方向 |
| 终止条件 | 所有任务完成或失败 | 质量评估通过 or 收敛判定 |

---

## 2. 整体架构

### 2.1 架构总览

```
                              ┌──────────────────────┐
                              │     用户 (Feishu)     │
                              │  /loop <产品诉求>     │
                              └──────────┬───────────┘
                                         │
                              ┌──────────▼───────────┐
                              │    LoopHandler        │
                              │  (命令路由 & UI回调)   │
                              └──────────┬───────────┘
                                         │
                    ┌────────────────────▼────────────────────┐
                    │              LoopEngine                  │
                    │         (迭代编排器 - 核心)               │
                    │                                         │
                    │  ┌─────────────────────────────────┐    │
                    │  │        迭代主循环                  │    │
                    │  │  while state == RUNNING:         │    │
                    │  │    ① 感知: 评估当前状态            │    │
                    │  │    ② 思考: 决策下一步行动          │    │
                    │  │    ③ 行动: 执行任务               │    │
                    │  │    ④ 评估: 判断是否完成            │    │
                    │  └─────────┬───────────────────────┘    │
                    │            │ 调用                        │
                    │  ┌─────────▼───────────────────────┐    │
                    │  │     七大核心模块                   │    │
                    │  │                                   │    │
                    │  │  ┌──────────┐  ┌──────────────┐  │    │
                    │  │  │需求解析器 │  │  角色分配器    │  │    │
                    │  │  │(Parser)  │  │  (RoleRouter) │  │    │
                    │  │  └──────────┘  └──────────────┘  │    │
                    │  │  ┌──────────┐  ┌──────────────┐  │    │
                    │  │  │任务管理器 │  │  上下文管理器  │  │    │
                    │  │  │(TaskMgr) │  │  (ContextMgr) │  │    │
                    │  │  └──────────┘  └──────────────┘  │    │
                    │  │  ┌──────────┐  ┌──────────────┐  │    │
                    │  │  │终止判定器 │  │  工具适配器    │  │    │
                    │  │  │(TermChk) │  │  (ToolAdapt)  │  │    │
                    │  │  └──────────┘  └──────────────┘  │    │
                    │  │  ┌──────────┐                    │    │
                    │  │  │循环控制器 │                    │    │
                    │  │  │(LoopCtrl)│                    │    │
                    │  │  └──────────┘                    │    │
                    │  └───────────────────────────────────┘    │
                    └──────────────────┬──────────────────────┘
                                       │
                          ┌────────────▼────────────┐
                          │    AI Session Layer      │
                          │  ┌──────┐  ┌─────────┐  │
                          │  │ Coco │  │ Claude  │  │
                          │  └──────┘  └─────────┘  │
                          └─────────────────────────┘
```

### 2.2 数据流详图

```
用户: /loop 实现用户登录注册功能，支持邮箱和手机号

    │
    ▼
┌───────────────────────────────────────────────────────────────┐
│ Phase 1: 需求解析 (RequirementAnalyzer)                        │
│                                                               │
│  输入: 原始文本                                                │
│  输出: LoopRequirement {                                      │
│    goal: "实现用户登录注册功能"                                  │
│    acceptance_criteria: [                                      │
│      "支持邮箱注册登录",                                        │
│      "支持手机号注册登录",                                      │
│      "包含输入验证",                                           │
│      "包含错误处理"                                            │
│    ]                                                          │
│    constraints: ["使用现有技术栈"]                               │
│    estimated_iterations: 4-6                                   │
│  }                                                            │
└───────────────────┬───────────────────────────────────────────┘
                    │
                    ▼
┌───────────────────────────────────────────────────────────────┐
│ Phase 2: 角色分配 (RoleRouter)                                 │
│                                                               │
│  分析需求 → 确定当前迭代需要的角色:                               │
│                                                               │
│  Iteration 1: ARCHITECT (架构设计)                             │
│    → "设计用户认证模块的整体架构和数据模型"                       │
│  Iteration 2: DEVELOPER (核心实现)                             │
│    → "实现邮箱注册登录的核心逻辑"                                │
│  Iteration 3: DEVELOPER (扩展实现)                             │
│    → "实现手机号注册登录功能"                                    │
│  Iteration 4: REVIEWER (代码审查)                              │
│    → "审查代码质量、安全性、边界情况"                             │
│  Iteration 5: TESTER (验证测试)                                │
│    → "编写和运行测试用例"                                       │
└───────────────────┬───────────────────────────────────────────┘
                    │
                    ▼
┌───────────────────────────────────────────────────────────────┐
│ Phase 3: 迭代执行循环                                          │
│                                                               │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │ Iteration N:                                             │  │
│  │                                                         │  │
│  │  ① LoopController.assess_state()                        │  │
│  │     → 收集: 已完成工作、当前代码状态、未满足的验收标准      │  │
│  │                                                         │  │
│  │  ② RoleRouter.select_role(state)                        │  │
│  │     → 根据当前进度选择角色 (ARCHITECT/DEVELOPER/...)      │  │
│  │                                                         │  │
│  │  ③ LoopController.build_iteration_prompt(role, state)   │  │
│  │     → 构建带角色视角的执行 prompt                         │  │
│  │                                                         │  │
│  │  ④ ToolAdapter.execute(prompt)                          │  │
│  │     → 通过 Coco/Claude session 执行                     │  │
│  │     → 流式输出 → UI 实时更新                              │  │
│  │                                                         │  │
│  │  ⑤ ContextManager.record(iteration_result)              │  │
│  │     → 记录本轮结果、代码变更、发现的问题                   │  │
│  │                                                         │  │
│  │  ⑥ TerminationChecker.evaluate(state, context)          │  │
│  │     → 检查: 验收标准满足? 收敛? 超限?                     │  │
│  │     → 决定: CONTINUE / COMPLETE / ABORT                  │  │
│  └─────────────────────────────────────────────────────────┘  │
│                                                               │
│  重复直到 TerminationChecker 返回非 CONTINUE                   │
└───────────────────────────────────────────────────────────────┘
```

---

## 3. 核心模块详细设计

### 3.1 循环控制模块 (LoopController)

**职责**: 编排迭代流程，组装每轮迭代的 prompt，协调各模块

```
┌─────────────────────────────────────────────────────┐
│                  LoopController                      │
├─────────────────────────────────────────────────────┤
│                                                     │
│  状态机:                                             │
│  ┌──────┐  plan()  ┌─────────┐  execute() ┌───────┐│
│  │ IDLE │────────→│ PLANNED │──────────→│RUNNING││
│  └──────┘         └─────────┘           └───┬───┘│
│      ▲                                      │     │
│      │         ┌──────────┐   stop()   ┌────▼───┐│
│      │         │COMPLETED │←──────────│STOPPING││
│      │         └──────────┘            └────────┘│
│      │         ┌──────────┐                      │
│      └─────────│ ABORTED  │◄── (max iter/fatal)  │
│                └──────────┘                      │
│                                                     │
│  每轮迭代编排:                                       │
│  ┌───────────┐   ┌──────────┐   ┌───────────┐     │
│  │assess_    │──→│ select_  │──→│ build_    │     │
│  │state()    │   │ role()   │   │ prompt()  │     │
│  └───────────┘   └──────────┘   └─────┬─────┘     │
│                                       │            │
│  ┌───────────┐   ┌──────────┐   ┌─────▼─────┐     │
│  │terminate_ │◄──│ record() │◄──│ execute() │     │
│  │check()    │   └──────────┘   └───────────┘     │
│  └───────────┘                                     │
└─────────────────────────────────────────────────────┘
```

**核心接口**:

```python
class LoopController:
    """迭代主循环编排器"""

    def assess_state(self) -> IterationState:
        """评估当前迭代状态: 已完成工作、代码变更、未满足标准"""

    def build_iteration_prompt(self, role: LoopRole, state: IterationState) -> str:
        """构建本轮迭代的 prompt (角色视角 + 上下文 + 目标)"""

    def run_iteration(self, callbacks: LoopCallbacks) -> IterationResult:
        """执行单轮迭代: assess → select_role → build_prompt → execute → record"""

    def run_loop(self, callbacks: LoopCallbacks) -> LoopProject:
        """主循环: while RUNNING { run_iteration(); terminate_check(); }"""
```

**状态评估 prompt 模板**:

```
你是一个迭代式开发助手。当前是第 {iteration} 轮迭代。

## 产品目标
{requirement.goal}

## 验收标准
{numbered_acceptance_criteria}

## 已完成的工作
{context_summary}  // 从 ContextManager 获取

## 当前已满足的标准
{satisfied_criteria}

## 尚未满足的标准
{unsatisfied_criteria}

## 你的角色: {role.display_name}
{role.system_prompt}

## 本轮任务
请以 {role.display_name} 的视角，针对尚未满足的标准，执行下一步最有价值的工作。
完成后输出 DEEP_TASK_SUCCESS。
```

### 3.2 产品诉求解析模块 (RequirementAnalyzer)

**职责**: 将模糊的用户需求转化为结构化的可验证诉求

```
┌─────────────────────────────────────────────────────┐
│                RequirementAnalyzer                    │
├─────────────────────────────────────────────────────┤
│                                                     │
│  输入: "实现用户登录注册功能，支持邮箱和手机号"        │
│         (模糊的自然语言)                              │
│                                                     │
│            ┌──────────────┐                          │
│            │  LLM 解析    │                          │
│            │  (ARK/Claude) │                          │
│            └──────┬───────┘                          │
│                   │                                  │
│            ┌──────▼───────┐                          │
│  输出:     │LoopRequirement│                          │
│            ├──────────────┤                          │
│            │goal          │ "实现用户登录注册"        │
│            │criteria[]    │ 可验证的验收标准列表       │
│            │constraints[] │ 技术/业务约束             │
│            │context       │ 代码库上下文摘要          │
│            │est_iterations│ 预估迭代次数              │
│            └──────────────┘                          │
│                                                     │
│  验收标准生成规则:                                    │
│  - 每条标准必须是可验证的 (能通过代码/测试判断)        │
│  - 标准粒度适中 (不过细也不过粗)                      │
│  - 覆盖功能、质量、安全三个维度                       │
│                                                     │
│  示例输出:                                           │
│  criteria:                                           │
│    ✓ "邮箱注册: 验证格式、去重、创建账号"              │
│    ✓ "手机号注册: 验证格式、去重、创建账号"            │
│    ✓ "登录: 支持邮箱/手机号+密码登录"                 │
│    ✓ "输入验证: 所有用户输入有前后端验证"              │
│    ✓ "错误处理: 覆盖常见错误场景"                     │
│    ✓ "单元测试: 核心逻辑有测试覆盖"                   │
└─────────────────────────────────────────────────────┘
```

**核心接口**:

```python
@dataclass
class LoopRequirement:
    """结构化的产品诉求"""
    goal: str                           # 核心目标
    acceptance_criteria: list[str]      # 可验证的验收标准
    constraints: list[str]              # 约束条件
    context_summary: str                # 代码库上下文
    estimated_iterations: int           # 预估迭代次数
    raw_text: str                       # 原始用户输入

class RequirementAnalyzer:
    """产品诉求解析器"""

    def analyze(self, text: str, cwd: str) -> LoopRequirement:
        """解析用户需求为结构化诉求 (LLM + 代码库扫描)"""

    def refine_criteria(self, requirement: LoopRequirement, feedback: str) -> LoopRequirement:
        """根据用户反馈精化验收标准"""
```

### 3.3 角色拆分模块 (RoleRouter)

**职责**: 根据当前迭代状态，动态选择最适合的执行角色

```
┌─────────────────────────────────────────────────────────────┐
│                        RoleRouter                            │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  预定义角色:                                                 │
│  ┌───────────────────────────────────────────────────────┐  │
│  │ ARCHITECT  │ 架构师 │ 系统设计、模块划分、接口定义      │  │
│  │ DEVELOPER  │ 开发者 │ 功能实现、代码编写               │  │
│  │ REVIEWER   │ 审查者 │ 代码审查、质量检查、安全审计      │  │
│  │ TESTER     │ 测试者 │ 编写测试、运行验证               │  │
│  │ DEBUGGER   │ 调试者 │ 问题诊断、Bug修复               │  │
│  │ INTEGRATOR │ 集成者 │ 模块集成、冲突解决、最终验证      │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                             │
│  选择策略:                                                   │
│                                                             │
│  ┌───────────┐     ┌──────────────────────┐                │
│  │当前状态    │────→│ 策略矩阵              │                │
│  │- 迭代轮次  │     │                      │                │
│  │- 完成进度  │     │ 第1轮: ARCHITECT      │                │
│  │- 上轮结果  │     │ 中间轮: DEVELOPER     │                │
│  │- 失败次数  │     │ 上轮失败: DEBUGGER    │                │
│  │- 未满足标准│     │ 功能完成: REVIEWER    │                │
│  └───────────┘     │ 审查通过: TESTER      │                │
│                     │ 测试通过: INTEGRATOR  │                │
│                     │ 集成完成: → 终止      │                │
│                     └──────────────────────┘                │
│                                                             │
│  LLM 辅助选择 (复杂场景):                                    │
│  当策略矩阵无法确定时, 用 LLM 分析上下文选择角色              │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

**核心接口**:

```python
class LoopRole(Enum):
    ARCHITECT  = "architect"
    DEVELOPER  = "developer"
    REVIEWER   = "reviewer"
    TESTER     = "tester"
    DEBUGGER   = "debugger"
    INTEGRATOR = "integrator"

@dataclass
class RoleSelection:
    role: LoopRole
    reason: str              # 选择理由
    focus: str               # 本轮关注点
    system_prompt: str       # 角色专属 system prompt

class RoleRouter:
    """动态角色选择器"""

    def select_role(self, state: IterationState) -> RoleSelection:
        """根据迭代状态选择角色 (规则优先, LLM兜底)"""

    def get_role_prompt(self, role: LoopRole) -> str:
        """获取角色的 system prompt 模板"""
```

**角色选择规则 (优先级从高到低)**:

| 优先级 | 条件 | 选择角色 | 理由 |
|--------|------|----------|------|
| 1 | 首轮迭代 | ARCHITECT | 先设计再编码 |
| 2 | 上轮执行失败 (连续≥2次) | DEBUGGER | 需要专注诊断问题 |
| 3 | 所有功能标准已满足 & 无测试 | TESTER | 功能完成，需要验证 |
| 4 | 测试通过 & 有未集成模块 | INTEGRATOR | 模块完成，需要集成 |
| 5 | 代码量较大 & 无审查记录 | REVIEWER | 质量关口 |
| 6 | 默认 | DEVELOPER | 持续推进功能实现 |

### 3.4 任务管理模块 (LoopTaskManager)

**职责**: 跟踪每轮迭代的状态、结果和验收标准的满足情况

```
┌──────────────────────────────────────────────────────────────┐
│                     LoopTaskManager                           │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│  LoopProject (顶层)                                          │
│  ├── requirement: LoopRequirement                            │
│  ├── status: LoopProjectStatus                               │
│  ├── iterations: list[IterationRecord]                       │
│  └── criteria_tracker: CriteriaTracker                       │
│                                                              │
│  IterationRecord (每轮迭代)                                   │
│  ├── iteration_id: int                                       │
│  ├── role: LoopRole                                          │
│  ├── status: IterationStatus (RUNNING/SUCCESS/FAILED)        │
│  ├── prompt: str                                             │
│  ├── output: str                                             │
│  ├── duration: float                                         │
│  ├── criteria_progress: dict[int, bool]                      │
│  └── summary: str  (LLM生成的本轮摘要)                       │
│                                                              │
│  CriteriaTracker (验收标准追踪)                               │
│  ┌────┬─────────────────────────────────┬────────┬────────┐  │
│  │ ID │ 标准                            │ 状态   │ 满足轮次│  │
│  ├────┼─────────────────────────────────┼────────┼────────┤  │
│  │ 1  │ 邮箱注册: 验证格式、去重、创建   │ ✅ 满足 │ #3     │  │
│  │ 2  │ 手机号注册: 验证格式、去重、创建 │ ✅ 满足 │ #4     │  │
│  │ 3  │ 登录: 支持邮箱/手机号+密码       │ 🔲 未满足│  -    │  │
│  │ 4  │ 输入验证: 前后端验证             │ 🔲 未满足│  -    │  │
│  │ 5  │ 错误处理: 覆盖常见错误场景       │ 🔲 未满足│  -    │  │
│  │ 6  │ 单元测试: 核心逻辑有测试覆盖     │ 🔲 未满足│  -    │  │
│  └────┴─────────────────────────────────┴────────┴────────┘  │
│                                                              │
│  进度可视化:                                                  │
│  标准完成: [████░░░░░░] 33% (2/6)                            │
│  迭代进度: 第4轮 / 预估6轮                                    │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

**核心接口**:

```python
class LoopProjectStatus(Enum):
    IDLE      = "idle"
    ANALYZING = "analyzing"    # 需求解析中
    RUNNING   = "running"      # 迭代执行中
    PAUSED    = "paused"       # 用户暂停
    COMPLETED = "completed"    # 全部标准满足
    ABORTED   = "aborted"     # 触发终止条件

@dataclass
class IterationRecord:
    iteration_id: int
    role: LoopRole
    status: IterationStatus
    prompt: str
    output: str
    duration: float
    criteria_progress: dict[int, bool]   # {criteria_id: satisfied}
    summary: str

@dataclass
class LoopProject:
    project_id: str
    requirement: LoopRequirement
    status: LoopProjectStatus
    iterations: list[IterationRecord]
    criteria_tracker: CriteriaTracker

    @property
    def current_iteration(self) -> int
    @property
    def satisfied_count(self) -> int
    @property
    def total_criteria(self) -> int
    @property
    def is_all_satisfied(self) -> bool

class LoopTaskManager:
    def create_project(self, requirement: LoopRequirement) -> LoopProject
    def start_iteration(self, role: LoopRole) -> IterationRecord
    def complete_iteration(self, output: str, criteria_progress: dict) -> None
    def fail_iteration(self, error: str) -> None
    def update_criteria(self, criteria_id: int, satisfied: bool) -> None
    def get_progress_summary(self) -> str
```

### 3.5 上下文管理模块 (LoopContextManager)

**职责**: 累积迭代历史，构建高质量的上下文 prompt，管理上下文窗口

```
┌──────────────────────────────────────────────────────────────┐
│                    LoopContextManager                         │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│  上下文层级:                                                  │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ L1: 需求上下文 (常驻)                                   │  │
│  │   goal, criteria, constraints — 每轮都携带              │  │
│  ├────────────────────────────────────────────────────────┤  │
│  │ L2: 累积上下文 (滑动窗口)                               │  │
│  │   最近 N 轮迭代的摘要 — 控制 token 用量                 │  │
│  ├────────────────────────────────────────────────────────┤  │
│  │ L3: 当前上下文 (单轮)                                   │  │
│  │   上一轮的完整输出 — 仅保留最新一轮                      │  │
│  ├────────────────────────────────────────────────────────┤  │
│  │ L4: 用户注入 (临时)                                     │  │
│  │   /loop_guide 注入的引导信息 — 使用后消费               │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                              │
│  上下文压缩策略:                                              │
│                                                              │
│  ┌─────┐  ┌─────┐  ┌─────┐  ┌─────┐  ┌─────┐              │
│  │ #1  │  │ #2  │  │ #3  │  │ #4  │  │ #5  │  ← 迭代轮次  │
│  │full │  │full │  │full │  │full │  │full │              │
│  └──┬──┘  └──┬──┘  └──┬──┘  └──┬──┘  └──┬──┘              │
│     │        │        │        │        │                    │
│     ▼        ▼        ▼        ▼        ▼                    │
│  ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐ ┌──────────┐          │
│  │1-line │ │1-line│ │brief │ │brief │ │ 完整输出  │          │
│  │摘要   │ │摘要  │ │摘要  │ │摘要  │ │ (L3)     │          │
│  └──────┘ └──────┘ └──────┘ └──────┘ └──────────┘          │
│  ^^^^^^^^^^^^^^^^   ^^^^^^^^^^^^^^^   ^^^^^^^^^^^^^          │
│  远期 (1-line)      近期 (brief)      最新 (full)            │
│                                                              │
│  Token 预算: 总 prompt ≤ max_context_tokens                  │
│  分配: L1 (10%) + L2 (30%) + L3 (50%) + L4 (10%)            │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

**核心接口**:

```python
class LoopContextManager:
    """迭代上下文管理器 (线程安全)"""

    def __init__(self, max_context_tokens: int = 8000):
        self._lock = threading.Lock()
        self._iterations: list[ContextEntry] = []
        self._user_injections: list[str] = []
        self._max_tokens = max_context_tokens

    def record_iteration(self, record: IterationRecord) -> None:
        """记录一轮迭代结果 (自动生成摘要)"""

    def inject_user_guidance(self, message: str) -> None:
        """注入用户引导信息 (/loop_guide)"""

    def build_context_prompt(self, level: str = "full") -> str:
        """构建上下文 prompt (自动压缩到 token 预算内)"""

    def get_iteration_summaries(self) -> list[str]:
        """获取所有迭代摘要 (用于终止判断)"""

    def has_user_guidance(self) -> bool:
        """是否有未消费的用户引导"""

    def consume_user_guidance(self) -> str:
        """消费并返回用户引导信息"""
```

**摘要生成 prompt**:

```
请用1-2句话总结以下迭代的工作成果:

角色: {role}
输出:
{output}

要求:
- 只描述实际完成的工作
- 提及关键文件/函数/模块名
- 如果失败，说明失败原因
```

### 3.6 多工具兼容模块 (ToolAdapter)

**职责**: 抽象 AI 后端差异，提供统一的执行接口

```
┌──────────────────────────────────────────────────────────────┐
│                       ToolAdapter                             │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│  统一接口:                                                    │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ execute(prompt, on_chunk, timeout, should_stop)         │  │
│  │   → str (output)                                       │  │
│  │                                                        │  │
│  │ check_success(output) → bool                           │  │
│  │ extract_error(output) → str                            │  │
│  │ get_engine_name() → str                                │  │
│  └────────────────────────────────────────────────────────┘  │
│                       │                                      │
│           ┌───────────┴───────────┐                          │
│           ▼                       ▼                          │
│  ┌─────────────────┐    ┌─────────────────┐                  │
│  │  CocoAdapter     │    │  ClaudeAdapter   │                  │
│  │                 │    │                 │                  │
│  │  session:       │    │  session:       │                  │
│  │   CocoSession   │    │   ClaudeSession │                  │
│  │                 │    │                 │                  │
│  │  特性:          │    │  特性:          │                  │
│  │  - ARK API      │    │  - Claude CLI   │                  │
│  │  - 无 session   │    │  - UUID session │                  │
│  │    过期问题     │    │  - 30min idle   │                  │
│  │  - 中文优化     │    │    timeout      │                  │
│  │               │    │  - 自动重连     │                  │
│  └─────────────────┘    └─────────────────┘                  │
│                                                              │
│  复用策略:                                                    │
│  - 直接使用现有 BaseSession 子类                              │
│  - 通过 BaseSessionManager 管理生命周期                       │
│  - session_id 格式: "loop-{chat_id}-{project_id}"            │
│  - 与 Coco/Claude 交互模式共享同一 session pool              │
│                                                              │
│  执行模式:                                                    │
│  ┌──────────────────────────────────────────────────────┐    │
│  │ 默认: streaming (实时输出到 Feishu 卡片)              │    │
│  │ 降级: blocking  (streaming 不可用时的回退)             │    │
│  └──────────────────────────────────────────────────────┘    │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

**核心接口**:

```python
class ToolAdapter:
    """AI 后端统一适配器"""

    def __init__(self, session: BaseSession, cwd: str):
        self._session = session
        self._cwd = cwd
        self._settings = get_settings()

    def execute(
        self,
        prompt: str,
        on_chunk: Optional[Callable[[str], None]] = None,
        timeout: Optional[int] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> ExecutionResult:
        """执行 prompt, 返回结果 (复用 Deep Engine 的 ExecutionResult)"""

    def check_success(self, output: str) -> bool:
        """复用 TaskExecutor._check_success 的启发式成功检测"""

    @property
    def engine_name(self) -> str:
        """返回 'Coco' 或 'Claude'"""
```

**设计决策**: `ToolAdapter` 与 Deep Engine 的 `TaskExecutor` 几乎等价，但有一个关键区别——`ToolAdapter` 不绑定 `DeepTask`，直接接受原始 prompt，因此可以在迭代循环中灵活使用。

### 3.7 终止判断模块 (TerminationChecker)

**职责**: 在每轮迭代后评估是否应该继续、完成或中止

```
┌──────────────────────────────────────────────────────────────┐
│                    TerminationChecker                         │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│  终止信号类型:                                                │
│  ┌──────────────────────────────────────────────────────┐    │
│  │ CONTINUE  │ 继续迭代 │ 仍有未满足的验收标准           │    │
│  │ COMPLETE  │ 正常完成 │ 所有验收标准已满足              │    │
│  │ CONVERGED │ 收敛终止 │ 连续N轮无新进展                │    │
│  │ MAX_ITER  │ 超限终止 │ 达到最大迭代次数               │    │
│  │ FATAL     │ 致命终止 │ 不可恢复的错误                 │    │
│  │ USER_STOP │ 用户终止 │ 用户主动停止                   │    │
│  └──────────────────────────────────────────────────────┘    │
│                                                              │
│  评估流程 (优先级从高到低):                                   │
│                                                              │
│  ┌─────────────────────────────────────────────────────┐     │
│  │ 1. 用户终止?  → USER_STOP                           │     │
│  │    (run_state == STOPPING)                          │     │
│  ├─────────────────────────────────────────────────────┤     │
│  │ 2. 致命错误?  → FATAL                               │     │
│  │    (连续 3 次同类错误 / session 不可用)               │     │
│  ├─────────────────────────────────────────────────────┤     │
│  │ 3. 超过最大迭代? → MAX_ITER                          │     │
│  │    (iteration > max_iterations)                     │     │
│  ├─────────────────────────────────────────────────────┤     │
│  │ 4. 所有标准满足? → COMPLETE                          │     │
│  │    (criteria_tracker.is_all_satisfied)              │     │
│  ├─────────────────────────────────────────────────────┤     │
│  │ 5. 收敛检测?  → CONVERGED                           │     │
│  │    (最近 N 轮无新标准被满足 & 输出相似度 > 阈值)       │     │
│  ├─────────────────────────────────────────────────────┤     │
│  │ 6. 默认       → CONTINUE                            │     │
│  └─────────────────────────────────────────────────────┘     │
│                                                              │
│  收敛检测算法:                                                │
│  ┌──────────────────────────────────────────────────────┐    │
│  │ convergence_window = 3  (观察窗口)                    │    │
│  │                                                      │    │
│  │ 条件 A: 最近 N 轮没有新的标准被满足                    │    │
│  │ 条件 B: 最近 N 轮的输出摘要高度相似 (LLM 判断)        │    │
│  │ 条件 C: 最近 N 轮都是同一角色在执行                    │    │
│  │                                                      │    │
│  │ 收敛 = A AND (B OR C)                                 │    │
│  └──────────────────────────────────────────────────────┘    │
│                                                              │
│  LLM 辅助的标准满足判定:                                      │
│  ┌──────────────────────────────────────────────────────┐    │
│  │ 每轮迭代结束后, 用 LLM 评估:                          │    │
│  │ "根据以下输出, 哪些验收标准现在已被满足?"               │    │
│  │                                                      │    │
│  │ 输入: 本轮输出 + 验收标准列表                          │    │
│  │ 输出: {criteria_id: bool} 映射                        │    │
│  └──────────────────────────────────────────────────────┘    │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

**核心接口**:

```python
class TerminationSignal(Enum):
    CONTINUE  = "continue"
    COMPLETE  = "complete"
    CONVERGED = "converged"
    MAX_ITER  = "max_iter"
    FATAL     = "fatal"
    USER_STOP = "user_stop"

@dataclass
class TerminationResult:
    signal: TerminationSignal
    reason: str
    summary: str    # 终止时的总结信息

class TerminationChecker:
    def __init__(self, max_iterations: int = 15, convergence_window: int = 3):
        ...

    def evaluate(self, project: LoopProject, run_state: EngineRunState) -> TerminationResult:
        """评估是否应该终止 (规则 + LLM)"""

    def assess_criteria(self, output: str, criteria: list[str]) -> dict[int, bool]:
        """LLM 评估本轮输出满足了哪些验收标准"""

    def detect_convergence(self, recent_iterations: list[IterationRecord]) -> bool:
        """检测是否陷入收敛 (无进展循环)"""
```

---

## 4. 模块间交互序列图

### 4.1 完整执行流程

```
用户                LoopHandler        LoopEngine          各模块
 │                      │                  │                 │
 │ /loop <需求>         │                  │                 │
 │─────────────────────→│                  │                 │
 │                      │ plan(text, cb)   │                 │
 │                      │─────────────────→│                 │
 │                      │                  │ analyze(text)   │
 │                      │                  │────────────────→│ RequirementAnalyzer
 │                      │                  │←────────────────│ LoopRequirement
 │                      │                  │                 │
 │  ← 📋 需求确认卡片   │ cb.on_analyzed   │                 │
 │◄─────────────────────│◄─────────────────│                 │
 │                      │                  │                 │
 │                      │ execute(cb)      │                 │
 │                      │─────────────────→│                 │
 │                      │                  │ ╔═══════════════╗
 │                      │                  │ ║ 迭代主循环     ║
 │                      │                  │ ╠═══════════════╣
 │                      │                  │ ║               ║
 │                      │                  │ ║ assess_state()║
 │                      │                  │──║──────────────→│ ContextManager
 │                      │                  │←─║──────────────│ IterationState
 │                      │                  │ ║               ║
 │                      │                  │ ║ select_role() ║
 │                      │                  │──║──────────────→│ RoleRouter
 │                      │                  │←─║──────────────│ RoleSelection
 │                      │                  │ ║               ║
 │  ← 🔄 迭代N开始      │ cb.on_iter_start │ ║               ║
 │◄─────────────────────│◄─────────────────│ ║               ║
 │                      │                  │ ║ execute()     ║
 │                      │                  │──║──────────────→│ ToolAdapter
 │  ← [streaming...]    │ cb.on_progress   │ ║               ║
 │◄─────────────────────│◄─────────────────│ ║               ║
 │                      │                  │←─║──────────────│ ExecutionResult
 │                      │                  │ ║               ║
 │                      │                  │ ║ record()      ║
 │                      │                  │──║──────────────→│ ContextManager
 │                      │                  │ ║               ║
 │                      │                  │ ║ evaluate()    ║
 │                      │                  │──║──────────────→│ TerminationChecker
 │                      │                  │←─║──────────────│ TerminationResult
 │                      │                  │ ║               ║
 │  ← ✅/🔄 迭代结果    │ cb.on_iter_done  │ ║ if CONTINUE  ║
 │◄─────────────────────│◄─────────────────│ ║ → next iter  ║
 │                      │                  │ ║               ║
 │                      │                  │ ║ if !CONTINUE  ║
 │                      │                  │ ║ → break       ║
 │                      │                  │ ╚═══════════════╝
 │                      │                  │                 │
 │  ← 📊 最终报告       │ cb.on_complete   │                 │
 │◄─────────────────────│◄─────────────────│                 │
```

### 4.2 用户干预流程

```
用户                LoopHandler        LoopEngine         ContextManager
 │                      │                  │                 │
 │ /loop_guide "增加     │                  │                 │
 │  错误重试逻辑"       │                  │                 │
 │─────────────────────→│                  │                 │
 │                      │ inject_guidance  │                 │
 │                      │─────────────────→│                 │
 │                      │                  │ inject_user_    │
 │                      │                  │ guidance(msg)   │
 │                      │                  │────────────────→│
 │                      │                  │                 │ (标记待消费)
 │  ← ✅ 引导已注入     │                  │                 │
 │◄─────────────────────│                  │                 │
 │                      │                  │                 │
 │          ... 下一轮迭代开始 ...                             │
 │                      │                  │                 │
 │                      │                  │ has_user_       │
 │                      │                  │ guidance()?     │
 │                      │                  │────────────────→│
 │                      │                  │◄────────────────│ True
 │                      │                  │                 │
 │                      │                  │ consume_user_   │
 │                      │                  │ guidance()      │
 │                      │                  │────────────────→│
 │                      │                  │◄────────────────│ "增加错误重试逻辑"
 │                      │                  │                 │
 │                      │       (融入本轮 prompt 中)          │
```

---

## 5. 文件结构与集成方案

### 5.1 新增文件

```
src/loop_engine/
├── __init__.py              # 公开 API 导出
├── models.py                # 数据模型 (LoopRequirement, LoopProject, IterationRecord, 枚举)
├── analyzer.py              # RequirementAnalyzer (产品诉求解析)
├── roles.py                 # RoleRouter + LoopRole (角色定义与选择)
├── context.py               # LoopContextManager (上下文管理, 压缩, 注入)
├── termination.py           # TerminationChecker (终止判断, 收敛检测)
├── adapter.py               # ToolAdapter (AI后端适配, 执行封装)
├── engine.py                # LoopEngine + LoopEngineManager (主引擎)
└── reporter.py              # LoopReporter (进度格式化)

src/feishu/handlers/
└── loop.py                  # LoopHandler (Feishu 命令路由 & 回调)

tests/
├── test_loop_engine.py      # LoopEngine 集成测试
├── test_loop_analyzer.py    # RequirementAnalyzer 测试
├── test_loop_roles.py       # RoleRouter 测试
├── test_loop_termination.py # TerminationChecker 测试
└── test_loop_context.py     # LoopContextManager 测试
```

### 5.2 修改现有文件

```
src/feishu/ws_client.py       # 添加 /loop 命令路由 + handler 初始化
src/feishu/handlers/base.py   # 添加 get_loop_handler() (可选)
src/config.py                 # 添加 loop_* 配置项
src/agent/intent_recognizer.py # 添加 LOOP_COMMAND intent 识别
```

### 5.3 配置项 (config.py)

```python
# Loop Engine 配置
loop_max_iterations: int = 15              # 最大迭代次数
loop_execution_timeout: int = 300          # 单轮执行超时 (秒)
loop_convergence_window: int = 3           # 收敛检测窗口
loop_max_context_tokens: int = 8000        # 上下文 token 预算
loop_default_max_retries: int = 2          # 单轮失败重试次数
```

### 5.4 用户命令

| 命令 | 说明 | 示例 |
|------|------|------|
| `/loop <需求>` | 启动 Loop 模式 | `/loop 实现用户登录注册` |
| `/loop_status` | 查看当前进度 | 显示迭代进度和标准满足情况 |
| `/loop_guide <信息>` | 注入引导信息 | `/loop_guide 优先实现邮箱注册` |
| `/loop_pause` | 暂停迭代 | 下轮迭代开始前暂停 |
| `/loop_resume` | 恢复迭代 | 继续执行 |
| `/stop_loop` | 停止 Loop | 优雅终止并输出总结 |

---

## 6. 关键设计决策与权衡

### 6.1 为什么不复用 DeepTask

**决策**: Loop Mode 不使用 `DeepTask`，而是使用自己的 `IterationRecord`。

**理由**:
- `DeepTask` 有静态的 `dependencies` 和 `max_retries`，是为预规划设计的
- Loop 的每轮迭代是动态决策的，没有预定义的依赖关系
- `IterationRecord` 更轻量，只记录角色、输出和标准进展

### 6.2 为什么角色选择用规则而非纯 LLM

**决策**: 规则优先，LLM 兜底。

**理由**:
- 角色选择是高频操作（每轮迭代都要），LLM 调用成本高
- 大多数场景的角色选择是确定性的（如首轮=ARCHITECT，失败后=DEBUGGER）
- 只在规则无法判定时 fallback 到 LLM

### 6.3 上下文压缩策略

**决策**: 三级压缩（远期1-line / 近期brief / 最新full）。

**权衡**:
- 方案A: 保留全部历史 → token 爆炸，且远期信息价值低
- 方案B: 只保留最新N轮 → 丢失重要历史决策
- **方案C (选择)**: 渐进压缩 → 远期保留关键信息，近期保留细节

### 6.4 验收标准满足判定

**决策**: LLM 判定 + 人工确认机制。

**理由**:
- 纯规则无法判断"代码质量"、"错误处理覆盖"等模糊标准
- LLM 可以理解代码输出并评估是否满足标准
- 提供 `/loop_guide` 让用户可以纠正误判

### 6.5 与 Deep Engine 的共存

**决策**: Loop Engine 与 Deep Engine 完全独立，可同时运行。

**理由**:
- 使用不同的 `queue_key` 避免调度冲突
- 使用独立的 session（不共享 AI session），避免上下文污染
- 通过 `LoopEngineManager` 管理生命周期（与 `DeepEngineManager` 平行）

---

## 7. 完整状态机

```
                    /loop <需求>
                         │
                         ▼
                   ┌───────────┐
                   │   IDLE    │
                   └─────┬─────┘
                         │ analyze()
                         ▼
                   ┌───────────┐
                   │ ANALYZING │──── 解析失败 ──→ ABORTED
                   └─────┬─────┘
                         │ 解析成功
                         ▼
                   ┌───────────┐
              ┌───│  RUNNING   │◄────────────────────────┐
              │   └──┬──┬──┬──┘                          │
              │      │  │  │                             │
   /stop_loop │      │  │  │  run_iteration()            │
              │      │  │  │                             │
              │      │  │  ▼                             │
              │      │  │ TerminationChecker             │
              │      │  │  ├── CONTINUE ─────────────────┘
              │      │  │  ├── COMPLETE ──→ COMPLETED
              │      │  │  ├── CONVERGED ─→ COMPLETED (with warning)
              │      │  │  ├── MAX_ITER ──→ ABORTED
              │      │  │  └── FATAL ─────→ ABORTED
              │      │  │
              │      │  │ /loop_pause
              │      │  ▼
              │      │ ┌───────────┐
              │      │ │  PAUSED   │
              │      │ └─────┬─────┘
              │      │       │ /loop_resume
              │      │       ▼
              │      │  (回到 RUNNING)
              │      │
              ▼      │
         ┌─────────┐ │
         │ STOPPING │ │ (优雅终止: 等待当前迭代完成)
         └────┬────┘ │
              │      │
              ▼      │
         ┌─────────┐ │
         │ ABORTED │ │
         └─────────┘ │
                     │
              ┌──────▼────┐
              │ COMPLETED  │
              └────────────┘
```

---

## 8. 扩展性考虑

### 8.1 新增角色

只需在 `roles.py` 中添加新的 `LoopRole` 枚举值和对应的 system prompt，无需修改引擎逻辑。

### 8.2 新增 AI 后端

实现新的 `BaseSession` 子类，`ToolAdapter` 自动兼容。

### 8.3 自定义终止策略

`TerminationChecker` 可通过配置调整阈值，或继承扩展新的终止条件。

### 8.4 与 Deep Engine 的融合可能

未来可考虑 "Deep + Loop" 混合模式：用 Deep Engine 做初始规划，Loop Engine 做迭代精化。两者通过共享 `ExecutionContext` 实现上下文传递。

---

## 9. 总结

Loop Mode 的核心价值在于**将产品诉求转化为可验证的验收标准，通过角色化的迭代闭环持续推进，直到所有标准被满足**。这种设计：

1. **比 Deep Engine 更智能**: 每轮动态决策而非一次性规划
2. **比交互模式更自主**: 系统驱动而非用户驱动
3. **有明确的完成标准**: 验收标准 + 收敛检测，不会无限循环
4. **完全兼容现有架构**: 复用 AI Session、TaskScheduler、Feishu Handler 等基础设施
