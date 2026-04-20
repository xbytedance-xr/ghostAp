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
