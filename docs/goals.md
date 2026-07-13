你需要继续完成 GhostAP 的长期开发目标。不要从头重做，也不要把当前 Foundation 阶段误认为整个目标已经完成。

  工作目录：
  /data00/home/jiataorui/work/github/ghostAp

  原始完整需求：
  /data00/home/jiataorui/.codex/attachments/3121b5aa-316a-41ca-bee6-6374a07ca4a0/pasted-text-1.txt

  首先必须完整读取：

  1. 原始需求附件
  2. /data00/home/jiataorui/work/github/ghostAp/AGENTS.md
  3. .Memory/Abstract.md
  4. .Memory/2026-07-12.md
  5. docs/2026-07-12-autonomous-agent-department-design.md
  6. docs/2026-07-12-autonomous-foundation-plan.md
  7. docs/2026-07-12-autonomous-data-plan.md

  然后以当前 Git、文件和测试结果为唯一权威状态进行检查：

  cd /data00/home/jiataorui/work/github/ghostAp
  git status --short --branch
  git log -15 --oneline
  git rev-parse HEAD
  git rev-parse origin/dev
  cat .superpowers/sdd/progress.md 2>/dev/null || true
  rg --files .superpowers/sdd 2>/dev/null || true

  重要：用户已经明确授权直接在 dev 分支开发，不要创建 worktree。保留用户已有改动，不要 reset、checkout 或删除任何不属于你的修改。

  当前已完成状态（必须用 Git 和源码重新核验，不可只相信此摘要）：

  - Foundation 已完成：
    - Canonical frozen Employee/BotPrincipal domain
    - AES-GCM Credential Vault
    - Journal-backed employee/Bot/alias/authority projection
    - 安全可重建 identity.json
    - Tenant-aware ProjectedAgentRegistry
    - Legacy/V5 authority cutover 与持久化失败恢复
    - Slock importer 随机 agt_ ID 和持久 alias/source hash
  - Foundation 历史里程碑已完成并保留。
  - Foundation 已推送过 dev，最终 Foundation HEAD 为：
    5de753ca9f12a3fc2140e2e633c43a4969cac278
  - 2026-07-13 权威审计状态：
    - 数据面 Task 1–6 已有 domain/service/projection/query/composition 模块与局部测试，但真实 Slock producer、handler 和 Supervisor 尚未接入，不能称为 production cutover。
    - Thread Context、Hire/Fire、Channel、Slash、Router、Response 已有端口或内存脚手架，主要由 fake 单测覆盖；没有生产 composition、真实 Channel 子进程或真实租户证据。
    - `lark-oapi` 已升级锁定到 `1.7.1`，官方 `register_app/aregister_app` 的 `app_preset/addons/create_only/app_id` 签名门禁和严格 `LarkAppRegistrar` 已实现。
    - `/hire` 不再降级写入 `AgentRegistry.legacy()`；只允许配置管理员在主 Bot 私聊使用，并只派发到注入的 `EmployeeHireService`，未装配时明确 fail-close。`/new-role` 继续负责 Slock 虚拟角色。
    - `AgentDepartmentBootstrap` 不再常量伪报 healthy；limit=0 是 dormant/unready，limit>0 必须六个具名组件探针全部通过。
    - 当前本地自动化证据：`tests/autonomous/` 872 passed；这只证明本地合约，不替代真实租户或 50 Bot soak。
    - acceptance manifest 仍全部 pending，且真实 `/hire` durable Saga、Vault/Journal binding、Channel/Slash/验证激活尚未形成闭环。
  - `autonomous_visible_employee_limit` 必须继续保持 0，直到 Provisioning、Channel 和真实租户门禁全部完成。

  完整最终目标：

  构建可以投入生产的 GhostAP Autonomous “Agent Department”：

  - 每个员工是独立飞书 Bot，拥有自己的 app_id/app_secret、名字、头像、Slash Commands。
  - 使用一键创建应用 SDK 完成 /hire Provisioning Saga。
  - 每员工使用独立 Channel SDK WebSocket。
  - 底层执行复用现有 Slock `_run_acp_session`。
  - 支持 /task、/status、/history、/memory、/stop。
  - 支持 /fire、团队 membership、/role add/remove。
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
  - 不得把 app_secret 写入 identity.json、Journal、日志、卡片或异常。
  按日历史 JSONL materializer、ProjectionState 权威查询、
  inclusive date range、稳定 cursor、可信 ACL context factory。

  Task 4：
  L1、memory_summary、skill、reasoning Journal-first 投影，
  canonical memory/MEMORY.md，安全摘要和 legacy compatibility facade。

  Task 5：
  旧 execution_history.jsonl、MEMORY.md、skill、reasoning 的幂等迁移，
  stable source locator、batch progress、quarantine、恢复和单写切换。

  Task 6：
  真实生产 composition 和 producer cutover：
  - ACP dispatch 前锚定 ExecutionAttemptContext
  - 所有真实终态写入一次 history occurrence
  - canonical L1/skill/reasoning producer
  - 独立 data authority cutover
  - /history 与 ACL-aware /memory read ports
  - Supervisor restart recovery
  - 禁止 canonical employee 继续写旧文件

  开始前：

  - 完整阅读 data plan Task 1。
  - 使用 rg 检查现有 domain、Vault、BlobStore、Settings 的代码模式。
  - 确认当前分支是 dev。
  - 确认工作树是否有用户改动。
  - 运行基线：

  uv sync --group dev
  uv run python -m pytest tests/autonomous/ -q
  uv run python -m pytest tests/test_docs_references.py -q
  uv run python -m src.main --validate
  git diff --check

  如果基线失败：

  - 先判断是现有失败、环境问题还是当前未提交改动。
  - 使用 systematic-debugging 查明根因。
  - 不得无视与当前任务相关的失败继续开发。

  数据面完成后，不要结束整个目标。继续按下列顺序写计划、grill、实现和验证：

  1. Thread Context：
     - employee-scoped FeishuMessageSource
     - root_id → thread_id 权威解析
     - 全量分页
     - watermark/revision
     - edit/delete
     - Thread/group 去重
     - protected system/current message
     - L2 → L1 → group recent → oldest Thread 裁剪
     - CONTEXT_UNAVAILABLE fail-closed
  2. Provisioning：
     - lark-oapi 版本门禁
     - 一键创建 SDK
     - /hire 可恢复 Saga
     - app manifest/scopes
     - callback/Vault receipt
  3. Slash Command Manager：
     - GET/diff/POST/PATCH/DELETE/GET 验证
     - /task /status /history /memory /stop
  4. Channel Connection Manager：
     - 每员工 fresh-interpreter 子进程
     - secret 仅通过一次性 pipe
     - generation fencing
     - durable ingress ACK
  5. Router 与 Slock gateway：
     - employee binding、tenant、membership、ACL、queue
     - 只调用一次现有 `_run_acp_session`
     - crash 后 UNKNOWN/ACTION_REQUIRED
  6. Employee Response Channel：
     - Durable Outbox
     - 稳定 UUID 创建卡片
     - child-owned CardStreamController
     - 禁止主 Bot fallback
  7. 团队 membership、/role add/remove、/stop 竞态。
  8. /fire Saga、Slash 清理、Channel 断开、Vault 销毁、归档。
  9. Production bootstrap/Supervisor 恢复与完整 release gates。
  10. 真飞书租户验收。

  每个阶段完成后都必须检查原始附件中的要求，不能把子计划完成当作整体完成。

  最终 completion audit 必须逐项证明：

  - /hire 真实创建独立飞书应用并得到 Vault credential ref。
  - 员工 Channel 能真实收消息、断线恢复。
  - Slash Commands 服务端 desired set 精确一致。
  - 多员工消息正确路由，员工 Bot 自己回复。
  - `_run_acp_session` 每 accepted attempt 只调用一次。
  - completed/failed/canceled/timeout/action_required 都有历史。
  - history 日期范围、ACL、重建正确。
  - Thread 全量 Context 与 watermark/failure 正确。
  - /stop 终态竞态正确。
  - /fire 可恢复且不伪报删除开放平台应用。
  - 主 Bot send count 在员工响应中为 0。
  - Deep/Spec/Worktree/Workflow 和主 Bot WS 无回归。
  - 自动化、故障注入、重启恢复、配置校验全部通过。
  - 真实飞书租户 E2E 完成。
  - dev 已 push，HEAD 与 origin/dev 一致。

  只有所有要求都有当前、权威证据时，才能宣布整体目标完成。
  如果只完成了某个阶段，要明确说明整体目标仍 active，并继续下一个阶段。
