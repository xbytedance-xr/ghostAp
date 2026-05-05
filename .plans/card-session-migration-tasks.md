# Card Session 迁移收尾 — 任务分解

## Phase-A: 常量提取 + DRY 修复

1. 创建 `src/card/session/_constants.py`，定义 `TTL_ENGINE_KEY_MAP` 常量（包含 /spec、/deep、/wt、/worktree、/loop 到对应 ui_text key 的映射） (依赖: 无)

2. 修改 `src/card/session/ttl.py` 的 `on_ttl_expired` 方法——删除内联 `_TTL_KEY_MAP` 局部变量，改为 `from ._constants import TTL_ENGINE_KEY_MAP` (依赖: 1)

3. 修改 `src/card/session/_ttl_mixin.py` 的 `force_terminate` 方法——删除内联 `_TTL_KEY_MAP` 局部变量，改为 `from ._constants import TTL_ENGINE_KEY_MAP` (依赖: 1)

4. 运行 `uv run pytest -x -q` 确认 Phase-A 无回归 (依赖: 2, 3)

## Phase-B: Protocol 整合 + 依赖方向修正

5. 将 `CardAPIClient` Protocol 从 `src/card/delivery/engine.py:28-59` 剪切至 `src/card/protocols.py`，`engine.py` 改为 `from src.card.protocols import CardAPIClient` (依赖: 4)

6. 移除 `src/card/protocols.py` 中 `from src.card.render.worktree import WorktreeCallbacks` 的 re-export 行及 `__all__` 中的 `WorktreeCallbacks` 条目 (依赖: 5)

7. 全项目 grep 找到所有 `from src.card.protocols import WorktreeCallbacks` 的消费方，逐一改为 `from src.card.render.worktree import WorktreeCallbacks` (依赖: 6)

8. 修改 `src/feishu/renderers/base.py:266` 的 import 路径，从 `from ...card.session_rotator import SessionRotator` 改为 `from ...card.session.rotator import SessionRotator` (依赖: 4)

9. 运行 `uv run pytest -x -q` 确认 Phase-B 无回归 (依赖: 5, 6, 7, 8)

## Phase-C: CardSession 职责收窄 + CardDelivery 接口清理

10. `CardDelivery._release_session_lock` 重命名为 `release_session_lock`（公共方法），更新 docstring (依赖: 9)

11. 修改 `src/card/session/core.py` 模块级 `_release_lock` 函数，将 `delivery._release_session_lock(session_id)` 调用改为 `delivery.release_session_lock(session_id)` (依赖: 10)

12. `CardDelivery.__init__` 新增可选参数 `registry: DeliveryRegistry | None = None`，注册逻辑改为 `(registry or delivery_registry).register(self)` (依赖: 10)

13. 在 `src/card/protocols.py` 中添加 `TTLManager` Protocol（含 `on_ttl_expired` 方法签名）和 `ActionDispatcher` Protocol（含 `route_closed`、`resolve` 方法签名） (依赖: 9)

14. 在 `src/card/session/factory.py` 中提取 `_build_ttl_stack()` 辅助函数——封装 TTLContext/TTLActuator/TTLHandler 的构建逻辑 (依赖: 13)

15. 在 `src/card/session/factory.py` 中提取 `_build_collaborators()` 辅助函数——封装 DispatchDeliveryCoordinator/SessionTimerManager/DeliveryTracker/ActionRouter 的构建逻辑 (依赖: 13)

16. 重构 `CardSession.__init__`——改为接收已构建好的协作者对象（由 factory 注入），确保方法体 ≤80 行、顶层 internal import ≤12 个 (依赖: 14, 15)

17. 在 `src/config.py` Settings 的 `model_validator` 中增加 `lock_undo_window_seconds >= lock_confirm_timeout` 交叉校验，违反时 `logger.warning` 并继续启动 (依赖: 9)

18. 运行 `uv run pytest -x -q` 确认 Phase-C 无回归 (依赖: 11, 12, 16, 17)

## Phase-D: UX 文案修复

19. 修改 `.env.example` 中 `CARD_SESSION_IDLE_WARN_AT_REMAINING` 注释默认值从 300 改为 420 (依赖: 18)

20. 修改 `src/card/buttons_config.py`——`enter_*` 按钮文案统一为「{emoji} 进入 {Name} 模式」，`exit_*` 统一为「🚪 退出 {Name} 模式」 (依赖: 18)

21. 修改 `src/card/ui_text.py` 的 `card_session_ttl_prewarning_notify`——改为同时提及「点击续期按钮」和「发送任意消息」两种续期方式，并前缀 `{engine_name}` (依赖: 18)

22. 修改 `src/card/ui_text.py` 的 `card_session_ttl_prewarning` banner 文案——前缀加 `{engine_name}` (依赖: 18)

23. 修改 `src/card/ui_text.py` 的 `card_btn_confirm_stop_danger_body`——追加辅助决策文案「已保存的进度不会丢失，但当前步骤将中断。」 (依赖: 18)

24. 修改 `src/card/ui_text.py` 的 `system_help_tips` 第 5 条——改为「超过 {timeout_display} 无操作会话会自动关闭，关闭前会提前提醒你续期」 (依赖: 18)

25. 修改 `src/card/actions/router.py` 的 `route_closed` 方法——所有 terminal_reason 的 toast 文案末尾追加迁移引导「如需操作，请重新发送对应命令获取新卡片」 (依赖: 18)

26. 在 stop action reducer 中增加 STOPPING 过渡态逻辑——设置 `footer.status_text = "等待当前步骤完成…未响应可强制停止"`，使 stop→stop_danger 升级路径对用户可见 (依赖: 18)

27. 运行 `uv run pytest -x -q` 确认 Phase-D 无回归 (依赖: 19, 20, 21, 22, 23, 24, 25, 26)

## Phase-E: 测试修复 + 全量验证

28. 运行 `uv run pytest tests/ -v` 全量测试，收集并分类所有失败项（文案断言 vs import 错误 vs mock 失效） (依赖: 27)

29. 修复因文案变更导致的测试断言失败——更新 expected 字符串匹配新文案 (依赖: 28)

30. 修复因 Protocol 迁移/import 路径变更导致的测试 import 错误 (依赖: 28)

31. 修复因 CardSession.__init__ 重构导致的测试 mock 注入方式失效 (依赖: 28)

32. 新增 `tests/test_card_session_constants.py`——验证 `TTL_ENGINE_KEY_MAP` 包含所有预期键及对应 ui_text key 存在 (依赖: 29, 30, 31)

33. 更新 `tests/test_card_delivery_engine.py`——新增用例验证 `CardDelivery(registry=mock_registry)` 不触发全局 singleton 注册 (依赖: 29, 30, 31)

34. 运行 `uv run pytest tests/ -v` 全量通过确认 exit code 0 (依赖: 32, 33)

35. 执行 AC 验收检查脚本——逐条验证 AC-1 至 AC-17 全部 PASS（grep 命令 + 行数统计 + 文件存在性检查） (依赖: 34)
