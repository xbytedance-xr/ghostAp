你需要继续完成 GhostAP 的长期开发目标。不要从头重做，也不要把局部模块、自动化测试或安全脚手架误认为整个目标已经完成。

  工作目录：
  /data00/home/jiataorui/work/github/ghostAp

  原始完整需求：
  /data00/home/jiataorui/.codex/attachments/3121b5aa-316a-41ca-bee6-6374a07ca4a0/pasted-text-1.txt

  首先必须完整读取：

  1. 原始需求附件
  2. /data00/home/jiataorui/work/github/ghostAp/AGENTS.md
  3. .Memory/Abstract.md
  4. .Memory/2026-07-12.md
  5. .Memory/2026-07-13.md
  6. docs/2026-07-12-autonomous-agent-department-design.md
  7. docs/2026-07-12-autonomous-foundation-plan.md
  8. docs/2026-07-12-autonomous-data-plan.md
  9. docs/2026-07-13-autonomous-hire-production-plan.md
  10. docs/2026-07-13-autonomous-thread-context-plan.md

  然后以当前 Git、源码、运行配置和新鲜测试结果为唯一权威状态进行检查：

  cd /data00/home/jiataorui/work/github/ghostAp
  git status --short --branch
  git log -15 --oneline
  git rev-parse HEAD
  git rev-parse origin/dev
  cat .superpowers/sdd/progress.md 2>/dev/null || true
  rg --files .superpowers/sdd 2>/dev/null || true

  重要：用户已经明确授权直接在 dev 分支开发，不要创建 worktree。保留用户已有改动，不要 reset、checkout 或删除任何不属于你的修改。

  当前权威状态（2026-07-13；必须用 Git、源码、配置和测试重新核验，不可只相信此摘要）：

  - 本文档不再固化会在下一次提交立即过期的 HEAD；每次继续开发必须以上述
    `git rev-parse HEAD/origin/dev` 的现场结果为准。Thread Context Task 2 开发前
    二者均位于 `f83585a504de3a372fa9a29106739efec7230393`。
  - Foundation 已完成并保留：
    - Canonical frozen Employee/BotPrincipal domain
    - AES-GCM Credential Vault
    - Journal-backed employee/Bot/alias/authority projection
    - 安全可重建 identity.json
    - Tenant-aware ProjectedAgentRegistry
    - Legacy/V5 authority cutover 与持久化失败恢复
    - Slock importer 随机 `agt_` ID 和持久 alias/source hash
  - 员工数据面已有严格 domain、独立 keyring、加密 Blob、Journal publish/replay、按日 history materializer、ACL query、L1/summary/skill/reasoning projection、legacy importer 和 composition；但真实 Slock employee producer、handler、Supervisor authority cutover 尚未全部接线，不能称为 production data cutover。
  - `/hire` 的生产形态代码已经实现，不再只是内存脚手架：
    - 官方 `lark-oapi==1.7.1` 一键创建应用 adapter 与精确 manifest
    - Journal/Vault-backed durable Hire Saga 与 callback bridge
    - PREPARED/EXECUTING 锚定、幂等恢复与 ACTION_REQUIRED 语义
    - 每员工 fresh-interpreter Channel 子进程、一次性 secret pipe、generation fencing 和 sandbox attestation
    - Slash GET/diff/POST/PATCH/DELETE/GET 精确 reconciliation
    - 真实员工 Bot `/status`、nonce、主 Bot send-count=0 的激活验证
    - `EmployeeDepartmentRuntime` 生产 composition、恢复 supervisor 和 FeishuWSClient 生命周期接线
    - 独立 Employee release manifest、哈希链 evidence、Ed25519 QA attestation 和默认 PENDING CLI
  - `/hire` 不再降级写入 `AgentRegistry.legacy()`；`/new-role` 继续只负责 Slock 虚拟角色。没有 production service 或 readiness 时必须 fail-close。
  - `/hire` 管理员 DM 卡片权限 Bug 已修复：消息事件入口保存官方 `event.message.chat_type` 与 origin/chat/operator；卡片回调不再读取不存在的 `context.chat_type`。只有服务端明确无 provenance 记录时才查询 Chat API，并且只读结构字段 `chat_mode`；API 结果必须原子写回完整可信绑定。来源查询/写入失败、残缺、过期、跨 chat、跨 operator 或并发冲突均 fail-close。
  - readiness 反馈已接入处理器：只有 provider 明确返回 `ready=True` 且无 blockers 才派发真实 Hire；否则向管理员显示具体安全门禁，不再误报“不是管理员”，也不降级创建本地虚拟角色。
  - 当前现场仍不能创建真实员工，但剩余原因是有意的发布门禁而非 DM 识别：`autonomous_visible_employee_limit=0`，且没有独立 QA release 公钥/有效 attestation；全新环境的 `EmployeeDepartmentRuntime` 保持 dormant，`employee_hire_service` 不注入。不得绕过门禁创建员工。
  - 以下模块仍主要是局部实现或内存脚手架，尚未组成真实员工任务闭环：
    - Thread Context Task 1-6 已完成并关闭 Phase 2：冻结 contracts/config、employee-scoped 官方
      `lark-oapi` message source、每员工 Vault credential lease、Get current 权威
      root/thread 解析、Thread 双遍历稳定 snapshot、Group 双窗口 recent、current
      propagation、revision/identity 去重、watermark、整单元预算裁剪、稳定错误与关闭
      线性化，以及 tenant-bound canonical L1、membership/chat-bound full L2、动态
      Projected Registry/BotPrincipal、requester/current-sender 绑定、mandatory atomic
      authority fence 均有回归；production runtime 现拥有 Context source/service 与
      canonical Data composition，拆分 hire/execution readiness，并覆盖 employee app probe、
      shared Journal 同步、restart/rotation/retirement invalidation 与逆序关闭；真实页间
      insert/edit/delete、timeout、重复 token、partial SDK、两把密钥轮换、restart 与 shutdown
      故障注入均证明 `CONTEXT_UNAVAILABLE` 零 task/ACP 派发。durable ingress 尚未接入该服务
    - `EmployeeMessageRouter` 尚未接 durable employee ingress、Projected Registry、ACL/membership 与真实 Slock `_run_acp_session`
    - `EmployeeResponseChannel` 明确仍是 in-memory outbox，缺 Journal-backed Durable Outbox、稳定 UUID 卡片和 child-owned stream controller
    - `FireSaga` 仍是可变内存顺序流程，未满足 Journal SSOT、Effect 锚定、unknown disposition、恢复与归档合同
    - 团队 membership、`/role add/remove`、`/stop` 终态竞态尚未形成生产闭环
  - 尚无获授权的真实测试/生产租户执行证据。Thread Context Phase 2 关闭时最新
    Autonomous 全量验证为 `1234 passed, 2 skipped`；两项 skip 分别是未授权真实
    租户验收和宿主不满足默认 bwrap attestation。这些本地测试只证明代码合同，
    不替代真实 Bot、双租户、桌面/移动 Slash、主 Bot 零代发和 1/10/50 Bot soak。
  - 生产 release 仍缺外部信任组件：不可变 build/workload provenance、部署侧固定 QA trust root、外部单调 attestation ledger、可续期 recovery capability、真实 main-Bot send audit provider 和生产级不可回滚 anchor/见证。
  - `autonomous_visible_employee_limit` 必须继续保持 0。不得为“先跑起来”而放宽 readiness、伪造 evidence、恢复 legacy/NullJournal fallback 或把测试 fake 当生产依赖。

  完整最终目标：

  构建可以投入生产的 GhostAP Autonomous “Agent Department”：

  - 每个员工是独立飞书 Bot，拥有自己的 app_id/app_secret、名字、头像、Slash Commands。
  - 使用一键创建应用 SDK 完成 `/hire` Provisioning Saga。
  - 每员工使用独立 Channel SDK WebSocket。
  - 底层执行复用现有 Slock `_run_acp_session`。
  - 支持 `/task`、`/status`、`/history`、`/memory`、`/stop`。
  - 支持 `/fire`、团队 membership、`/role add/remove`。
  - 执行历史按日 JSONL，但 Journal + encrypted Blob 才是事实源。
  - L1、skill、reasoning 使用 Journal-first 数据面。
  - Context 顺序严格为：
    Thread 全量消息 > 群最近消息 > L1 > L2。
  - 员工必须用自己的 Bot 输出状态卡和结果，绝不 fallback 到主 Bot。
  - 不引入数据库，继续使用文件存储。
  - 最后完成自动化、故障注入和真实飞书租户验收，并 push 到 dev。

  绝对不能破坏：

  - Deep / Spec / Worktree / Workflow 引擎逻辑和路由。
  - 主 GhostAP Bot 现有 WebSocket 连接和消息入口。
  - Slock `_run_acp_session` 内部执行语义。
  - SMART、普通编程模式和 topic-scoped engine 状态合同。
  - TTADK CLI bridge 语义。
  - Journal SSOT、frozen domain、Effect dispatch 前锚定、默认拒绝策略。
  - 不得把 app_secret 写入 identity.json、Journal、日志、卡片、异常、argv、环境变量或普通 IPC。

  开始任何新阶段前：

  - 阅读该阶段对应设计、计划和最近 Memory 证据。
  - 使用 `rg` 检查现有 domain、Vault、BlobStore、Settings 和相邻生产模式。
  - 确认当前分支、HEAD、origin/dev 和工作树状态。
  - 先运行最相关的基线；修改共享路由、卡片、锁、配置、会话或启动 composition 时扩大回归。

  推荐基线：

  uv sync --group dev
  uv run python -m pytest tests/autonomous/ -q
  uv run python -m pytest tests/test_docs_references.py -q
  uv run python -m src.main --validate
  git diff --check

  如果基线失败：

  - 先判断是现有失败、环境问题还是当前未提交改动。
  - 使用 systematic-debugging 查明根因。
  - 不得无视与当前任务相关的失败继续开发。

  从当前 HEAD 继续的实施顺序：

  1. 已完成（2026-07-13）修复 `/hire` 卡片权限上下文：
     - 不再读取 SDK 模型中不存在的 `context.chat_type`
     - 使用服务端保存、绑定 origin message/chat/operator 的可信 DM provenance
     - provenance 查询失败、残缺、过期、跨 chat、跨 operator 或冲突时 fail-close；仅明确无记录可用 Chat API `chat_mode` 原子回填
     - readiness 未满足时展示具体安全阻断，不误报“不是管理员”
  2. 已完成（Task 1-6）Thread Context 生产接线：
     - 已完成：冻结 scope/revision/watermark/config contracts
     - 已完成：employee-scoped 官方 FeishuMessageSource、root_id → thread_id
       权威解析、严格全量分页 primitive、revision/edit/delete/content normalization
     - 已完成：Thread 双遍历稳定 snapshot、current propagation、Group recent 双窗口与
       cutoff cohort、watermark/source boundary、跨层 identity/revision 去重
     - 已完成：trusted constraints reserve + protected current，以及
       L2 → L1 → group recent → oldest Thread 整单元裁剪
     - 已完成：tenant/projected-owner canonical L1、membership/chat-bound full L2、
       ACTIVE/visible/principal/app/credential/generation/requester ACL、current sender
       绑定、`CONTEXT_UNAVAILABLE` 零执行和 mandatory atomic authority-fence contract
     - 已完成：Data projection rebuild/read/publish/GC 统一锁所有权，以及 canonical
       L1 root/parent/final dir-fd no-follow containment
     - 已完成：Task 5 production composition、employee-scoped execution readiness、
       recovery/rotation/retirement invalidation、shared Journal coordination 与 shutdown ownership
     - 已完成：Task 6 真实页间 mutation、deadline/token/partial SDK、restart/rotation/shutdown
       failure injection；所有 mandatory Context failure 均零 delegate/task/ACP 派发，三路终审批准
     - Phase 2 handoff 已关闭；下一阶段是 durable employee ingress
  3. 进行中：Durable employee ingress、Router 与 Slock gateway：
     - 设计与现有实现审计已完成，实施计划见
       `docs/2026-07-13-autonomous-durable-ingress-plan.md`；当前生产 Router/Inbox
       仍是不可接受的内存脚手架，Phase 3 尚未完成
     - 已纠正 Channel ACK 假设：高层 `FeishuChannel` 消息回调会先 schedule 后返回，
       不能证明平台 ACK 发生在 Journal fsync/anchor 之后；实现必须通过锁定版本的
       low-level dispatcher 黑盒验证消息和 CardAction 两条路径，任一路径不满足即
       保持 execution readiness 关闭
     - Channel ACK 前 Journal durable Inbox
     - encrypted payload/blob、并发安全 dedup、稳定 duplicate ACK 与 attachment staging
     - employee/app/generation binding、tenant、membership、ACL 和有界队列
     - ACP dispatch 前锚定 ExecutionAttemptContext
     - 每个 accepted attempt 只调用一次现有 `_run_acp_session`
     - completed/failed/canceled/timeout/action_required 全部进入数据面
  4. Employee Response Channel：
     - Journal-backed Durable Outbox
     - 稳定 UUID 幂等创建单张状态卡
     - child-owned CardStreamController 或已验证的 employee REST patch backend
     - 员工 Bot 自己发送，主 Bot fallback 和 send count 必须为 0
  5. 团队 membership、`/role add/remove` 与 `/stop` 终态竞态。
  6. `/fire` durable Saga：
     - RETIRING 立即关闭 ingress
     - Slash 清理、Channel 断开、membership disposition、Vault 销毁、归档
     - 每个外部 Effect 先锚定；未知结果保持 ACTION_REQUIRED，不伪报成功
  7. 数据面真实 producer/handler/Supervisor cutover：
     - canonical employee 禁止继续写旧 execution_history.jsonl 或根 MEMORY.md
     - `/history` 与 ACL-aware `/memory` 使用权威 read ports
     - restart replay/rebuild 和独立 data authority fencing
  8. Production bootstrap 与外部 release trust integration：
     - 真实 main-Bot send audit provider
     - 不可变 build/workload provenance、固定 QA trust root、外部单调 ledger
     - 生产级 anchor/见证、recovery capability、secret/sandbox 审计
  9. 真飞书租户验收与放行：
     - staging + production Provisioning
     - 真实员工独立收发、桌面/移动 Slash、附件/话题/CardAction
     - 1/10/50 Bot soak、断线/重启/限流/故障注入
     - 只有全部证据有效，才允许把 visible limit 从 0 调高

  每个阶段完成后都必须检查原始附件中的要求，不能把子计划完成当作整体完成。

  最终 completion audit 必须逐项证明：

  - `/hire` 真实创建独立飞书应用并得到 Vault credential ref。
  - 员工 Channel 能真实收消息、断线恢复。
  - Slash Commands 服务端 desired set 精确一致。
  - 多员工消息正确路由，员工 Bot 自己回复。
  - `_run_acp_session` 每 accepted attempt 只调用一次。
  - completed/failed/canceled/timeout/action_required 都有历史。
  - history 日期范围、ACL、重建正确。
  - Thread 全量 Context 与 watermark/failure 正确。
  - `/stop` 终态竞态正确。
  - `/fire` 可恢复且不伪报删除开放平台应用。
  - 主 Bot send count 在员工响应中为 0。
  - Deep/Spec/Worktree/Workflow 和主 Bot WS 无回归。
  - 自动化、故障注入、重启恢复、配置校验全部通过。
  - 真实飞书租户 E2E 完成。
  - dev 已 push，HEAD 与 origin/dev 一致。

  只有所有要求都有当前、权威证据时，才能宣布整体目标完成。
  如果只完成了某个阶段，要明确说明整体目标仍 active，并继续下一个阶段。
