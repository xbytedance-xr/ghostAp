# GhostAP Persistent Employee Runtime and Knowledge Workspace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 GhostAP 的“员工”从常驻飞书 Channel + 单次 ACP 调用，升级为可恢复的长期逻辑角色：有固定身份、独立任务邮箱、可复用后端会话、长期知识工作目录，并由主协调 Agent 在群内持续调度、迭代和收敛团队任务。

**Architecture:** Journal 与加密 Blob 继续作为唯一事实源；每名员工由 `EmployeeActor` 作为长期逻辑进程管理，ACP 能力允许时保留热会话，不允许时使用可恢复的冷启动适配器。员工目录是由事实源生成的只读控制投影和受控维护的 LLM Wiki；主 Bot 统一沉淀群事件，`TeamCoordinatorActor` 按职责和负载派发，不把每条群消息广播成所有模型调用。

**Tech Stack:** Python >= 3.11、ACP 0.11、lark-oapi 1.7.1、lark-channel-sdk 1.1.0、PyYAML、Journal/加密 Blob、pytest、ruff、uv。

## Global Constraints

- 仅使用 `uv`，不得使用 pip/conda。
- Journal 是员工身份、权限、生命周期、群成员关系、任务、会话检查点和知识发布记录的唯一事实源；Markdown 文件都是可重建投影，不能反向成为权威写入面。
- `AGENTS.md`、`IDENTITY.md`、`NOW.md` 和 Wiki 索引必须排除 credential、token、`.env`、原始私聊正文、隐藏提示词和模型思维链。
- 员工不能通过文件工具修改自己的身份、权限、预算或当前任务状态；这些文件以 `0600` 原子生成，运行时工具策略按路径拒绝写入。
- 不依赖所有后端都会自动发现 `AGENTS.md`。Codex 使用 `CODEX_HOME` 全局指令，同时所有后端都接收带 digest 的显式 bootstrap 指令。
- TTADK 员工仍只使用 CLI 桥接，不为 `ttadk_*` 启动 ACP。
- “员工常驻”定义为逻辑身份、邮箱、检查点和知识可恢复；底层模型进程允许热驻留、健康检查、回收和重建，不承诺某个 OS PID 永不退出。
- 主 Bot 统一记录其可见的群事件。员工 Bot 的直接 @ 事件进入同一 durable group ledger；不得让每个员工 Bot 各自维护互相冲突的群历史。
- 普通群消息可进入员工的资格评估，但只有直接 @、已分配任务、成功抢占或显式职责规则才可唤醒模型，避免成本失控和 Bot 风暴。
- 上下文“权威不足”和“内容不完整”必须分开。身份、权限、当前消息缺失继续 fail-close；远端分页、排序、修订读取失败可降级为带质量标记的已持久化上下文，不能再把内部 Team assignment 终止为泛化的 `context_unavailable`。
- 所有 UI 改动先在 `ux/` 建立 HTML 预览；卡片继续遵守 handler -> session -> render/delivery 单向边界。
- 所有行为变更先写失败测试；先跑最相关测试，再扩大到 `tests/autonomous/`、共享 Slock/Feishu 测试和配置验证。

---

## 1. 自动 Grill 后冻结的架构决策

本文按用户授权自动接受以下推荐答案，不再把它们作为实施时的开放问题：

| 决策点 | 采用方案 | 原因 |
| --- | --- | --- |
| 员工的“常驻”是什么 | 长期逻辑 Actor + 可选热会话 | ACP/CLI 进程会崩溃、升级和超时；长期契约必须可恢复 |
| `AGENTS.md` 是否是事实源 | 否，只读启动索引 | 防止模型通过文件自我改名、扩权或篡改任务状态 |
| 长期知识放哪里 | 加密源记录 + 受控 Wiki + 可重建 Markdown 投影 | 兼顾审计、隐私、可读性和后端通用性 |
| 是否复制所有原始消息到员工目录 | 否 | 原始消息可能含敏感数据；目录只放安全摘要和 source reference |
| 是否给每名员工永久占用 ACP Server | 能力允许时热驻留，否则冷启动恢复 | 对外保证 READY，不把产品可用性绑定在单一后端实现 |
| 是否每条群消息唤醒所有员工 | 否 | durable 广播事件，选择性唤醒模型 |
| Team 谁控制 | 主服务中的长期 `TeamCoordinatorActor` | 它持有全群 canonical context、运行图和所有员工状态 |
| Team 协调模型 | 新配置项，默认 tool=`coco`、model 为空表示 provider 默认 | 支持改为 Traex 等工具，不把某个模型硬编码在业务代码 |
| LLM Wiki 是否首期引入向量库 | 否 | 先用索引、frontmatter、wikilink 和受控全文检索；有测量证据后再加 embedding |
| 模型推理是否进入 Wiki | 否 | 只保存结论、决策、证据引用、技能和验证结果，不保存隐藏思维链 |

## 2. 研究依据及在 GhostAP 中的映射

[Karpathy 的 LLM Wiki 原始说明](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)提出三个核心层次：不可变原始来源、LLM 维护的互链 Markdown Wiki、定义维护方式的 schema/agent instructions；核心操作是 ingest、query、lint，并通过持续整合让知识复利，而不是每次查询都从原始文档重新做一次 RAG。

[nashsu/llm_wiki](https://github.com/nashsu/llm_wiki)在这个模式上增加了 `purpose.md`、持久摄入队列、source hash 增量处理和异步 review。GhostAP 借鉴这些运行机制，但不复制桌面 UI、图数据库、向量库或展示思维链。

[Codex 官方 AGENTS.md 指南](https://developers.openai.com/codex/guides/agents-md)说明 Codex 在每次 run/session 启动时构建一次指令链：先读 `CODEX_HOME` 下的全局 `AGENTS.md`，再从项目根到 cwd 读取项目指令，默认总量上限 32 KiB。由此得出两个直接约束：

1. 员工指令必须在新会话建立前准备好，身份版本变化需要回收旧会话。
2. 员工 `AGENTS.md` 限制为 8 KiB，为项目自己的 AGENTS 指令预留空间；详细历史必须下沉到 Wiki。

GhostAP 的对应关系固定为：

| LLM Wiki 层 | GhostAP 权威数据 | 员工目录投影 |
| --- | --- | --- |
| Raw sources | Journal frame + 加密 Blob + execution history | `sources/manifest.yaml` 只保存无秘密引用和 digest |
| Compiled wiki | 经 policy/来源校验发布的知识文档 | `wiki/**/*.md`、`wiki/index.md`、`wiki/log.md` |
| Schema/instructions | 固定模板 + 员工身份投影 + workspace policy | `AGENTS.md`、`purpose.md`、`schema.md` |
| Runtime state（GhostAP 扩展） | TeamRun/Assignment/Checkpoint Journal 事件 | `NOW.md`、`tasks/active.md`、`tasks/archive/index.md` |

## 3. 当前实现：具体是什么，不是什么

### 3.1 已经长期存在的部分

- `/hire` 通过 `src/autonomous/provisioning/hire_service.py` 把员工和 Bot principal 写入 Journal，并由 `EmployeeIdentityMaterializer` 生成 `identity.json`。
- `src/autonomous/supervisor/employee_channels.py` 为每名可见员工启动独立飞书 Channel 子进程；重启时会按 Journal 投影恢复 Channel。
- `EmployeeDefinition` 已有 `role`、`persona`、`personality_traits`、`capabilities`、`permissions`、tool/model/profile/effort 等完整字段。
- `src/autonomous/data/` 已有加密 history、L1 memory、summary、skill profile 和 reasoning document 的 Journal-backed 数据面。
- Durable Inbox、Router、Gateway、Outbox 已经保证员工消息受理和发送经过锚定、幂等、generation fence 与权限检查。

### 3.2 目前没有长期存在的部分

- 可见 `/hire` 的卡片链路只构造 name/tool/model/profile/effort；`EmployeeHireRequest` 虽有 `role/persona`，但 UI 没有传入，traits/capabilities/permissions 甚至不在 request 合同中。
- 员工 Channel 子进程只负责飞书 WebSocket transport，不承载 ACP/CLI 模型会话。
- `src/slock_engine/engine.py::_run_acp_session()` 每次任务调用 `create_engine_session()`，执行一个 prompt 后立即 `close_session_safely()`；`thread_id` 目前只是参数，没有形成员工级持久会话池。
- 员工 ACP 的 cwd 是 `SlockEngine.root_path`，不是 `agent.workspace_path`；因此现有员工目录不会自动成为模型启动上下文。
- `identity.json`、memory、history、reasoning、skill profile 分散存在，没有一个让员工启动时快速回答“我是谁、现在做什么、以前做过什么、去哪里查”的受控入口。

### 3.3 当前 Team 模式的真实逻辑

`src/autonomous/team/service.py::EmployeeTeamService` 不是主协调 LLM，而是固定程序：

```text
targets 按 agent_id 排序
  -> 第 1 名做 analysis
  -> 第 2 名做 review（没有第 2 名就仍用第 1 名）
  -> 第 1 名做 synthesis
  -> 主 Bot 发送最终结果
```

它不根据 role/capability/负载做动态分配，不支持员工主动 claim，不支持群内多轮任务图，也没有 coordinator 的独立 tool/model/persona。`team.run.created` 只保存 task digest，原始 task 仍在内存；所以进程重启后 `recover()` 只能把运行中任务终止为 `restart_instruction_unavailable`。

### 3.4 `context_unavailable` 为什么会出现

Team assignment 被包装成内部 ingress 后，仍复用员工普通消息的 `EmployeeThreadContext`：它会使用员工 Bot 凭据重新读取飞书 thread 和 group window，并对分页顺序、message position、重复消息、revision 稳定性做严格校验。

`ContextUnavailableReason.ORDERING` 当前被归入 transient reason。Router 有界重试后仍失败就终止为统一 `context_unavailable`，Team service 再显示：

> 团队任务未能自动收敛，已安全停止并转为人工处理。

问题不是“ACP 员工不够常驻”，而是内部已锚定 assignment 被错误地依赖一次新的、严格的远端群历史抓取。只要飞书缺 position、分页顺序和本地校验假设不一致，或同一消息在两层里呈现不同，就会在模型调用前失败。

## 4. 目标架构

```text
主 Bot 群事件 ───────┐
员工 Bot 直接 @ ─────┼─> Durable Group Event Ledger ─> Route Decision
员工 Bot 协作回复 ───┘              │                       │
                                    │                       ├─ direct @ -> EmployeeActor mailbox
                                    │                       ├─ task -> TeamCoordinatorActor
                                    │                       └─ chat -> 仅持久化/不唤醒模型
                                    │
                                    ├─ canonical group context
                                    └─ encrypted payload/source refs

TeamCoordinatorActor（每 tenant + group + project 一个逻辑 Actor）
  ├─ configured coordinator tool/model/persona
  ├─ durable run graph / done criteria / turn budget
  ├─ role + capability + load based assignment
  ├─ CAS claim / handoff / review / revise / complete
  └─ all employee runtime status + group context
                │
                ├─> EmployeeActor A ─> BackendSessionLease ─> ACP/CLI
                ├─> EmployeeActor B ─> BackendSessionLease ─> ACP/CLI
                └─> EmployeeActor N ─> BackendSessionLease ─> ACP/CLI
                          │
                          ├─ project repo（按权限读写）
                          └─ employee workspace（控制投影只读）

Journal + encrypted Blob
  ├─ workforce / membership / inbox / outbox
  ├─ group events / team runs / assignments / checkpoints
  ├─ employee history / safe knowledge sources
  └─ WorkspaceProjector -> AGENTS.md + NOW.md + wiki/
```

### 4.1 员工运行状态

`EmployeeActorStatus` 使用以下稳定状态，不与飞书 Channel 状态混为一谈：

```text
RECOVERING -> READY_COLD -> STARTING_SESSION -> READY_WARM -> BUSY
                 ^               |                 |          |
                 └───────────────┴─────────────────┴──────────┘

任意可恢复后端错误 -> DEGRADED -> READY_COLD
退役或关闭 -> STOPPING -> STOPPED
```

- `READY_COLD`：身份、目录、邮箱和权限已恢复，收到任务后可以启动后端。
- `READY_WARM`：已有健康后端会话，可以复用。
- `BUSY`：邮箱中一个 assignment 正在执行；同一员工默认串行。
- `DEGRADED`：Channel 可以在线，但模型后端暂不可用；Coordinator 不再给它新任务。

### 4.2 会话作用域

后端会话 key 固定为：

```python
@dataclass(frozen=True, slots=True)
class EmployeeSessionKey:
    tenant_key: str
    agent_id: str
    project_id: str
    tool: str
    model: str
    profile: str
    effort: str
    identity_version: int
    instruction_digest: str
```

- 身份、模型、权限、AGENTS 指令 digest 或项目变化时创建新 key，旧会话 drain 后关闭。
- Wiki 内容变化不强制重启会话；下一次 assignment 注入 `knowledge_generation` 和变更摘要，模型按需查询新页面。
- ACP 后端保留 session；CLI 后端允许每次 assignment 新建进程，但复用同一个 Actor mailbox、checkpoint 和 workspace。
- warm session 受 `autonomous_employee_max_warm_sessions=8` 和 `autonomous_employee_session_idle_seconds=900` 控制；回收不改变员工 READY 状态。

### 4.3 员工目录

保留现有 `~/.ghostap/slock/agents/<agent_id>/` 根，扩展为：

```text
agents/<agent_id>/
├── identity.json                     # 现有、可重建、非秘密身份投影
├── memory/MEMORY.md                  # 现有 canonical L1 materialization
├── history/YYYY-MM-DD.jsonl          # 现有受 ACL 保护的执行历史物化
├── reasoning/                        # 现有结构化结果；不写隐藏 CoT
├── skill_profile.json                # 现有技能画像
├── workspace/
│   ├── AGENTS.md                     # <= 8 KiB，只读启动索引
│   ├── IDENTITY.md                   # 完整非秘密身份/职责/工具/权限说明
│   ├── NOW.md                        # 当前 assignment、进度、checkpoint 引用
│   ├── purpose.md                    # 该员工长期工作目标和知识范围
│   ├── schema.md                     # Wiki page/frontmatter/引用规则
│   ├── wiki/
│   │   ├── index.md                  # 页面目录和最近更新
│   │   ├── overview.md               # 长期能力与知识总览
│   │   ├── log.md                    # 从 Journal 生成的知识变更时间线
│   │   ├── projects/<project_id>.md
│   │   ├── decisions/<slug>.md
│   │   ├── concepts/<slug>.md
│   │   ├── skills/<slug>.md
│   │   └── outcomes/<yyyy-mm>/<slug>.md
│   ├── tasks/
│   │   ├── active.md
│   │   └── archive/index.md
│   └── sources/manifest.yaml         # source_id/hash/type/时间/可见级别，无正文
└── runtime/
    ├── codex-home/AGENTS.md          # 与 workspace/AGENTS.md 同 digest 的投影
    └── checkpoints/                  # 后端无关的安全会话/任务检查点
```

推荐的 `AGENTS.md` 固定骨架：

```markdown
# Employee: <name>

You are GhostAP employee `<agent_id>`.

## Identity
- Role: <role>
- Strengths: <capabilities>
- Tool/model: <tool>/<model-or-provider-default>

## Current work
Read `NOW.md`, then `tasks/active.md`. Never infer an active task from chat history.

## Knowledge
Read `wiki/index.md` before opening individual pages. Cite page path and source IDs.
Use `sources/manifest.yaml` only to locate authorized source records.

## Boundaries
- `AGENTS.md`, `IDENTITY.md`, `NOW.md`, `tasks/`, and `wiki/` are managed projections.
- Do not edit identity, permissions, task state, or source manifests directly.
- Do not store credentials, private raw messages, or hidden reasoning in Markdown.

## Project
The assigned project root and its repository instructions are supplied per assignment.
```

### 4.4 Wiki 文档合同

每个受控 Wiki page 使用 PyYAML 可解析的 frontmatter：

```yaml
---
schema_version: 1
page_id: know_<stable-id>
kind: decision
title: Use durable group context for team assignments
source_ids:
  - hist_<id>
source_hashes:
  - <sha256>
confidence: verified
status: active
knowledge_generation: 17
updated_at: 2026-07-17T00:00:00Z
---
```

规则：

- `source_ids` 必须能在 canonical data projection 中解析，hash 必须匹配。
- `confidence` 只能是 `observed`、`inferred`、`verified`；只有 verifier/测试结果可产生 `verified`。
- 矛盾内容不覆盖旧页面；创建 review item，旧 claim 标为 `disputed`。
- 删除源只 tombstone 相关 claim，保留操作日志，不静默抹去历史。
- Wiki 编译在任务终态之后异步执行；编译失败不把已完成任务改成失败。
- 有价值的最终答复可以形成 outcome page，再由编译器抽取 decision/concept/skill；不得直接把整段 prompt/output 当作 Wiki。

### 4.5 上下文质量合同

新增：

```python
class ContextQuality(str, Enum):
    COMPLETE = "complete"
    CANONICAL_PARTIAL = "canonical_partial"

@dataclass(frozen=True, slots=True)
class CanonicalGroupContext:
    quality: ContextQuality
    current_event_id: str
    ordered_event_ids: tuple[str, ...]
    source_watermark: str
    warnings: tuple[str, ...]
```

- Team assignment 的 authority 来自已锚定的 `team.assignment.created` 和原始 group event，不再重新抓飞书作为执行前置条件。
- 直接 @ 的当前消息必须存在且绑定正确；扩展历史抓取出现 ordering/pagination/revision/deadline/source 问题时，使用 ledger 中截至当前事件的窗口，标为 `CANONICAL_PARTIAL` 并在结果卡披露。
- scope、credential、permission、membership、current event 和 content 校验失败仍终止，错误码保持具体，例如 `context_scope_denied`，不再统一吞成 `context_unavailable`。

### 4.6 Team 运行合同

Team run 不再固定 analysis/review/synthesis，而由 Coordinator 输出受 schema 限制的决策：

```python
@dataclass(frozen=True, slots=True)
class CoordinatorDecision:
    action: Literal["assign", "request_revision", "request_review", "complete", "block"]
    target_agent_id: str
    assignment_brief: str
    depends_on: tuple[str, ...]
    done_checks: tuple[str, ...]
```

运行边界：

- 每 run 最多 12 个 coordinator turns、32 个 employee assignments、4 个并行员工、8 次 handoff。
- Coordinator 优先使用显式 @ 目标；否则按 capability、role、membership、READY 状态和邮箱长度形成候选，再由 Coordinator 在候选内选择。
- “抢占”通过 `team.assignment.claimed` 的 Journal CAS 完成，只有一个员工获得执行权。
- 员工输出成为 `team.contribution.published`，可投递到协作群，也会进入其他 assignment 的 context；Bot-origin event 带 causal ID，防止被重新分类成新用户任务。
- Coordinator 判断 done criteria 满足后才能 `team.run.completed`；无法收敛时给出具体缺口、已完成贡献和可继续操作，不再只显示泛化错误。
- 原始任务、计划、assignment brief、contribution 和 checkpoint 写入加密 Blob；Journal 事件保存 ref/hash，因此重启后可继续而不是 `restart_instruction_unavailable`。

---

## 5. 分阶段实施计划

### Task 1: 冻结现状与失败合同

**Files:**
- Create: `tests/autonomous/contract/test_persistent_employee_runtime_contract.py`
- Create: `tests/autonomous/integration/test_team_context_ordering_recovery.py`
- Modify: `tests/autonomous/unit/test_employee_team_service.py`
- Modify: `docs/2026-07-12-autonomous-agent-department-design.md`

**Interfaces:**
- 记录旧行为：Channel READY 不代表模型 session READY。
- 复现 `ORDERING -> defer -> context_unavailable -> team.run.action_required`。
- 冻结未来替代合同：Team assignment 拥有 durable payload、可恢复 checkpoint 和具体 context quality。

- [x] **Step 1: 写当前实现证据测试**

  证明连续两个员工任务会创建并关闭两个 session；Team 目标按 `agent_id` 排序且固定三段执行；restart 只能产生 `restart_instruction_unavailable`。

- [x] **Step 2: 写 ordering 故障回归测试**

  构造缺失/逆序 `message_position` 的 Lark page，证明内部 Team assignment 在当前实现中不会到达 ACP backend。

- [x] **Step 3: 运行测试并保存失败/通过基线**

  Run: `uv run python -m pytest tests/autonomous/contract/test_persistent_employee_runtime_contract.py tests/autonomous/integration/test_team_context_ordering_recovery.py tests/autonomous/unit/test_employee_team_service.py -q`

  Expected: 旧行为证据通过；面向新合同的测试以缺少 runtime/ledger 类型失败。

- [x] **Step 4: 更新旧设计文档状态**

  在旧部门设计中明确“Slock 单次 ACP”是已实现的 v1 过渡方案，并链接本文，避免后续维护者误把它当成最终常驻员工语义。

- [x] **Step 5: 提交基线合同**

  Commit: `test(autonomous): freeze employee runtime v1 gaps`

### Task 2: 完整冻结 `/hire` 员工身份

**Files:**
- Modify: `src/autonomous/provisioning/hire_port.py`
- Modify: `src/autonomous/provisioning/hire_service.py`
- Modify: `src/autonomous/manager/cards.py`
- Modify: `src/feishu/handlers/slock.py`
- Create: `ux/employee-hire-profile.html`
- Modify: `tests/autonomous/unit/test_employee_hire_service.py`
- Modify: `tests/test_slock_role_creation.py`

**Interfaces:**
- Extend `EmployeeHireRequest` with immutable tuples `personality_traits`, `capabilities`, `permissions`.
- Card produces role/persona template and bounded trait/capability choices.
- Hire Journal event persists every selected field before external App creation.

- [x] **Step 1: 先做 Hire 资料卡 HTML 预览**

  显示名字、职责、工作风格、能力、工具/模型摘要和权限预览；不让用户在卡片输入任意 system prompt。

- [x] **Step 2: 写失败测试**

  验证 visible `/hire` 不再丢弃 role/persona/traits/capabilities/permissions，空字段使用受版本控制的安全默认模板。

- [x] **Step 3: 运行目标测试**

  Run: `uv run python -m pytest tests/autonomous/unit/test_employee_hire_service.py tests/test_slock_role_creation.py -q`

  Expected: FAIL because request contract/UI payload does not carry the new fields.

- [x] **Step 4: 扩展 request、卡片和 Journal payload**

  对 trait/capability 使用 allowlist；permission 只允许管理员从 policy profile 选择，员工名字和 persona 不能隐式扩权。

- [x] **Step 5: 验证身份投影重建**

  Run: `uv run python -m pytest tests/autonomous/unit/test_employee_hire_service.py tests/autonomous/unit/test_employee_projection.py tests/test_slock_role_creation.py -q`

  Expected: PASS.

- [x] **Step 6: 提交**

  Commit: `feat(hire): persist complete employee profiles`

### Task 3: 建立员工知识工作目录投影

**Files:**
- Create: `src/autonomous/workspace/__init__.py`
- Create: `src/autonomous/workspace/models.py`
- Create: `src/autonomous/workspace/layout.py`
- Create: `src/autonomous/workspace/templates.py`
- Create: `src/autonomous/workspace/projector.py`
- Create: `src/autonomous/workspace/lint.py`
- Modify: `src/autonomous/workforce/projection.py`
- Modify: `src/autonomous/data/composition.py`
- Create: `tests/autonomous/security/test_employee_workspace_projection.py`
- Create: `tests/autonomous/unit/test_employee_workspace_lint.py`

**Interfaces:**

```python
@dataclass(frozen=True, slots=True)
class EmployeeWorkspaceSnapshot:
    agent_id: str
    identity_version: int
    knowledge_generation: int
    active_assignment_id: str
    instruction_digest: str
    projection_sequence: int
    projection_hash: str

class EmployeeWorkspaceProjector:
    def rebuild(self, tenant_key: str, agent_id: str) -> EmployeeWorkspaceSnapshot: ...
    def verify(self, snapshot: EmployeeWorkspaceSnapshot) -> None: ...
```

- [x] **Step 1: 写布局、权限和 no-follow 失败测试**

  覆盖非法 agent ID、symlink 中间目录、文件越界、过宽权限、短写、rename 前后 crash 和重复 rebuild。

- [x] **Step 2: 写内容测试**

  验证新员工目录包含本文冻结的文件；`AGENTS.md <= 8192 bytes`；不出现 credential ref、app secret、token、`.env` 值或原始 private message。

- [x] **Step 3: 运行测试**

  Run: `uv run python -m pytest tests/autonomous/security/test_employee_workspace_projection.py tests/autonomous/unit/test_employee_workspace_lint.py -q`

  Expected: FAIL because `src.autonomous.workspace` does not exist.

- [x] **Step 4: 实现确定性模板和原子 projector**

  使用逐组件 dir-fd、`O_NOFOLLOW`、临时文件、fsync、rename；模板只接收类型化 projection，不接收任意 dict。

- [x] **Step 5: 接入 Hire 激活和全量 rebuild**

  员工进入 ACTIVE 前生成并 lint workspace；启动恢复时从 workforce/data/team projection 重建，不扫描 Markdown 反推状态。

- [x] **Step 6: 验证**

  Run: `uv run python -m pytest tests/autonomous/security/test_employee_workspace_projection.py tests/autonomous/unit/test_employee_workspace_lint.py tests/autonomous/integration/test_employee_hire_composition.py -q`

  Expected: PASS.

- [x] **Step 7: 提交**

  Commit: `feat(workspace): project durable employee workspaces`

### Task 4: 后端无关的员工启动指令与文件权限

**Files:**
- Create: `src/autonomous/runtime/employee_session.py`
- Modify: `src/autonomous/gateway/env_scope.py`
- Modify: `src/agent_session/factory.py`
- Modify: `src/slock_engine/engine.py`
- Modify: `src/acp/sync_adapter.py`
- Create: `tests/autonomous/contract/test_employee_backend_bootstrap.py`
- Create: `tests/autonomous/security/test_employee_tool_isolation.py`

**Interfaces:**

```python
@dataclass(frozen=True, slots=True)
class EmployeeSessionBootstrap:
    session_key: EmployeeSessionKey
    project_root: str
    workspace_root: str
    codex_home: str
    instruction_text: str
    instruction_digest: str
    read_only_roots: tuple[str, ...]
    writable_roots: tuple[str, ...]
```

- [x] **Step 1: 写跨后端启动测试**

  覆盖 Codex、Coco、Traex、Claude CLI 和 TTADK CLI：每种后端都获得 identity/bootstrap digest；Codex 额外获得 employee-specific `CODEX_HOME`；TTADK 不走 ACP。

- [x] **Step 2: 写路径权限测试**

  project root 按员工 permission 可读写；workspace 控制文件可读不可写；`.env`、Vault、Journal、其他员工目录不可见；未声明根默认拒绝。

- [x] **Step 3: 运行测试**

  Run: `uv run python -m pytest tests/autonomous/contract/test_employee_backend_bootstrap.py tests/autonomous/security/test_employee_tool_isolation.py -q`

  Expected: FAIL because factory only captures process env and Slock uses one set of allowed roots.

- [x] **Step 4: 扩展显式 session bootstrap**

  不使用全局 `os.environ` 修改；把 `CODEX_HOME` 加入 employee process env allowlist，并在 provider spawn 时一次性捕获。所有后端首个 prompt 都包含同 digest 的 bootstrap envelope。

- [x] **Step 5: 将工具过滤改为读写根分离**

  文件读取允许 project + workspace；文件写入和 shell cwd 只允许 policy 指定的 project/work scratch；拒绝修改 generated control files。

- [x] **Step 6: 验证**

  Run: `uv run python -m pytest tests/autonomous/contract/test_employee_backend_bootstrap.py tests/autonomous/security/test_employee_tool_isolation.py tests/test_slock_security.py -q`

  Expected: PASS.

- [x] **Step 7: 提交**

  Commit: `feat(runtime): bootstrap employee identity across backends`

### Task 5: 员工 Actor、邮箱和可复用会话

**Files:**
- Create: `src/autonomous/runtime/employee_actor.py`
- Create: `src/autonomous/runtime/employee_supervisor.py`
- Modify: `src/autonomous/runtime/__init__.py`
- Modify: `src/autonomous/provisioning/composition.py`
- Modify: `src/autonomous/gateway/coordinator.py`
- Modify: `src/config/settings.py`
- Modify: `.env.example`
- Create: `tests/autonomous/unit/test_employee_actor.py`
- Create: `tests/autonomous/integration/test_employee_session_reuse.py`
- Create: `tests/autonomous/chaos/test_employee_actor_recovery.py`

**Interfaces:**

```python
class EmployeeRuntimeSupervisor:
    def recover(self) -> int: ...
    def status(self, agent_id: str) -> EmployeeActorStatus: ...
    def submit(self, assignment: EmployeeAssignment) -> str: ...
    def cancel(self, assignment_id: str) -> EmployeeCancellationOutcome: ...
    def recycle(self, agent_id: str, reason: str) -> None: ...
    def close(self) -> None: ...
```

- [ ] **Step 1: 写 Actor 串行邮箱测试**

  同一员工一次只执行一个 assignment；不同员工可并行；重复 assignment ID 幂等；cancel、timeout 和 close 均有唯一终态。

- [ ] **Step 2: 写 ACP session 复用测试**

  相同 `EmployeeSessionKey` 的两个连续任务只启动一次后端；identity/instruction/project 变化会 drain 旧 session 并创建新 session；idle TTL 回收后员工仍为 READY_COLD。

- [ ] **Step 3: 写 crash recovery 测试**

  在 mailbox anchor 后、session start 前、prompt 中、result anchor 前 kill；恢复不得重复已提交 effect，并从安全 checkpoint 重新派发或进入具体 action-required。

- [ ] **Step 4: 运行测试**

  Run: `uv run python -m pytest tests/autonomous/unit/test_employee_actor.py tests/autonomous/integration/test_employee_session_reuse.py tests/autonomous/chaos/test_employee_actor_recovery.py -q`

  Expected: FAIL because supervisor/actor are absent.

- [ ] **Step 5: 实现 actor 和 session lease**

  复用 `SyncSession` 的 `is_server_healthy()`、`cancel()`、`close()`；不修改旧 `AgentRuntime` 的 broker turn-loop语义。所有 ACP 外部调用继续满足 effect PREPARED/EXECUTING 先锚定。

- [ ] **Step 6: Gateway 切到 supervisor**

  `EmployeeSlockGateway` 只负责授权和兼容 prompt 构建；不再直接调用 `_run_acp_session()`。保留 `autonomous_employee_runtime_mode=legacy_one_shot|shadow|actor` 作为发布期显式切换，失败时不自动回退。

- [ ] **Step 7: 验证**

  Run: `uv run python -m pytest tests/autonomous/unit/test_employee_actor.py tests/autonomous/integration/test_employee_session_reuse.py tests/autonomous/chaos/test_employee_actor_recovery.py tests/autonomous/integration/test_employee_slock_gateway.py -q`

  Expected: PASS.

- [ ] **Step 8: 提交**

  Commit: `feat(runtime): add recoverable employee actors`

### Task 6: Durable group ledger 与可降级上下文

**Files:**
- Create: `src/autonomous/context/group_ledger.py`
- Modify: `src/autonomous/context/models.py`
- Modify: `src/autonomous/context/service.py`
- Modify: `src/autonomous/context/assembler.py`
- Modify: `src/autonomous/gateway/coordinator.py`
- Modify: `src/autonomous/ingress/router.py`
- Modify: `src/feishu/dispatcher.py`
- Create: `tests/autonomous/unit/test_group_context_ledger.py`
- Modify: `tests/autonomous/integration/test_team_context_ordering_recovery.py`
- Modify: `tests/autonomous/integration/test_employee_slock_gateway.py`

**Interfaces:**
- `GroupEventRecord` 保存 tenant/chat/thread/message、transport principal、Journal sequence、encrypted payload ref、dedup key 和 causal event ID。
- `CanonicalGroupContext` 明确 `COMPLETE` 或 `CANONICAL_PARTIAL`。

- [ ] **Step 1: 写 main Bot/employee Bot 跨入口 dedup 测试**

  同一飞书 message/event 被两个 Bot 看到时只形成一个 canonical event；authority 字段不能由 payload 覆盖。

- [ ] **Step 2: 写排序降级测试**

  ordering/pagination/revision/deadline/source 失败时，已锚定当前事件使用 Journal sequence 构造 partial window；scope/permission/current-event 失败仍拒绝。

- [ ] **Step 3: 运行测试**

  Run: `uv run python -m pytest tests/autonomous/unit/test_group_context_ledger.py tests/autonomous/integration/test_team_context_ordering_recovery.py tests/autonomous/integration/test_employee_slock_gateway.py -q`

  Expected: FAIL because no canonical ledger or quality contract exists.

- [ ] **Step 4: 接入 group ledger**

  主 Bot 普通群消息和员工 Bot direct mention 在 durable ACK 后发布 group event；内部 Team assignment 只引用 event/Blob，不伪装成需要重新抓取的飞书消息。

- [ ] **Step 5: 拆分 hard failure 与 enrichment warning**

  删除 `_TRANSIENT_CONTEXT_REASONS` 的一刀切终态语义；Router 只对 authority failure 终止，对 enrichment failure 记录 `context.warning.recorded` 并继续。

- [ ] **Step 6: 验证错误码**

  Run: `uv run python -m pytest tests/autonomous/unit/test_group_context_ledger.py tests/autonomous/integration/test_team_context_ordering_recovery.py tests/autonomous/integration/test_employee_slock_gateway.py -q`

  Expected: PASS；测试中不再出现 Team `context_unavailable`，并能看到 `canonical_partial/order_unavailable`。

- [ ] **Step 7: 提交**

  Commit: `fix(context): decouple team work from lark ordering`

### Task 7: LLM Wiki 摄入、查询、Lint 和异步 Review

**Files:**
- Create: `src/autonomous/knowledge/__init__.py`
- Create: `src/autonomous/knowledge/models.py`
- Create: `src/autonomous/knowledge/compiler.py`
- Create: `src/autonomous/knowledge/query.py`
- Create: `src/autonomous/knowledge/lint.py`
- Create: `src/autonomous/knowledge/review.py`
- Modify: `src/autonomous/data/models.py`
- Modify: `src/autonomous/data/facades.py`
- Modify: `src/autonomous/data/composition.py`
- Modify: `src/autonomous/gateway/coordinator.py`
- Create: `tests/autonomous/unit/test_employee_knowledge_compiler.py`
- Create: `tests/autonomous/unit/test_employee_knowledge_query.py`
- Create: `tests/autonomous/security/test_employee_knowledge_redaction.py`
- Create: `tests/autonomous/chaos/test_employee_knowledge_recovery.py`

**Interfaces:**

```python
class EmployeeKnowledgeService:
    def enqueue_terminal(self, terminal: AuthenticatedExecutionTerminal) -> str: ...
    def query(self, request: AuthorizedKnowledgeQuery) -> KnowledgeQueryResult: ...
    def lint(self, tenant_key: str, agent_id: str) -> KnowledgeLintReport: ...
    def recover(self) -> int: ...
```

- [ ] **Step 1: 写 source hash 和幂等摄入测试**

  同 source/hash 不重复调用编译器；内容变化产生新 generation；queue 在重启后继续；每员工串行摄入避免并发覆盖 index。

- [ ] **Step 2: 写安全测试**

  prompt injection 不能修改 schema/identity/permissions；credential/PII/原始 private message/hidden reasoning 不能出现在 Markdown；越权 source ID 在读取 Blob 前被拒绝。

- [ ] **Step 3: 写 Wiki lint 测试**

  检测 broken wikilink、orphan page、缺失 source、hash 不匹配、重复 page ID、frontmatter schema 错误、contradiction、stale index 和超大 AGENTS。

- [ ] **Step 4: 运行测试**

  Run: `uv run python -m pytest tests/autonomous/unit/test_employee_knowledge_compiler.py tests/autonomous/unit/test_employee_knowledge_query.py tests/autonomous/security/test_employee_knowledge_redaction.py tests/autonomous/chaos/test_employee_knowledge_recovery.py -q`

  Expected: FAIL because knowledge package/data kinds do not exist.

- [ ] **Step 5: 扩展 canonical data kinds**

  增加 `KNOWLEDGE_PAGE`、`KNOWLEDGE_INDEX`、`KNOWLEDGE_REVIEW`，内容写加密 Blob；Markdown projector 只消费已提交文档。

- [ ] **Step 6: 实现两阶段编译**

  第一阶段只输出受 schema 限制的 claims/source links；第二阶段更新页面和互链。任何结构不合法、来源缺失或权限越界都进入 review，不直接写 Wiki。

- [ ] **Step 7: 实现 query-first-index**

  先读取 `wiki/index.md`/类型化索引，再按 page ID 读取正文；结果必须返回 page/source citations。首期使用标准全文 token 化和 wikilink 邻接扩展，不增加向量依赖。

- [ ] **Step 8: 验证**

  Run: `uv run python -m pytest tests/autonomous/unit/test_employee_knowledge_compiler.py tests/autonomous/unit/test_employee_knowledge_query.py tests/autonomous/security/test_employee_knowledge_redaction.py tests/autonomous/chaos/test_employee_knowledge_recovery.py -q`

  Expected: PASS.

- [ ] **Step 9: 提交**

  Commit: `feat(knowledge): compile employee history into a wiki`

### Task 8: 持久 TeamRun 图和动态 Coordinator

**Files:**
- Create: `src/autonomous/team/models.py`
- Create: `src/autonomous/team/projection.py`
- Create: `src/autonomous/team/coordinator.py`
- Modify: `src/autonomous/team/service.py`
- Modify: `src/autonomous/provisioning/composition.py`
- Modify: `src/config/settings.py`
- Modify: `.env.example`
- Create: `tests/autonomous/unit/test_team_coordinator.py`
- Create: `tests/autonomous/contract/test_team_run_projection.py`
- Create: `tests/autonomous/chaos/test_team_run_recovery.py`

**Interfaces:**
- `TeamRunV2` 引用 encrypted task Blob，保存 goal、done criteria、turn count、assignment IDs 和 coordinator session key。
- `TeamCoordinatorActor` 是每 `(tenant_key, chat_id, project_id)` 的串行逻辑 Actor。
- `CoordinatorDecision` 只接受冻结的 action/schema/bounds。

- [ ] **Step 1: 写 projection/state machine 测试**

  覆盖 created -> planning -> dispatching -> reviewing/revising -> completed/blocked/canceled；任意未解决 effect 不得进入终态。

- [ ] **Step 2: 写动态分配测试**

  按 role/capability/READY/mailbox load 形成候选；explicit mention 优先；CAS claim 只有一个赢家；无能力员工不会被硬塞任务。

- [ ] **Step 3: 写 Coordinator bounds 测试**

  非法 agent ID、循环依赖、超过 12 turns/32 assignments/4 fanout/8 handoffs、空 done checks 或伪造完成都被拒绝。

- [ ] **Step 4: 写 restart 测试**

  planning、assignment running、contribution committed、final notify executing 四个 kill 点恢复后继续或幂等收敛，不产生 `restart_instruction_unavailable`。

- [ ] **Step 5: 运行测试**

  Run: `uv run python -m pytest tests/autonomous/unit/test_team_coordinator.py tests/autonomous/contract/test_team_run_projection.py tests/autonomous/chaos/test_team_run_recovery.py -q`

  Expected: FAIL because Team v1 stores only digests and fixed pipeline.

- [ ] **Step 6: 实现 Coordinator actor**

  新配置：`autonomous_team_coordinator_tool="coco"`、model/profile/effort 空值委托 provider 默认；所有配置可显式覆盖为 Traex/Codex 等 ACP 工具。

- [ ] **Step 7: 让 v1 service 成为兼容 facade**

  `autonomous_team_runtime_mode=legacy_pipeline|coordinator` 控制显式切换；coordinator 模式不再进入固定三段 `_execute()`。

- [ ] **Step 8: 验证**

  Run: `uv run python -m pytest tests/autonomous/unit/test_team_coordinator.py tests/autonomous/contract/test_team_run_projection.py tests/autonomous/chaos/test_team_run_recovery.py tests/autonomous/unit/test_employee_team_service.py -q`

  Expected: PASS.

- [ ] **Step 9: 提交**

  Commit: `feat(team): add durable model-led coordination`

### Task 9: 群协作、直接 @ 和选择性唤醒

**Files:**
- Modify: `src/autonomous/ingress/router.py`
- Modify: `src/autonomous/membership/service.py`
- Modify: `src/autonomous/team/coordinator.py`
- Modify: `src/autonomous/outbox/service.py`
- Modify: `src/feishu/dispatcher.py`
- Modify: `src/feishu/handlers/slock.py`
- Create: `tests/autonomous/integration/test_employee_group_collaboration.py`
- Create: `tests/autonomous/integration/test_employee_direct_mention_actor.py`
- Create: `tests/autonomous/security/test_employee_bot_loop_guard.py`

**Interfaces:**
- `RouteDecision` 精确区分 `direct_employee`、`team_task`、`collaboration_event`、`ambient_chat`。
- `collaboration_event` 带 `team_run_id/assignment_id/causal_event_id`，只能推进现有 run。

- [ ] **Step 1: 写路由优先级测试**

  直接 @ 某员工只进入该员工邮箱；显式 Team task 进入 Coordinator；普通聊天只入 ledger；Bot contribution 不被重新识别为新 Team task。

- [ ] **Step 2: 写选择性唤醒测试**

  群消息对所有合资格员工可见于 durable ledger，但没有 mention/assignment/claim 时模型调用数为 0；被分配员工调用数为 1。

- [ ] **Step 3: 写协作回合测试**

  员工 A contribution 可成为员工 B review context；A/B 的可见消息使用各自 employee Bot 发送；最终结论由 coordinator/main Bot 发送。

- [ ] **Step 4: 写 loop guard 测试**

  重复 causal ID、超过 handoff、过期 run、非成员 Bot、跨 tenant event 和任意伪造 assignment ID 均拒绝。

- [ ] **Step 5: 运行测试**

  Run: `uv run python -m pytest tests/autonomous/integration/test_employee_group_collaboration.py tests/autonomous/integration/test_employee_direct_mention_actor.py tests/autonomous/security/test_employee_bot_loop_guard.py -q`

  Expected: FAIL before routing/actor integration.

- [ ] **Step 6: 接入生产路由和 Outbox**

  保持主 Bot/员工 Bot 零代发边界；每条员工贡献由该员工 app/generation 发送，Coordinator 最终结果走主 Bot transport。

- [ ] **Step 7: 验证**

  Run: `uv run python -m pytest tests/autonomous/integration/test_employee_group_collaboration.py tests/autonomous/integration/test_employee_direct_mention_actor.py tests/autonomous/security/test_employee_bot_loop_guard.py tests/test_feishu_dispatcher.py -q`

  Expected: PASS.

- [ ] **Step 8: 提交**

  Commit: `feat(team): route selective employee collaboration`

### Task 10: 启动恢复、迁移和生命周期收口

**Files:**
- Modify: `src/autonomous/supervisor/supervisor.py`
- Modify: `src/autonomous/provisioning/composition.py`
- Modify: `src/autonomous/migration/slock_compat.py`
- Modify: `src/autonomous/provisioning/fire_effects.py`
- Create: `src/autonomous/migration/employee_workspace_v1.py`
- Create: `tests/autonomous/integration/test_employee_runtime_startup_order.py`
- Create: `tests/autonomous/chaos/test_employee_runtime_restart_matrix.py`
- Modify: `tests/autonomous/integration/test_employee_fire_authority.py`
- Modify: `tests/autonomous/unit/test_employee_fire_effects.py`

**Interfaces:**
- 恢复顺序固定为 Journal/data projection -> workspace projection -> group ledger -> actor mailboxes -> Team coordinator -> employee Channels -> admission open。
- 关闭顺序反向：admission -> coordinators -> actors/session -> outbox drain -> Channels -> data -> Journal。

- [ ] **Step 1: 写启动依赖测试**

  workspace lint 失败、knowledge Blob 缺失、assignment checkpoint 损坏、actor 未恢复时不得对外报告 employee READY。

- [ ] **Step 2: 写旧员工迁移测试**

  从现有 identity/memory/history projection 创建 workspace；不把 legacy mutable files 当事实源；重复迁移幂等；symlink/冲突目录 fail-close。

- [ ] **Step 3: 写重启矩阵**

  覆盖 1/10/50 员工的 cold ready、部分 warm session、active TeamRun、pending knowledge ingest、Channel reconnect 和 Fire 并发。

- [ ] **Step 4: 运行测试**

  Run: `uv run python -m pytest tests/autonomous/integration/test_employee_runtime_startup_order.py tests/autonomous/chaos/test_employee_runtime_restart_matrix.py tests/autonomous/integration/test_employee_fire_authority.py tests/autonomous/unit/test_employee_fire_effects.py -q`

  Expected: FAIL until composition owns the new services.

- [ ] **Step 5: 接入 Supervisor**

  Actor 和 Coordinator 不各自扫描 Journal；共享一次投影/快照，按 generation fence 启动。Fire 先关闭 mailbox、取消 assignment、关闭 session，再撤权/归档 workspace。

- [ ] **Step 6: 验证**

  Run: `uv run python -m pytest tests/autonomous/integration/test_employee_runtime_startup_order.py tests/autonomous/chaos/test_employee_runtime_restart_matrix.py tests/autonomous/integration/test_employee_fire_authority.py tests/autonomous/unit/test_employee_fire_effects.py -q`

  Expected: PASS.

- [ ] **Step 7: 提交**

  Commit: `feat(runtime): recover employee actors and knowledge`

### Task 11: 可观察性、卡片和运维命令

**Files:**
- Create: `ux/employee-runtime-status.html`
- Modify: `src/autonomous/manager/cards.py`
- Modify: `src/feishu/handlers/slock.py`
- Create: `src/autonomous/team/renderer.py`
- Modify: `src/autonomous/workspace/lint.py`
- Create: `tests/autonomous/unit/test_employee_runtime_cards.py`
- Modify: `tests/test_slock_status_card.py`

**Interfaces:**
- `/roster` 显示 Channel 与 Actor 两个状态：例如 `Bot READY / Agent READY_COLD`。
- `/status` 显示 active assignment、mailbox depth、session warm/cold、identity version、knowledge generation、last checkpoint 和 context quality。
- 新增管理员动作：recycle session、rebuild workspace、lint knowledge、retry review item；不得提供直接编辑权限文件的动作。

- [ ] **Step 1: 先完成状态卡 HTML 预览**

  清晰区分“飞书连接正常”“模型会话热驻留”“员工可接任务”“任务部分上下文”四种概念。

- [ ] **Step 2: 写卡片纯渲染测试**

  空 assignment/空 warning 不渲染空块；长 Team run 使用续卡；每名员工任务仍是一张独立卡。

- [ ] **Step 3: 写错误消息测试**

  context partial 显示可理解的降级说明；action-required 显示 run/assignment、具体 code、已完成贡献和恢复动作。

- [ ] **Step 4: 运行测试**

  Run: `uv run python -m pytest tests/autonomous/unit/test_employee_runtime_cards.py tests/test_slock_status_card.py -q`

  Expected: FAIL before UI integration.

- [ ] **Step 5: 实现 handler -> session -> render/delivery 路径**

  handler 只调用 runtime facade，不读取 projector 或 session 内部结构。

- [ ] **Step 6: 验证**

  Run: `uv run python -m pytest tests/autonomous/unit/test_employee_runtime_cards.py tests/test_slock_status_card.py tests/test_card_session.py -q`

  Expected: PASS.

- [ ] **Step 7: 提交**

  Commit: `feat(ui): expose employee runtime health`

### Task 12: 灰度、切换和删除 v1 固定流水线

**Files:**
- Modify: `src/config/settings.py`
- Modify: `.env.example`
- Modify: `src/autonomous/team/service.py`
- Modify: `src/slock_engine/engine.py`
- Modify: `docs/goals.md`
- Modify: `docs/adr-employee-runtime-profiles.md`
- Modify: `.Memory/2026-07-17.md`
- Modify: `.Memory/Abstract.md`
- Create: `tests/autonomous/acceptance/test_persistent_employee_department.py`

- [ ] **Step 1: Shadow workspace/knowledge**

  v1 仍执行，v2 只投影目录、编译知识和比较 context；任何 mismatch 记录 secret-free audit，不改变结果。

- [ ] **Step 2: 单员工 direct @ 切到 actor**

  证明 session reuse、identity recycle、workspace access、cancel/restart 和员工自有 Outbox 后，把 `autonomous_employee_runtime_mode` 默认改为 `actor`。

- [ ] **Step 3: Team 切到 coordinator**

  真实租户完成两名以上员工的分工、review、revision、群内贡献、主进程重启续跑和 final notify 后，把 `autonomous_team_runtime_mode` 默认改为 `coordinator`。

- [ ] **Step 4: 删除运行时 fallback**

  删除 Team 固定 analysis/review/synthesis 执行路径和员工 one-shot `_run_acp_session` 生产调用点；保留必要的 legacy virtual Slock role 路径，不能误删普通 Slock 功能。

- [ ] **Step 5: 运行完整自动验证**

  Run:

  ```bash
  uv run python -m pytest tests/autonomous/ -q
  uv run python -m pytest tests/test_slock*.py tests/test_feishu*.py tests/test_card_session.py -q
  uv run ruff check src/autonomous/ src/agent_session/ src/slock_engine/ src/feishu/
  uv run python -m src.main --validate
  uv run python -m pytest tests/test_docs_references.py -q
  git diff --check
  ```

  Expected: all commands exit zero.

- [ ] **Step 6: 真实租户验收**

  验收表必须记录以下证据，不能用 mock 代替：

  1. 新 Hire 员工冷启动后 `Bot READY / Agent READY_COLD`。
  2. 同员工连续两个任务只启动一个 ACP session；idle 回收后可重新 warm。
  3. 直接 @ 员工可以独立完成任务，并读取自己的身份/Wiki。
  4. 两到四名员工在 Team run 中按职责分工、互评和修订。
  5. ordering 缺失只产生 partial context 告警，不终止内部 assignment。
  6. Team run 中途重启后继续，最终只发送一次结果。
  7. 未被 mention/assign 的员工模型调用数为零。
  8. 员工无法读取其他员工目录、Vault、Journal 或修改自己的控制投影。
  9. Fire 后 mailbox/session/Channel/permissions 全部撤销，workspace 按归档合同处理。
  10. 1/10/50 employee cold recovery 和 Channel reconnect soak 没有无界 session/process 增长。

- [ ] **Step 7: 更新文档和 Memory**

  记录最终默认模式、真实验收范围、剩余 hardened profile 风险和回滚方式。

- [ ] **Step 8: 提交**

  Commit: `refactor(team): cut over to persistent employee actors`

---

## 6. 完成定义

只有同时满足以下条件，才能宣称用户目标完成：

- `/hire` 固化完整员工身份、职责、能力、工具、模型、性格和权限 profile。
- 重启后员工自动恢复到 READY_COLD，无需重新创建身份或手工加载记忆。
- ACP 能力支持时连续任务复用会话；不支持时能从目录/检查点等价恢复。
- 员工启动必然获得身份和目录导航，不依赖某个后端碰巧读取项目里的 AGENTS。
- 员工能查到“正在做什么、做过什么、为什么这样做、证据在哪里”，且不能借此自我扩权。
- Team 由真实 Coordinator Agent 动态分工，不再固定第一个员工 analysis/synthesis。
- Team task 原文和执行图可恢复，进程重启不再出现 `restart_instruction_unavailable`。
- `ORDERING` 等远端 enrichment 问题不再把内部 assignment 变成 `context_unavailable`。
- 群事件对协作系统可见，但只有必要员工被模型唤醒。
- 自动测试、真实租户验收、文档、Memory 和安全边界全部通过。

## 7. 明确非目标

- 首期不做向量数据库、知识图谱 UI、Obsidian 插件或通用个人知识库产品。
- 不保证所有后端都提供真正长期 OS 进程；提供的是一致的长期员工语义。
- 不让每个员工复制完整主 Bot context，也不让每个 Bot 对每条群消息自由发言。
- 不把隐藏 chain-of-thought、完整原始 prompt 或所有聊天正文写入可读 Markdown。
- 不用本方案替换 Deep/Spec/Worktree/Workflow 的执行策略；员工 runtime 是 transport/session/knowledge 层，策略仍保持独立。

## 8. 方案自检清单

- [ ] 所有新类型都有单一模块归属，未与现有 `AgentRuntime`、Channel Supervisor 或 Slock TaskQueue 混名。
- [ ] 所有状态变更都能映射到 Journal event 和 frozen projection。
- [ ] 所有外部 effect 在执行前已 fsync/anchor，终态前已 disposition。
- [ ] 所有 Markdown 都可从 Journal/Blob 重建，删除整个 workspace 后 rebuild 结果相同。
- [ ] 所有 source ref 都先 ACL 再解密，materializer 不扩大数据可见性。
- [ ] `AGENTS.md`、bootstrap prompt、Codex global AGENTS 的 digest 一致。
- [ ] 文档中没有未决占位语或未冻结的接口名。
- [ ] `git diff --check`、文档引用测试和配置验证通过。
