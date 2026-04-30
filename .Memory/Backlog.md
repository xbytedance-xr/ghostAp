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
| B-001 | 2026-04-20 | `JsonLinesExporter` except 子句记录 `<class 'Exception'>` 而非实际错误信息 | Medium | Audit | Done | `d1b87f1` |
| B-002 | 2026-04-20 | `hard_floor` 自适应超时下限硬编码为 15s，不可通过配置调整 | Low | Audit | Done | `d1b87f1` |
| B-003 | 2026-04-20 | `get_metrics_exporter()` 单例在类型变更时返回旧缓存实例 | Low | Audit | Done | `d1b87f1` |
| B-004 | 2026-04-20 | `DeepEngine._drain_pending_context` L336 使用 `str(e) or repr(e)` 而非 `get_error_detail(e)`，内部 logger 路径不一致 | Low | Audit | Done | `d1b87f1` |
| B-005 | 2026-04-20 | `engine_base.py` 4 处 + `spec.py` 2 处 TimeoutError 分支 logger 使用 `str(e) or repr(e)` 而非 `get_error_detail(e)`，风格不一致 | Low | Audit | Done | `d1b87f1` |
| B-006 | 2026-04-20 | `sync_adapter.py` L682/L819/L1499/L1521 共 4 处非 TimeoutError 的 `except Exception` 块使用 `str(e) or repr(e)` 而非 `get_error_detail(e)`，风格不一致（内部诊断/调试路径，无用户可见影响） | Low | Audit | Done | `d1b87f1` |
| B-007 | 2026-04-20 | 全代码库 `str(e) or repr(e)` → `get_error_detail(e)` 统一：30+ 文件 90+ 处替换，含变量名变体 `str(exc/err/ex/cb_exc/error) or repr(...)` 全覆盖；新增 `test_empty_error_guard.py::TestBanStrOrReprPattern` 静态扫描回归门禁，仅 `errors.py` 自身实现豁免 | Low | Audit | Done | `d1b87f1` |
| B-008 | 2026-04-20 | TTADK 子系统 7 处 `str(e) or ""/(empty)` → `get_error_detail(e)` 增量加固（command_exec.py 2 处 + model_fetcher.py 3 处 + strategies.py 2 处）+ 新增 TestTimeoutErrorE2EDetail 6 个端到端测试 | Low | Incremental | Done | `60b2db6` |
| B-009 | 2026-04-23 | `worktree_engine/dispatcher.py` — `execute_units` 添加 ThreadPoolExecutor pool-level timeout（600s 默认），`as_completed(timeout)` + `except TimeoutError` + 未完成 task 批量取消 + 域语义文案，对齐 perspective_worker.py 黄金模式 | Medium | Scope Creep | Done | — |
| B-010 | 2026-04-23 | `acp/sync_adapter.py` — `_run_async()` 正则清洗 stdlib 的 `"N (of M) futures unfinished"` 内部诊断信息，避免传播到用户可见错误 | Medium | Scope Creep | Done | — |
| B-011 | 2026-04-23 | `spec_engine/perspective_worker.py` — 两处 `get_error_detail(e) or repr(e)` → `get_error_detail(e, default="未知错误")`，彻底消除 stdlib 诊断信息泄露入口 | Low | Scope Creep | Done | — |
| B-012 | 2026-04-23 | `config.py` — 将 dispatcher pool_timeout 从硬编码 600s 改为配置项 `worktree_pool_timeout`，与 spec_review_timeout/loop_review_timeout 对齐 | Low | Scope Creep | Done | — |
| B-013 | 2026-04-23 | `worktree_engine/models.py` — 新增 unit 级 `_cancelled` 字段与 `to_dict()` 重写，支持 worktree 执行中断场景的细粒度状态区分（cancelled vs failed vs stopped） | Low | Scope Creep | Done | — |
| B-014 | 2026-04-23 | 超时/取消回调函数集封装 — 将 dispatcher.py 中分散的 timeout/cancelled/safe_callback 代码封装为通用工具集，简化未来类似逻辑编写 | Low | Scope Creep | Done | — |
| B-015 | 2026-04-23 | 新增独立测试文件 `tests/test_worktree_dispatcher_timeout.py` — 5 个测试覆盖 pool timeout / inner timeout / callback / fast path | Low | Scope Creep | Done | — |
| B-016 | 2026-04-30 | **卡片系统旧路径全量迁移总览** — `CardSession → CardDelivery` 新管线目前仅覆盖编程模式流式响应，其余全部走旧路径，需逐模块迁移并最终移除旧 API。**收口路线图**：Phase 1（High）DiagnosticsHandler + WorktreeHandler → Phase 2（Medium）Deep/Loop/Spec Handler + Renderer 桥接 + BaseEngineHandler → Phase 3（Low）System/Lock/Programming 非流式 + BaseRenderer 兜底 → Phase 4 移除旧 API | Medium | Card Audit | In Progress | — |
| B-016-1 | 2026-04-30 | **DiagnosticsHandler `/diff`** (`handlers/diagnostics.py:414-538`) — 手动管理 `card_message_id` + 多次 `patch_message()` 模拟流式更新，最典型旧模式，迁移到 `CardSession` | High | Card Audit | Open | — |
| B-016-2 | 2026-04-30 | **WorktreeHandler** (`handlers/worktree.py`) — `send_message()` + 14 处 `patch_message()` 做进度更新，应迁移到 `CardSession` 流式更新 | High | Card Audit | Open | — |
| B-016-3 | 2026-04-30 | **DeepHandler 命令响应** (`handlers/deep.py:158,226,260,311,362`) — 启动卡片、看板、停止确认、上下文注入全用 `reply_message()` / `send_message()`，迁移到新卡片系统 | Medium | Card Audit | Open | — |
| B-016-4 | 2026-04-30 | **LoopHandler 命令响应** (`handlers/loop.py:137,261`) — 启动卡片、引导注入用 `reply_message()` / `send_message()`，迁移到新卡片系统 | Medium | Card Audit | Open | — |
| B-016-5 | 2026-04-30 | **SpecHandler 命令响应** (`handlers/spec.py:147,183,233,251,270,288,356,641,759`) — 启动、历史、指标、配置、引导注入、恢复等 ~8 处旧 API 调用，迁移到新卡片系统 | Medium | Card Audit | Open | — |
| B-016-6 | 2026-04-30 | **DeepRenderer / LoopRenderer / SpecRenderer → EngineCardSender 桥接** (`renderers/deep_renderer.py:46`, `loop_renderer.py:46`, `spec_renderer.py:48`) — 3 个 renderer 通过 deprecated `EngineCardSender` 发送卡片，需迁移到 `CardSession → CardDelivery` 主管线 | Medium | Card Audit | Open | — |
| B-016-7 | 2026-04-30 | **BaseEngineHandler 生命周期消息** (`handlers/engine_base.py:76,94,141,157,163,228`) — 暂停/恢复/停止/冲突等提示消息全用 `reply_message()`，迁移到新卡片系统 | Medium | Card Audit | Open | — |
| B-016-8 | 2026-04-30 | **SystemHandler** (`handlers/system.py`) — 帮助、ACP 命令、菜单、Shell 执行回复等全部走旧 `reply_message()` API，迁移到新卡片系统 | Low | Card Audit | Open | — |
| B-016-9 | 2026-04-30 | **LockCommandsMixin** (`handlers/lock_commands.py:78-132`) — `/lock` `/unlock` 卡片用 `reply_message()` / `send_message()`，迁移到新卡片系统 | Low | Card Audit | Open | — |
| B-016-10 | 2026-04-30 | **ProgrammingModeHandler 非流式部分** (`handlers/programming.py:837-885` + enter/exit/info/card actions) — 非流式降级路径、模式进入/退出、信息展示等用 `reply_message()` / `reply_message_with_id()`，迁移到新卡片系统 | Low | Card Audit | Open | — |
| B-016-11 | 2026-04-30 | **BaseRenderer status 卡片兜底** (`renderers/base.py:329-351`) — `_patch_or_send()` 通过 `handler.patch_message()` / `handler.reply_message()` 发送状态卡片，迁移到新卡片系统 | Low | Card Audit | Open | — |
| B-016-12 | 2026-04-30 | **BaseHandler 旧 API 移除** (`handlers/base.py:163-371`) — `reply_message()` / `patch_message()` / `send_message()` / `reply_message_with_id()` 旧方法，待所有调用方迁移完成后移除 | Low | Card Audit | Open | — |
| B-016-13 | 2026-04-30 | **文档过时引用** — `AGENTS.md:67`、`CLAUDE.md:67`、`README.md:91,455` 仍提及已删除的 `StreamingCardManager`，需更新为新架构描述 | Low | Card Audit | Done | — |
| B-017 | 2026-04-30 | 新卡片 reducer 尚未接入 `APPROVAL_REQUESTED` / `APPROVAL_RESOLVED` 的真实状态语义，审批事件当前被当作无变化处理 | Medium | Card Audit | Done | — |
| B-018 | 2026-04-30 | 卡片测试缺口：未覆盖分页 shrink、编程模式 non-streaming fallback 最终文本保留、approval 事件渲染语义 | Low | Card Audit | Done | — |
