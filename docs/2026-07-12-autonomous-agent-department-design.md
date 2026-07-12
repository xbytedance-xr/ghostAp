# GhostAP 生产级自主 Agent 部门设计

日期：2026-07-12  
状态：已按用户授权自动采纳 `grill-me` 推荐决策

## 1. Goal Snapshot

- Goal：把 GhostAP 建设为可投入生产使用的 Agent 部门管控平台，每名员工都是独立飞书 Bot，并由现有 ACP 工具和模型执行任务。
- Success criteria：完整实现 `/hire`、独立 Bot Provisioning、Channel 生命周期、Slash Commands、Multi-Bot 路由、员工独立响应、`/fire`、按日历史、Thread Context、状态卡，以及重启恢复、权限、安全和真实租户验收。
- Constraints：Journal 是唯一事实源；保持文件存储；不修改 Deep/Spec/Worktree/Workflow 引擎逻辑与路由；不替换主 GhostAP Bot WebSocket；不修改 Slock `_run_acp_session` 执行语义；使用官方飞书 SDK。
- Non-goals：本轮不引入数据库；不承诺 GhostAP 能删除飞书开放平台应用；不把旧 Slock 虚拟 Agent 冒充独立 Bot；不提供 Web 管理后台。

## 2. 当前状态与问题定义

当前 `src/autonomous/` 已具备 Journal、Projection、Scheduler、Policy、Broker、Verifier、Reporter 和 Supervisor 等安全内核，但生产 composition 尚未完成。真实 `/hire` 仍创建共享主 Bot 身份下的 Slock 虚拟 Agent；独立 Bot Provisioning、Channel、Slash、Router 和 `/fire` 均未形成闭环。

目前还存在三套员工模型：

1. `src/slock_engine/models.py::AgentIdentity`：旧 Slock 文件身份。
2. `src/autonomous/employees.py::Employee`：可变、仅内存的临时模型。
3. `src/autonomous/domain/employees.py::EmployeeDefinition`：冻结的 v5 领域模型。

生产实现不能继续扩展三方双写，必须先统一事实源和生命周期。

## 3. 方案比较

### 方案 A：直接扩展旧 Slock

把凭证、Channel 和 Slash 字段直接加到 `AgentIdentity`，让旧 `/hire` 完成所有外部调用。

优点：短期改动少，能快速复用现有 handler。  
缺点：绕过 v5 Journal，无法可靠恢复 Hire/Fire 部分失败；会扩大 Registry 与 Autonomous 双事实源；不满足项目当前 Journal-only 不变量。

结论：拒绝。

### 方案 B：v5 Journal SSOT + Slock Runtime Adapter

以 v5 Journal 记录员工、Bot principal、Saga、membership、Inbox、Outbox 和外部 Effect；`AgentRegistry` 与文件布局作为 Projection。独立员工 transport 通过类型化 gateway 调用 Slock，最终复用原 `_run_acp_session`。

优点：满足耐久性、安全和审计要求；保留成熟 ACP 路径；可逐步迁移旧数据；不会触碰独立引擎。  
缺点：初期需要补齐 production composition、Projection 和迁移门禁。

结论：推荐并采用。

### 方案 C：每名员工完整独立 GhostAP 服务

每个 Bot 都启动完整 GhostAP runtime、Journal 和 Slock 实例。

优点：隔离最强。  
缺点：状态与团队协调分散，运维成本随员工数线性增长，无法自然实现统一部门级控制面。

结论：拒绝。只把 Channel transport 放入独立子进程，不复制业务 runtime。

## 4. 总体架构

```text
主 GhostAP Bot（现有 WS，不改）
        │ /hire /fire /new-team /team dissolve
        ▼
DepartmentCommandFacade
        │
        ▼
EmployeeLifecycleService ───── CredentialVault
        │                             │
        ▼                             │
JournalWriter ──► Projections ──► AgentRegistry
        │                    ├── identity.json
        │                    ├── slash_commands.json
        │                    └── membership / health
        │
        ├── Hire/Fire Saga + Effect reconciliation
        ├── Durable Inbox / Outbox
        └── Supervisor
                  │
                  ├── Employee Channel child A ── Bot A
                  ├── Employee Channel child B ── Bot B
                  └── Employee Channel child N ── Bot N
                              │ authenticated local IPC
                              ▼
                    EmployeeBotRouter
                              │
                              ▼
                    SlockDispatchGateway
                              │
                              ▼
                    existing _run_acp_session
```

### 4.1 单一事实源

- Journal 是员工生命周期、Bot principal、群关系、Saga、Inbox、Outbox 和 Effect disposition 的唯一事实源。
- `AgentRegistry` 是全局查询 facade；其内存状态与 `identity.json` 是可重建 Projection。
- handler、Slock 和 Channel worker 不得直接写身份文件。
- L1、history、reasoning、skill profile 等大内容保留文件存储；Journal 保存路径、版本与 hash。
- 删除运行路径中的可变临时 `Employee`，统一使用冻结领域对象和 `dataclasses.replace()`。

### 4.2 员工标识

- `agent_id` 使用不可变随机 ID，不再由 `tool:model:name` 派生。
- 工具、模型、Effort、角色变化不改变 ID。
- 新员工一律使用随机 ID；legacy 迁移也生成随机 canonical ID，并在 Journal 保存唯一、持久的 `legacy_id_alias`。Importer 不得临时选择保留旧派生 ID。
- 活跃员工名称按 Unicode `casefold()` 做部门级唯一约束。
- 一个 active `app_id` 只能绑定一名员工；一个员工只能有一个 active Channel generation。

### 4.3 原始凭证要求的安全化解释

原始目标中的“凭证存入 AgentIdentity”解释为：`AgentIdentity` 持有完整的逻辑凭证绑定 `app_id + credential_ref`，可通过受权的 Vault resolver 取得 secret；真实 `app_secret` 不进入任何身份投影。这是满足生产安全的等价实现，验收必须同时证明身份可解析凭证、所有投影均无明文 secret。

## 5. 数据模型与文件布局

```text
~/.ghostap/slock/
├── agents/<agent_id>/
│   ├── identity.json
│   ├── memory/MEMORY.md
│   ├── history/YYYY-MM-DD.jsonl
│   ├── reasoning/
│   ├── skill_profile.json
│   └── slash_commands.json
├── credentials/<credential_ref>.enc
├── archives/agents/<agent_id>/<retired_at>/
│   ├── archive_manifest.json
│   └── <archived employee files>
```

```text
~/.ghostap/autonomy/
├── journal/
├── snapshots/
├── inbox/
└── outbox/
```

`~/.ghostap/autonomy/` 是 v5 控制状态唯一 canonical root；员工业务投影继续位于 `~/.ghostap/slock/agents/`。若历史版本产生其他 autonomy/autonomous 目录，启动执行一次性原子迁移并留下 tombstone；两个有效 root 同时存在时 fail-close，禁止按 mtime 猜测事实源。

`identity.json` 包含非敏感字段：

- agent ID、tenant、owner、name、emoji；
- tool、model、profile、effort；
- role、persona、personality traits、permissions；
- worker type、lifecycle state、version；
- bot principal ID、app ID、credential ref；
- member groups；
- created/updated timestamps。

`app_secret` 不进入 identity、Journal、snapshot、日志、卡片或 history。

## 6. Credential Vault

- 使用认证加密（AES-GCM），每条凭证独立 nonce，并把 `agent_id + app_id` 作为 associated data。
- master key 由 `src/config` 从 `.env` 读取；密钥不与密文同目录。
- 目录权限 `0700`、文件权限 `0600`；临时文件写入、fsync、原子 rename。
- 支持 put/resolve/rotate/destroy；错误中只出现不可逆 ref/hash。
- credential ref 由 `hire_intent_id + provisioning_attempt_id` 确定生成；加密 envelope 包含认证过的 key ID、agent/app/attempt metadata 和 durable receipt。
- 支持多 key 读取、active key 写入和原子 rewrap；旧 key 只有在所有条目验证迁移后才能撤销。启动时扫描未被 Journal 引用的 receipt，并按 attempt 与 Hire Saga 对账。
- 未配置 master key 时，VISIBLE employee 功能 fail-close；禁止明文降级。
- secret 只交给 Provisioning/Slash/Channel transport，不进入 ACP shell 环境。

## 7. `/hire` Provisioning Saga

生命周期：

```text
DRAFT
  → PROVISIONING_APP
  → STORING_CREDENTIAL
  → CONFIGURING
  → VALIDATING
  → READY_PENDING_VERIFICATION
  → ACTIVE

任一不可自动恢复的外部未知结果 → ACTION_REQUIRED
```

流程：

1. 验证命令来自主 GhostAP Bot P2P/DM，调用者是配置管理员。
2. 生成稳定 `hire_intent_id`，预留部门级唯一名称，Journal 提交 `hire.requested`。
3. 工具、模型、Profile、Effort、角色和 persona 固化在 intent 中；卡片只携带 opaque intent ID。
4. durable activity 调用官方 `lark_oapi.aregister_app()`；其 `on_qr_code/on_status_change` 是同步 callback，通过 `ProvisioningCallbackBridge` 把不可变数据调度到 async Journal/Outbox 后立即返回，由 activity 追踪并等待 durable ACK。callback 自身不直接做飞书网络回复。
5. 创建卡显示十分钟链接、申请人、到期时间和取消入口。
6. SDK 返回 `client_id/client_secret` 后，先以 attempt ID 原子写 Vault 并 fsync，再把 app ID + credential ref 写 Journal。
7. 启动员工 Channel 子进程。
8. Reconcile `/task`、`/status`、`/history`、`/memory`、`/stop`。
9. 验证 token、Bot identity、Channel ready 和 Slash 服务端状态，进入 `READY_PENDING_VERIFICATION`。
10. 创建卡提供“打开员工 Bot 并发送 `/status`”的人机验证入口；收到第一条真实消息并由同一 Bot 成功回复后，才提交 `employee.activated` 并显示“已就绪”。

`ProvisioningCallbackBridge` 必须追踪每个 callback 对应的持久化 future；activity 返回或退出前等待这些 future 完成。故障注入覆盖 callback 已触发、durable ACK 前后 kill 的两个边界。

Bridge 明确采用非阻塞方案：同步 callback 只 `put_nowait/create_task` 并立即返回，绝不等待同一 event loop 上的 Journal future；activity 保存所有 future，并在退出前逐个 await durable 结果。增加同线程死锁回归测试。

`READY_PENDING_VERIFICATION` 使用专用 `VerificationRouter`：只接受绑定扫码管理员、hire intent、一次性 nonce/TTL 的 Bot DM `/status`，只执行健康回环，不开放普通 task 或工具。成功回复后原子提交 ACTIVE；验证超时会提醒、允许重新发入口或管理员取消，不能无限占用 visible employee 配额。

Hire 链接过期、用户取消或 activity 无终态分别提交 `hire.expired`、`hire.cancel_requested`、`hire.abandoned`。确定未创建应用时停止 activity、释放名称；外部应用存在性未知时把 reservation 转成 tombstone 并进入 ACTION_REQUIRED。管理员提供 status、retry-safe-step、confirm-orphan、abandon 操作，禁止直接自动重建同名应用。

### 7.1 SDK 版本门禁

当前锁定 `lark-oapi==1.6.5` 的运行时签名不支持 `app_preset/addons/create_only/app_id`。已验证官方 `1.7.1` wheel 支持这些参数，因此实施时固定升级到 `1.7.1`，更新 `uv.lock`，并增加 signature contract test。

启动 capability gate 必须确认：

- `aregister_app` 参数完整；
- `create_only=True` 可用；
- app preset 和 addons 能被传入；
- 测试租户真实 E2E 证明名称、头像、权限、事件和 callback 生效。
- 行为门禁还必须证明平台接受这些配置、应用可用、Bot identity 可解析且 Slash POST/GET 成功；签名存在或 SDK 返回成功都不是充分证据。

门禁失败时 `visible_employee` 保持隐藏，不回退手写 OAuth HTTP。

### 7.3 Employee App Permission Manifest

Provisioning 使用版本化、带 hash 的最小权限 manifest，并设置 `addons.preset=False`，避免官方宽泛默认 preset 自动加入文档/通讯录等无关权限。初始 desired manifest：

- tenant scopes：`application:application:self_manage`、`application:bot.basic_info:read`、`application:app_slash_command:read`、`application:app_slash_command:write`、`cardkit:card:read`、`cardkit:card:write`、`im:chat.members:bot_access`、`im:chat:read`、`im:message.group_at_msg:readonly`、`im:message.group_at_msg.include_bot:readonly`、`im:message.p2p_msg:readonly`、`im:message:readonly`、`im:message:send_as_bot`、`im:message:update`、`im:resource`；
- tenant events：`im.message.receive_v1`、`im.chat.member.bot.added_v1`、`im.chat.member.bot.deleted_v1`；
- callbacks：`card.action.trigger`；
- capability：Bot enabled、WebSocket event mode、Slash desired spec version。

创建后读取 observed config 并与 manifest 精确 diff；缺失、额外或平台忽略的未知项都阻止 ACTIVE，进入 ACTION_REQUIRED。Manifest 升级同样走可恢复 reconciliation，不做隐式扩权。

### 7.2 Provisioning 崩溃窗口

官方 Device Flow 无可持久化 resume token，不能承诺外部应用 exactly-once：

- Vault 已 fsync、Journal 未引用：恢复时按 attempt ID 补交本地事件。
- 平台可能已创建、SDK 尚未返回 secret 时进程崩溃：标记 `ACTION_REQUIRED`，提示管理员检查孤儿应用；禁止自动再次 `create_only`。
- 相同 SDK 状态通知和卡片 action 按 intent、nonce、tenant、user 和 TTL 幂等处理；凭证由 `aregister_app()` 返回值取得，不假设存在额外 HTTP 凭证回调。
- CONFIGURING/VALIDATING 的确定性失败会停止 Channel child、保留加密凭证用于有界次数和期限的 safe-step retry，并清理已确认创建的部分 Slash；到期仍未恢复则进入 ACTION_REQUIRED。未知外部结果不做盲目补偿，也不释放可能仍对应外部应用的名称 tombstone。

## 8. Employee Channel Connection Manager

### 8.1 子进程隔离

锁定的 `lark-channel-sdk==1.1.0` 在 `lark_channel/ws/client.py` 使用模块级全局 event loop。多个 `FeishuChannel` 同进程并发会有 loop ownership 风险。

因此当前生产策略是“一员工 Channel 一子进程”：

- worker 必须通过 `spawn/exec` 启动 fresh interpreter，禁止使用默认 `fork`；启用 close-on-exec FD allowlist，只继承一次性 credential pipe 和明确的事件 IPC。
- 子进程只持一个 `FeishuChannel` 和一份员工凭证。
- 主进程 Supervisor 管理 PID、generation、重启次数、backoff 和健康状态。
- 业务 Router、Journal、Slock runtime 均留在主进程。
- 通过认证的本地 IPC 传输规范事件与响应指令；IPC 不传 master key，不写 secret 日志。
- 主进程只在启动 worker 时解析该员工 secret，并通过一次性继承 pipe 交付后立即关闭；secret 不进 argv、环境变量或常规事件 IPC，worker 也无法读取其他员工的 Vault 条目。
- 安全测试必须证明 worker 看不到 master key、其他员工 secret 和未授权文件描述符。
- `ChannelWorkerSandbox` 优先使用独立低权限 UID/user namespace；同时设置 `PR_SET_DUMPABLE=0`、`RLIMIT_CORE=0`、no-new-privileges、最小文件系统视图和 IPC allowlist。若部署平台不能提供独立 UID，必须验证 parent/worker 的 ptrace 与 `/proc` 隔离；验证失败时 VISIBLE employee fail-close。
- 未来只有 Channel SDK 移除全局 loop，且 10/50 Bot 同进程真实 soak 通过后，才能增加 in-process backend。

### 8.2 生命周期

启动：

1. 从一次性 bootstrap pipe 读取该员工 secret；主进程已完成 Vault 解析，worker 不持有 master key。
2. 构造 `lark_channel.FeishuChannel`。
3. 使用 SDK 常量注册 message、cardAction、reconnecting、reconnected、error、botAdded、botLeave。
4. `connect_until_ready(timeout=...)`，检查 connection snapshot。
5. 向 Supervisor 报 READY。

停止：

1. 关闭 ingress gate。
2. drain/cancel handler。
3. `await disconnect()`。
4. 等待子进程退出；超时才 TERM/KILL。

生产最终使用 Channel SDK strict security mode；灰度期可先 audit。SDK 内存 dedup 是第一层，跨进程权威 dedup 仍由 Durable Inbox 提供。

SDK 1.1.0 的 reconnect 通知是同步调用，必须注册同步、非阻塞 shim，只做线程安全 IPC enqueue；message/cardAction 使用 async handler。Contract test 禁止未 await coroutine warning，并验证 Supervisor 收到状态。

## 9. Durable Ingress、Router 与 ACP

### 9.1 ACK 边界

Channel child 收到 `InboundMessage` 或 `CardActionEvent` 后：

1. 以进程绑定的 employee ID 和 app ID 构造 `EmployeeEnvelope`。
2. 通过 IPC 交主进程。
3. 主进程写 Durable Inbox、fsync 并返回 ACK。
4. child handler 得到 ACK 后才能返回。

Dedup key 优先使用 `tenant + employee + event_id`；无 event ID 时使用 `tenant + employee + message_id + event_type + action identity`。

Journal 不可写时默认拒绝，绝不先执行后补记。

IPC 请求携带稳定 envelope ID 和 Channel generation；主进程对首次写入和已存在 dedup record 返回同一 ACK。child 使用小于飞书处理期限的可配置 ACK timeout；超时让 handler 失败以触发平台重投，并记录 backpressure、ACK latency 和 timeout 指标，禁止无限阻塞 SDK handler。

Ingress 在创建 task 前解析 sender principal，默认忽略自己和所有 GhostAP 托管 Bot 的普通消息、卡片更新与 mention，防止多 Bot 同群自动回环。只有显式、带授权 correlation ID 的 inter-agent protocol 才允许 Bot-to-Bot 消息进入任务路由。

### 9.2 规范消息与附件

`EmployeeEnvelope` 是带类型的 parts 列表，覆盖 text、post、image、file、reply/thread 和 card action：

- 图片/文件由目标员工凭证通过 Channel SDK 下载；
- 配置 MIME allowlist、单文件大小、总大小、数量和下载超时；
- 写入员工隔离的任务临时目录，权限 `0700/0600`，记录 hash，不使用用户文件名拼路径；
- 拒绝可执行文件、路径穿越、超限和类型不匹配；任务结束或恢复处置后清理；
- Gateway 使用现有 ACP attachment/path 表达把安全本地路径交给 Slock runtime。

每种 message part 都必须有 Router→Slock E2E，不能用“事件被解析”替代“附件实际可供 ACP 使用”。

### 9.3 路由

- employee ID 来自 Channel worker binding，不信任消息文本、mention 名或卡片 payload。
- 普通 Router 校验 lifecycle=ACTIVE、channel generation、tenant、群 membership 和调用者 ACL；唯一例外是第 7 节受限的 `VerificationRouter`，它不能创建普通 task。
- 单员工默认并发 1，使用独立有界队列；多员工可并行，并受全局和每群上限控制。
- 队列满时由该员工 Bot 返回 busy 状态，不丢消息。
- Router 只调用类型化 `SlockDispatchGateway`。
- Gateway 在单进程存活期间进入现有 Slock agent execution，并以 spy contract 证明一个 accepted attempt 只调用一次原 `_run_acp_session`。
- 不复制 Coco/Claude/Aiden/Codex/Gemini/Traex/TTADK 后端分支；TTADK 继续保持 CLI bridge 语义。
- 具备 `shell` 权限的员工可以沿现有 ACP shell 路径调用 `lark-cli`；无 shell 权限时必须拒绝。测试同时证明 `lark-cli` 可用且 ACP 无法读取 Vault、master key或其他员工凭证。

### 9.4 ACP 崩溃语义

现有 `_run_acp_session` 不是可查询或幂等的外部 Effect，不能承诺跨进程崩溃 exactly-once：

1. 调用前 fsync `attempt.dispatch_committed`。
2. 若进程在终态提交前崩溃，该 attempt 进入 `UNKNOWN/ACTION_REQUIRED`，禁止自动重跑。
3. 管理员显式 retry 创建新的 attempt，并披露原 attempt 可能已产生副作用。

RPO=0 仅表示入站事件不丢、重复事件不重复创建逻辑 task；不表示 ACP 副作用跨崩溃恰好一次。

## 10. Context 组装

严格顺序：

```text
Protected: system constraints + current user message
1. 当前 Thread 全量消息（受配置上限）
2. 当前群最近消息（去除 Thread 中已出现 message_id）
3. 员工 L1
4. 群 L2
```

Thread key 是 `(tenant, chat_id, thread_root_id)`。Context assembler 通过官方消息 API 分页读取 Thread；不能用群最近 N 轮冒充 Thread。

Assembler 在开始时记录 thread revision/watermark，完成后生成不可变 `ContextSnapshot`。分页期间新增消息按 watermark 进入下一次 snapshot；删除/编辑按 API 返回版本记录。缺 scope、API 超时、分页失败或顺序无法确定时，任务进入显式 `CONTEXT_UNAVAILABLE`，员工卡披露原因；禁止静默用群 replay 代替 Thread。

配置：

- `autonomous_thread_context_max_messages`
- `autonomous_thread_context_max_chars`
- `autonomous_group_context_max_messages`
- `autonomous_context_max_tokens`

超限时从最低优先级删除：L2 → L1 → 群最近 → Thread 最旧消息。系统约束和当前消息不可裁剪。每次记录各层大小和 truncation reason，但不记录未脱敏正文。

## 11. 员工响应与状态卡

所有输出先写 employee-scoped Durable Outbox，再由同一员工 Channel 发送；绝不回退主 Bot。

状态机：

```text
QUEUED → RUNNING → COMPLETED
                 → FAILED
                 → CANCELED
                 → ACTION_REQUIRED
```

- 每个 task 一张主状态卡。
- 首次建卡是已锚定的 Effect，使用稳定 `outbox_id` 作为飞书 UUID/idempotency key；员工 child 先通过 `channel.send(..., opts.uuid)` 幂等创建主卡并持久化 message binding。
- child 随后构造并独占 `CardStreamController`，其 `ensure_created` 返回已绑定 message ID，producer 从 child 内的 `outbox_id → snapshot queue` 消费并调用 `controller.update(snapshot)`；controller 不跨 IPC、不由主进程持有，也不阻塞入站 message handler。
- `_run_acp_session` 保持不变，因此流式输出的是真实进度与状态快照；最终结果在 session 返回后写入终态快照，不伪造 token delta。
- `terminal_version` 单调递增；终态提交后拒绝迟到 progress。
- controller 不能持久化；Outbox 持久化 card snapshot、message ID 和 terminal version。重启后用员工凭证重建发送或 REST patch。
- 卡片投递失败不改变任务终态，只留下待重试 Outbox。
- `ACTION_REQUIRED` 表示“结果未知、可能已有外部副作用、禁止自动重跑”，不是 FAILED。`/status` 展示 unknown attempt；`/stop` 不能改写它；管理员只能显式 dispose 或创建新 retry attempt，旧 attempt 的任何迟到结果均被 fencing 拒绝。
- 只复用现有 card 的纯 render/state/protocol；新增 `EmployeeChannelDelivery` 实现相同 delivery protocol 并由 Employee Response Port 注入。主 Bot delivery 与员工 delivery 不能互相 fallback，import guard 锁定该边界。

启动 capability contract 必须验证 SDK 的稳定 UUID send、既有 message ID controller 和 patch 行为。若官方 public controller 无法安全绑定预建卡，使用 employee-scoped REST `update_card()` 作为更新 backend，但 VISIBLE employee 在满足“单卡幂等 + 流式状态”真实 E2E 前保持关闭；不得直接调用当前 `stream()` 的非幂等建卡路径。

发布门禁逐一验证 QUEUED/RUNNING/COMPLETED/FAILED/CANCELED：发送 app ID 必须等于目标员工 app ID、主 Bot send count 为零、每 task 仅一张主卡、断线重启后 eventual delivery、终态后任意迟到 progress 均被拒绝。

## 12. Slash Command Manager

使用官方 `lark-oapi Client.arequest(BaseRequest)` 调用 `/open-apis/application/v7/app_slash_commands`，不手写 HTTP 鉴权。

Desired set：

- `task`：给该员工分配任务；
- `status`：读取 Journal Projection；
- `history`：按日期范围读取员工历史；
- `memory`：按 ACL 返回 L1 摘要；
- `stop`：取消当前员工 attempt。

每次 reconcile：GET 服务端全量 → diff → POST/PATCH/DELETE → GET 验证。`slash_commands.json` 保存 observed ID、spec hash 和 reconcile 时间，只是缓存；文件缺失时可由服务端重建。

Slash 配置子门禁只要求服务端 desired set 精确匹配，不等待客户端缓存；整体 ACTIVE 仍需第 7 节的首次真实收发验证。卡片显示“服务端已配置，客户端传播中”，真实验收同时覆盖桌面端和移动端传播。

## 13. 团队与 `/role add`

- `/new-team <name>` 仅允许主 Bot DM 管理员调用，由 Manager REST gateway 创建真实群并建立 Slock team projection。
- `/team dissolve` 仅允许主 Bot DM 管理员调用；它终结 GhostAP 团队，不默认承诺删除飞书群。
- 群内 `/role add <employee>` 只允许部门管理员或 team owner。
- 主 Manager Bot 调用群成员 API，使用 `member_id_type=app_id` 添加员工 Bot；每 chat 串行 mutation。
- 主 Bot 必须具备成员写权限并已在群内；能力不足时显示手动添加指引，不伪造成功。
- 远端成功后才提交 membership Projection；远端结果未知时先查询群成员再决定 commit/retry。
- `/role remove` 只移除该群 membership，不删除全局员工。
- `botAdded/botLeave` 事件先进入 Durable Inbox，再触发 membership reconciliation Effect；周期性服务端成员审计修复人工增删导致的漂移。远端移除后立即关闭该 chat 的 ingress/outbox 权限；状态未知时标记 DEGRADED，不继续信任本地 Projection。

## 14. `/stop` 与终态竞态

1. Journal 提交 `cancel_requested(attempt_id, epoch)`。
2. 调用现有 Slock cancel/session stop 路径。
3. 提交唯一终态。
4. Outbox 更新员工状态卡。

若 COMPLETED/FAILED 先提交，`/stop` 返回“已终结”；若 cancel 先提交，迟到 ACP 结果不能改写为 COMPLETED。`/stop` 幂等，仅允许部门管理员、team owner 或原任务发起者。

## 15. `/fire` Saga

```text
ACTIVE → RETIRING → ARCHIVED
          └────────→ ACTION_REQUIRED
```

`RETIRING` 一经提交，Router 立即拒绝新任务。

顺序：

1. 主 Bot DM 管理员鉴权，提交 `fire.requested`。
2. 停止新任务，默认请求取消当前执行并等待可配置 grace period；超时把 attempt 置 ACTION_REQUIRED，禁止销毁凭证或进入 ARCHIVED，除非管理员显式 disposition。需要等待自然完成时必须显式使用 `/fire --drain`，且有最大期限与卡片披露。
3. 删除 Slash Commands 并 GET 验证。
4. 断开员工 Channel。
5. 从已知团队移除员工 Bot，并提交 membership disposition。
6. 幂等销毁 Vault secret；销毁后不得重新连接该员工。
7. 生成并 fsync 最终 `archive_manifest.json`，包含文件 hash、日期范围、cleanup disposition 和 credential destroyed 证据。
8. 执行已锚定的同文件系统原子目录移动；rename 失败时保持 RETIRING，从原目录继续恢复。
9. 所有 Effect 已处置后提交 `employee.archived`。

任一步结果未知时保持 RETIRING/ACTION_REQUIRED 并重试，不显示成功。

当前没有已验证的官方应用删除 API，终态卡必须披露：GhostAP 已停止托管、清理命令并移除本地凭证，但开放平台应用可能仍需管理员手动停用或删除。

archive manifest 持久记录 `external_app_disposition=manual_deletion_required`、app ID hash、管理员确认时间与确认人；员工审计视图持续展示。只有管理员明确确认平台已停用/删除后才改为 `externally_disposed`，且不得声称由 GhostAP 删除。

### 15.1 外部 Effect 锚定矩阵

所有外部步骤遵守 `PREPARED + fsync/anchor → EXECUTING + fsync/anchor → external call → query/COMMITTED`：

| Effect | 幂等键 | 查询/恢复 | Unknown 处置 |
| --- | --- | --- | --- |
| App provisioning | hire intent + attempt | SDK 无可靠 resume/query secret | ACTION_REQUIRED，禁止自动重建 |
| Vault put/rewrap/destroy | credential ref + key version | 扫 receipt/envelope | 对账后 commit 或继续重试 |
| Channel start/stop | employee + generation | child health/PID/identity | fence 旧 generation 后 reconcile |
| Slash create/update/delete | employee + spec hash + command | 服务端 GET | GET 后 commit/retry |
| 群 add/remove Bot | chat + employee + membership epoch | 查询群成员 | 查询后 commit/retry |
| Archive rename | fire intent + manifest hash | 检查源/目标/manifest | 保持 RETIRING 并完成单向恢复 |

任何不能查询且非幂等的外部调用都不得在崩溃恢复时盲目重发。

## 16. 执行历史与记忆

### 16.1 按日历史

- 路径：`history/YYYY-MM-DD.jsonl`。
- 分片时区默认 UTC，可通过 `autonomous_history_timezone` 显式配置。
- 每条记录有 UTC timestamp + Journal sequence。
- 日期范围为 `[start_date, end_date]` 闭区间，只打开相关分片。
- 终态 execution record 先写 content-addressed blob/staging 并 fsync，再由 Journal event 提交 record ID/hash；按日 JSONL 是由 Projection writer 幂等生成、可重建的查询投影。
- 成功、失败、取消、超时都写入历史。
- 记录 task/run/message/thread/chat、start/end/duration、tool/model/effort、结果状态、安全摘要、token/tool usage。
- 旧 `execution_history.jsonl` 一次性迁移，禁止长期双写。
- 恢复时清理未被 Journal 引用的孤儿 staging，并从 Journal/blob 重建缺失或损坏分片；Journal 提交前必须验证 blob 已 durable。

### 16.2 L1、L2、技能与推理

- L1 迁移到 `memory/MEMORY.md`，兼容读取旧根目录 `MEMORY.md`，迁移成功后单写新路径。
- L2 保持群级共享记忆。
- skill profile 与 reasoning 继续使用现有文件格式，但写入增加版本/hash 和原子替换。
- `/memory` 只返回摘要，并执行 tenant、team、requester ACL。
- L1、skill 和 reasoning 先写 content-addressed blob，再提交 ref/hash/version event，最后 materialize 投影文件；崩溃恢复始终以 Journal sequence 重建。
- 完整 L1 默认仅部门管理员在主 Bot DM 可读；群内 `/memory` 只返回当前 chat/thread 派生的安全摘要。
- `/history` 按 tenant/chat/task/requester 做行级过滤；跨群完整历史仅部门管理员可读。

## 17. 权限与卡片安全

| 操作 | 通道 | 权限 |
| --- | --- | --- |
| `/hire`、`/fire` | 主 Bot P2P/DM | configured admin |
| `/new-team`、`/team dissolve` | 主 Bot P2P/DM | configured admin |
| `/role add/remove` | 目标群 | admin 或 team owner |
| 员工 `/task` | 员工 DM/所属群 | tenant/member policy |
| 员工 `/status/history/memory` | 员工 DM/所属群 | read policy |
| 员工 `/stop` | 员工 DM/所属群 | admin/owner/requester |

当 `ADMIN_USER_IDS` 为空时，只有既有 `/setadmin` bootstrap 可写；其他部门 mutation fail-close。

卡片 action 只携带 opaque intent ID；服务端保存绑定的 tenant、user、chat、agent、action、nonce、TTL 和 lifecycle version。回调不信任 payload 中的 employee/chat/admin 字段。

## 18. Supervisor 与恢复

启动顺序：

1. 获取单实例锁。
2. 验证 Journal hash chain 与 anchor。
3. replay snapshot/projection。
4. 恢复员工、Bot principal、Saga、membership、Registry 和 Vault binding。
5. reconcile unresolved Effect、lease、attempt、Inbox 和 Outbox。
6. 恢复未完成 Hire/Fire Saga。
7. 为 ACTIVE 员工启动带 generation fencing 的 Channel child。
8. reconcile Slash Commands。
9. 启动 Scheduler/Worker。
10. 最后打开 ingress admission。

Journal/anchor 全局损坏时进入只读 assist，禁止继续写。单员工凭证损坏只隔离该员工为 DEGRADED/ACTION_REQUIRED，不拖垮主 Bot和其他员工。

关闭顺序：关闭 ingress → drain/cancel workers → flush outbox → 关闭 Channel children → snapshot → 关闭 Journal。

## 19. Legacy 迁移

采用四阶段单写迁移：

1. `LEGACY_WRITE`：旧 Slock 唯一 writer，v5 importer 只建 durable mapping。
2. `SHADOW_READ`：比较 v5 Projection 与 legacy 文件，只记录 diff。
3. `V5_WRITE_LEGACY_READ`：v5 Journal 唯一 writer，旧 AgentRegistry 改为 Projection facade，Slock runtime 继续读取统一身份。
4. `V5_ONLY`：移除 legacy identity/membership 写入口，保留历史迁移工具。

Importer 必须执行 `legacy agent → employee`、`group → team`，为旧 ID 建立 durable alias 并保持 memory/history 连续性；mapping、source hash、import version 写 Journal。重复启动 import 必须零新增。

这里的“连续性”通过 durable `legacy_id_alias` 和文件迁移映射实现，不把旧 `tool:model:name` 当作新的 canonical agent ID。

禁止“双写后失败自动回退旧 writer”。切换前比较员工数、身份字段、membership、memory hash、history 日期范围和 skill profile。

Cutover 使用 Journal 持久化 `authority_epoch + cutover_sequence`。切换时关闭 ingress、drain legacy writer、flush 文件、提交新 epoch 后再开放 v5 ingress；所有 legacy mutation 写前校验 epoch，旧进程、迟到线程或旧卡片的写入一律拒绝并审计。

## 20. 配置与可观测性

新增配置集中在 `src/config`，至少包括：

- visible employee limit；
- Vault master key/key ID；
- provisioning concurrency/TTL；
- Channel start/stop/reconnect deadlines；
- per-employee/per-team/global queue limits；
- context 分层上限；
- history timezone；
- Saga retry/backoff；
- IPC 路径和权限。

结构化日志/trace 字段：tenant、employee、app ID hash、channel generation、chat/thread/message/event、dedup hash、task/attempt、outbox、journal sequence。禁止 secret、token、完整 credential ref 和未脱敏正文。

核心指标：Channel 连接/重连、Ingress fsync/ACK、dedup、队列深度/年龄、ACP 耗时/结果、cancel latency、Outbox oldest age、context 分层大小、history fsync、未授权 mutation、recovery RTO、unresolved effects。

### 20.1 Production Release Profile v1

版本化 release profile 固定初始放行指标：

- Durable Inbox ACK：p95 ≤ 100ms，p99 ≤ 500ms；
- 单员工 Channel cold ready：p95 ≤ 30s；50 Bot Supervisor 恢复：≤ 120s；
- 非故障网络下重连：p95 ≤ 60s，hard deadline 5min；
- `/stop` 从 durable cancel request 到 session close：p95 ≤ 2s，hard deadline 10s；
- 健康网络下 terminal Outbox：99.9% 在 60s 内送达，oldest age 不超过 5min；网络恢复后 2min 内清空可重试 terminal；
- 50 Bot soak：跨多个群连续 2h，身份串线、重复逻辑 task、未捕获异常、终态覆盖均为 0；无注入故障时每 Bot 每小时异常重连 < 1；
- Journal replay + Projection 校验：50 Bot 数据集 ≤ 30s，完整服务恢复仍受 120s fleet RTO 约束。

任何指标、正确性门禁或真实租户证据未满足时，`autonomous_visible_employee_limit` 必须保持 0。Release profile 可在后续版本收紧，但不能通过运行时配置放宽生产验收标准。

## 21. UI 设计

实施前在 `ux/` 创建或更新 HTML 预览，覆盖：

- `/hire` 工具/模型/Profile/Effort 选择；
- 十分钟创建链接与倒计时；
- Provisioning/Configuring/Validating/Ready/Action Required；
- 员工 queued/running/completed/failed/canceled 流式状态卡；
- `/fire` 清理进度、部分失败和平台应用保留披露；
- Slash 传播中状态；
- 移动端窄屏。

生产卡片复用现有纯 render/state/protocol 分层；主 Bot 继续使用原 CardSession delivery，员工 Bot 使用实现同一 protocol 的 `EmployeeChannelDelivery`。两种 delivery 互不导入、互不 fallback。

## 22. 测试与发布门禁

### 22.1 自动化

- Unit：领域状态机、Vault、Projection、history、context、ACL、payload validation。
- Contract：官方 SDK 签名、Channel API、Slash BaseRequest、主 WS/引擎 import boundary。
- Contract 还覆盖同步 provisioning callback 不死锁、reconnect handler 同步 shim、stable UUID initial card、prebound `CardStreamController` 和 fresh-interpreter FD allowlist。
- Integration：Hire/Fire Saga、Channel child IPC、Router→Slock→`_run_acp_session`、Inbox/Outbox、role add、Slash reconciliation。
- Integration 还必须覆盖 text/post/image/file/reply 的真实 attachment 路径、managed-bot 回环拒绝、Thread watermark/pagination failure、五种状态卡由目标员工 app ID 投递且主 Bot 零代发。
- Chaos：在每个 fsync/外部调用/ACK/终态边界 kill -9；remote success/local crash；旧 generation 迟到事件；Outbox 反复失败。
- Security：secret 扫描、文件权限、card replay、跨 tenant/user/chat、伪造 employee ID、ACP env isolation。
- Security 还覆盖 worker UID/namespace、ptrace/proc/core dump、master-key rotation、Vault orphan receipt 和 L1/history 行级 ACL。
- Migration：重复 import 零新增、Projection diff、旧 history/L1 一次迁移。
- Regression：主 Bot WS 实例和入口不变；Deep/Spec/Worktree/Workflow 路由快照不变；现有 Slock ACP 路径测试不变；`lark-cli` 有权限成功、无权限拒绝且无法读取 Vault。

### 22.2 真实租户验收

VISIBLE employee 默认保持关闭，以下证据齐全后才允许 limit > 0：

1. 测试租户和生产租户各完成一次带 preset/addons/create_only 的真实 Provisioning。
2. 两名真实员工 Bot 同群并发执行，头像、名字、Slash 和回复身份完全独立。
3. 1/10/50 Bot 启停、网络断开、重连风暴、宿主重启 soak。50 Bot 分布到多个测试群，预先记录应用/WS/审批/群机器人配额，并分别采集 PID、FD、RSS、重连速率和 API 限流；配额不足属于待满足的外部门禁，不能用 mock 替代。
4. 文本、富文本、图片、文件、reply/thread、CardAction 全覆盖。
5. 五条 Slash create/list/update/delete/rebuild，桌面和移动端传播确认。
6. `/role add` 普通群、话题群、权限不足、机器人上限和重复添加。
7. `/fire` 分步故障、重启恢复、secret 销毁和外部应用保留披露。
8. 日志、Journal、identity、IPC、archive 中扫描不到 app secret。
9. RPO=0：重复事件只产生一个逻辑 task；单进程存活期间一个 attempt 只调用一次 `_run_acp_session`；ACP 调用后崩溃的 unknown attempt 不自动重跑；RTO 达到配置门槛。
10. Thread 缺 scope、分页超时或 revision 不一致时明确 `CONTEXT_UNAVAILABLE`；不得静默退化为群最近消息。

## 23. 实施顺序

1. 统一领域员工模型、Journal Projection、Registry facade 和 Vault。
2. 按日 history、L1 路径迁移、Context assembler。
3. 升级并门控 Provisioning SDK，完成 Hire Saga。
4. 实现每员工 Channel 子进程、IPC、Supervisor、Inbox/Outbox。
5. 实现 Router、Slock gateway、Response Channel、status/stop。
6. 实现 Slash reconciliation 和群 membership Effect。
7. 实现 Fire Saga 与 archive。
8. 接入主 Bot DM 管理命令和 UI 预览/卡片。
9. production composition、legacy 单写迁移、故障注入和真实租户验收。

每一阶段必须以 TDD 开始、通过任务级 spec/quality review、更新 `.Memory` 并独立提交。实现结束后按 mloop 从产品、架构、工程、QA、安全和 UI/UX 视角无状态复审，连续两轮无实质建议才停止。

## 24. 外部依据

- 飞书一键创建智能体应用：<https://open.feishu.cn/document/mcp_open_tools/integrating-agents-with-feishu/overview>
- 官方 Python SDK：<https://github.com/larksuite/oapi-sdk-python>
- Channel SDK Python：<https://github.com/larksuite/channel-sdk-python>
- Slash Commands：<https://open.larkoffice.com/document/mcp_open_tools/agent-best-practices/agent-supports-slash-commands>
- 群成员 API：<https://open.feishu.cn/document/server-docs/group/chat-member/create>
