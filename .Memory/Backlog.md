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
| B006 | 2026-05-10 | 卡片 v2 新增 API（CardMetadata.frozen/session_started_at/working_dir/card_sequence/bridge_phrase、CardSessionFactory.create_subagent、tools.build_subagent_dispatch_atom/render_subagent_dispatch_panel、ACPEventRenderer.snapshot_turns、LiveTicker）目前是死代码，无任何生产 caller。需在 wiring 批次接入 reducer/orchestrator/engine handler，否则线上只渲染退化版 v2 header。Deadline: 2026-05-24 | Medium | card v2 slice review | 待处理 | — |
| B007 | 2026-05-10 | `_should_render_v2_header` 用 `metadata.tool_name` 真值触发 → Deep/Loop/Spec 引擎卡也会切到 v2 header，覆盖 reducer 构造的 title/subtitle（含 phase/状态）。需补引擎卡 header 回归测试并确认 phase 信息不丢（移到 status 区或保留在 subtitle）。src/card/render/header.py:42-49 | Medium | card v2 slice review | 待处理 | — |
| B008 | 2026-05-10 | `CardMetadata.card_sequence` 与既有 `continuation_seq` 语义重复（都表示第几张卡），双 SSOT。需二选一：复用 continuation_seq 或迁移掉它。src/card/state/models.py:41,46 | Medium | card v2 slice review | 待处理 | — |
| B009 | 2026-05-10 | 卡片 elapsed 计算时钟混用脆弱：`_elapsed_seconds` fallback 用 `time.monotonic() - metadata.session_started_at`，依赖该字段始终经 monotonic clock 写入。需注释强约束 monotonic 或只通过注入 clock 写。src/card/render/header.py:100-120 | Low | card v2 slice review | 待处理 | — |
| B010 | 2026-05-10 | `task_list.group_tasks` 把 failed/cancelled 归进 "✅ 已完成 (N)" 桶（计数+图标自相矛盾）。建议单独一行或标签改"已结束"。src/card/render/task_list.py:30-44 | Low | card v2 slice review | 待处理 | — |
| B011 | 2026-05-10 | `render_task_list_panel(compact=...)` 变死参，sticky_head.py:65 仍传 compact=True 但函数体不再分支。要么真做 compact（sticky 省高度），要么删参数。 | Low | card v2 slice review | 待处理 | — |
| B012 | 2026-05-10 | 工具状态判定不统一：footer._tool_status 收 {"in_progress","running"} 但 ContentBlock BlockStatus 仅 Literal["active","completed","failed"]（task_list 那边对，ACP plan entry 用 "in_progress"）。挑 canonical 集合统一并去掉死分支。 | Low | card v2 slice review | 待处理 | — |
| B013 | 2026-05-10 | 杂项：footer.py `import json` 不按字母序（在 math 后）；LiveTicker 默认帧 ("🟢","🔵","🟣","🟠") 四色轮播与 spec 期望的 🟢↔⚪ 闪不符，且 🟠 与 subagent 橙主题撞色。 | Low | card v2 slice review | 待处理 | — |
