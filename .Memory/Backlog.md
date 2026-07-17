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
| B049 | 2026-07-16 | Feishu API 硬超时后 daemon SDK worker 无法取消；本地删除 binding 后，迟到 PATCH 可能越过新代际远端写入。需设计 request generation/远端见证并做故障注入。 | Medium | Deep 卡片顺序分页审计 | Open | — |
| B050 | 2026-07-16 | ACP `PromptResponse` 在已有文本时不等待尾部 `session/update`，立即清 event handler；末个 subagent DONE 可能丢失。需协议级 drain/终态屏障测试。 | Medium | Deep ACP 事件审计 | Open | — |
| B051 | 2026-07-16 | 员工 Contact/Context/群历史 SDK 调用缺少 endpoint、员工 app、message_id、平台错误码与分段耗时关联；异常目前多被压缩为 false/unknown，现场只能结合 Journal 推断。需补脱敏结构化观测。 | Medium | Team 员工延迟日志审计 | Open | — |

> **归档注释**：B020-B048 已按 `fixed`、`already satisfied`、`retired/superseded` 或 `external profile` 逐项记录处置依据；实现文件、精确测试/文档证据与保留边界见 [2026-07-16.md](2026-07-16.md)。强化多副本档的外部验收条件由 [employee runtime profiles ADR](../docs/adr-employee-runtime-profiles.md) 持续承载，不作为本地代码已证明能力。
