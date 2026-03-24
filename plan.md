# GhostAP 全量治理续做计划（未完成事项清单）

更新时间：2026-03-23 21:00:45

## 1. 当前状态（已完成）

以下改动已落地，并已通过一轮核心回归：

- 新增统一引擎身份解析：
  - `src/utils/engine_identity.py`
  - `src/utils/__init__.py` 已导出 `EngineIdentity` / `resolve_engine_identity`
- `BaseHandler.get_engine_name()` 已改为统一解析：
  - `src/feishu/handlers/base.py`
- Deep/Loop/Spec 的 manager 级引擎身份推导已统一接入：
  - `src/deep_engine/engine.py`
  - `src/loop_engine/engine.py`
  - `src/spec_engine/engine.py`
- 已补充基础测试：
  - `tests/test_handlers.py` 新增 Aiden/Codex 的 engine name 断言
- 已跑通过测试（本地）：
  - `uv run python -m pytest tests/test_handlers.py tests/test_deep_engine.py tests/test_loop_engine.py tests/test_spec_engine.py -q`
  - 结果：`430 passed`

## 2. 关键未完成事项（必须继续实现）

### A. 编程模式互斥规则收口（高优先级）

目标：所有编程模式（Coco/Claude/Aiden/Codex/Gemini/TTADK）互斥逻辑完全一致，避免模式残留和项目快照串扰。

当前进度：
- `ProgrammingModeHandler` 已新增 `_deactivate_other_project_modes()` 并接入部分路径：
  - `enter_mode()` 的恢复路径和新建路径
  - `handle_card_resume()` 的 project 路径

剩余工作：
1. 补全各 `*ModeHandler` 的 `_is_in_opposite_mode()` 覆盖范围，统一检查全部其他编程模式。
2. 补全各 `*ModeHandler` 的 `_exit_opposite_mode()` 调用链，确保能正确退出其余模式 handler。
3. 清理 TTADK 子类里“只清 coco/claude”的硬编码，改由基类统一互斥逻辑驱动。
4. 对 card 入口/恢复/新会话路径做一致性校验，确保不会出现“mode_manager 已切换但 project flags 未同步”。

建议修改文件：
- `src/feishu/handlers/programming.py`
- `src/feishu/ws_client.py`（只在 cross-reference wiring 缺失时补齐，不做行为改写）

测试补充：
- `tests/test_handlers.py`
  - Coco/Claude/TTADK 的 opposite-mode 识别覆盖
  - 进入模式后 project flags 互斥断言
  - card resume/new 路径下 flags 同步断言

完成判据：
- 上述测试新增并通过；
- 任意编程模式进入后，`ProjectContext` 中仅目标模式为 `True`。

### B. ModeManager 统一入口（架构演进项）

目标：引入“统一进入编程模式”入口，减少 handler 侧分叉，保留原有 API 完整兼容。

剩余工作：
1. 在 `ModeManager` 增加统一入口（例如 `enter_programming_mode(...)`），内部处理目标模式设置与 future-proof 互斥策略。
2. 现有 `enter_coco_mode / enter_claude_mode / ...` 作为兼容包装调用统一入口。
3. 保持 `get_mode` / `is_*_mode` / `is_programming_mode` 对外语义不变。

建议修改文件：
- `src/mode/manager.py`
- `tests/test_mode_manager.py`

测试补充：
- 新增统一入口测试（chat 级 + project 级）
- 回归原入口 API 测试，保证兼容

完成判据：
- 原测试通过且新增入口测试通过；
- 不出现旧 API 行为回归。

### C. 会话管理语义别名（架构演进项）

目标：保留 `ACPSessionManager` 兼容名，同时提供语义准确的别名（例如 `AgentSessionManager`），明确其是 ACP+CLI 路由器。

剩余工作：
1. 在 `src/acp/manager.py` 添加兼容别名类或导出别名。
2. 在 `src/acp/__init__.py` 导出别名，保持旧导入路径可用。
3. 文档与注释统一语义（不改现有调用点也可通过）。

建议修改文件：
- `src/acp/manager.py`
- `src/acp/__init__.py`
- `tests` 中若有类型名断言需补充兼容断言

完成判据：
- 旧代码不改也可运行；
- 新别名可被 import 且行为等价。

### D. 文档一致性收口（剩余）

目标：把本轮“策略层 × 传输层”与实际实现完全对齐，避免文档继续漂移。

剩余工作：
1. `README.md`：修正普通模式与三引擎支持矩阵、Claude/TTADK transport 表述。
2. `docs/acp_provider_guide.md`：补充 `ttadk_*` 强制 CLI 的约束点与示例。
3. `AGENTS.md`：检查并补齐与最终代码一致性（已做部分更新，需最终对齐）。

完成判据：
- 文档中不存在“Claude 一律 ACP 直连”“TTADK 可直连 ACP”等不准确描述。

## 3. 建议执行顺序（直接按此做）

1. 先完成 `programming.py` 互斥收口（A）。
2. 运行定向测试：`tests/test_handlers.py tests/test_mode_manager.py`。
3. 再完成 `mode/manager.py` 统一入口（B）。
4. 运行模式相关全套：`tests/test_handlers.py tests/test_mode_manager.py tests/test_ws_client_patch.py`。
5. 完成会话别名与导出（C）。
6. 完成文档收口（D）。
7. 跑全量：`uv run pytest -x -q`。

## 4. 最终验收清单

必须全部满足：

- 功能一致性：
  - 普通模式 + Deep/Loop/Spec 的引擎身份映射一致
  - 编程模式互斥逻辑一致
  - TTADK 仍保持 CLI-only 约束
- 兼容性：
  - 现有命令语义不变
  - `ACPSessionManager` 现有导入与调用不破坏
- 测试：
  - 定向新增测试通过
  - `uv run pytest -x -q` 全量通过

## 5. 风险与回滚点

高风险点：
- `programming.py` 的互斥链路改动可能导致某些 handler 被重复退出或漏退出。
- ModeManager 入口重构可能影响 project 级模式优先级。

回滚策略：
- 每个子任务独立提交；
- 一旦出现行为漂移，优先回滚对应子任务提交，不回滚已验证通过的引擎身份解析改动。
