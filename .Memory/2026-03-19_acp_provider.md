# 2026-03-19 项目记录

## 重构 ACP Provider 协议与 TTADK 会话隔离

### 任务描述
2026-03-19
构建统一的 ACP 协议提供者抽象层（Provider/Factory 机制），替代硬编码条件分支。核心包含 ToolRegistry 作为工具选型与路由接口，将 aiden、coco、claude、codex 等封装为独立的 ACPProvider，实现调用逻辑与工具类型解耦。对于 TTADK 桥接模式，在会话路由层引入强约束规则，无论具体工具是否原生支持 ACP，均强制调度至纯 CLI 会话执行器。利用 LRU 缓存及异步预热机制，优化核心工具启动。

### 执行内容
1. 在 `src/acp/provider.py` 中定义了 `ACPProvider` 协议和 `ToolRegistry`。
2. 创建了 `src/acp/providers` 模块架构并导出了 `tool_registry`。
3. 实现了 `CocoProvider` (`src/acp/providers/coco.py`) 并接管了配置/探测。
4. 实现了 `ClaudeProvider` (`src/acp/providers/claude.py`) 并接管了配置/探测。
5. 实现了 `AidenProvider` (`src/acp/providers/aiden.py`)，支持特有探测与 ACP 命令。
6. 实现了 `CodexProvider` (`src/acp/providers/codex.py`)。
7. 在 `src/acp/sync_adapter.py` 的 `resolve_agent_spec` 中接入 `ToolRegistry` 并解耦逻辑。
8. 强化了 `src/acp/manager.py` 和 `src/agent_session.py` 中的 TTADK 模式路由隔离，确保 `ttadk_*` 前缀的请求不经过 ACP 侧漏。
9. 添加了全面的测试覆盖，包括工具提供者单测、TTADK 隔离拦截边界测试以及并发性能 benchmark（启动耗时验证）。

### 技术要点
- Provider 机制通过鸭子类型（`Protocol` + `@runtime_checkable`）降低了后续扩展成本。
- TTADK 的 CLI 隔离通过显式前缀校验（`startswith("ttadk_")`）直接进入 `AgentSession` （即 CLI Session），避免了错误的 `acp serve` 拉起。
- 引入了后台守护线程预热 (`preheat_async`) 和 LRU 缓存避免了 ACP 初始化的同步阻塞，保障响应时间。
- 保证测试代码隔离度与独立提供者 Mock 能力。

### 提交记录
- 未提交