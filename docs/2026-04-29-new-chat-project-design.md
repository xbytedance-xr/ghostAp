# /new-chat 项目-群绑定设计文档

> Date: 2026-04-29
> Branch: harness
> Status: Proposed

## 1. 问题陈述

当前 `/new <name> <path>` 在主对话所在 chat 创建项目并把该 chat 绑定为 owner。这种用法把"项目工作"和"主对话杂事"混在同一个 chat 里：

- 项目相关消息和系统对话、临时 shell 互相打断；
- 多项目并行时，主对话很快变成滚动条灾难；
- 没有原生的"项目专属上下文"——所有项目的卡片、流式输出、@bot 都堆在一个 chat。

期望：用一个新命令 `/new-chat`，建项目同时建一个**项目专属飞书群**，把发起人和 bot 拉进去；之后该项目的所有编程对话都在专属群里进行，与主对话和其它群完全隔离；多项目可同时并发。

## 2. 设计目标

1. **手机一键开项目**：默认场景下输入 `/new-chat` 即可，无需任何参数。
2. **项目↔目录↔群严格 1:1:1**：同一目录只允许一个活跃项目，活跃项目至多绑定一个专属群。
3. **解耦**：新功能集中在独立模块 `src/project_chat/`，对其它模块的侵入收敛到三处增量：`ProjectContext` 加字段、`intent_recognizer` 加分支、`dispatcher` 加 case。
4. **复用现有隔离**：消息路由、mode 状态、repo lock 等已按 `chat_id` / `root_path` 隔离，新群天然进入这套机制；零新增隔离代码。
5. **幂等**：对同一目录重复发 `/new-chat` 永远引导到现有群，不会失控建出多个群。
6. **失败可回滚**：建群-建项目-绑定三步任一失败都要把已建副作用清理干净。

## 3. 命令形态

```
/new-chat                         → name=basename(working_dir), path=working_dir, suffix=settings.project_chat_suffix
/new-chat <name>                  → 覆盖 name，path/suffix 默认
/new-chat <name> <suffix>         → 覆盖 name+suffix，path 默认
/new-chat <name> <suffix> <path>  → 全显式
```

群名格式：`{name}-{suffix}`。

新增配置项（`src/config.py` 现有 Settings）：

```python
project_chat_suffix: str = "dev"   # /new-chat 默认后缀
```

> Bot 自身的 open_id / app_id 从已有 settings（建群拉自身用）取，不新增配置。

## 4. 整体流程

```
用户在主对话发  "/new-chat" / "/new-chat name suffix path"
   │
   ▼
intent_recognizer  →  IntentType.NEW_CHAT_PROJECT, data={name?, suffix?, path?}
   │
   ▼
dispatcher  →  ProjectChatService.handle(message_id, chat_id, sender_open_id, data)
   │
   ▼
1. 解析默认值
   name   ← data.name   or basename(path)
   path   ← data.path   or working_dir(chat_id)
   suffix ← data.suffix or settings.project_chat_suffix
2. 进入 (chat_id, normalized_path) 维度的进程内 lock
3. ProjectManager.find_project_by_path(path)  →  ctx
       ├─ ctx 存在 + bound_chat_id 非空    →  分支 A:已绑定，回跳转卡，结束
       ├─ ctx 存在 + bound_chat_id 为空    →  分支 B:补群（legacy 项目）
       └─ ctx 不存在                        →  分支 C:全新建（先建群拿 chat_id，再建项目）
4. 建群（B/C 共用）
   lark_chat_client.create_chat(
       name=f"{name}-{suffix}",
       description=<§5 模板>,
       user_id_list=[sender_open_id],
       bot_id_list=[<bot self>],
   )  →  new_chat_id, new_chat_name
5. 写绑定
   分支 B：ctx.bound_chat_id = new_chat_id
           ctx.bound_chat_name = new_chat_name
           ctx.bound_chat_created_at = time.time()
           ctx.add_chat_id(new_chat_id)        # 加入 allowed_chat_ids
           ProjectManager._save_projects()
   分支 C：ProjectManager.create_project(
              project_id=None,
              project_name=name,
              root_path=path,
              chat_id=new_chat_id,             # ⚠ owner = 新群（不是主对话）
           ) → ctx_new
           ctx_new.bound_chat_id = new_chat_id
           ctx_new.bound_chat_name = new_chat_name
           ctx_new.bound_chat_created_at = time.time()
           ProjectManager._save_projects()
6. 主对话回 "项目就绪+跳转卡"；新群里 bot 发 welcome 卡
7. 释放进程内 lock

> **owner 语义说明**：与 `/new` 不同，`/new-chat` 的发起 chat（主对话）**不**成为项目 owner，也不进入 `allowed_chat_ids`。新群才是 owner。这是刻意设计：用户的诉求就是"把项目从主对话搬到专属群"，主对话事后无需再看到该项目（项目板列表里也不会出现，因为 chat-scoped 不可见）。
```

## 5. 群描述模板

建群时一次性写入 `description` 字段（约 500 字上限，超长截断仓库 URL）：

```
🎯 项目: {project_name}
📁 目录: {root_path}
🔗 仓库: {git_remote_url_or_empty}
🤖 在这个群直接对话即可：默认 Coco / 显式 /claude /codex 等。
```

第一版**不**使用群公告（announcement）：announcement 走 docx 增量更新协议，复杂度十倍于 description；description 已能在群资料卡可见，且首次建群就能一次性写入，足够本期需求。announcement 留作后续增强。

git remote 探测失败 → 该行留空，不阻塞建群。

## 6. 数据模型

### 6.1 ProjectContext 增量字段（`src/project/context.py`）

```python
@dataclass
class ProjectContext:
    # ...existing fields...
    bound_chat_id: str = ""              # 项目专属群 chat_id；空 = 没有专属群
    bound_chat_name: str = ""            # 缓存群名，仅用于卡片展示
    bound_chat_created_at: float = 0.0   # 建群时间戳
```

`to_snapshot` / `from_snapshot` 同步加这三字段。

> 不引入独立持久化表的原因：和 `owner_chat_id` / `allowed_chat_ids` 同源，复用 `ProjectManager._save_projects()` 的原子写入与文件锁。

### 6.2 配置项（`src/config.py`）

```python
project_chat_suffix: str = "dev"
```

### 6.3 不变更

- `ProjectManager` API 表面不动（`create_project` 现有 `chat_id` 参数即满足绑定语义）。
- `close_project` 不动——删除项目时 `bound_chat_id` 随项目一起消失，旧飞书群保留（见 §8）。
- `ProgrammingModeHandler` / `repo_lock` / `mode_manager` / `ChatLockGate` 零改动。

## 7. 模块设计

### 7.1 新增 `src/project_chat/`

```
src/project_chat/
  __init__.py            # public: ProjectChatService, NewChatRequest
  service.py             # ProjectChatService.handle(...)：编排器
  lark_chat_client.py    # 飞书 chat API 包装：create_chat / add_members / patch_description
  group_naming.py        # name+suffix → 群名；校验 name/suffix 字符集
  cards.py               # 跳转卡 / welcome 卡 / 错误卡的构造
  config.py              # 集中读 Settings.project_chat_*，便于 mock
  errors.py              # ProjectChatError、CreateChatError、BindError
```

模块对外只暴露 `ProjectChatService.handle`，调用方只有 `dispatcher`。

### 7.2 `lark_chat_client.py` 三个原子方法

```python
class LarkChatClient:
    def create_chat(self, *, name: str, description: str,
                    user_id_list: list[str], bot_id_list: list[str]) -> CreateChatResult: ...
    def delete_chat(self, chat_id: str) -> None: ...           # 用于回滚
    def patch_description(self, chat_id: str, description: str) -> None: ...
```

底层用已存在的 `lark_oapi.api.im.v1`（`CreateChatRequest` / `DeleteChatRequest` / `UpdateChatRequest`）。复用 `FeishuIMClient._execute_with_retry` 的重试与错误码降噪习惯（同模式实现，不直接共享类，保持解耦）。

### 7.3 接入点（对其它模块的三处增量改动）

1. **`src/agent/intent_recognizer.py`** — 新增 `IntentType.NEW_CHAT_PROJECT` 和 `/new-chat` 解析分支（≤ 15 行），紧邻已有 `/new` 分支：

   ```python
   if text_lower.startswith("/new-chat"):
       parts = text.split()
       data = {}
       if len(parts) >= 2: data["name"] = parts[1]
       if len(parts) >= 3: data["suffix"] = parts[2]
       if len(parts) >= 4: data["path"] = parts[3]
       return IntentResult.single(intent=IntentType.NEW_CHAT_PROJECT, ...)
   ```

2. **`src/feishu/dispatcher.py`** — `_dispatch_project` 加 case：

   ```python
   elif intent == IntentType.NEW_CHAT_PROJECT:
       self.client._project_chat_service.handle(message_id, chat_id, sender_open_id, data)
   ```

3. **`src/project/context.py`** — §6.1 字段增量。

`FeishuWSClient.__init__` 处实例化一次 `ProjectChatService`，挂在 `self._project_chat_service`，与现有 handler 列表风格一致。

## 8. 生命周期与边界场景

### 8.1 删项目

`/close <name>`（已有路径）：
- 项目从 `_projects` 中移除，`bound_chat_id` 随之消失；
- 旧飞书群**保留**，bot 不退群；
- 旧群再 @ bot：`get_active_project(chat_id)` 返回 None → 走默认管线（普通对话/shell），不会误触老数据。

> 不主动解散群的原因：飞书群可重名，删项目后用户可在同目录再 `/new-chat`，新建一个新群覆盖 `bound_chat_id`，旧群留给历史查阅。

### 8.2 legacy 项目（无 bound_chat_id）

走分支 B（补群）：
- 不新建项目，不变 `project_id`；
- 直接建一个群，把 `bound_chat_id` 写回该 ctx；
- `allowed_chat_ids` 加入新 chat_id，走 LRU 既有规则。

### 8.3 幂等

对已绑群的项目重复 `/new-chat`：
- 不调用任何飞书 API；
- 主对话回卡片"项目 `<name>` 已就绪 · 群 `<bound_chat_name>`"；
- 卡片提示用户在飞书侧边栏搜索群名进入。

### 8.4 想"再开一个新群"

当前规则下不允许（A 幂等）。逃生口：先 `/close <name>`，再 `/new-chat <name>` 即可重建。后续若要支持"同目录多群"，改成 `find_project_by_path` 的匹配键加入 `suffix`——本期不做。

### 8.5 失败回滚

| 失败点 | 已发生副作用 | 回滚动作 |
|---|---|---|
| 建群 API 失败 | 无 | 主对话报错卡，无需回滚 |
| 建群成功，写 `bound_chat_id` 失败 | 飞书有新群 | `lark_chat_client.delete_chat(new_chat_id)` |
| 分支 C 中建群成功，`create_project` 失败 | 飞书有新群 | 同上 + 主对话报错卡 |
| 分支 C 中 `create_project` 成功，`_save_projects()` 持久化失败（磁盘满 / 文件锁异常） | 项目在内存 + 群已建 | `delete_chat` + `_projects.pop(project_id)`，并尝试再写一次磁盘清掉脏内存态 |
| Welcome 卡发送失败 | 群已建、项目已绑 | 仅日志告警，不回滚（用户在主对话已收到跳转卡，体验不受影响） |

## 9. 隔离与并发

直接复用现有机制，不新增隔离层：

| 关切 | 复用机制 | 备注 |
|---|---|---|
| 新群消息 vs 主对话消息互不干扰 | `dispatcher` / `mode_manager` / `ACPSessionManager` 全部以 `chat_id` 为 key | 新群 chat_id 自然得到独立的 mode 状态、session 状态 |
| 同一目录被多个群发起编程 | `repo_lock.py` 按 `root_path` 互斥；`LockConflictError` 已有卡片提示 | 完全保留现状 |
| 项目对当前群可见 | `ProjectContext._is_visible(chat_id)` + `allowed_chat_ids` LRU | 建群成功后把 new_chat_id 加入 allowed_chat_ids |
| `/new-chat` 自身并发（用户连点） | `ProjectChatService` 入口对 `(chat_id, normalized_path)` 加进程内轻量锁 | 新增；防"同一目录两次建群"赛跑 |

非编程场景的"对同一目录的多种操作"（如 `/shell ls`）维持现状，不额外加锁。

## 10. UX：跳转

飞书无跨端稳定 deeplink。采用"双通道"方案：

- **主对话回卡**：标题 `项目 <name> 已就绪 · 群:<群名>`；正文显示 项目名 / 目录 / 仓库 / chat_id 末 6 位；底部一句"在飞书侧边栏搜索 `<群名>` 进入"。
- **新群 welcome 卡**：bot 在新群内首发一条卡片，含项目摘要 + "/coco /claude /codex 任选其一开始编程"。该消息触发飞书原生未读通知，用户点通知直接进群——这是移动端最稳的"跳转"。

未来可加：检测客户端类型，桌面端额外附 `lark://client/chat/open_chat_id?openChatId=<chat_id>`，移动端隐藏该链接（不稳）。

## 11. 权限前置

bot 需要的飞书 scope（实施前一次性确认）：

- `im:chat`（建群、解散群、改群信息）
- `im:chat:create`（如平台对建群分项授权时需要）
- `im:message`（已有）

如 scope 不足，`create_chat` 返回 230xx 错误码 → ProjectChatService 回错卡，引导去飞书后台开权限；不静默吞错。

## 12. 测试

新增 `tests/test_project_chat.py`，mock `LarkChatClient` + 真 `ProjectManager`（用临时 storage_path），覆盖：

| 用例 | 期望 |
|---|---|
| 全新目录 `/new-chat` | 项目+群均创建，`bound_chat_id` 写入，主对话回跳转卡 |
| 同目录二次 `/new-chat`（已绑群） | 不调建群 API，主对话回"已绑定"卡，群只有 1 个 |
| legacy 项目 `/new-chat` | 项目 `project_id` 不变，`bound_chat_id` 写入新群 |
| 建群 API 失败 | 项目未创建（分支 C），主对话回错卡 |
| 写绑定后建群被人为删掉（mock：先建群再让 manager 抛异常） | `delete_chat` 被调用，`_projects` 不残留 |
| 默认值传递 | 不传 name/path/suffix 时按 §3 默认计算 |
| 群名拼接 | `{name}-{suffix}`，name/suffix 含空格被拒（`group_naming.validate`） |

## 13. 实施前置检查（不属于本设计的实现，但实施时第一步要确认）

1. bot 应用的 scope 是否含 `im:chat`、`im:chat:create`、`im:chat:readonly`。
2. 现有 `Settings` 类的扩展点：在哪里加 `project_chat_suffix` 默认值（看 `src/config.py` 已有约定）。
3. `sender_open_id` 在 dispatcher 现有签名里如何拿到（`P2ImMessageReceiveV1.event.sender.sender_id.open_id`），如未透传到 `_dispatch_project` 则需要顺延 1 个参数。

## 14. 非目标

- **群解散/退群** — 本期不做（§8.1）。
- **announcement** — 本期不做，使用 description（§5）。
- **跨端 deeplink** — 本期不做（§10）。
- **同目录多群** — 本期不做（§8.4）。
- **主对话→新群之间的上下文同步** — 本期不做。新群是独立 chat，独立 mode、独立 session。
