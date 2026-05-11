# 多群隔离改造设计文档

> Date: 2025-04-25 (updated 2026-04-26)
> Branch: harness
> Status: Implemented

## 1. 问题陈述

当前 GhostAP 允许多个飞书群同时 @机器人。虽然消息路由、任务调度、ACP 会话已按 `chat_id` 隔离，但**底层操作的是同一个文件系统**。当两个群指向同一个仓库目录时：

- 两个 ACP 进程 / Engine 同时修改同一 git 仓库 → 代码丢失、index.lock 冲突
- ProjectContext 的 status/mode 标记被互相覆盖
- ModeManager project 级 mode 跨群冲突
- Shell 命令并发操作同一 cwd 无保护
- Worktree Engine 的 merge/checkout/reset 等重操作无互斥
- 服务重启后群的工作环境绑定丢失

## 2. 设计目标

1. **不同群的任务不阻塞** — 操作不同仓库时完全并行
2. **上下文不串** — 每个群的 mode、project 状态完全独立
3. **同一仓库不允许多群并发操作** — 活跃操作期间锁定，空闲自动释放
4. **私聊特权** — 单独与机器人私聊时无视锁，可操作任何仓库
5. **群绑定工作环境持久化** — 重启后自动恢复每个群的主项目

## 3. 整体架构

```
┌──────────────────────────────────────────────────────────────┐
│                    消息入口层                                  │
│  _handle_message / _handle_card_action                       │
│  ┌─────────────────────────────────────┐                     │
│  │ 提取 chat_type (group/p2p)          │                     │
│  │ 提取 chat_id, project_id, root_path │                     │
│  └──────────────┬──────────────────────┘                     │
│                 ▼                                            │
│  ┌─────────────────────────────────────┐                     │
│  │ RepoLockManager.check()             │                     │
│  │ - p2p → 跳过                        │                     │
│  │ - 同 chat_id → 允许（重入）          │                     │
│  │ - 其他 chat_id 持有 → 拒绝           │                     │
│  └──────────────┬──────────────────────┘                     │
│                 ▼                                            │
│  ┌─────────────────────────────────────┐                     │
│  │ TaskScheduler / Engine / Sandbox    │                     │
│  │ 正常执行                             │                     │
│  └─────────────────────────────────────┘                     │
└──────────────────────────────────────────────────────────────┘
```

## 4. 模块设计

### 4.1 RepoLockManager — 仓库操作锁

**新增文件**: `src/repo_lock.py`

```python
class RepoLockManager:
    """仓库级互斥锁管理器（单例，内存态）"""

    _locks: dict[str, RepoLockEntry]
    # key = normalized root_path
    # value = RepoLockEntry(chat_id, refcount, last_active_time)

    def acquire(root_path: str, chat_id: str, is_p2p: bool = False) -> AcquireResult:
        """尝试获取仓库锁
        - is_p2p=True → 始终成功（私聊特权）
        - 同 chat_id → refcount++，成功（可重入）
        - 其他 chat_id 持有 → 拒绝，返回持有者信息
        """

    def release(root_path: str, chat_id: str) -> None:
        """释放锁，refcount--，降为 0 时删除条目"""

    def force_release(root_path: str) -> None:
        """强制释放（私聊管理员命令用）"""

    def list_locks() -> list[RepoLockInfo]:
        """列出所有活跃锁（诊断用）"""
```

**路径归一化**: 统一使用 `os.path.realpath(os.path.expanduser(path))` 作为锁 key，展开所有软链接和 `~`，消除路径别名问题。提供 `normalize_repo_path()` 公共函数，后续所有需要比较路径的地方都调用它。

**超时自动释放**: 后台守护线程每 60s 扫描一次，清理 `last_active_time` 超过 `repo_lock_idle_timeout`（默认 300s）的条目。防止群 A 崩溃/断连后锁永不释放。

**内存态**: 不持久化，服务重启后所有锁自然清空（因为重启后所有子进程都已终止，不存在活跃操作）。

**线程安全**: 内部使用 `threading.Lock` 保护 `_locks` dict。

### 4.2 ChatWorkspace — 群绑定工作环境（持久化）

**修改文件**: `src/project/manager.py`

不新增独立文件，而是增强现有 ProjectManager：

1. **`_active_project` 已经是 `dict[chat_id, project_id]`**，已在 `projects.json` 中持久化
2. **当前问题**: `_load_projects()` 恢复了 `_active_project` 映射，但服务启动时**不会主动恢复群的工作环境状态**
3. **改造**: 在 `set_active_project()` 调用 `_save_projects()` 时自动持久化（当前已实现），确保 `_load_projects()` 在启动时正确恢复所有群绑定

需要验证/修复的点：
- `_load_projects()` 是否正确恢复 `_active_project`
- 恢复后 `ProjectContext.status` 是否正确设为 ACTIVE
- 多个群绑定同一 project 时的 status 语义

### 4.3 ModeManager 修复 — 消除跨群 mode 冲突

**修改文件**: `src/mode/manager.py`

当前 `_project_modes` 的 key 是纯 `project_id`，两个群对同一 project 设置不同 mode 会互相覆盖。

**改造**: 将 `_project_modes` 的 key 改为 `{chat_id}:{project_id}` 复合键。

```python
# Before
self._project_modes: dict[str, ModeState] = {}  # project_id → ModeState

# After
self._project_modes: dict[str, ModeState] = {}  # {chat_id}:{project_id} → ModeState
```

同步修改 `set_mode()` / `get_mode()` / `clear_mode()` / `clear_all_modes()` 中对 `_project_modes` 的访问。

### 4.4 chat_type 提取 — 私聊识别

**修改文件**: `src/feishu/ws_client.py`

当前 `_handle_message()` 不读取 `chat_type` 字段，无法区分群聊/私聊。

**改造**: 在 `_handle_message()` 入口提取 `chat_type`：

```python
chat_type = getattr(data.event.message, "chat_type", None)  # "group" | "p2p"
is_p2p = (chat_type == "p2p")
```

将 `is_p2p` 透传到 TaskSpec / HandlerContext，下游通过它决定是否跳过 RepoLockManager。

同样在 `_handle_card_action()` 路径中提取 `chat_type`（卡片回调消息中也应包含 chat 信息）。

### 4.5 ProjectContext mode 标记隔离

**修改文件**: `src/project/context.py`

当前 `ProjectContext` 上的 `coco_mode`、`claude_mode`、`ttadk_mode` 等布尔标记直接挂在共享对象上，两个群对同一 project 设置不同模式会互相覆盖。

**改造方案**: 将 mode 标记从 `ProjectContext` 上移除，统一由 `ModeManager`（已按 `{chat_id}:{project_id}` 隔离）管理。`ProjectContext` 不再持有 per-chat 的 mode 状态。

## 5. 锁注入点

### 5.1 Engine 系列（Deep/Spec/Worktree）

**修改文件**: `src/engine_base.py` 或各 engine 的 execute 方法

在 `BaseEngine` 层注入，所有 engine 自动继承：

```python
# engine_base.py — BaseEngine
def _execute_with_lock(self, execute_fn, *args, **kwargs):
    repo_lock = get_repo_lock_manager()
    if not repo_lock.acquire(self.root_path, self.chat_id, is_p2p=self._is_p2p):
        raise RepoLockError(f"仓库 {self.root_path} 正在被其他会话使用")
    try:
        return execute_fn(*args, **kwargs)
    finally:
        repo_lock.release(self.root_path, self.chat_id)
```

### 5.2 ACP Session

**修改文件**: `src/acp/manager.py`

在 `ensure_session()` / `start_session()` 时 acquire，在 `end_session()` / session 超时清理时 release。

### 5.3 SandboxExecutor

**修改文件**: `src/sandbox/executor.py`

在 `execute()` 方法入口，根据 `cwd` 参数检查锁：

```python
def execute(self, command, cwd=None, chat_id=None, is_p2p=False, ...):
    if cwd and chat_id:
        repo_lock = get_repo_lock_manager()
        result = repo_lock.acquire(cwd, chat_id, is_p2p=is_p2p)
        if not result.success:
            return ExecuteResult(error=f"仓库被其他会话占用: {result.holder_info}")
        try:
            # ... 原执行逻辑
        finally:
            repo_lock.release(cwd, chat_id)
```

需要在调用链中透传 `chat_id` 和 `is_p2p` 到 SandboxExecutor。

### 5.4 Worktree Engine

**修改文件**: `src/worktree_engine/manager.py`

WorktreeManager 的 `ensure_worktrees()`、`merge_to_base()`、`cleanup_worktrees()` 等方法操作 git 仓库，需要在执行前 acquire 锁。

### 5.5 Card Action 路径

**修改文件**: `src/feishu/ws_client.py`

在 `_process_card_action_async()` 中，对会触发仓库写操作的 action（如 `worktree_merge`、`worktree_cleanup`）增加 repo 锁检查。`stop` 类动作（`deep_stop`、`spec_stop`）属于安全中断操作，允许绕过锁。

## 6. 配置项

在 `src/config.py` 的 `Settings` 中新增：

```python
# RepoLockManager
repo_lock_idle_timeout: int = 300        # 锁空闲超时（秒），超时自动释放
repo_lock_cleanup_interval: int = 60     # 清理线程扫描间隔（秒）
```

## 7. 用户交互

### 被锁拒绝时的提示

当群 B 尝试操作群 A 正在使用的仓库时，返回卡片提示：

```
🔒 仓库锁定中

该仓库 `/home/user/repo1` 正在被其他会话使用。
请等待对方操作完成后重试。

持有者: 群 [chat_id_prefix...]
锁定时间: 3 分钟前
```

### 诊断命令

可选：新增 `/locks` 命令（仅私聊可用），列出所有活跃锁状态。

## 8. 影响范围

### 新增文件
- `src/repo_lock.py` — RepoLockManager

### 修改文件
| 文件 | 改动 |
|------|------|
| `src/config.py` | 新增 repo_lock 配置项 |
| `src/mode/manager.py` | `_project_modes` key 改为 `{chat_id}:{project_id}` |
| `src/project/context.py` | 移除 per-chat mode 标记（coco_mode 等） |
| `src/project/manager.py` | 验证/修复 `_load_projects` 恢复逻辑 |
| `src/engine_base.py` | 注入 repo 锁 acquire/release |
| `src/acp/manager.py` | session 启动/结束时 acquire/release |
| `src/sandbox/executor.py` | execute 入口增加锁检查，透传 chat_id |
| `src/worktree_engine/manager.py` | git 操作前 acquire 锁 |
| `src/feishu/ws_client.py` | 提取 chat_type，透传 is_p2p |
| `src/feishu/handler_context.py` | HandlerContext 增加 is_p2p 字段 |
| `src/feishu/handlers/base.py` | 透传 chat_id/is_p2p 到 SandboxExecutor |
| `src/utils/path.py` | 新增 normalize_repo_path() |

### 测试文件
- `tests/test_repo_lock.py` — RepoLockManager 单元测试
- `tests/test_mode_manager_isolation.py` — ModeManager 隔离测试
- 现有测试中涉及 ModeManager、ProjectContext mode 标记的需要更新

## 9. 风险与缓解

| 风险 | 缓解措施 |
|------|---------|
| 锁超时导致活跃操作被中断 | idle_timeout 默认 300s，且基于 last_active_time（每次 acquire 刷新） |
| 路径归一化不统一导致锁失效 | 统一 `normalize_repo_path()` 使用 `os.path.realpath` |
| 透传 chat_id/is_p2p 改动面广 | 通过 HandlerContext 统一携带，减少参数传递链 |
| ProjectContext mode 标记移除的兼容性 | 全量搜索 `coco_mode` / `claude_mode` 等引用点，逐一迁移到 ModeManager |
| 卡片回调路径遗漏锁检查 | stop 类允许绕过，写操作类（merge/cleanup）必须检查 |

## 10. 不在本次范围

- Thread 上下文持久化（当前不持久化，重启后回退到 active_project）
- 用户级隔离（同一群内多个用户的隔离）
- 分布式锁（当前单进程部署，不需要）
- `/locks` 诊断命令（可后续迭代）

## 11. 质量改进 (2026-04-26)

### 架构层 (A)

| # | 改进项 | 文件 | 说明 |
|---|--------|------|------|
| A1 | ModeManager 内存泄漏修复 | `mode/manager.py` | 新增 `clear_modes_for_chat()` 方法，ProjectManager LRU 淘汰时联动清理 |
| A2 | TOCTOU 竞态修复 | `project/manager.py` | 将 `_is_visible` 检查移入 `_lock` 临界区 |
| A3 | LockHelper 解耦 | `handlers/lock_helper.py` | 引入 `LockHandlerProtocol` (typing.Protocol) 窄接口 |
| A4 | 优雅关停 | `ws_client.py` | lock_dedup 清理线程使用 `threading.Event.wait()` |
| A5 | 严格锁模式 | `sandbox/executor.py`, `config.py` | 新增 `sandbox_strict_lock_mode` 配置，启用时 executor 直接拒绝跨群执行 |
| A6 | 签名兼容起点 | `card/builders/lock.py`, `config.py` | `sig_compat_deploy_date` 空值自动回退到进程启动日期 |
| A7 | 配置校验器 | `config.py` | `chat_lock_max_duration`/`chat_lock_cleanup_interval` 必须 > 0 |

### UX 层 (B)

| # | 改进项 | 文件 | 说明 |
|---|--------|------|------|
| B1 | 术语统一 | `card/styles_lock.py` | "群管理员" → "Bot 管理员" (6 处用户可见文案) |
| B2 | 硬编码消除 | `feishu/retry_handler.py` | 3 处中文字符串迁移到 `UI_TEXT` 常量 |
| B3 | 冲突提示增强 | `card/builders/lock.py`, `styles_lock.py` | `repo_lock_hint_active` 增加 `{timeout_min}` |
| B4 | 配置脱敏 | `styles_lock.py` | `chat_lock_no_admin_config` 移除 .env 变量名泄露 |
| B5 | 时长中文化 | `card/builders/lock.py` | `format_lock_duration` 输出改为中文 (2h 5m → 2 小时 5 分钟) |
| B6 | 超时通知增强 | `card/builders/lock.py`, `styles_lock.py` | `lock_hard_timeout_reclaim_notify` 增加 `{max_hours}` |
| B7 | 签名豁免 | `feishu/retry_handler.py` | `SIGNATURE_EXEMPT_COMMANDS` 让 /status 按钮无需签名 |
| B8 | P2P 按钮修复 | `feishu/handlers/system.py` | 使用 `_build_p2p_multi_url` 统一构建深链接 |
| B9 | 自动解锁提示 | `card/builders/lock.py`, `styles_lock.py` | 群锁卡片显示自动解除倒计时 |
| B10 | 分隔符移除 | `styles_lock.py` | `lock_status_section_header` 移除多余的 `---` |
| B11 | 缓存优化 | `card/builders/system.py` | help card 缓存 key 移除 `chat_id` 避免缓存膨胀 |
| B12 | 命令长度提升 | `card/builders/lock.py` | `MAX_COMMAND_TEXT_LENGTH` 500 → 1000 |

### 配置变更

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `SANDBOX_STRICT_LOCK_MODE` | `false` | 启用后 SandboxExecutor 严格拒绝跨群执行 |
| `SIG_COMPAT_DEPLOY_DATE` | `""` | 空值自动使用进程启动日期 |
| `CHAT_LOCK_MAX_DURATION` | `86400` | 群锁最大持续秒数 (须 > 0) |
| `CHAT_LOCK_CLEANUP_INTERVAL` | `60` | 群锁清理扫描间隔秒数 (须 > 0) |
