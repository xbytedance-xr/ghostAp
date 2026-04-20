# GhostAP 项目记忆索引

> **维护性 Backlog**: Low/Medium severity 审计缺口不再即时修复，统一录入 [Backlog.md](Backlog.md) 集中在维护窗口处理。分级标准与流程详见 Backlog 文件头部说明。

## 2026-04-20
- **局部耦合重构：引入 Registry 模式解耦 HandlerContext 与 FeishuWSClient** — 引入服务注册表模式，将硬编码单例引用替换为基于 HandlerContext 容器的动态查找；移除 FeishuWSClient 交叉注入循环；124 tests passed；`7b0ed1e` → [详细记录](2026-04-20.md)
- **第二十二次闭环验证：9 步任务分解 + 白名单全量审计确认** — 9 步任务并行+顺序执行（静态门禁 112 passed + TimeoutError 单元 22 passed + e2e 40 passed + review 36 passed + 17 处白名单 str(e) 逐一审计零风险 + Backlog B-001~B-008 全部 Done 8/8 + 全量 2329 passed 47.96s + 零代码改动仅验证归档）；第二十二次独立确认无退化无新缺口，问题闭环 → [详细记录](2026-04-20.md)
- **TTADK 子系统 str(e) → get_error_detail(e) 增量加固** — TTADK 3 个文件 7 处 `str(e) or ""/(empty)` → `get_error_detail(e)` 替换 + 新增 TestTimeoutErrorE2EDetail 6 个端到端测试；src/ str(e) 站点从 15 降至 8；全量 2329 passed 零回归 → [详细记录](2026-04-20.md)
- **第二十一次闭环验证：7 步任务列表顺序执行确认** — 7 步任务顺序执行（TimeoutError 专项 182 passed 2.87s + 静态门禁 1 passed + grep 扫描零残留 + Backlog B-001~B-007 全 Done 7/7 + 全量 2323 passed 48.41s + 零代码改动仅验证归档）；第二十一次独立确认无退化无新缺口，问题彻底闭环 → [详细记录](2026-04-20.md)
- **第二十次闭环验证：8 步任务列表 + str(e) 白名单全量审计** — 8 步任务严格顺序执行（Backlog B-001~B-007 全 Done 7/7 + src/ 全量 str(e)/repr(e) 扫描 18 处逐一归类为白名单/内部诊断/实现层全部语义正确零可改进 + 回归 106 passed 2.35s + 全量 2323 passed 49.12s + 语义评估零 High/Medium/Low 缺口 + Backlog 无变更）；第二十次独立确认无退化无新缺口 → [详细记录](2026-04-20.md)
- **第十九次闭环验证：7 步任务列表严格顺序执行确认** — 7 步验证任务并行+顺序执行（TimeoutError 专项 106 passed + e2e 40 passed + review 36 passed + 静态扫描 3 项零残留 + 全量 2323 passed 46.57s + Backlog B-001~B-007 全部 Done commit 引用更新为 `d1b87f1` + 零缺陷零修复）；第十九次独立确认无退化无新缺口 → [详细记录](2026-04-20.md)
- **全量统一提交落地：`str(e) or repr(e)` → `get_error_detail(e)` + Backlog 归档** — 30+ 文件 90+ 处全量替换 + `TestBanStrOrReprPattern` 静态门禁 + Backlog B-001~B-007 全部 Done；验证（专项 106 passed + 全量 2323 passed + 5 项 grep 零残留）后提交 `d1b87f1`（44 files, +288/-131）；提交后二次回归 2323 passed 零退化 → [详细记录](2026-04-20.md)
- **第十八次闭环验证：9 步任务列表严格顺序执行确认** — 9 步任务含依赖关系严格顺序执行（TimeoutError 专项 106 passed 2.36s + 全量 2323 passed 48.87s + spec_engine/review.py 6 项防御完整 + loop_engine/engine.py 与 spec 一致含收敛跳过 + review_helpers.py 三函数参数合理边界有保护 + Backlog B-001~B-007 全部 Done 7/7 + 零缺陷零修复）；第十八次独立确认无退化无新缺口 → [详细记录](2026-04-20.md)
- **第十七次闭环验证：完整 Plan 分解执行确认** — Spec→Plan→Task 方法论分解 8 个任务按依赖执行（TimeoutError 专项 106 passed + 全量 2323 passed 47.49s + spec_engine/review.py 防御完整 + loop_engine/engine.py 与 spec 一致 + review_helpers.py 参数合理 + Backlog B-001~B-007 全部 Done + 零缺陷零修复）；第十七次独立确认无退化无新缺口 → [详细记录](2026-04-20.md)
- **第十六次闭环验证：任务列表逐步执行确认** — 8 步任务严格顺序执行（依赖同步 84pkg + TimeoutError 专项 106 passed + 静态 lint 门禁 6 passed + 全量 2323 passed 47.87s + Backlog B-001~B-007 全部 Done + 零缺口零修复）；第十六次独立确认无退化无新缺口 → [详细记录](2026-04-20.md)
- **第十五次闭环验证：任务分解执行确认** — 8 个任务按依赖顺序逐一执行（依赖同步 + TimeoutError 专项 106 passed + 静态 lint 门禁 6 passed + 全量 2323 passed + Backlog B-001~B-007 全部 Done + 零缺口零修复）；第十五次独立确认无退化无新缺口 → [详细记录](2026-04-20.md)
- **第十四次闭环验证：审查执行异常改进建议最终归档确认** — 全量 2323 passed (47.72s) + TimeoutError 专项 245 passed + Backlog B-001~B-007 全部 Done 零 Open + 静态零残留 3 项验证通过（str(e) or repr(e) 仅 errors.py 白名单/asyncio.wait_for 仅 safe_wait_for 内部/raise TimeoutError() 零残留）；第十四次独立确认无退化无新缺口，改进建议完全落实，问题归档关闭 → [详细记录](2026-04-20.md)
- **B-006 + 全代码库 `str(e) or repr(e)` → `get_error_detail(e)` 统一** — 关闭最后一个审计缺口 B-006（sync_adapter.py 4 处）；同时全代码库 30+ 文件 90+ 处 `str(e/exc/err/ex/cb_exc/error) or repr(...)` 全量替换为 `get_error_detail()`；新增 `TestBanStrOrReprPattern` 静态扫描回归门禁（仅 `errors.py` 自身实现豁免）；2323 tests passed 零回归；Backlog B-001~B-007 全部 Done → [详细记录](2026-04-20.md)
- **第十三次闭环验证：10 层防御体系零缺口确认** — 11 步任务清单全量执行（全量 2322 passed + TimeoutError 专项 245 passed + 回归 lint 15 passed + 空消息守卫 105 passed + E2E 40 passed + Grep 扫描零裸 asyncio.wait_for + 14 处 TimeoutError except 块全部受保护）；零新增缺口、零代码改动；Backlog B-001~B-005 全部 Done → [详细记录](2026-04-20.md)
- **第十二次闭环验证 + ws_client/dispatcher 残余 TimeoutError 日志路径修复** — 11 步任务清单全量执行（全量 2322 passed + TimeoutError 专项 245 passed + 回归 lint 15 passed + 空消息守卫 105 passed + E2E 40 passed + Grep 扫描零裸 asyncio.wait_for）；发现并修复 ws_client.py 2 处 + dispatcher.py 1 处 TimeoutError except 块日志层 `str(e) or repr(e)` → `get_error_detail(e)` 统一；修复后 2322 passed 零回归；Backlog B-001~B-005 全部 Done → [详细记录](2026-04-20.md)
- **B-005 修复：engine_base.py / spec.py TimeoutError 分支 logger 统一到 get_error_detail** — `engine_base.py` 4 处 + `spec.py` 2 处 `str(e) or repr(e)` → `get_error_detail(e)` + 移除 spec.py 2 处冗余 local import（修复 UnboundLocalError）；2322 passed 零回归 + 回归 lint 105 passed 零违规；Backlog B-001~B-005 全部 Done 无 Open 条目 → [详细记录](2026-04-20.md)
- **B-004 修复：DeepEngine logger 路径统一到 get_error_detail** — `engine.py` 4 处 `str(e) or repr(e)` → `get_error_detail(e)`（_drain_pending_context×2 + _build_on_event + load_state）；2322 passed 零回归 + 回归 lint 105 passed 零违规；Backlog B-001~B-004 全部 Done 无 Open 条目 → [详细记录](2026-04-20.md)
- **第十一次独立验证：15 项任务清单全量闭环确认** — 全量 2322 passed (47.63s) + TimeoutError 专项 249 passed (3.13s, 6 个测试文件) + 静态回归扫描 4 项零违规 + 10 层关键代码逐层抽查全部完整 + Backlog B-001/B-002/B-003 Done；新发现 B-004（DeepEngine._drain_pending_context logger 路径 Low severity）录入 Backlog；第十一次独立确认无退化无用户可见缺口 → [详细记录](2026-04-20.md)
- **第十次独立验证：10 层防御体系完全闭环确认** — 全量 2322 passed (47.11s) + TimeoutError 专项 249 passed (3.13s, 6 个测试文件) + 静态回归扫描 4 项零违规（裸 f"{e}"/裸 asyncio.wait_for/裸 logger %s,e/裸 raise TimeoutError()）+ Backlog 三项 Done 无新增；第十次独立确认无退化无新缺口，问题彻底闭环 → [详细记录](2026-04-20.md)
- **第九次独立验证：10 层防御体系持续闭环确认** — 全量 2322 passed (47.83s) + TimeoutError 专项 145 passed (2.59s) + 静态回归扫描 5 项零违规（裸 f"{e}"/裸 asyncio.wait_for/裸 logger %s,e/裸 raise TimeoutError()）；第九次独立确认无退化无新缺口 → [详细记录](2026-04-20.md)
- **第八次独立验证：10 层防御体系最终闭环确认** — 全量 2322 passed (48.38s) + TimeoutError 专项 245 passed + 回归 lint 105 passed + E2E 40 passed + Grep 4 项零违规 + Backlog 三项 Done 无新增 + 10 层关键文件抽查 10/10 通过（safe_wait_for/fmt_error/get_error_detail/三引擎 except/SlidingWindowTracker/lightweight_lint/handle_review_exception/LoopReporter）；第八次独立确认无退化无新缺口 → [详细记录](2026-04-20.md)
- **第七次独立验证：10 层防御体系持续完整** — Grep 4 项零残留 + Backlog 三项 Done + 回归 lint 105 passed + E2E 40 passed + TimeoutError 专项 245 passed + 全量 2322 passed (46.32s) + 代码审查 4 项（\_run\_async/send\_prompt/handle\_review\_exception/safe\_wait\_for）确认非空友好文案；第七次独立确认无退化无新缺口 → [详细记录](2026-04-20.md)
- **第六次独立验证：10 层防御体系持续完整** — 全量 2322 passed (47.40s) + Grep 4 项零残留 + Backlog 三项 Done + 代码审查 3 项（handle_review_exception/\_run\_async/safe\_wait\_for）确认非空友好文案 + 回归 lint 105 passed；第六次独立确认无退化无新缺口 → [详细记录](2026-04-20.md)
- **第五次独立验证：10 层防御体系持续完整** — Grep 4 项零残留 + Backlog 三项 Done + 回归 lint 105 passed + E2E 40 passed + TimeoutError 专项 245 passed + 全量 2322 passed (49.86s)；第五次独立确认无退化无新缺口 → [详细记录](2026-04-20.md)
- **第四次独立验证：10 层防御体系持续完整** — Grep 4 项零残留 + Backlog 三项 Done + 回归 lint 105 passed + E2E 40 passed + TimeoutError 专项 245 passed + 全量 2322 passed；第四次独立确认无退化无新缺口 → [详细记录](2026-04-20.md)
- **三次确认验证：10 层防御体系执行闭环** — 按 9 项任务清单逐步执行（Grep 4 项零残留 + Backlog 三项 Done + 回归 lint 105 passed + 全量 2322 passed, 46.91s）；第三次独立确认无退化无新缺口，改进建议已完全落实 → [详细记录](2026-04-20.md)
- **二次确认验证：10 层防御体系持续完整** — Grep 4 项零残留 + Backlog 三项 Done + 回归 lint 105 passed + 全量 2322 passed；10 层防御无退化无新缺口 → [详细记录](2026-04-20.md)
- **最终验证闭环：TimeoutError (empty message) 10 层防御体系完整性确认** — 全量 2322 tests passed + grep 4 项零残留 + 代码审查 6 项确认（_run_async/send_prompt/LoopReporter/review_diagnostics/三引擎 TimeoutError 分支/CircuitState 持久化）+ Backlog 三项 Done；10 层防御体系完整闭合，无代码改动需求 → [详细记录](2026-04-20.md)
- **引入维护性 Backlog 机制** — 新建 `.Memory/Backlog.md` 收集 Low/Medium severity 审计缺口；AGENTS.md Workflow Rules 新增第 5 条分级处理规则；Abstract.md 添加 Backlog 入口；避免低优先级修复打断主线开发节奏 → [详细记录](2026-04-20.md)
- **三项审计缺口修复：metrics_exporter bug + hard_floor 可配置化 + 单例重配置** — (A) JsonLinesExporter except 子句 `str(Exception)` → `str(e)` 修复日志记录类名 bug；(B) config.py 新增 `spec_review_hard_floor`/`loop_review_hard_floor`，Spec/Loop review 传递到 `compute_adaptive_timeout`；(C) `get_metrics_exporter` 单例支持类型变更时自动重建；+10 新测试，2322 tests passed；零回归 → [详细记录](2026-04-20.md)
- **_run_async 空消息包装 + LoopReporter (empty message) 过滤** — sync_adapter._run_async 补空消息 TimeoutError 包装（与 send_prompt 对齐）；LoopReporter.format_iteration_done 过滤 (empty message)/空/None 错误文本替换为友好提示；+9 新测试，2312 tests passed；零回归 → [详细记录](2026-04-20.md)
- **三项增量改进：Metrics Exporter + 滑动窗口熔断 + Lint 降级** — (A) 新增 `metrics_exporter.py` 模块（ReviewMetricsExporter 协议 + LoggerExporter + JsonLinesExporter），review_helpers 通过接口输出 metrics，config 可切换 exporter 类型；(B) 新增 `SlidingWindowTracker` 类，CircuitState 新增 `recent_outcomes` 字段，handle_review_exception 集成滑动窗口动态熔断（与 max_consecutive 并列触发，window_size/threshold 可配置）；(C) 新增 `lightweight_lint.py` 模块（ast.parse + ruff check），Spec/Loop 熔断跳过分支自动运行本地 lint 并注入 suggestions（可配置开关+超时）；+60 新测试，2303 tests passed（baseline 2243 + 60）；零回归 → [详细记录](2026-04-20.md)
- **TimeoutError (empty message) 增量加固提交落地** — should_retry isinstance 短路+prompt_with_retry 可观测性日志+compute_adaptive_timeout hard_floor=15s+normalize_review_diagnostics error_text 500 字符截断+lint 禁止裸 raise TimeoutError()+concurrent.futures.TimeoutError 6 层 E2E 测试；2243 tests passed（+26）；`9d0ffb9` → [详细记录](2026-04-20.md)
- **落实改进建议：ReviewCircuitState 持久化提交落地** — 将 9 个文件 +609 行未暂存改动提交（ReviewCircuitState to_dict/from_dict 序列化、SpecEngine/LoopEngine save/load_state_with_circuit、Loop skip overrun 保护、12 个 E2E empty message 守卫测试）；全量 2189 passed + 94 回归 lint + 193 超时专项全绿；`a7c8e64` → [详细记录](2026-04-20.md)
- **Spec ReviewCircuitState consecutive_skips 对齐 + resume circuit 恢复** — Spec ReviewCircuitState 补齐 consecutive_skips 字段+序列化+skip overrun 检测，与 Loop 熔断器能力对齐；Spec/Loop 两引擎 resume() 新增 load_state_with_circuit() 自动恢复持久化 circuit state（消除进程重启后熔断状态丢失风险）；+8 新测试，2197 tests passed；`f75fa41` → [详细记录](2026-04-20.md)
- **Review 重试总耗时约束 + 可观测性增强** — RetryPolicy/prompt_with_retry 新增 total_timeout 约束（review 场景 = review_timeout×2），防止重试阻塞失控；agent_session 5 处 + SpecEngine 链路全部适配；CircuitState 新增 last_review_elapsed_ms + metrics 新增 total_elapsed_ms；+20 新测试 + 1 lint 回归守卫，2217 tests passed；`ee75916` → [详细记录](2026-04-20.md)

## 2026-04-19
- **ReviewCircuitState 持久化 + Loop 审查跳过率保护 + E2E empty message 测试** — 将 Spec/Loop 的 ReviewCircuitState 纳入状态持久化（save/load_state round-trip，旧快照兼容）；LoopEngine 新增 `consecutive_skips` 字段和 `review_skip_overrun` warning；补充 5 个 E2E empty message 端到端测试 + 7 个 `build_review_error_suggestion` 输出守卫；2189 tests passed → [详细记录](2026-04-19.md)
- **review 异常处理统一抽取 handle_review_exception** — 将 Spec/Loop 两引擎 ~160 行重复 except 分支抽取到 `review_helpers.py` 的 `handle_review_exception()` 共享函数；统一 timeout 检测逻辑（Spec 侧补齐 isinstance+detail 冗余检查）；新增 `_is_timeout_error()`、`ReviewExceptionResult` NamedTuple；+18 新测试，2167 tests passed；`433c2c4` → [详细记录](2026-04-19.md)
- **统一 _has_timeout_in_chain + review_timeout 哨兵修复 + metrics 测试覆盖** — 消除 errors.py 和 review_diagnostics.py 的 `_has_timeout_in_chain` 重复实现（合并 isinstance+类名匹配逻辑，review_diagnostics 改为导入）；修复 Spec/Loop review_timeout `'in dir()'` 不可靠检查改为哨兵默认值；新增 15 个测试（8 链检测一致性+7 metrics 结构验证），2149 tests passed；`433c2c4` → [详细记录](2026-04-19.md)
- **异常链遍历增强 + 结构化 metrics 日志** — `_infer_fail_reason()` 和 `get_error_detail()` 增加异常链 (`__cause__`/`__context__`) 遍历（最大深度 10 层），包装在非 TimeoutError 内的 TimeoutError 也能正确识别；SpecEngine + LoopEngine 审查异常块新增结构化 metrics 日志（JSON 格式，含 metric_type/fail_reason/consecutive_timeouts/circuit_open 等字段）；+16 新测试，2134 tests passed → [详细记录](2026-04-19.md)
- **Review 熔断器指数退避 + 渐进超时 + 异常处理统一** — 新增 `src/utils/review_helpers.py` 共享模块（3 个函数：`build_review_error_suggestion`/`compute_exponential_cooldown`/`compute_adaptive_timeout`）；SpecEngine + LoopEngine 的 ReviewCircuitState 新增 `backoff_level`/`consecutive_timeouts`；熔断器 cooldown 从固定值升级为指数退避（3→6→12，上限可配置）；review timeout 渐进缩短（120→60→30s）；suggestion 文案生成统一到共享函数；config.py 新增 4 配置项；+36 新测试，2118 tests passed → [详细记录](2026-04-19.md)
- **最终验证确认：8 层 TimeoutError 防御体系完整闭合** — 全量 2082 tests + 147 专项测试 + 4 类 grep 扫描全绿；src/ 零裸 asyncio.wait_for/f"{e}"/裸 logger %s,e/裸 str(e)；8 层防御体系无退化，问题彻底解决 → [详细记录](2026-04-19.md)
- **超时用户通知 + programming handler 超时专用分支** — ws_client 消息/卡片超时从静默日志改为主动通知用户（TTADK 发软失败卡片，通用路径发文本）；programming handler 两处 send_prompt 插入 except TimeoutError 专用分支（文案区分超时/异常）；+4 新测试，2082 tests passed → [详细记录](2026-04-19.md)
- **最终验证闭环：TimeoutError (empty message) 改进建议落实确认** — 全量2078测试+66回归Lint+131超时专项全绿；grep零残留裸asyncio.wait_for；补上ws_client.py:1615/2258两处fire-and-forget日志盲点（`as e` + `str(e) or repr(e)`）；8层防御体系全部就位，问题彻底解决 → [详细记录](2026-04-19.md)
- **验证审查：8 层防御体系闭合确认 + 2 处增量修复** — 全面审查 8 层 TimeoutError 防御体系（全量 2078 tests + 4 lint 扫描器 + 140 专项测试全绿）；修复 ws_client.py 卡片动作 `str(e) or repr(e)` → `get_error_detail(e)` + worktree dispatcher 新增 logger.warning + except Exception 兜底；2078 tests passed → [详细记录](2026-04-19.md)
- **TimeoutError (empty message) 8 层纵深防御体系最终闭合** — 审查确认 8 层防御（核心兜底→用户可见→logger→引擎→review 断路器→收敛保护→回归 lint→safe_wait_for 源头防御）全部就位；src/ 零残留裸 asyncio.wait_for / f"{e}" / str(e)；`(empty message)` 源头消灭；2078 tests passed + 82 回归 lint 测试全绿 → [详细记录](2026-04-19.md)
- **logger 路径 bare %s,e 全量加固 + safe_wait_for 测试补全** — 30 个 src 文件共 93 处 `logger.xxx("...%s", e)` bare exception 变量统一替换为 `str(e) or repr(e)` 守卫；新增 `_BARE_LOGGER_PERCENT_RE` 回归 lint；扩展 safe_wait_for 4 个边界/取消测试 + 新建 4 个集成测试（ACP stream/healthcheck/shutdown 超时）；2078 tests passed → [详细记录](2026-04-19.md)
- **safe_wait_for 源头防御 + 回归 lint 扩展** — 新增 `src/utils/async_helpers.py` 封装 `asyncio.wait_for` 为 `safe_wait_for`，自动为空消息 TimeoutError 附加 action 文案；替换 session.py 2处 + shutdown.py 1处；扩展回归 lint 检测裸 asyncio.wait_for；+8 新测试 +1 lint 测试，2069 tests passed → [详细记录](2026-04-19.md)
- **最终一致性加固: ttadk 内部路径 bare f"{e}" 消除** — strategies.py:302 + ttadk_wrapper.py:458,480 共 3 处内部诊断路径 bare `f"{e}"` → `str(e) or repr(e)` 一致性加固；项目中零残留裸异常格式化；2060 tests passed → [详细记录](2026-04-19.md)
- **回归扫描器加固 + 残余裸异常消除 + asyncio.TimeoutError e2e 覆盖** — 修复 `_SKIP_GUARDS` 的 `str(` 过宽漏检问题（移除 `str(`，仅保留 `" or "` 守卫）；扩展 lint 正则变量名覆盖（+ex/te/error/exception）和用户可见函数覆盖（+_reply_message/reply_text/update_card）；修复 sync_adapter.py:819 + gc_monitor.py:59,68 共 3 处残余裸 `f"{e}"` / `f"{ex}"`；为 Deep/Loop/Spec 引擎补充 asyncio.TimeoutError e2e 用例；2060 tests passed → [详细记录](2026-04-19.md)
- **review_diagnostics 源头消灭 (empty message) 标记 + 低风险路径增量加固** — review_diagnostics 层 `(empty message)` 标记从下游过滤升级为源头消灭（空消息按 timeout/非timeout 分流中文友好文案）；补强 worktree dispatcher/manager、base handler fallback、deep engine logger 共 5 处低风险路径；+14 新测试，2057 tests passed；`a962ee7` → [详细记录](2026-04-19.md)
- **完成零盲区 str(exc) 空值加固提交落地** — 将 12 轮增量修复的 20 个文件（+707/-35 行）统一提交：17 个 src/ 文件的用户可见/logger/内部诊断路径全量加固 + 245 行 guard 测试 + 33 个端到端超时测试；8 层纵深防御体系完整闭环（核心兜底→用户可见→logger→引擎→review 断路器→收敛保护→回归 lint→测试覆盖）；2043 tests passed；`d2b28da` → [详细记录](2026-04-19.md)
- **内部诊断路径 logger 裸 f"{e}" 全量加固 + 回归 lint 扩展** — 修补 13 处 logger.warning/error 中裸 `f"{e}"` 引用（intent_recognizer/engine_base/project manager/artifacts + ws_client/action_dispatcher/errors/strategies），统一加 `str(e) or repr(e)` 守卫；扩展回归 lint 覆盖 logger 路径（`_BARE_LOGGER_RE`）；+13 新测试（4 组 guard + 1 个 logger lint），2043 tests passed → [详细记录](2026-04-19.md)
- **system.py handle_refresh_ttadk_models 最后一处裸 f"{e}" 修复 + lint 回归检查** — 修复 system.py:1476 `reply_error` 裸 `f"{e}"` → `get_error_detail(e)`；新增 3 个集成测试（`TestSystemHandlerRefreshModelsIntegration`）+ 1 个 regex lint 回归检查（`TestNoBareFStringInUserVisibleErrors`），2030 tests passed → [详细记录](2026-04-19.md)
- **用户可见 f"{e}" 裸引用最终收尾（6 处修复 + 6 测试）** — 修补 programming.py ACP执行/模型切换、ws_client.py 卡片操作、agent_session.py Claude/TTADK 执行、diagnostics.py Diff报告共 6 处用户可见 `f"...{e}"` 裸引用，统一替换为 `get_error_detail(e)` 或 `str(e) or repr(e)`；+6 测试（TestUserFacingEmptyGuardFinal），2026 tests passed → [详细记录](2026-04-19.md)
- **内部诊断路径 str(e) 零盲区加固 + 端到端 TimeoutError 集成测试** — 加固 8 处 internal-only `str(e)` 路径（acp/client 2处、coco_model/manager 1处、sandbox/executor 1处、ttadk/manager 2处、ttadk/cache 2处）统一加 `or repr(e)` 守卫；新增 `test_timeout_e2e.py` 21 个端到端测试覆盖 Formatter/Card/Deep/Loop/Spec/Sandbox/内部诊断全链路；2020 tests passed → [详细记录](2026-04-19.md)
- **彻底消除剩余 str(e) 空值缺口（6 文件 12 处）** — deep/loop/spec handler 项目创建、spec handler 导出/恢复/保存状态、system handler TTADK refresh、spec_engine last_error/rewrite_requirement、worktree manager init/merge、main.py 顶层异常，统一替换为 `get_error_detail(e)` 或直接传异常对象给 `fmt_error()`；+33 测试（test_empty_error_guard.py），1999 tests passed；`be61258` → [详细记录](2026-04-19.md)
- **增量闭合 str(exc) 空值守卫：5 处缺口修补** — `build_error_card` 改用 `get_error_detail`、`send_error_card` fallback 空值兜底、`scheduler` `state.error` 用 `repr(e)` 兜底、`fmt_exception` 非超时路径用 `repr(exc)` 兜底、`worktree dispatcher` 统一到 `get_error_detail()`；+15 新测试，1981 tests passed；`359fb82` → [详细记录](2026-04-19.md)
- **闭合「审查执行异常: TimeoutError (empty message)」残余缺口** — engine_base.py `_safe_lifecycle_action` 用户消息用 `get_error_detail` 替代裸 `str(e)` 消除空尾；loop_engine/spec_engine review 非 timeout 异常分支 `(empty message)` 替换为中文友好文案；同步更新 test_convergence/test_log_noise 测试 fixture；1966 tests passed；`3b237e5` → [详细记录](2026-04-19.md)
- **三引擎 execute/resume 顶层 TimeoutError 分支加固** — Deep/Loop/Spec 三引擎的 execute/resume 顶层 except Exception 前插入 except TimeoutError 分支，超时日志从 ERROR 降为 WARNING、文案区分"超时"/"异常"；Deep Engine 额外加固 _drain_pending_context；+7 新测试，1966 tests passed；`3b237e5` → [详细记录](2026-04-19.md)
- **Loop Engine 结构化审查诊断：与 Spec Engine 对齐** — 提取 `build_review_exception_diagnostics` / `format_review_exception_log_line` 到 `src/utils/review_diagnostics.py` 可复用模块；Loop Engine `_conduct_review` 引入结构化 diag dict、`LoopReviewCircuitState.last_review_failure_diag` 存储、结构化日志；Spec Engine 改为 re-export 零风险；+6 新测试，1959 tests passed；`41b5970` → [详细记录](2026-04-19.md)
- **Loop Engine review 熔断器 + 收敛检测加固** — 将 Spec Engine 的三层 TimeoutError 防御推广到 Loop Engine：新增 `LoopReviewCircuitState` 熔断器（连续 3 次 review 异常后跳过 review 3 轮冷却）、`IterationRecord.review_decision` 字段、收敛检测跳过 `review_failed` 轮次防止误判；3 个配置项（`loop_review_failure_circuit_enabled/max_consecutive/cooldown_iterations`）；+15 新测试，1953 tests passed → [详细记录](2026-04-19.md)
- **修复 Spec Engine 收敛检测误判** — review 连续 timeout 时 fallback suggestions 固定文本导致 `detect_convergence` 误判为收敛退出；修复：异常轮次（`review_decision` 以 `review_failed` 开头）不参与收敛比较；+4 测试，30 convergence tests passed → [详细记录](2026-04-19.md)
- **改进 Spec Engine 审查超时体验** — 解决 `TimeoutError (empty message)` 不友好文案：sync_adapter 为 TimeoutError 附加有意义消息、review 诊断层对 timeout 用中文友好文案、fallback suggestions 区分 timeout/非 timeout、review timeout 从硬编码改配置项 `spec_review_timeout`、熔断器默认开启；+7 新测试，1920 tests passed → [详细记录](2026-04-19.md)
- **审查验证：TimeoutError 改进落实确认** — 全面审查 commit 416c13a/e1b99c4 的三层防御（Transport/Diagnostics/Safety），确认 sync_adapter re-raise、review 诊断友好文案、熔断器、收敛检测跳过、其他引擎兼容均无遗漏；+14 新测试（test_review_timeout.py），1934 tests passed → [详细记录](2026-04-19.md)
- **Worktree Engine TimeoutError 加固** — 将 spec_engine 的 TimeoutError 防御推广到 worktree_engine：dispatcher._run_single_unit 增加 except TimeoutError 友好消息、execute_units/manager 空串兜底；+4 新测试，1938 tests passed → [详细记录](2026-04-19.md)

## 2026-04-18
- **/simplify 续做：LLM 缓存复用、渲染器收口与项目持久化一致性** — 新增 `src/utils/llm.py` 并接入 Loop/Spec/Intent；`ACPEventRenderer` 改脏标记重建+完成计数收口；`ProjectManager` 补齐 touch 持久化一致性；定向测试 `419 passed` → [详细记录](2026-04-18.md)
- **Worktree 编排系统实现** — 新增 `/wt` 交互选择多工具-模型对+独立 worktree 创建+并行执行+合并+清理；session 工厂/6 卡片构建器/8 handler/8 card action 路由/git merge-remove-cleanup/manager merge-cleanup/dispatcher 回调；30 个新测试，全量 `1892 passed` → [详细记录](2026-04-18.md)

## 2026-04-17
- **全项目 simplify 清理（渲染器缓存/项目持久化/调度器别名）** — `ACPEventRenderer` 先改增量文本缓存，`TaskScheduler` 清理未使用 camelCase 兼容别名，`ProjectManager` 激活路径收口（持久化一致性在 2026-04-18 续补）；定向测试 `434 passed` → [详细记录](2026-04-17.md)
- **/exit 误报二次修复（project_id 传递缺失）** — `_is_in_this_mode`/`_is_in_opposite_mode`/`_is_any_other_programming_mode` 只传 chat_id 不传 project_id，项目级模式查 chat 级返回 False 导致误报；统一为所有模式判断方法增加 project_id 透传，6 个子类全量修复；`44f67b6` → [详细记录](2026-04-17.md)

## 2026-04-14
- **/spec_guide 目标重写修复（类 btw 命令语义）** — 原实现仅临时注入引导，现改为 LLM 合并原始目标+引导生成新目标并持久化到 `project.requirement`，降级到 `inject_guidance` 当 LLM 失败；237 spec tests passed → [详细记录](2026-04-14.md)
- **/exit 误报"不在模式中"修复** — 进入模式未发消息时 ACP session 未创建，exit_to_smart 前捕获 was_in_this_mode 新增 is_mode_only_exit 分支；`c806030` → [详细记录](2026-04-14.md)
- **Deep Agent 完成卡片无内容修复** — 修复 `on_project_done` 显示 0% 进度条 + 执行输出近空问题；改用 closure 本地 renderer、空内容兜底提示、total_steps=0 时不显示进度条；`format_summary()` 增加 kind 拆分（如 `search: 90 · execute: 5`）；78 tests passed → [详细记录](2026-04-14.md)
- **/help 卡片扁平化重构** — 移除 4 个 tab 切换，所有命令分 6 个 section 一次展开；顶部新增 6 个手机友好快捷入口按钮（Deep/TTADK/ACP/状态/切换项目/新建项目），全部复用已注册 callback；`category` 参数保留向后兼容；123 tests passed → [详细记录](2026-04-14.md)

## 2026-04-12
- **测试套件加速 3.1x** — test_coco_model 新增 autouse fixture 阻止真实 ACP 探测(140s→0.16s)、test_spec_gc 构造前 patch get_settings 压缩 cycles(16.8s→0.58s)、test_force_interactive_env 补 mock _read_until_prompt(10s→0)；总耗时 225s→72s，1837 tests 全绿，零生产代码改动 → [详细记录](2026-04-12.md)

## 2026-04-11
- **Spec 引擎执行日志与渲染稳定性修复** — _run_phase 添加阶段开始/完成日志解决执行无日志问题、session=None 防御性检查、session 重建失败 ERROR 日志、on_review_done 修复 criteria_section 未折叠渲染 bug 和 sp 变量潜在 NameError → [详细记录](2026-04-11.md)

## 2026-04-04
- **话题编程模式 chat_id 降级防御（第六轮终结）** — 前五轮均未能解决持续对话中断；本轮确认真正根因：_dispatch_to_thread 首条消息后 ModeManager.exit_to_smart 导致模式状态仅存于 ThreadContext(root_id 可查)，后续消息 root_id 不匹配时模式永久丢失；实施第三层防御 chat_id 降级(get_by_chat)覆盖 _resolve_message_context/safety-net/_handle_message 三处；mloop 2 轮收敛(2/2 CLEAN)，2075 tests passed → [详细记录](2026-04-04.md)
- **话题编程模式双键注册修复（第五轮）** — ThreadContextManager.register 支持 alias_keys 双键存储(reply_id+message_id)、canonical thread_root_id 全链路传播、get_by_chat/active_count 去重、remove 规范化到 canonical；mloop 3 轮收敛，2073 tests passed → [详细记录](2026-04-04.md)
- **话题编程模式持续对话交付（reply_id 根因修复）** — 继续收口 thread 持续编程回归；确认真正 thread root 必须使用机器人创建的话题 reply_id，补齐 project=None 时 active project fallback 与 handler 恢复，并完成 mloop 两轮收敛；全量验证 2060 passed → [详细记录](2026-04-04.md)
- **话题编程模式多层防御修复（第三轮）** — 前两轮修复(thread_root_id/跳过enter_mode)未解决问题；本轮发现 _resolve_message_context 中 project 查找失败导致 auto_enter_mode 丢失、_dispatch_to_thread 仅在 project 非 None 时注册 ThreadContext、handle_message 在 project=None 时静默返回等多个断裂点；实施四层防御：(1) _resolve_message_context 解耦 mode 和 project 查找+始终返回不 fall-through (2) _process_message_async 安全网 (3) _dispatch_to_thread 无条件注册 (4) handle_message 统一恢复路径；mloop 4 轮收敛(2/2 CLEAN)，2058 tests passed → [详细记录](2026-04-04.md)

## 2026-04-03
- **One-Shot Pending Slot 编程模式重构** — 主对话开启编程模式后进入 pending 状态（仅设 ModeManager 不建 session），首条编程指令自动 _dispatch_to_thread 创建话题并运行会话，shell 命令保护不消费机会；mloop 4 轮审查收敛(2/2 CLEAN)，2029 tests passed → [详细记录](2026-04-03.md)
- **话题编程模式优化：单链接约束 + 引导提示** — 新增 _find_active_thread + 跨模式单链接清理（旧话题 session 根据 mode 动态查 handler）；引导提示从 SMART 前置拦截改为意图识别失败时精准触发；mloop 3 轮收敛(2/2 CLEAN)，2037 tests passed → [详细记录](2026-04-03.md)
- **修复话题编程持续对话失败** — thread_root_id 使用 reply_message_with_id 返回值而非原始 message_id 导致 ThreadContext 查找失败，后续消息回退 SMART 模式；3 行核心修复；mloop 2 轮收敛(2/2 CLEAN)，2038 tests passed → [详细记录](2026-04-03.md)
- **话题内持续编程模式** — _dispatch_message_logic 每条话题消息调用 enter_mode 导致 project snapshot 旧 session_id 覆盖 thread session；跳过 enter_mode 直接 handle_message + snapshot 安全网 + defer_exit；mloop 3 轮收敛，2048 tests passed → [详细记录](2026-04-03.md)

## 2026-04-02
- **Thread 并发编程 R6-R10 修复与收敛** — exit_mode 双重清理修复（remove移到finally）、StreamingCard 存储 thread_root_id 替代 threading.local、enter_mode 孤儿session清理+用户反馈、rebind_thread 冲突检查、enter_mode _set_mode_on_project 条件对齐；on_evict 测试 7 cases + rebind overwrite test 1 case；mloop 10 轮审查收敛(8/8旅程全PASS)，2018 tests passed → [详细记录](2026-04-02.md)

## 2026-04-01
- **Thread 编程 ACP Session rebind_thread 修复** — enter_mode 创建 session 时 thread_id=None，后续 thread 消息以 response_id 查找失败；新增 ACPSessionManager.rebind_thread() 迁移 session key，3 tests passed → [详细记录](2026-04-01.md)
- **Thread 感知模式路由修复（R3-Fix1 + R3-Fix2）** — `_dispatch_message_logic` 新增 auto_enter_mode 下 /exit 和编程入口命令拦截；新增 `_get_effective_mode` 辅助方法实现 Thread 级模式感知，替换 `_process_with_intent` 和 `_dispatch_empty_text` 的模式获取逻辑 → [详细记录](2026-04-01.md)
- **基于 Claude Code Agent Loop 分析的 Loop/Spec 引擎优化** — LoopEngine 接入 LoopContextManager 三级压缩+防漂移锚点、增强收敛检测（标准停滞+连续失败）；SpecEngine 阶段间结构化产物传递、循环间 Session 重建压缩上下文（+配置项 spec_rebuild_session_between_cycles）；修复 5000 cycle 测试超时（120s→9s），1981 tests passed → [详细记录](2026-04-01.md)
- **Thread 模块单元测试** — 为 src/thread/ 编写 4 个测试类 26 个用例（ThreadContext 模型、ThreadContextManager CRUD+TTL 淘汰、thread-local 隔离、单例），26 passed → [详细记录](2026-04-01.md)
- **R4-Fix4 + R4-Fix5: Thread 编程会话恢复与淘汰清理** — handle_card_resume 恢复会话后 re-register ThreadContext；ThreadContextManager 添加 on_evict 回调，淘汰/移除时自动清理 ACP Session（遍历 6 个 manager）；附带修复 test_handlers settings mock，2006 tests passed → [详细记录](2026-04-01.md)

## 2026-03-31
- **Deep Engine ProgressReporter 单元测试** — 为 reporter.py 编写 10 个测试类 45 个用例，覆盖全部公开方法，45 passed → [详细记录](2026-03-31.md)
- **rloop Round 2 审查修复** — shutdown/cleanup 线程安全、convergence backlog 误报修复、compact NO_TOOLS 防护，1869 tests passed → [详细记录](2026-03-31.md)
- **三项独立模块改进（4.6/4.10/4.11）** — SpecEngine BUILD 验证钩子(verify_command+_verify_build_result)、Context Compression Framework(compact.py)、Hook System Framework(hooks.py)，20 tests passed → [详细记录](2026-03-31.md)
- **基于 Claude Code 分析的高优先级优化（5项）** — 重试系统升级(max_delay+jitter+prompt_with_retry)、重试与熔断器联动、统一异常体系(is_ghostap_error)、覆盖率工具(pytest-cov)、共享测试Fixture(FakeSessionBase)，1780 tests passed → [详细记录](2026-03-31.md)
- **Coverage 门控 + CleanupRegistry 工具** — pyproject.toml 添加 fail_under=60、新建 src/utils/cleanup.py 异步清理注册工具 + 4 个测试用例，4 passed → [详细记录](2026-03-31.md)
- **CircuitBreaker 增强（4项功能扩展）** — async_call 异步调用、reset 强制重置、on_state_change 状态变更回调、滑动窗口失败追踪(deque+window_duration)，67 tests passed → [详细记录](2026-03-31.md)
- **normalize_startup_diagnostics 管线化重构** — 234行长函数拆分为7个辅助函数(_resolve_diag_config/_init_diag_container/_normalize_fields/_apply_fallbacks/_apply_redaction/_apply_truncation/_final_guard)，主函数变为清晰管线调用，35 tests passed → [详细记录](2026-03-31.md)
- **Spec Engine convergence.py 增强（4项功能扩展）** — compute_cycle_metrics 权重参数化、detect_convergence 容差参数、detect_backlog_stuck 新函数、should_stop backlog_stuck 参数，26 new tests + 5 existing passed → [详细记录](2026-03-31.md)
- **Graceful Shutdown 模块** — src/utils/shutdown.py（graceful_shutdown/install_signal_handlers/is_shutting_down），参考 Claude Code gracefulShutdown.ts 模式，幂等+超时安全，4 tests passed → [详细记录](2026-03-31.md)
- **深度代码审计与优化 Round 2** — 11 处静默异常→debug 日志 + 111 新测试 + 6 模块 __all__ + 修复双层缓存去重 bug → [详细记录](2026-03-31.md)

## 2026-03-30
- **日志 WARNING 修复与优化** - 修复 3 类 WARNING：throttling 回退降级为 DEBUG、ProbeStrategy 超时降级为 INFO、emoji_type 修正（Rocket→Fire, Skull→SKULL, OneSec→OneSecond 对照飞书官方列表） → [详细记录](2026-03-30.md)
- **Spec/Loop 引擎独立卡片交互模式** - 每轮 cycle/iteration 完成时发独立消息卡片，增强内容展示（各 phase 产出摘要、角色/审查/标准进度） → [详细记录](2026-03-30.md)
- **TTADK 模型选择跳过与双重鉴权修复** - 非YOLO模式强制显示模型选择卡片、auto_update改异步daemon thread、sandbox鉴权目录符号链接保留OAuth token → [详细记录](2026-03-30.md)
- **TTADK 交互优化：YOLO 语义重定义 + 鉴权修复增强** - YOLO改为"自动执行"语义、移除选择fallback、sandbox符号链接覆盖旧目录 → [详细记录](2026-03-30.md)

## 2026-03-29
- **【完成】引擎层架构规范化 Spec 全量实施** - 5 Phase 14 Tasks：共享模型迁移(engine_base.py)、卡片命名统一(EngineCardState)、引擎接口统一(inject_guidance/on_analyzing_*)、spec_engine/engine.py 拆分(3183→1190行，10+子模块)、rloop 审查通过。1759 tests passed → [详细记录](2026-03-29.md)
- **Spec Engine 内联薄包装方法** - 移除 engine.py 中 15 个仅委托到模块级函数的薄包装方法，在调用点直接内联模块级函数调用，engine.py 1238→1190 行，1759 tests passed → [详细记录](2026-03-29.md)
- **Spec Engine 拆分第六部分：提取 criteria 评估逻辑到 criteria.py** - 从 engine.py 提取 _decompose_criteria_with_llm/_evaluate_criteria 到 criteria.py（2函数），engine.py 1387→1238 行，1759 tests passed → [详细记录](2026-03-29.md)
- **Spec Engine 拆分第五部分：提取状态持久化方法到 persistence.py** - 从 engine.py 提取 _project_to_compact_dict/save_state/load_state 到 persistence.py（3函数），engine.py 从 1387→1238 行，persistence.py 从 285→380 行，1038 tests passed → [详细记录](2026-03-29.md)
- **Spec Engine 拆分第四部分：提取核心 review 编排逻辑到 review.py** - 从 engine.py 提取 _conduct_review/_parse_review_output/_parse_review_with_llm 到 review.py（3函数+ReviewCircuitState），engine.py 从 1754→1387 行，review.py 从 363→620 行，1759 tests passed → [详细记录](2026-03-29.md)
- **Spec Engine 拆分第三部分：提取 persistence.py、discovery.py、session_utils.py** - 从 engine.py 提取持久化逻辑(12函数)到 persistence.py、Discovery/Spec 生成(5函数)到 discovery.py、Session 工具(6函数)到 session_utils.py，engine.py 从 2204→1754 行，1759 tests passed → [详细记录](2026-03-29.md)
- **Spec Engine 拆分第二部分：提取 review.py 和 convergence.py** - 从 engine.py 提取 review 诊断/解析逻辑到 review.py（6函数/常量）、收敛检测到 convergence.py（ContinuationPolicy+2函数），engine.py 减少~512行，1759 tests passed → [详细记录](2026-03-29.md)
- **Spec Engine 拆分第一部分：提取 prompts.py 和 artifacts.py** - 从 engine.py 提取 7 个 prompt 构建函数到 prompts.py、9 个 artifact 解析函数到 artifacts.py，更新调用点和 32 处测试引用，1759 tests passed → [详细记录](2026-03-29.md)
- **Phase 3 重构：引擎接口统一（Task 4-7）** - inject_context→inject_guidance、on_planning_*→on_analyzing_*、cleanup()统一到BaseEngine、LoopEngine retry改用send_prompt_with_retry，全量保留向后兼容，1759 tests passed → [详细记录](2026-03-29.md)
- **Spec 模式卡片信息完整性优化** - 实现 phase 级进度指示器(on_phase_start/on_phase_done)、移除 content 过度折叠、增强 cycle_done review 建议展示、抽取辅助方法去重、+8新测试，rloop 3轮审查 → [详细记录](2026-03-29.md)
- **Phase 1 重构：共享模型迁移到 engine_base.py** - 将 EngineRunState/ReviewPerspective/PerspectiveReview/ReviewResult 从具体引擎迁移到 engine_base.py，消除跨引擎不合理依赖，保留向后兼容 re-export，1759 tests passed → [详细记录](2026-03-29.md)
- **Phase 2 重构：卡片层命名统一（Task 3）** - DeepCardState→EngineCardState、deep_project_id→engine_project_id、build_deep_card→build_engine_card，三重向后兼容（别名+property），29文件批量重命名，1759 tests passed → [详细记录](2026-03-29.md)

## 2026-03-28
- **项目全局优化（rloop）** - 死代码清理(-120行)、Renderer去重(-65行)、TTADK精简(-200行)、warning banner逻辑bug修复、多角色3轮审查，总减~586行 → [详细记录](2026-03-28.md)
- **Spec 指令机制优化** - 系统指令门控精细化、引擎控制动作优先级提升、resume BUG 修复、代码质量清理 → [详细记录](2026-03-28.md)

## 2026-03-27
- **TTADK 模式增强** - 自动更新 + 工具/模型选择流程优化 + 会话保活 keepalive → [详细记录](2026-03-27.md)
- **ACPSessionManager Keepalive 后台线程** - 添加 keepalive 守护线程定期检测空闲会话存活状态并自动清理 dead session，5 测试全通过 → [2026-03-27.md](2026-03-27.md)
- **TTADK 自动更新功能** - 新增 `auto_update_ttadk()` 模块级函数，进程生命周期内仅执行一次 `ttadk update`，在 `handle_ttadk_command` 入口处调用，+5 测试全通过 → [2026-03-27.md](2026-03-27.md)
- **移除 TTADK 工具/模型选择 fast-path** - 移除 `handle_ttadk_command` 中两个跳过选择的快速路径，用户重新进入 TTADK 时始终可选工具/模型 → [2026-03-27.md](2026-03-27.md)
- **更新 Git 忽略规则（忽略 .aiden）** - 在 `.gitignore` 增加 `.aiden/`，避免本地 Aiden 目录被纳入版本控制 → [2026-03-27.md](2026-03-27.md)

## 2026-03-26
- **修复卡片流式输出速度缓慢/卡顿问题** - 异步化飞书 PATCH 更新以避免阻塞底层流读取 → [2026-03-26.md](2026-03-26.md)
- **Spec 流式卡片 PATCH 兼容 + 审查超时配置** - PATCH 载荷改为 schema 2.0 + legacy-safe elements，新增 loop_review_timeout 并接入 review 调用，补充断连/停止日志与 streaming 测试更新（36/110/187 passed）→ [2026-03-26.md](2026-03-26.md)

## 2026-03-25
- **TTADK CLI 模式 prompt 传递修复** - `SyncTTADKCLISession.send_prompt()` 将 prompt 作为位置参数传给 `ttadk code` 导致 "too many arguments" 错误，改为通过 `-a` passthrough 传递（coco/claude/gemini 使用 `-p` print 模式，codex 等使用位置参数），新增 `_build_ttadk_passthrough_prompt` + 扩展 preamble 过滤 + debug 日志 + 15 个新测试，1697 passed → [2026-03-25.md](2026-03-25.md)
- **TTADK 卡片 banner 过滤 + 标题增强** - ASCII art banner 第 3 行含单引号未被过滤（正则补 `'"`）；卡片标题增加 TTADK 代理工具名和模型名显示（`🎮 项目 · TTADK · claude(glm-5)`），流式卡片和非流式卡片均支持，10 个新测试，1679 passed → [2026-03-25.md](2026-03-25.md)
- **TTADK 手机端最小交互 + YOLO 模式** - TTADK 项目新增 yolo 开关与状态展示，工具/模型自动选择与静默切换、菜单强制选择入口、ttadk_flow_duration_ms 耗时统计，补充卡片/路由/项目/流程测试，314 passed → [2026-03-25.md](2026-03-25.md)
- **TTADK 进入失败兜底与错误提示优化** - TTADK 入口/工具/模型失败改为温和提示与重试引导，TTADK 启动超时/异常改为警告提示，卡片动作异常对 TTADK 走柔性提示；补充入口失败回归测试，170 passed → [2026-03-25.md](2026-03-25.md)
- **TTADK 软失败卡片与恢复入口** - 新增 TTADK 软失败卡片（含“重新进入TTADK”按钮），统一 System/Programming/WS 的软失败提示为卡片；补齐 card/ws_client/handler 回归测试，279 passed → [2026-03-25.md](2026-03-25.md)
- **TTADK 软失败提示统一服务** - 提供统一 soft-failure 文案模板与卡片入口（build_ttadk_soft_failure_card_for），替换分散调用点并修复 model 失败分支 project_id；补齐入口/模型/卡片异常软失败测试，279 passed → [2026-03-25.md](2026-03-25.md)
- **TTADK 入口成功判定测试** - 新增 TTADK 进入成功 UI 要素测试（状态条与入口按钮），验证卡片关键元素 → [2026-03-25.md](2026-03-25.md)

## 2026-03-24
- **全局优化精简（Phase 1+2）** - 修复 `_send_text_reply` 运行时 bug、删除死代码（scripts/archive/ 13 文件 + sys_monitor.py + 重复定义 + 23 个 camelCase 别名）、提取 BaseEngine/BaseEngineManager 基类消除三引擎重复、TTADK 去重、ACP Provider 表驱动合并、SpecHandler 继承 BaseEngineHandler；净减 532 行 5 个文件，1687 tests passed → [2026-03-24.md](2026-03-24.md)
- **ACP Provider 表驱动合并** - 5 个独立 provider 文件合并为 `providers/__init__.py` 表驱动系统（`_ProviderConfig` + `GenericACPProvider`），新增 provider 只需添加一项配置，1447 tests passed → [2026-03-24.md](2026-03-24.md)
- **SpecHandler 继承 BaseEngineHandler 重构** - SpecHandler 从 BaseHandler 改为继承 BaseEngineHandler，实现 5 个抽象方法，pause/stop 复用 generic 后追加 save_state，resume 保留磁盘恢复+多状态特化逻辑，1447 tests passed → [2026-03-24.md](2026-03-24.md)
- **提取 BaseEngine / BaseEngineManager 基类** - 从 Deep/Loop/Spec 三引擎提取共同模式到 `src/engine_base.py`，含 __init__/properties/stop/cleanup/save_state/get_rendered_content 及泛型 Manager 基类 → [2026-03-24.md](2026-03-24.md)
- **LoopEngine/LoopEngineManager 继承 BaseEngine/BaseEngineManager 重构** - LoopEngine 继承 BaseEngine 消除重复属性/方法，LoopEngineManager 继承 BaseEngineManager 仅保留工厂方法，115 tests passed → [2026-03-24.md](2026-03-24.md)
- **DeepEngine/DeepEngineManager 继承 BaseEngine/BaseEngineManager 重构** - DeepEngine 继承 BaseEngine 消除重复属性/方法/`_context_lock`→`_lock`，DeepEngineManager 继承 BaseEngineManager 仅保留工厂/remove/find_by_deep_project_id，142 tests passed → [2026-03-24.md](2026-03-24.md)
- **SpecEngine/SpecEngineManager 继承 BaseEngine/BaseEngineManager 重构** - SpecEngine 继承 BaseEngine 消除重复属性/方法/`_state_lock`→`_lock`，SpecEngineManager 继承 BaseEngineManager 保留工厂/resolve_engine_identity/get_or_create/load_or_create_from_disk，166 tests passed → [2026-03-24.md](2026-03-24.md)
- **崩溃/卡住风险修复（会话恢复一致性 + 引擎清理竞态）** - 修复 resume 先切模式后建会话不一致、Deep/Loop/Spec cleanup_all 运行中引擎引用丢失、close 链路会话清理覆盖不足，新增回归并全量验证通过（`1681 passed, 10 skipped`）→ [2026-03-24.md](2026-03-24.md)
- **TTADK manager.py / command_exec.py 代码去重** - 消除 7 处重复定义，command_exec.py 为 SSOT，manager.py 通过导入+委托消除重复代码约 150 行，183 tests passed → [2026-03-24.md](2026-03-24.md)

## 2026-03-23
- **全量治理续做计划落地（A/B/C/D）** - 完成编程模式互斥全量收口、ModeManager 统一编程入口、`AgentSessionManager` 语义别名导出与文档一致性修正，新增/更新回归测试并通过全量验证（`1677 passed, 10 skipped`）→ [2026-03-23.md](2026-03-23.md)
- **TTADK ACP 输出噪声过滤与 JSON 提取修复** - 修复 `ttadk_wrapper` 仅按首行 `{` 切换透传导致的混杂输出污染：改为逐行提取所有 JSON object/array 并持续过滤噪声，补充 noisy line / post-start noise 回归，完成全量验证（`1662 passed, 10 skipped`）→ [2026-03-23.md](2026-03-23.md)
- **架构深度审计（四层策略 + ACP/CLI 传输矩阵）** - 逐模块核对普通/deep/loop/spec 四层策略与 ACP/CLI/TTADK 桥接实现一致性，确认 `ttadk_*` 强制 CLI 隔离、并修正文档中 Claude/TTADK 传输描述偏差 → [2026-03-23.md](2026-03-23.md)

## 2026-03-22
- **`/acp` 无响应根因修复** - 修正 ACP 工具发现与真实 CLI 协议漂移：coco 探测超时放宽、Aiden 改为 `aiden acp`、Gemini 改为 `gemini --acp`、热工具负缓存支持同步复探，恢复 `/acp` 交互入口并完成全量验证（`1660 passed, 10 skipped`）→ [2026-03-22.md](2026-03-22.md)
- **飞书 WS 长连接静默失活根因修复** - 追到 `lark_oapi.ws.Client` 仅处理显式断连、未处理 half-open/stale socket，导致进程存活但不再收消息；在 `ws_client` 增加连接活动观测与 watchdog 主动断连重连，完成全量验证（`1655 passed, 10 skipped`）→ [2026-03-22.md](2026-03-22.md)
- **飞书卡片 schema 2.0 根级 `elements` 发送失败修复** - 在 `BaseHandler` 发送层统一规范化 interactive card，移除 schema 2.0 非法根级 `elements`，修复 `ErrCode: 200621`，并完成全量验证（`1653 passed, 10 skipped`）→ [2026-03-22.md](2026-03-22.md)
- **Gemini CLI ACP 接入收口与全量验证** - 补齐 Gemini 的意图识别、ws_client 自动进入/空文本/编程态路由、`/gemini_info` 系统命令与流式卡片测试适配，完成全量 `uv run pytest -x -q` 验证（`1652 passed, 10 skipped`）→ [2026-03-22.md](2026-03-22.md)
- **ACP 统一入口交互实现（/acp 工具/模型选择）** - 新增 `/acp` 两段式交互（选工具→选模型）、基于 ACP `new_session.available_models` 的实时模型拉取、快捷菜单入口与动作路由，并将选择结果持久化到项目快照 → [2026-03-22.md](2026-03-22.md)

## 2026-03-21
- **全量测试收敛补丁（1647 passed）** - 清理四批次后暴露的剩余失败（deep/loop/spec/card/unified_context），全量 `uv run pytest -x -q` 达成 1647 passed → [2026-03-21.md](2026-03-21.md)
- **全项目四批次收敛修复（批次1~4）** - 完成调度器状态收敛、项目串行一致性、TTADK cwd 归一化根因修复与四批次合并回归（646 passed）→ [2026-03-21.md](2026-03-21.md)
- **全项目实现深度审计（整体潜在问题）** - 跨 ACP/会话管理、引擎/调度/持久化、Feishu 交互/卡片三层识别潜在风险，重点定位 scheduler stop 竞态、多模式路由缺口、上下文污染与 TTADK 会话一致性问题 → [2026-03-21.md](2026-03-21.md)
- **Spec 恢复链路与卡片操作根因修复** - 为失败任务/磁盘状态引入统一 runtime_context 恢复语义，修正 TTADK 恢复身份、成功后删除快照时机、暂停态继续按钮与错误态重试卡片 → [2026-03-21.md](2026-03-21.md)
- **Spec 模式深度稳定性与卡片操作完整性审计** - 全链路复核启动/恢复/停止与卡片动作闭环，定位恢复快照删除时机、TTADK 恢复路由、暂停态按钮缺失等残留风险 → [2026-03-21.md](2026-03-21.md)
- **工作区改动提交并推送** - 执行 `git status` 核查后按规则进行提交/推送准备，记录测试现状与推送校验步骤 → [2026-03-21.md](2026-03-21.md)

## 2026-03-20
- **Spec 停机并发导致模型失败与崩溃修复** - 修复停机阶段 Spec 仍触发模型切换与并发 cleanup 导致 `NoneType.cycles` 崩溃；补充 Spec/ws_client 回归测试并完成定向验证 → [2026-03-20.md](2026-03-20.md)

## 2026-03-19
- **重构 ACP Provider 协议与 TTADK 会话隔离** - 构建统一的 ACP 协议提供者抽象层并强化 TTADK 桥接模式的会话路由拦截规则 → [2026-03-19_acp_provider.md](2026-03-19_acp_provider.md)
- **工作区改动提交并推送** - 按规则执行 `git add/commit/push`，并补充 Memory 记录与推送后状态校验 → [2026-03-19.md](2026-03-19.md)
- **Spec 触发顺序与模型初始化策略修复** - 将 `/spec*` 与 `/coco|/claude|/ttadk` 初始化命令串行化；coco 模型切换改为 ACP-first 动态列表校验；Spec 增加 `send_prompt_with_retry` 缺失时的 `send_prompt` 兼容回退，并补齐定向回归测试 → [2026-03-19.md](2026-03-19.md)
- **低风险死代码清理** - 清理 TTADK/ACP 中已确认无引用的私有 helper 与无效局部状态，保持兼容签名与现有功能不变，并完成定向回归验证 → [2026-03-19.md](2026-03-19.md)
- **内存监控稳定性加固与临时文件清理** - `gc_monitor` 在 `psutil` 缺失时改为优雅降级，补充回归测试，并清理未引用的根目录临时脚本/日志文件 → [2026-03-19.md](2026-03-19.md)

## 2026-03-18
- **修复 TTADK 模式路由切换失败** - `ws_client` 显式判断 `ttadk` auto_enter_mode，修正 `is_in_programming` 条件（避免写死枚举），补充路由分发与上下文映射逻辑，完善测试用例 → [2026-03-18.md](2026-03-18.md)
- **统一多引擎重试机制架构 (Deep/Loop/Spec)** - 将重试逻辑抽象为全局模块，`SyncSession` 新增 `send_prompt_with_retry` 接口并加入重试前的 `before_retry` 清理钩子，实现跨引擎底层超时与连接异常恢复 → [2026-03-18.md](2026-03-18.md)
- **Spec 模式崩溃防御与失败任务持久化强化** - 回调安全封装 + 失败任务兜底保存 + 任务持久化 fallback 目录 + spec 文件落盘 best-effort + 测试适配 → [2026-03-18.md](2026-03-18.md)

## 2026-03-17
- **修复 Spec Engine 交互异常与 KeyError** - 修复在异步刷新卡片由于字典键错误引发整个执行循环失败的 bug，以及补充遗漏的 action 转发映射 `_toggle_spec_ac`，全量测试通过 → [2026-03-17.md](2026-03-17.md)
- **TTADK 状态面板体验优化** - 实现富交互卡片 `build_ttadk_info_card`，根据 Product 审查移除冗余提示与手动刷新按钮，实现模型获取失败时的优雅降级 (Graceful Degradation)，+4测试全通过 → [2026-03-17.md](2026-03-17.md)

## 2026-03-15
- **修复三模式单元测试适配 TTADK 启动逻辑** - 修复 Deep/Loop/Spec 测试 mock 逻辑，解决 Spec 循环策略冲突，全量测试通过 → [2026-03-15.md](2026-03-15.md)
- **SpecEngine 循环策略与收敛修复** - 增加 `spec_min_cycles` 配置，修复 MagicMock 配置读取漂移与 `spec_convergence_window=1` 误触发收敛 → [2026-03-15.md](2026-03-15.md)
- **Spec 模式 PRODUCT 审查提示增强** - PRODUCT 视角加入 Apple 风格高标准审查准则（默认体验/一致性/体面失败）→ [2026-03-15.md](2026-03-15.md)
- **Spec 模式修复与验证** - 修复 SpecReporter 参数错误并补全缺失方法，验证三模式正常工作 → [2026-03-15.md](2026-03-15.md)

## 2026-03-09
- **TTADK 模型列表获取问题诊断** - 诊断发现 ttadk 0.3.8 无 models 子命令，coco/trae/cursor 工具 Available models 为空，ProbeStrategy 部分失败，待确定解决方案 → [2026-03-09.md](2026-03-09.md)

## 2026-03-06
- **TTADK 模型列表误识别本地文件修复** - 修复模型提取过宽问题：仅在模型语义字段中提取，避免将 `image.png` 等目录文件当作模型；新增来源日志与2个回归测试，TTADK测试17通过 → [2026-03-06.md](2026-03-06.md)
- **TTADK 工具模型列表动态获取实现** - 新建 model_fetcher.py 使用 pty 模拟终端交互获取模型列表，TTADKModel 添加 friendly_name 字段，Manager 集成 Fetcher + 工具级缓存，+7测试，15测试全通过 → [2026-03-06.md](2026-03-06.md)
- **TTADK 帮助文档完善与命令实现** - 更新 show_full_help() 添加 TTADK 内容，实现 /ttadk_info、/ttadk_tool、/ttadk_model 命令，更新 exit_current_mode() 支持 TTADK 模式退出，1120测试全通过 → [2026-03-06.md](2026-03-06.md)
- **TTADK 帮助文档完善与命令实现** - 更新 show_full_help() 添加 TTADK 内容，实现 /ttadk_info、/ttadk_tool、/ttadk_model 命令，更新 exit_current_mode() 支持 TTADK 模式退出，1120测试全通过 → [2026-03-06.md](2026-03-06.md)
- **TTADK 模式 Deep/Loop/Spec 引擎兼容性完善** - 更新三个引擎的 __init__ 方法添加 model_name 参数，在 get_or_create() 中添加 TTADK 模式支持，更新所有 create_engine_session() 调用传递 model_name，1120测试全通过 → [2026-03-06.md](2026-03-06.md)
- **项目文档确认与兼容性验证** - 确认 README.md、帮助文档、配置文件都已更新，全面验证 TTADK 模式与现有功能的兼容性，1120测试全通过 → [2026-03-06.md](2026-03-06.md)

## 2026-03-05
- **TTADK 统一模式完整实现与测试验证** - 运行完整测试套件，修复 9 个测试失败（HandlerContext 缺少 ttadk_manager、unified_context 缺少 TTADK 条目、性能测试超时），更新 checklist.md 所有检查点为已完成，1120测试全通过 → [2026-03-05.md](2026-03-05.md)
- **TTADK 引擎支持完善** - 在 src/feishu/handlers/base.py 的 get_engine_name() 中添加 TTADK 支持，在 src/agent_session.py 的 create_sync_session() 和 create_engine_session() 中添加 ttadk_ 前缀支持，Deep/Loop/Spec 引擎兼容，115测试全通过 → [2026-03-05.md](2026-03-05.md)
- **TTADK 工具和模型选择卡片实现** - 在 src/card/builder.py 中实现 build_ttadk_tool_select_card() 和 build_ttadk_model_select_card() 方法，使用按钮组实现选择，支持所有 8 个 ttadk 工具，+5测试，105卡片测试全通过 → [2026-03-05.md](2026-03-05.md)
- **TTADK 配置管理模块实现** - 创建 src/ttadk/ 目录，实现 models.py（TTADKTool/TTADKModel/ToolListResult/ModelListResult）、manager.py（TTADKManager 管理工具和模型列表，支持 8 个预设工具和 8 个预设模型）、在 config.py 中添加 ttadk_default_tool/ttadk_default_model 配置项、__init__.py 导出模块，+6测试，1108测试全通过 → [2026-03-05.md](2026-03-05.md)
- **resolve_agent_spec 函数添加 ttadk 支持** - 在 src/acp/sync_adapter.py 中添加对 ttadk_ 前缀 agent_type 的支持，构建 ["ttadk", "code", "-t", tool_name, "-a", "acp serve"] 命令，支持可选 model_name 参数，+4测试，93测试全通过 → [2026-03-05.md](2026-03-05.md)
- **ProjectContext 中添加 TTADK 字段和方法** - 在 src/project/context.py 中添加 ttadk_mode 和 ttadk_session_snapshot 字段，添加 set_ttadk_mode() 和 update_ttadk_snapshot() 方法，在 to_snapshot() 和 from_snapshot() 中添加序列化和反序列化支持，保持与 coco/claude 一致的代码风格，41测试全通过 → [2026-03-05.md](2026-03-05.md)
- **TTADK 编程模式支持** - 在 ModeManager 中添加 TTADK 模式，包括 enter_ttadk_mode()、is_ttadk_mode()，更新 is_programming_mode() 和 get_mode_display_name()，保持与 COCO/CLAUDE 一致的代码风格，+2测试，24测试全通过 → [2026-03-05.md](2026-03-05.md)

## 2026-03-02
- **Coco 模型管理与 Spec 任务稳定性增强** - 新增 `/models`、`/model` 命令动态切换模型；Spec 任务失败自动重试+模型切换；`/spec_recover` 恢复中断任务；+51测试，963测试全通过 → [2026-03-02.md](2026-03-02.md)

## 2026-02-28
- **Spec/Loop 模式三项优化：审查解析 + 截断修复 + 卡片布局** - loose parsing 三策略兜底审查解析、移除 format_phase_done 500字截断、build_deep_card 结构化布局(status_line/duration_line/criteria_section/footer_note)、+21测试，1052测试全通过 → [2026-02-28.md](2026-02-28.md)

## 2026-02-27
- **Spec Engine 全自主决策** - 移除澄清问题打断机制，LLM 自主选择最优方案继续迭代，用户可随时 /spec_guide 注入信息，1031测试全通过 → [2026-02-27.md](2026-02-27.md)
- **Coco ACP Server 自动更新** - coco 不支持 ACP 时自动执行 `coco update` + 缓存清除 + 重检测，+12测试，1031测试全通过 → [2026-02-27.md](2026-02-27.md)
- **编程模式卡片空白修复 + 完成摘要优化** - handle_response 三级 fallback + close_streaming 空保护 + render_summary()，1019测试全通过 → [2026-02-27.md](2026-02-27.md)
- **Deep/Loop/Spec 引擎优化：限速自适应 + 统一状态 + 实时时长 + 架构去重 + 测试补全** - RateLimitAwareSession 自动重试、流式卡片实时时长、统一 /status、BaseHandler 共享回调工厂、+25工具函数测试，1011测试全通过 → [2026-02-27.md](2026-02-27.md)

## 2026-02-26
- **Spec Engine 迭代4：spec_review_enabled配置尊重** - `_run_cycle_loop`条件化review阶段，与loop模式行为对齐，+1测试，941测试全通过 → [2026-02-26.md](2026-02-26.md)
- **Spec Engine 迭代3：架构优化+测试补全** - execute/resume代码去重(_run_cycle_loop)、收敛检测增强(review趋势)、on_phase_start接线、+16集成测试，940测试全通过 → [2026-02-26.md](2026-02-26.md)
- **Spec Engine 全新实现** - 结构化开发模式(spec→plan→task→build→review)，7新文件+8修改文件+97测试，924测试全通过 → [2026-02-26.md](2026-02-26.md)
- **Deep 模式执行过程消息移除截断** - 移除4处应用层截断(1400/2000/2000/3000)，完整展示执行输出，827测试全通过 → [2026-02-26.md](2026-02-26.md)
- **冗余清理+系统命令阻塞修复** - 删除~280行死代码(DeepTask等6类)+系统命令路由扩展+提取2个共享函数，827测试全通过 → [2026-02-26.md](2026-02-26.md)
- **项目文档全面更新** - README.md/AGENTS.md/Loop架构文档重写，反映ACP重构后的完整架构和功能 → [2026-02-26.md](2026-02-26.md)
- **Deep 模式 TodoWrite 卡片内容丢失修复** - `_parse_tool_call` 提取 raw_input + renderer 新增 `_todo_content` 独立区块，827测试全通过 → [2026-02-26.md](2026-02-26.md)
- **Loop 多视角审查输出解析三级容错** - 正则增强(5EN模式+增强ZH) + LLM兜底解析 + 诊断日志，814测试全通过 → [2026-02-26.md](2026-02-26.md)

## 2026-02-24
- **Loop 模式验收标准截断修复** - 口语化输入用 LLM 拆解为结构化验收标准，移除 100 字符截断，800测试全通过 → [2026-02-24.md](2026-02-24.md)
- **Deep 模式卡片显示空白工具条目修复** - 空 title 工具不渲染 + `render_plan_view()` 分离计划视图避免内容膨胀，797测试全通过 → [2026-02-24.md](2026-02-24.md)
- **Deep/Loop 模式 Claude 引擎不生效修复** - `get_engine_name()` 未传 `project_id` 导致项目级 Claude 模式被忽略，始终回退到 Coco，793测试全通过 → [2026-02-24.md](2026-02-24.md)

## 2026-02-12
- **ACP 流式缓冲区溢出修复** - Deep 模式长时间执行 "chunk is longer than limit" 崩溃，asyncio StreamReader 64KB 上限→10MB，792测试全通过 → [2026-02-12.md](2026-02-12.md)
- **Shell 命令结果卡片渲染优化** - Shell 结果从纯文本改为 interactive 卡片（schema 2.0），新增 `build_shell_result_card`，792测试全通过 → [2026-02-12.md](2026-02-12.md)
- **Shell 命令执行无限递归修复** - submit_shell_command 的 _run 回调 message_callback 形成无限循环，改为直接调用 SandboxExecutor.execute()，792测试全通过 → [2026-02-12.md](2026-02-12.md)

## 2026-02-11
- **Shell 命令卡死 + 会话上下文串台修复** - Shell 快速通道绕过项目队列阻塞 + ACPSessionManager 按 (chat_id, project_id) 隔离会话，792测试全通过 → [2026-02-11.md](2026-02-11.md)
- **Loop Engine 多视角审查系统（Ralph Loop）** - 每轮迭代后从架构师/产品/用户/测试四视角审查，审查建议驱动下一轮迭代，764测试全通过 → [2026-02-11.md](2026-02-11.md)
- **架构优化（14项）** - CLI流式输出+权限配置提取+shell路径统一+引擎会话去重+转发表setattr+snapshot统一+ref_note提取+终端TTL清理+杂项修复，718测试全通过 → [2026-02-11.md](2026-02-11.md)
- **架构审查修复（8项）** - Engine状态卡死修复、EngineManager线程安全、ACPSessionManager并发保护、resume会话泄漏修复、inject_context队列模式、超时cancel、on_event错误可见性、用户错误反馈，716测试全通过 → [2026-02-11.md](2026-02-11.md)
- **Loop Engine 卡片显示修复 + 迭代上限放开** - CriteriaTracker 初始化/更新修复、输出截断移除、迭代上限10→100、duration/focus 修复、卡片验收标准展示，716测试全通过 → [2026-02-11.md](2026-02-11.md)
- **ACP 实现缺陷修复（5项）** - inject_context 实装、resume 实装、引擎 retry 接入、auto_approve 配置化、进程崩溃 watchdog，716测试全通过 → [2026-02-11.md](2026-02-11.md)
- **性能优化审查（10项）** - O(n^2)字符串拼接→list+join、regex预编译、health check分层、持久化watchdog、on_event去重、StreamingCard自动清理、EngineManager二级索引，716测试全通过 → [2026-02-11.md](2026-02-11.md)

## 2026-02-10
- **ACP 协议重构实施** - subprocess CLI→ACP (JSON-RPC 2.0 over stdio)，新增 src/acp/ 7文件，重写 deep_engine(6→4文件)、loop_engine(7→4文件)，删除 src/session/，704测试全通过 → [2026-02-10.md](2026-02-10.md)
- **Bug修复：流式错误恢复元组解包崩溃** - ClaudeSession 4元组→3变量解包 + .env 配置字段名修正 → [2026-02-10.md](2026-02-10.md)
- **ACP 协议重构** - subprocess→ACP 结构化 agent 通信，8阶段重构 → [2026-02-10.md](2026-02-10.md)
- **Loop Engine 移植到 multicoco** - ACP→subprocess 改造，4新文件+6修改+61测试 → [2026-02-10.md](2026-02-10.md)
- **Loop Engine 架构补全** - 角色系统+终止判定+需求解析+标准回写+上下文集成，3新文件+1重写+3修改，842测试全通过 → [2026-02-10.md](2026-02-10.md)
- **Loop Engine 测试补全** - 新增104个测试(84→188)，覆盖8个测试类：边界条件+集成测试+线程安全+优先级验证，946测试全通过 → [2026-02-10.md](2026-02-10.md)
- **Loop Engine 代码规范整理** - ruff lint 14处修复 + format 9文件 + __init__.py 导出精简，946测试全通过 → [2026-02-10.md](2026-02-10.md)

## 2026-02-09
- **Loop Mode 集成** - Loop Engine 端到端接入主消息流 + 帮助文档 + CLAUDE.md → [2026-02-09.md](2026-02-09.md)
- **Loop Engine 测试覆盖** - 5 个核心模块 142 个新测试（1408→1550） → [2026-02-09.md](2026-02-09.md)

## 2026-02-02
- **表情类型无效 & Deep 卡片重复修复** - EmojiType 替换 + build_deep_card 去重 → [2026-02-02.md](2026-02-02.md)
- **项目结构精简** - themes.py 合并 + 会话模块统一目录 → [2026-02-02.md](2026-02-02.md)
- **项目级任务隔离与系统命令快速通道** - TaskSpec 增强 + ModeManager 项目级模式 → [2026-02-02.md](2026-02-02.md)
- **项目持久化原子写与损坏恢复** - 跨进程文件锁 + 原子写入 + 损坏备份 → [2026-02-02.md](2026-02-02.md)
- **ws_client.py God Class 拆分** - 3444→1170 行，6 个 Handler 架构 → [2026-02-02.md](2026-02-02.md)
- **Deep Engine 实时上下文调整** - ExecutionContext + adapt_task_prompt + /deep_update → [2026-02-02.md](2026-02-02.md)
- **流式卡片更新修复 + Claude 会话闲置优化** - Patch API 替换 + 闲置检测 → [2026-02-02.md](2026-02-02.md)
- **编程模式命令拦截 + 即时反馈** - 系统命令拦截 + 卡片渲染优化 → [2026-02-02.md](2026-02-02.md)

## 2026-02-01
- **项目大扫除 6 阶段重构** - Session 基类 + 卡片 schema 2.0 + 配置 + 日志 → [2026-02-01.md](2026-02-01.md)
- **统一编程模式回复为 CardKit 流式卡片** - 消除两套卡片渲染实现 → [2026-02-01.md](2026-02-01.md)
- **高优先级代码质量修复** - FILE_CHANGE 死代码 + max_entries=0 边界 + 日志 → [2026-02-01.md](2026-02-01.md)
- **卡片 Markdown 渲染测试用例** - 56 个新测试覆盖三维度 → [2026-02-01.md](2026-02-01.md)
- **项目级统一上下文管理系统** - UnifiedContext + 跨模式桥接 → [2026-02-01.md](2026-02-01.md)
- **项目切换上下文保留与恢复** - preserve/restore + bridge inject → [2026-02-01.md](2026-02-01.md)
- **任务调度器 + Deep Engine 多后端 + 卡片 UI** - TaskScheduler + Claude 后端 → [2026-02-01.md](2026-02-01.md)

## 2026-01-29
- **Claude 编程模式全面修复** - UUID Session ID + 卡片按钮 + 项目管理兼容 → [2026-01-29.md](2026-01-29.md)
- **Claude 编程模式初始实现** - 会话管理 + 模式扩展 + 意图识别 → [2026-01-29.md](2026-01-29.md)
- **Deep Engine 模块** - 复杂任务编排引擎（parser/planner/executor/engine/reporter） → [2026-01-29.md](2026-01-29.md)
- **Deep 命令 Coco 模式拦截修复** - /deep 命令在 Coco 模式下被错误转发 → [2026-01-29.md](2026-01-29.md)
- **代码重构与优化** - emoji 提取 + MessageCache 独立模块 → [2026-01-29.md](2026-01-29.md)

## 2026-01-22~23
- **流式卡片输出 + Card JSON 2.0 + 按钮布局优化** - 多轮修复与适配 → [2026-01-22.md](2026-01-22.md)
- **代码清理** - 移除未使用模块/依赖/配置 → [2026-01-22.md](2026-01-22.md)
- **卡片回调 200671/200340 修复** - SDK bug + monkey patch → [2026-01-22.md](2026-01-22.md)

## 2026-01-18
- **多项目并行开发架构** - ProjectManager + context + mapper + card → [2026-01-18.md](2026-01-18.md)
- **安全工具链** - SafeShellTool + FileEditorTool + ToolManager → [2026-01-18.md](2026-01-18.md)
- **三种模式重构** - 智能/编程/Shell 模式 + 回复自动进入 → [2026-01-18.md](2026-01-18.md)
- **意图识别整合** - 项目管理意图 + 自然语言支持 → [2026-01-18.md](2026-01-18.md)

## 2026-01-09
- **项目创建** - 核心功能完成 + 飞书 WebSocket + Coco 会话 + ReAct 意图识别 → [2026-01-09.md](2026-01-09.md)
