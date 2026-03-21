# GhostAP 项目记忆索引

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
