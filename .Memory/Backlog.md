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
| B001 | 2026-05-04 | 删除 10 个 re-export shim 文件（session_config/session_factory/session_rotator/static_session/delivery_tracker/action_dispatch/action_ids/action_router/timer_manager/timer_scheduler），迁移所有内部调用方到 canonical 路径。Deadline: 2026-06-01 | Medium | card-refactor audit | 待处理 | — |
| B002 | 2026-05-05 | test_skips_reset_on_success 使用 xfail(strict=False) 掩盖全局状态泄漏，需补充 fixture 隔离或重构测试。Deadline: 2026-05-19 | Medium | card-migration review | 待处理 | — |
| B003 | 2026-05-05 | config.py 单文件 1039 行，拆分为 src/config/ 包（按领域 card/acp/spec/lock 组织子模块），通过 __init__.py 统一导出 get_settings()。Deadline: 2026-06-15 | Low | architecture review | 待处理 | — |
| B004 | 2026-05-05 | 确认 timer_manager/timer_scheduler/timers/ 三套 timer 抽象与 action_dispatch/action_ids/action_router/actions/ 四套 action 路由的 canonical 模块无功能重叠。当前 shim 已有 B001 删除计划，但需额外验证 canonical 内部不存在多余公共接口。Deadline: 2026-06-01 | Low | architecture review | 待处理 | — |
| B005 | 2026-05-05 | DeepHandler.start_deep_engine() 用 CardBuilder.build_info_card() + reply_card() 发送 planning 卡片，show_deep_board() 仍走旧 CardBuilder。两套投递机制共存，需统一迁移到 CardSession/StaticCardSession。Deadline: 2026-06-15 | Medium | architecture review | 待处理 | — |
