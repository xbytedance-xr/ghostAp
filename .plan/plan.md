# 群隔离锁机制 — 质量加固与体验增强 实施计划 v2

> 基于 Spec v1.0 审查反馈，共 20 个 AC，分 6 个 Phase 按依赖顺序执行。
> 每个 Step 完成后运行 `uv run pytest tests/ -x -q` 验证无回归。

---

## Phase 1: Protocol 类型安全与解耦 (F01, F02, F03, F18)

### Step 1 — RetryDispatchProtocol 返回类型替换 Any → 具体类型
- **文件**: `src/feishu/retry_handler.py:30-52`
- **变更**:
  - 在 `TYPE_CHECKING` 守卫下导入 `ProjectContext`、`RepoLockManager`、`BaseHandler`
  - `get_project_for_chat` → `Optional["ProjectContext"]`
  - `get_active_project` → `Optional[Any]`（保留 Any，因实际返回值多态）
  - `get_repo_lock_manager` → `Optional["RepoLockManager"]`
  - `get_base_handler` → `Optional["BaseHandler"]`
- **验证**: `grep 'Any' src/feishu/retry_handler.py` 仅剩 `get_active_project` 和 `process_with_intent` 的 project 参数
- **AC**: AC-R01

### Step 2 — Protocol 新增 send_lock_conflict_card 方法，消除私有方法耦合
- **文件**:
  - `src/feishu/retry_handler.py` — Protocol 新增方法；删除 `_get_base_handler`；重写 `_send_probe_conflict_card` 和 `_handle_lock_conflict`
  - `src/feishu/action_registry.py` — `_RetryDispatchAdapter` 实现新方法
  - `src/feishu/handlers/lock_helper.py` — `_send_lock_conflict_card` 改名为 `send_lock_conflict_card`（去掉下划线，变为公开方法）
  - `src/feishu/handlers/base.py` — 更新转发方法名
- **变更详情**:
  1. Protocol 新增: `def send_lock_conflict_card(self, exc: Any, message_id: str, command_text: str, retry_count: int = 0) -> None: ...`
  2. `_RetryDispatchAdapter.send_lock_conflict_card()` 实现：内部获取 `_system_handler`，调用 `handler.lock_helper.send_lock_conflict_card()`，无 handler 时 fallback 到 `reply_message`
  3. `RetryCommandHandler` 中提取公共方法 `_send_conflict_card(exc, mid, cmd, retry_count)` 直接调用 `self._dispatch.send_lock_conflict_card()`
  4. `_send_probe_conflict_card` 缩减为：构造 `LockConflictError` → 调用 `_send_conflict_card`（≤5 行 body）
  5. `_handle_lock_conflict` 缩减为：直接调用 `_send_conflict_card`（≤3 行 body）
  6. 删除 `_get_base_handler` 方法和 Protocol 中的 `get_base_handler`
- **验证**: `grep -n 'handler\._' src/feishu/retry_handler.py` 返回 0 结果；`grep -c 'get_base_handler' src/feishu/retry_handler.py` == 0
- **AC**: AC-R02, AC-R03

### Step 3 — action_registry.py PEP 8 双空行 + _resolve_project 辅助函数
- **文件**: `src/feishu/action_registry.py`
- **变更**:
  1. `_RetryDispatchAdapter` 末尾与 `init_action_registry` 之间改为 2 行空行
  2. 在 `init_action_registry` 内部（闭包）定义 `_resolve_project(pid, cid)` 辅助函数替代 13 处重复的 `client._project_manager.get_project_for_chat(pid, cid) if pid else None`
- **验证**: `grep -c 'get_project_for_chat' src/feishu/action_registry.py` == 1（仅在 Adapter 类的方法定义中）
- **AC**: AC-R04, AC-R18

### Step 4 — 更新测试 mock 使用 spec=RetryDispatchProtocol
- **文件**: `tests/test_retry_handler.py`, `tests/test_action_retry.py`
- **变更**:
  1. `test_retry_handler.py:_make_handler()` 中 `dispatch = MagicMock()` → `dispatch = MagicMock(spec=RetryDispatchProtocol)`
  2. 适配新 Protocol（移除 `get_base_handler` 相关 mock，新增 `send_lock_conflict_card` mock）
  3. `test_action_retry.py` 中相关 mock 同步更新
- **验证**: 全量测试通过
- **AC**: AC-R19

---

## Phase 2: 静默失败修复与文案改善 (F05, F06, F12, F15)

### Step 5 — retry_handler 空命令回复用户
- **文件**: `src/feishu/retry_handler.py:72-74`
- **变更**: `if not cmd: return` → `if not cmd: self._dispatch.reply_message(mid, UI_TEXT["retry_empty_command"]); return`
- **新增测试**: `tests/test_retry_handler.py` 新增测试用例 `test_empty_cmd_replies_error`
- **AC**: AC-R05

### Step 6 — ADMIN_USER_IDS 为空时文案改为引导联系部署者
- **文件**:
  - `src/card/styles_lock.py:75` — 修改 `chat_lock_no_admin_config_user` 文案为 "群锁定功能暂未开放，请联系服务部署者在后台配置 Bot 管理员"
  - `src/feishu/handlers/system.py:_reply_lock_permission_error` — 确认 `NO_ADMIN_CONFIG_USER` code 的展示路径使用更新后的文案
- **新增测试**: `tests/test_chat_lock.py` 新增 `test_lock_no_admin_config_message` 验证文案不含模糊的"管理员"指向
- **AC**: AC-R06

### Step 7 — 签名过期文案增加因果说明
- **文件**: `src/card/styles_lock.py:106`
- **变更**: `retry_command_sig_mismatch` 从 `"⚠️ 此重试按钮已过期，请手动重新输入命令"` 改为 `"⚠️ 此重试按钮已过期（可能因安全配置更新），请手动重新输入命令"`
- **AC**: AC-R12

### Step 8 — /lock 在私聊中的解释性文案
- **文件**: `src/card/styles_lock.py:84`
- **变更**: `lock_cmd_p2p_only` 从 `"/lock 和 /unlock 仅适用于群聊，请在需要锁定的群聊中发送 /lock"` 改为 `"群锁仅适用于群聊。私聊中你可以直接操作，不受锁限制。"`
- **AC**: AC-R15

---

## Phase 3: UI_TEXT 字符串统一 (F07, F08, F09)

### Step 9 — format_lock_duration 硬编码中文迁移到 UI_TEXT
- **文件**:
  - `src/card/styles_lock.py` — 新增 4 个 key: `lock_held_seconds`、`lock_held_minutes`、`lock_held_hours_minutes`、`lock_held_hours`
  - `src/card/builders/lock.py:109-122` — 将 4 条 f-string 替换为 `UI_TEXT[key].format(...)` 调用
- **新增测试**: `tests/test_lock_cards.py` 新增 `test_format_lock_duration_uses_ui_text` 验证输出与 UI_TEXT 值一致
- **AC**: AC-R07

### Step 10 — build_chat_lock_card 命令列表动态生成
- **文件**: `src/card/builders/lock.py:246`
- **变更**: 
  ```python
  # Before
  cmd_list = "`/help` `/status`"
  # After
  from src.chat_lock import READONLY_COMMANDS, SAFE_INTERRUPT_COMMANDS
  _admin_only = {"/lock", "/unlock"}
  _dynamic_cmds = sorted((READONLY_COMMANDS | SAFE_INTERRUPT_COMMANDS) - _admin_only)
  cmd_list = " ".join(f"`{c}`" for c in _dynamic_cmds)
  ```
- **新增测试**: `tests/test_lock_cards.py` 新增 `test_chat_lock_card_dynamic_cmd_list` patch READONLY_COMMANDS 加入新命令后验证卡片自动包含
- **AC**: AC-R08

### Step 11 — locked_by_name 为空时 fallback 名称统一
- **文件**:
  - `src/card/builders/lock.py:230` — `"Bot 管理员"` → `UI_TEXT["ws_fallback_admin_name"]`
  - 确认 `src/feishu/handlers/lock_helper.py:271` 已使用 `UI_TEXT.get("ws_fallback_admin_name", "Bot 管理员")`，两处引用同一 key
- **AC**: AC-R09

---

## Phase 4: UX 交互增强 (F13, F14, F16, F17)

### Step 12 — LRU 驱逐通知改为带按钮卡片
- **文件**:
  - `src/card/builders/lock.py` — 新增 `build_eviction_notify_card(project_name, project_id)` 函数，返回 (markdown, buttons) 格式，按钮 action 为 `show_status`（触发 /project 效果）
  - `src/card/styles_lock.py` — 新增 `eviction_notify_title`、`eviction_notify_body`、`eviction_notify_btn_rebind` 三个 UI_TEXT key
  - `src/feishu/ws_client.py:852-866` — `_on_project_evicted` 从纯文本 `reply()` 改为卡片回复
- **新增测试**: `tests/test_project_context_lru.py` 新增 `test_eviction_sends_card_with_button` 验证回调使用卡片 API
- **AC**: AC-R13

### Step 13 — app_id 未配置时 WARNING 日志
- **文件**: `src/card/builders/lock.py:162-165` (build_repo_lock_card) 和 `src/card/builders/lock.py:284-290` (build_chat_lock_card)
- **变更**: `if not app_id:` 分支中增加 `logger.warning("app_id not configured, P2P deeplink button will not be rendered")` 日志（仅首次警告，使用模块级 flag 避免重复）
- **AC**: AC-R14

### Step 14 — 强制释放确认卡片传入 holder_hint
- **文件**: `src/feishu/handlers/system.py:handle_force_release_repo_lock`（约 line 535-537）
- **变更**: 从 `repo_lock_mgr.get_lock_info(root_path)` 获取锁信息，构建 `holder_hint` 字符串包含：持有群标识（chat_id 前缀）、锁定时长（via `format_elapsed_ago`）、最后活跃时间距今。传入 `build_force_release_confirm_card(repo_token, repo_name, holder_hint=holder_hint)`
- **新增 UI_TEXT key**: `lock_force_release_holder_hint` = `"持有者: {holder} | 锁定时长: {duration} | 最后活跃: {last_active}"`
- **AC**: AC-R16

### Step 15 — /status 中群锁和仓库锁之间增加视觉分隔
- **文件**: `src/feishu/handlers/diagnostics.py:163`
- **变更**: `"\n".join(parts)` → `"\n\n".join(parts)` （两个 parts 之间用双换行分隔，创建段落间距）
- **AC**: AC-R17

---

## Phase 5: 配置文档与注释 (F10, F11)

### Step 16 — .env.example APP_SECRET 注释交叉引用
- **文件**: `.env.example:3`
- **变更**: 在 APP_SECRET 注释行后追加一行: `# 轮换密钥时可配合 SIG_COMPAT_DEPLOY_DATE / SIG_COMPAT_WINDOW_DAYS 参数实现平滑迁移`
- **AC**: AC-R10

### Step 17 — .env.example MAX_ALLOWED_CHAT_IDS 注释补充用户影响
- **文件**: `.env.example:52`
- **变更**: 注释从 `# 每个项目最多关联的群聊数量（LRU 淘汰，默认 50）` 改为 `# 每个项目最多关联的群聊数量（LRU 淘汰，默认 50）。超出时最早的群将自动解绑并收到通知卡片`
- **AC**: AC-R11

---

## Phase 6: 全量回归验证

### Step 18 — 全量测试
- **命令**: `uv run python -m pytest tests/ -v`
- **AC**: AC-R20（原有 AC-01 至 AC-14 全部继续 PASS）
- **额外验证**:
  - `grep -rn 'handler\._' src/feishu/retry_handler.py` → 0 结果 (AC-R02)
  - `grep -c 'get_project_for_chat' src/feishu/action_registry.py` → 1 (AC-R04)
  - 检查 action_registry.py 类与函数间空行为 2 (AC-R18)

---

## 文件变更清单

### 修改文件（17 个）
| 文件 | 涉及 Step |
|------|-----------|
| `src/feishu/retry_handler.py` | 1, 2, 5 |
| `src/feishu/action_registry.py` | 2, 3 |
| `src/feishu/handlers/lock_helper.py` | 2 |
| `src/feishu/handlers/base.py` | 2 |
| `src/card/builders/lock.py` | 9, 10, 11, 12, 13 |
| `src/card/styles_lock.py` | 6, 7, 8, 9, 12, 14 |
| `src/card/styles.py` | 无直接改动（styles_lock 自动合并） |
| `src/feishu/handlers/system.py` | 14 |
| `src/feishu/handlers/diagnostics.py` | 15 |
| `src/feishu/ws_client.py` | 12 |
| `src/chat_lock.py` | 无代码改动（文案通过 UI_TEXT 间接更新） |
| `.env.example` | 16, 17 |
| `tests/test_retry_handler.py` | 4, 5 |
| `tests/test_action_retry.py` | 4 |
| `tests/test_lock_cards.py` | 9, 10 |
| `tests/test_project_context_lru.py` | 12 |
| `tests/test_diagnostics_isolation.py` | 15 |

### 无新增文件
所有变更在现有文件内完成。
