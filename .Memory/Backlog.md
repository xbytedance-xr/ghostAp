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
| B024 | 2026-05-20 | Slock `TaskRouter.extract_skill_keywords` 无匹配时默认 `["code"]`，非技术消息会污染 coder 技能画像 | Low | Slock audit | ⏳ Open | - |
| B025 | 2026-05-20 | Slock `SkillProfile.from_dict` 未做范围归一化，外部/旧数据可写入负数或超过 100 的 success_rate | Low | Slock audit | ⏳ Open | - |
| B026 | 2026-05-20 | Slock L1/L2/Wiki/message archive 缺少大小上限或轮转策略，长期运行会持续增长 | Low | Slock audit | ⏳ Open | - |
| B027 | 2026-05-20 | Slock `_max_parallel_agents` 默认 4 与 settings 暴露项的职责需梳理，避免配置语义歧义 | Low | Slock audit | ⏳ Open | - |
| B028 | 2026-05-20 | Slock `AgentRegistry.remove` 只删 `identity.json` 不删空目录，长期操作会积累空 agent 目录 | Low | Slock audit | ⏳ Open | - |
| B029 | 2026-05-20 | Slock 同一 channel 内 Agent name 无唯一约束，命令按名称查找时可能出现歧义 | Low | Slock audit | ⏳ Open | - |
| B030 | 2026-05-20 | Slock observer queue `deque(maxlen=...)` 在 enqueue 满载时仍会静默挤掉最老记录，需增加丢弃计数/日志或显式拒绝策略 | Low | Slock audit | ⏳ Open | - |
| B031 | 2026-05-20 | Slock `_trim_done_tasks` 按 `claimed_at or 0` 清理 DONE；未 claim 但 DONE 的异常任务会优先被清理，需改用完成时间/创建时间 | Low | Slock audit | ⏳ Open | - |
| B032 | 2026-05-20 | Slock 重启后缺少 `IN_REVIEW` 孤儿任务恢复/降级策略，崩溃恢复语义需明确 | Medium | Slock audit | ⏳ Open | - |
| B033 | 2026-05-20 | Slock `AgentRegistry._ensure_loaded` 首次访问全量扫描 agent 目录，agent 数量增长后启动/首次访问延迟线性增长 | Medium | Slock audit | ⏳ Open | - |
| B034 | 2026-05-20 | Slock `MemoryManager` L1/L2/L3 共用单锁，4 Agent 并行时内存读写串行化；可按层/路径拆锁并保留原子 RMW 语义 | Medium | Slock audit | ⏳ Open | - |

> **注**: B001-B005、B014-B019 已全部修复并清理；Refactoring Analysis 1–28 已以 [.Memory/2026-05-11.md](2026-05-11.md) 顶部最终矩阵完成收口，已完成项不再留在 Backlog。
