# Card Migration — 可执行任务分解

## Phase 1: 类型安全与契约修复

1. [src/card/state/models.py — TerminalStatus Literal 添加 'archived'，TerminalReason Literal 添加 'archived'] (依赖: 无)
2. [src/card/hooks.py — 审查 EmojiHook.on_terminal 确认 'archived' 分支已 exhaustive 覆盖，无遗漏] (依赖: 1)
3. [src/card/state/reducers/criteria.py — 入口添加 engine_ext is None 防御（logger.warning + return state）] (依赖: 无)
4. [src/card/state/reducers/phase.py — 入口添加 engine_ext is None 防御（logger.warning + return state）] (依赖: 无)
5. [src/card/state/reducers/cycle.py — 入口添加 engine_ext is None 防御（logger.warning + return state）] (依赖: 无)
6. [src/card/render/throttle.py — StreamThrottle.__init__ 签名从 settings: Any 改为 min_interval: float, min_chars: int 显式参数] (依赖: 无)
7. [src/feishu/renderers/spec_renderer.py — StreamThrottle 实例化改为传入 settings.deep_stream_interval 和 settings.deep_stream_min_chars 两个标量] (依赖: 6)
8. [运行 uv run pytest tests/ -x -q 验证 Phase 1 无回归] (依赖: 1, 2, 3, 4, 5, 6, 7)

## Phase 2: 模块提取与职责瘦身

9. [创建 src/card/delivery/ttl_set.py — 将 _TTLSet 类从 delivery/engine.py 提取为公开的 TTLSet，保持同签名同行为] (依赖: 8)
10. [修改 src/card/delivery/engine.py — 删除内联 _TTLSet，改为 from .ttl_set import TTLSet 并别名为 _TTLSet 保持内部兼容] (依赖: 9)
11. [src/card/render/worktree.py — 提取 _render_merge_notes(merge_notes, base_branch, header_key) 共享 helper] (依赖: 8)
12. [src/card/render/worktree.py — _render_worktree_merge 和 _render_worktree_cleanup 改为调用 _render_merge_notes + 各自尾部逻辑] (依赖: 11)
13. [src/card/hooks.py — 新增 HookFirer 类，封装 fire_dispatched(hooks, event, state) 和 fire_terminal(hooks, session_id, state, reason) 方法] (依赖: 8)
14. [新建 src/card/delivery_orchestrator.py — DeliveryOrchestrator 类封装 deliver_and_track / schedule_terminal_retry / pending_action_to_event 逻辑] (依赖: 8)
15. [src/card/session.py — CardSession 改为组合 HookFirer + DeliveryOrchestrator，删除已提取的内联方法，目标 ≤500 行] (依赖: 13, 14)
16. [运行 uv run pytest tests/ -x -q 验证 Phase 2 无回归] (依赖: 9, 10, 11, 12, 15)

## Phase 3: UX 文案修正

17. [src/card/ui_text.py — card_session_fallback_cmd 从 '命令' 改为 '对应命令'] (依赖: 16)
18. [src/card/ui_text.py — card_session_recovery_banner / card_session_max_failures_banner / card_session_warning_render_fail 中 '卡片' 替换为 '进度显示'/'状态更新'] (依赖: 16)
19. [src/card/ui_text.py — card_session_terminal_retry_failed 改为 '✅ 任务已完成。如进度未刷新，可重新发送 {engine_cmd} 查看'] (依赖: 16)
20. [src/card/ui_text.py — card_session_toast_ttl_closed 和 ttl_force_close_notice 统一为 {engine_cmd} 动态模式] (依赖: 16)
21. [更新 session.py / terminal.py 中 toast_ttl_closed 和 ttl_force_close_notice 的 format() 调用点，确保传入 engine_cmd 参数] (依赖: 20)
22. [更新测试文件中所有引用旧文案的断言字符串，匹配新文案] (依赖: 17, 18, 19, 20, 21)
23. [运行 uv run pytest tests/ -x -q 验证 Phase 3 无回归] (依赖: 22)

## Phase 4: 按钮对齐与 DirectCardSession 降级

24. [src/card/render/buttons.py — 单按钮场景从 action+flow 改为 column_set 包裹 + 居中 column（与双按钮结构一致）] (依赖: 23)
25. [src/card/direct_session.py — logger.warning 降为 logger.debug，保留 DeprecationWarning 仅 dev/test 触发] (依赖: 23)
26. [确认 grep -r 'DirectCardSession' src/feishu/renderers/ 返回空结果，记录 diagnostics.py 为唯一剩余调用者] (依赖: 25)
27. [运行 uv run pytest tests/ -x -q 验证 Phase 4 无回归] (依赖: 24, 25, 26)

## Phase 5: 测试覆盖补全

28. [tests/test_card_render_components.py — 第 483 行 TestBannerUnifiedPosition 重命名为 TestBannerMultiPagePosition] (依赖: 27)
29. [新建 tests/test_loop_adapter_sequence.py — 覆盖 loop engine started→text→iteration_done→rotate→completed 序列、error 路径、dispatch-after-close] (依赖: 27)
30. [tests/test_card_render_components.py 或 tests/test_reducers.py — 添加 reduce_criteria 和 reduce_phase 直接单元测试（criteria_total=0/1/多个、expanded 标志、PHASE_STARTED/DONE/orphan）] (依赖: 27)
31. [tests/test_card_session.py — 新增 test_ttl_close_without_dispatch_skips_hooks：构造 session 不 dispatch，触发 TTL expired，断言 hooks 未调用 + debug 日志] (依赖: 27)
32. [新建 tests/test_ttl_set.py — TTLSet 独立单测（add/contains/expire/max_size 边界/eviction_batch/过期淘汰）] (依赖: 9, 10, 27)
33. [tests/test_card_render_components.py — 添加单按钮居中渲染断言（输出包含 column_set + flex_mode 或等效居中属性）] (依赖: 24, 27)
34. [运行 uv run pytest tests/ -v 全量测试确认零 failure] (依赖: 28, 29, 30, 31, 32, 33)

## Phase 6: 最终验证与清理

35. [执行验收检查：grep 确认 renderers/ 无 DirectCardSession / _create_direct_session / CardBuilder.build_engine_card 引用] (依赖: 34)
36. [wc -l src/card/session.py 确认 ≤500 行] (依赖: 34)
37. [代码审查确认四引擎 adapter 映射完整性（deep/loop/spec/worktree 各有 CardEvent dispatch 路径）] (依赖: 34)
38. [手动或集成测试验证 Spec 卡片展示（progress_bar/criteria_section/phase/retry 按钮正常）] (依赖: 34)
39. [手动或集成测试验证 Worktree 交互式卡片流程（tool_select → confirm → execute 正常）] (依赖: 34)
40. [更新 .Memory/ 记录本次迁移完成内容] (依赖: 35, 36, 37, 38, 39)
