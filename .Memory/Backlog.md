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

> **注**: B001-B005、B014-B016 已在 2026-05-10 维护窗口全部修复并清理。
