# Maintenance Backlog

> **用途**：收集 Low/Medium severity 的审计缺口，集中在维护窗口批量处理，避免打断主线开发节奏。
>
> **工作流**：Review/Audit 产出的 gap 按分级标准评估 → High 立即修复 → Low/Medium 录入本表 → 每两周维护窗口集中处理。

## 分级标准

| Severity | 定义 | 处理方式 |
|----------|------|----------|
| **High** | 影响正确性、安全性、数据丢失 | 立即修复，不入 Backlog |
| **Medium** | 可观测性、可运维性缺口（如日志错误、配置缺失） | 录入 Backlog，维护窗口处理 |
| **Low** | 代码风格、文档一致性、命名规范 | 录入 Backlog，维护窗口处理 |

## Backlog 条目

| ID | 日期 | Gap 描述 | Severity | 来源 | 状态 | 解决 Commit |
|----|------|----------|----------|------|------|-------------|
| B020 | 2026-05-18 | 架构师审查超时(240s)导致阻塞：已实现 per-role timeout multiplier + diff 截断 + timeout 降级 | Medium | Spec Engine audit | ✅ Fixed | (pending commit) |
| B021 | 2026-05-20 | Slock `task_router` 在 IDLE 预过滤后 `availability` 评分恒为 1.0；需统一成软评分或删除冗余权重，避免设计意图和实际评分不一致 | Medium | Slock audit | ⏳ Open | - |
| B022 | 2026-05-20 | Slock `AgentRegistry._persist` 在 registry 锁内做磁盘 I/O，高频注册/更新时会阻塞 `get/list/find` 读路径 | Medium | Slock audit | ⏳ Open | - |
| B023 | 2026-05-20 | Slock escalation timeout I/O 仍依赖 4 线程 executor 包住慢 Feishu 调用；底层调用超时前不可中断，极端慢 API 下可能堆积 | Medium | Slock audit | ⏳ Open | - |
| B024 | 2026-05-20 | Slock `TaskRouter.extract_skill_keywords` 无匹配时默认 `["code"]`，非技术消息会污染 coder 技能画像 | Low | Slock audit | ✅ Fixed | verified 2026-07-01 |
| B025 | 2026-05-20 | Slock `SkillProfile.from_dict` 未做范围归一化，外部/旧数据可写入负数或超过 100 的 success_rate | Low | Slock audit | ✅ Fixed | verified 2026-07-01 |
| B026 | 2026-05-20 | Slock L1/L2/Wiki/message archive 缺少大小上限或轮转策略，长期运行会持续增长 | Low | Slock audit | ⏳ Open | - |
| B027 | 2026-05-20 | Slock `_max_parallel_agents` 默认 4 与 settings 暴露项的职责需梳理，避免配置语义歧义 | Low | Slock audit | ⏳ Open | - |
| B028 | 2026-05-20 | Slock `AgentRegistry.remove` 只删 `identity.json` 不删空目录，长期操作会积累空 agent 目录 | Low | Slock audit | ✅ Fixed | verified 2026-07-01 |
| B029 | 2026-05-20 | Slock 同一 channel 内 Agent name 无唯一约束，命令按名称查找时可能出现歧义 | Low | Slock audit | ✅ Fixed | verified 2026-07-01 |
| B030 | 2026-05-20 | Slock observer queue `deque(maxlen=...)` 在 enqueue 满载时仍会静默挤掉最老记录，需增加丢弃计数/日志或显式拒绝策略 | Low | Slock audit | ✅ Fixed | this commit |
| B031 | 2026-05-20 | Slock `_trim_done_tasks` 按 `claimed_at or 0` 清理 DONE；未 claim 但 DONE 的异常任务会优先被清理，需改用完成时间/创建时间 | Low | Slock audit | ✅ Fixed | verified 2026-07-01 |
| B032 | 2026-05-20 | Slock 重启后缺少 `IN_REVIEW` 孤儿任务恢复/降级策略，崩溃恢复语义需明确 | Medium | Slock audit | ⏳ Open | - |
| B033 | 2026-05-20 | Slock `AgentRegistry._ensure_loaded` 首次访问全量扫描 agent 目录，agent 数量增长后启动/首次访问延迟线性增长 | Medium | Slock audit | ⏳ Open | - |
| B034 | 2026-05-20 | Slock `MemoryManager` L1/L2/L3 共用单锁，4 Agent 并行时内存读写串行化；可按层/路径拆锁并保留原子 RMW 语义 | Medium | Slock audit | ⏳ Open | - |
| B035 | 2026-07-12 | Autonomous 生产 composition 尚未把 Manager/Admission/Coordinator 注入飞书 dispatcher；当前 `manager/handler.py` 仍引用旧 Admission API，并存在直接修改 frozen 域对象的遗留路径。命令入口已 fail-closed，后续应按 Journal/Projection/Coordinator 当前契约重新接线，不得恢复旧兼容 API | Medium | recent-change restoration audit | ⏳ Open | - |
| B036 | 2026-07-13 | Employee Hire 的本地 `FileAnchor` 具备跨进程 CAS 与 fsync，但不具备独立系统防回滚能力；生产启用前需接入远端 KMS/HSM/透明日志等独立 anchor，并完成恢复与故障注入 | Medium | `/hire` production review | ⏳ Open | - |
| B037 | 2026-07-13 | Employee 真实租户验收工具当前只验证并封装预脱敏采集结果；测试/生产租户 Provisioning、员工收发、桌面/移动 Slash、主 Bot send-count 和 1/10/50 Bot soak 仍需独立 QA 在真实租户执行并签署 attestation | Medium | `/hire` acceptance review | ⏳ Open | - |
| B038 | 2026-07-13 | 每员工 Channel 默认依赖 bwrap 的 user/mount/PID namespace 与最小只读文件系统；生产宿主需把实际 attestation 和 Feishu-only egress policy 作为部署前置探针，并准备独立容器后端覆盖禁用 namespace 的环境 | Medium | `/hire` channel review | ⏳ Open | - |
| B039 | 2026-07-13 | Employee runtime release 当前有意 hard-close；启用前需接入不可变 build/image digest、workload identity、部署侧固定 QA trust root、外部单调 attestation ledger，以及带 expiry/tenant/release/instance 绑定且每次 dispatch 续验的 recovery capability | Medium | `/hire` release convergence review | ⏳ Open | - |
| B040 | 2026-07-13 | 激活门禁已要求独立主 Bot send-count audit，但 `FeishuWSClient` 尚无真实租户审计 provider；启用前必须接入可按 tenant/challenge 时间窗查询的外部审计源，不能用进程内计数或常量零替代 | Medium | `/hire` activation security review | ⏳ Open | - |
| B041 | 2026-07-14 | 员工 hire 缺终态用户通知：成功（应发"去消息激活新 Bot"）与失败/超时（应发"需人工处理"）当前只记日志。需新增 `notification_ready`/`notification_failed` 通道并接线，避免"请勿重复发送 /hire"成为悬空承诺 | Medium | 注册400日志 grill 复审 | ⏳ Open | - |
| B042 | 2026-07-14 | `AsyncCallbackBridge` 的 link/status 回调在员工事件循环上同步调用阻塞式 `reply_message`，设备授权长轮询期会短暂阻塞循环/轮询节奏；可用 `asyncio.to_thread` 包装同步回复 | Low | 注册400日志 grill 复审 | ⏳ Open | - |

> **注**: B001-B005、B014-B019 已全部修复并清理；Refactoring Analysis 1–28 已以 [.Memory/2026-05-11.md](2026-05-11.md) 顶部最终矩阵完成收口，已完成项不再留在 Backlog。
