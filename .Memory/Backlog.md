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
| B017 | 2026-05-10 | 验证 `notation` text_size 在移动端飞书客户端的实际渲染效果，确保 activity_digest 行可读。涉及 4 处：`base.py` ref_note、`renderer.py` thought section、`renderer.py` tool_panel、`renderer.py` activity_digest。验证方法：在移动端飞书发送包含 activity_digest 行的编程卡片，确认字号可读。Deadline: 2026-06-01 | Low | card-slim-flow review | 需人工验证 | — |
| B018 | 2026-05-10 | `page_mutator.update_page()` 把 `code=230099` 一律视作 permanent transport error → `remove_page()` 强制重建卡片；若失败原因是内容本身非法（content-parse 错误，飞书把 200621/200861 等包在 230099 里返回），重建会产出同样非法 JSON 形成卡片刷屏。建议按错误子码区分：仅 `99992354`（message_id 不存在）走重建；content-parse 错误改为渲染 fallback 卡并停止重试。当前直接触发源（reasoning panel 非法 div）已修，但兜底策略仍是隐患。 | Medium | 编程卡片刷屏排查 | 待处理 | — |

> **注**: B001-B005、B014-B016 已在 2026-05-10 维护窗口全部修复并清理；Refactoring Analysis 1–28 已以 [.Memory/2026-05-11.md](2026-05-11.md) 顶部最终矩阵完成收口，已完成项不再留在 Backlog。B017/B018 来源分别为 card-slim-flow review 与编程卡片刷屏排查，不属于 `docs/2026-05-11-refactoring-analysis.md` 的未闭环项。
