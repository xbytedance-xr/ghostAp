# GhostAP 项目进展记录

## 项目概述
GhostAP 是一个飞书机器人Shell沙箱服务，通过飞书机器人对话来安全执行本地shell命令，并支持 Coco AI 和 Claude AI 远程开发模式。

## 最新更新
**更新时间**: 2026-01-29 21:00:00

### Claude 编程模式全面修复（2026-01-29 21:00:00）
修复 Claude 编程模式无法使用的问题（报错 `Invalid session ID. Must be a valid UUID`），并全面适配卡片按钮、项目管理和会话快照。

#### 根因修复
- **Session ID 格式**: 从 `feishu_claude_{chat_id}_{timestamp}` 改为 `uuid.uuid4()` 生成的合法 UUID
- **命令构造冲突**: 第一条消息用 `--session-id`，后续消息用 `--resume`，不再同时传两个参数

#### 卡片按钮适配
- Claude 模式下显示「退出Claude」+「切换项目」按钮
- Smart 模式下显示「Coco模式」+「Claude模式」两个入口按钮
- 流式卡片同步适配，传 `is_claude_mode=True`
- 项目创建卡片新增「开始 Claude」按钮

#### 项目管理兼容
- `ProjectContext` 新增 `claude_mode`、`claude_session_snapshot` 字段
- 新增 `ClaudeSessionSnapshot` 数据类
- 新增 `set_claude_mode()`、`update_claude_snapshot()` 方法
- 快照序列化/反序列化包含 Claude 字段，兼容旧数据
- 退出 Claude 模式时保存会话快照，下次可恢复
- 切换项目时显示 Claude 恢复卡片

#### 卡片动作处理
- 新增 `enter_claude`、`exit_claude`、`resume_claude`、`new_claude` 四个卡片动作
- 进入 Claude 时自动退出 Coco（互斥保护，反之亦然）
- 回复编程消息自动识别 Claude/Coco 模式并自动进入
- 项目状态面板显示 Claude 模式信息

#### 改动文件
- 修改 `src/claude/session.py` — UUID 生成 + 命令构造修复
- 修改 `src/project/context.py` — 新增 Claude 字段和方法
- 修改 `src/project/__init__.py` — 导出 ClaudeSessionSnapshot
- 修改 `src/card/builder.py` — Claude 模式按钮、标题、恢复卡片
- 修改 `src/card/streaming.py` — 流式卡片 Claude 适配
- 修改 `src/feishu/ws_client.py` — 卡片动作、状态管理、快照保存
- 修改 `tests/test_claude.py` — 新增 15 个测试（UUID、快照、按钮）
- 修改 `tests/test_streaming.py` — 适配按钮文案变更

#### 测试
- 总测试数 214 个全部通过

---

### Claude 编程模式初始实现（2026-01-29 19:30:00）
新增 Claude 编程模式，与 Coco 模式平行，用户可以选择使用不同的 AI 进行编程。

#### 新增功能
- **Claude 会话管理器**: `src/claude/session.py` - 类似 CocoSession 的实现
- **模式扩展**: ModeManager 支持 SMART、COCO、CLAUDE 三种模式
- **意图识别扩展**: 新增 ENTER_CLAUDE、EXIT_CLAUDE、CLAUDE_MESSAGE、SHOW_HELP 意图
- **帮助命令**: `/help` 和 `/帮助` 显示完整使用说明

#### 命令说明
| 命令 | 说明 |
|------|------|
| `/coco` | 进入 Coco 编程模式（默认） |
| `/claude` | 进入 Claude 编程模式 |
| `/exit` | 退出当前编程模式 |
| `/coco_info` | 查看 Coco 会话信息 |
| `/claude_info` | 查看 Claude 会话信息 |
| `/help` 或 `/帮助` | 显示完整帮助信息 |

#### 改动文件
- 新增 `src/claude/__init__.py`
- 新增 `src/claude/session.py`
- 修改 `src/mode/manager.py` - 添加 CLAUDE 模式支持
- 修改 `src/agent/intent_recognizer.py` - 添加 Claude 相关意图
- 修改 `src/feishu/ws_client.py` - 添加 Claude 模式处理逻辑
- 新增 `tests/test_claude.py` - 24 个测试用例

#### 测试
- 新增 24 个 Claude 相关测试用例
- 总测试数 199 个全部通过

---

### Bug 修复（2026-01-29 17:00:00）
- **问题**: 在编程模式（Coco 模式）下发送 `/deep` 命令时，消息被直接转发给 Coco，导致报错 "slash command '/deep ...' not found"
- **原因**: `_process_with_intent` 方法在 Coco 模式下只检查退出命令，其他消息都直接发给 Coco
- **解决**: 添加 `_is_deep_command()` 和 `_handle_deep_command()` 方法，在 Coco 模式下优先拦截 Deep 相关命令
- **测试**: 新增 4 个测试用例，总测试数 175 个全部通过

### 已完成功能
1. ✅ 项目初始化 - pyproject.toml、目录结构
2. ✅ 配置管理模块 - 支持环境变量和.env文件
3. ✅ 沙箱命令执行器 - 危险命令检测、超时控制、输出截断
4. ✅ 飞书长连接客户端 - WebSocket方式接收消息，无需公网IP
5. ✅ AI Agent - 使用 ARK 方舟大模型进行意图识别
6. ✅ Coco 远程对话模式 - 通过飞书与 Coco 进行远程开发
7. ✅ ReAct 智能意图识别 - 推理式意图理解，支持任务拆解
8. ✅ 消息过期丢弃 - 超过30秒的旧消息自动丢弃
9. ✅ 表情回复 - 消息状态反馈（OK、GET、Typing、Done等）
10. ✅ 多项目并行开发架构 - 支持单对话框管理多个项目
11. ✅ 两种交互模式 - 智能模式、编程模式
12. ✅ 消息卡片优化 - 支持代码块渲染（markdown 组件）
13. ✅ 流式卡片输出 - 打字机效果的实时输出
14. ✅ 单元测试 - 171 个测试全部通过
15. ✅ **Deep Engine** - 复杂任务自动拆解与执行引擎（新增）

### Deep Engine 模块（2026-01-29 新增）

Deep Engine 是一个复杂任务编排引擎，能够将用户的复杂需求自动拆解为多个子任务，并依次调用 Coco 执行，实时反馈进度。

#### 架构设计
```
deep_engine/
├── __init__.py          # 模块导出
├── models.py            # 数据模型（DeepTask, DeepProject, ParsedRequirement 等）
├── parser.py            # RequirementParser - 需求解析器（使用 LLM）
├── planner.py           # TaskPlanner - 任务规划器（生成任务列表）
├── executor.py          # TaskExecutor - 任务执行器（调用 Coco）
├── engine.py            # DeepEngine - 顶层编排器
└── reporter.py          # ProgressReporter - 进度报告器
```

#### 核心组件
| 组件 | 职责 |
|------|------|
| **RequirementParser** | 使用 LLM 解析用户需求，提取 goals、constraints、tech_stack |
| **TaskPlanner** | 将需求拆解为可执行的任务列表，支持任务依赖 |
| **TaskExecutor** | 调用 Coco 执行单个任务，支持流式输出和失败重试 |
| **DeepEngine** | 顶层编排器，协调规划和执行流程 |
| **ProgressReporter** | 生成用户友好的进度消息 |

#### 使用方式
| 命令 | 说明 |
|------|------|
| `/deep <需求描述>` | 启动 Deep Engine 执行复杂任务 |
| `/deep_status` | 查看当前 Deep 任务进度 |
| `/stop_deep` | 停止正在执行的 Deep 任务 |

#### 执行流程
1. 用户发送 `/deep 帮我写一个爬虫...`
2. RequirementParser 解析需求，提取目标和约束
3. TaskPlanner 生成任务列表（如：创建项目、实现抓取、解析数据、保存文件）
4. DeepEngine 依次执行每个任务
5. 每个任务完成后，发送进度消息给用户
6. 全部完成后，发送汇总报告

#### 进度反馈示例
```
🧠 Deep Engine 启动
📝 正在分析需求...

✅ 任务规划完成
📂 项目: my_crawler
📊 共 5 个任务

🔄 执行任务 [1/5]
[█░░░░░░░░░] 0%
📌 创建项目结构

✅ 任务完成 [1/5]
[██░░░░░░░░] 20%
⏱️ 耗时: 3.5s

...

🎉 全部任务完成！
[██████████] 100% (5/5)
⏱️ 总耗时: 45.2s
```

### 三种交互模式

| 模式 | 图标 | 说明 | 进入方式 | 退出方式 |
|------|------|------|----------|----------|
| **智能模式** | 🧠 | 默认模式，根据意图自动选择 Shell 或编程 | 默认 / 退出编程模式后 | - |
| **Coco 编程模式** | 🤖 | 所有消息都发给 Coco，支持流式输出 | `/coco` / "进入编程模式" | `/exit` / "退出模式" |
| **Claude 编程模式** | 🔮 | 所有消息都发给 Claude，支持流式输出 | `/claude` / "进入claude模式" | `/exit` / "退出模式" |

#### 模式切换命令
| 命令 | 作用 |
|------|------|
| `/coco` | 进入 Coco 编程模式 |
| `/claude` | 进入 Claude 编程模式 |
| `/exit` 或 "退出模式" | 退出编程模式，回到智能模式 |
| `/help` 或 `/帮助` | 显示完整帮助信息 |

#### 表情回复规则
| 模式 | 首次回复 | 处理中 |
|------|----------|--------|
| 智能模式 | OK 👌 | Typing ⌨️ |
| 编程模式 | GET 🤙 | Typing ⌨️ |

#### 自动进入编程模式
当用户**回复机器人的编程消息**时：
1. 自动识别消息关联的项目
2. 自动切换到该项目
3. 如果该项目之前在编程模式，自动进入编程模式
4. 用户的消息直接作为编程指令处理

### 目录概念

| 概念 | 图标 | 说明 |
|------|------|------|
| **工作目录** | 📁 | 全局唯一的当前目录，跟随 `cd` 命令变化 |
| **项目目录** | 📂 | 项目代码所在目录，创建时绑定，不会改变 |

- **Coco 编程**使用项目目录 (`root_path`)
- **Shell 命令**使用工作目录 (全局 `_working_dirs`)

### 支持的功能

#### 🧠 智能模式（默认）
根据用户意图自动选择执行方式：
- Shell 命令 → 执行命令
- 编程需求 → 进入编程模式
- 目录切换 → 切换工作目录
- 项目管理 → 创建/切换/查看项目

#### 🤖 编程模式
与 Coco AI 进行远程开发对话：
- 说「进入编程模式」或 `/coco` - 进入编程模式
- 说「退出模式」或 `/exit` - 退出编程模式
- `/coco_info` - 查看会话信息
- 回复编程消息自动进入编程模式

#### 💻 Shell 模式
所有消息直接作为 Shell 命令执行：
- 说「进入shell模式」或 `/shell` - 进入 Shell 模式
- 说「退出模式」或 `/exit` - 退出 Shell 模式

#### 📁 目录切换
- 说「切换到xxx目录」- 智能切换工作目录
- 支持自然语言描述：「切换到用户目录下的workspace」

#### 📂 多项目并行开发
单对话框管理多个开发项目，**全部通过自然语言交互**：

**自然语言支持：**
- 「创建项目」→ 使用当前目录名作为项目名
- 「创建项目 myapp」→ 创建名为 myapp 的项目
- 「切换到 test 项目」→ 切换项目
- 「看看有哪些项目」→ 显示项目列表
- 「项目状态」→ 查看当前项目状态

**命令支持：**
- `/projects` - 查看所有项目状态面板
- `/new <名称> [目录]` - 创建新项目
- `/switch <名称>` - 切换当前项目
- `/close <名称>` - 关闭项目
- `/status` - 查看当前项目详情

**特性：**
- 引用消息自动关联对应项目
- 交互式卡片快捷操作
- Coco 响应显示项目目录和工作目录
- 全局激活项目，切换后工作目录自动跟随

####  表情回复
- 收到消息：OK 表情
- Coco 处理中：Typing 表情
- 完成：Done 表情
- 多任务执行：Rocket 表情

### 技术栈
- Python 3.11+
- lark-oapi (飞书SDK，长连接模式)
- LangChain + LangGraph (AI Agent + ReAct 意图识别)
- ARK 方舟大模型（字节跳动）
- pydantic-settings (配置管理)
- coco CLI (远程开发)

### 安全机制
1. 正则表达式检测危险命令模式（20+）
2. 命令黑名单配置
3. 命令执行超时控制
4. 输出长度限制
5. 消息过期丢弃（30秒）

### 连接方式
使用飞书SDK的**长连接模式（WebSocket）**：
- ✅ 无需公网IP或域名
- ✅ 无需内网穿透
- ✅ 本地只要能访问公网就能接收消息
- ✅ 自动加密传输

### 代码统计
| 类型 | 行数 |
|------|------|
| 源代码 | ~4,000 行 |
| 测试代码 | ~1,100 行 |
| **总计** | **~5,100 行** |

## 历史记录

### 2026-01-29 15:30:00（代码重构与优化）
- **修复 Bug**：移除 `reply` 方法中的重复发送代码（调试遗留）
- **代码拆分**：
  - 新增 `src/feishu/emoji.py` - 提取 `EmojiType` 和 `EmojiReaction` 类
  - 新增 `src/feishu/message_cache.py` - 独立的消息缓存管理器
- **消息缓存优化**：
  - 使用 `MessageCache` 类替代原有的 `OrderedDict` 实现
  - 支持后台定时清理线程（每 60 秒清理过期消息）
  - 快速清理限制每次最多清理 100 条，避免阻塞
  - 线程安全，支持并发访问
- **ws_client.py 精简**：从 1288 行减少到 1131 行
- **新增测试**：`tests/test_message_cache.py`（10 个测试）
- **测试**：130 个单元测试全部通过

### 2026-01-23 10:25:00（添加流式输出开关配置）
- **新增配置项**：`STREAMING_ENABLED`（默认 `true`）
- **改动文件**：
  - `config.py`：新增 `streaming_enabled: bool = True`
  - `.env.example`：新增 `STREAMING_ENABLED=true`
  - `ws_client.py`：从配置读取 `_enable_streaming`
- **使用方式**：在 `.env` 中设置 `STREAMING_ENABLED=false` 即可关闭流式输出
- **效果**：关闭后使用普通卡片回复，不使用流式打字机效果

### 2026-01-23 10:15:00（优化卡片按钮布局适配移动端）
- **改动**：将按钮布局从 `action` 改为 `column_set`
- **优化点**：
  - 使用 `column_set` 两列布局，按钮等宽排列
  - `flex_mode: stretch` 确保按钮填满宽度
  - 按钮前增加分隔线 `hr`，视觉更清晰
- **影响文件**：`streaming.py`、`builder.py`
- **测试**：28 个单元测试全部通过

### 2026-01-23 08:45:00（流式卡片引用回复）
- **新增**：流式卡片支持引用回复用户消息
- **改动**：
  - `StreamingCard` 新增 `reply_to_message_id` 字段
  - `create_streaming_card` 新增 `reply_to_message_id` 参数
  - `send_streaming_card` 支持 `reply` API（引用回复）和 `create` API（直接发送）
  - `_handle_coco_streaming` 传递 `message_id` 作为引用目标
- **效果**：流式卡片回复会显示在用户消息下方，形成引用关系
- **测试**：28 个单元测试全部通过

### 2026-01-23 08:25:00（统一卡片按钮格式与状态区分）
- **按钮格式统一**：将 `value` 改为 `behaviors` 格式，兼容 Card JSON 2.0
- **编程模式按钮**：🚪 退出Coco、🔄 切换项目
- **非编程模式按钮**：🤖 编程模式、📋 选择项目
- **改动文件**：`streaming.py`、`builder.py`
- **测试**：28 个单元测试全部通过

### 2026-01-23 08:10:00（修复流式卡片 Card JSON 2.0 兼容问题）
- **问题**：`code=200861, msg=cards of schema V2 no longer support this capability; ErrorValue: unsupported tag action`
- **根因**：Card JSON 2.0 的 `body.elements` 不支持 `action` 标签
- **修复**：将按钮从 `body.elements` 移到顶层 `actions` 字段
- **测试**：14 个单元测试全部通过

### 2026-01-23 07:53:32（重新实现流式卡片输出）
- **重写 StreamingCardManager**
  - 增加 `project_id` 字段支持按钮回调关联项目
  - 优化打字机效果配置：`print_frequency_ms=30`, `print_step=3`, `print_strategy=fast`
  - 增加详细日志：创建/发送/更新/关闭各阶段
  - 新增 `cleanup_expired_cards` 方法清理过期卡片
  - 按钮 value 统一使用 `behaviors` 对象结构
- **优化 ws_client 流式处理**
  - 传递 `project_id` 到流式卡片
  - 增加更新计数与最终长度日志
  - 关闭流式时传递 `final_content` 确保最终内容完整
  - 缩短 `chunk_interval` 到 0.3 秒
- **新增单元测试**：`tests/test_streaming.py`（14 个测试全部通过）

### 2026-01-22 22:23:12（卡片回调日志加强 + value 结构调整 + SDK 升级）
- **日志增强**
  - 卡片回调记录 value 预览、解析失败提示与处理耗时
- **卡片按钮 value 改为对象**
  - 统一按钮回传 value 为 dict，避免 SDK 字符串解析歧义
- **依赖升级**
  - lark-oapi 升级至 >=1.5.2
- **测试**：tests/test_card.py 通过

### 2026-01-22 22:10:14（移除卡片回调 Patch + 增强日志）
- **移除 WS client patch**
  - 不再 monkey patch SDK 的 CARD 消息处理逻辑
- **增强卡片回调日志**
  - 记录 event_id、open_message_id、action 元信息与 value 类型
  - 记录 operator 与 value 解析后的 key 集合
- **测试**：tests/test_ws_client_patch.py 通过

### 2026-01-22 22:00:23（卡片回调 200671 复发修复）
- **修复卡片回调空响应再次触发 200671**
  - 避免对空对象/空 JSON（"{}"、"null"）写入 resp.data，保持标准空响应
  - 保留非空响应的 Base64 data 序列化
  - 测试更新：新增非空响应 data 设置用例，调整空响应断言
- **测试**：tests/test_ws_client_patch.py 通过

### 2026-01-22（代码清理）
- **移除未使用的模块和依赖**
  - 删除 `src/notification/` 模块（未被使用）
  - 删除 `src/tools/` 模块（未被使用）
  - 删除 `src/agent/shell_agent.py`（未被使用）
  - 删除 `docs/TOOL_CHAIN_REPORT.md`（过时文档）
  - 删除 `test_marshal.py`（临时测试脚本）
  - 删除 `tests/test_tools.py`、`tests/test_notification.py`（对应模块已删除）
- **清理依赖**
  - 移除 `fastapi`、`uvicorn`、`httpx`、`pycryptodome`（未使用）
- **清理配置**
  - 移除 `verification_token`、`encrypt_key` 配置项（HTTP 模式遗留）
  - 移除 `reload_settings()` 函数（未使用）
  - 更新 `.env.example` 移除废弃配置
- **清理代码**
  - 移除 `message_formatter.py` 中未使用的方法
  - 移除 `mode/manager.py` 中未使用的方法
  - 移除 `streaming.py` 中未使用的方法
  - 移除 `sandbox/executor.py` 中未使用的方法
- **测试**：107 个测试全部通过

### 2026-01-22（卡片回调 200671 修复）
- **修复卡片按钮点击报错 200671**
  - 根本原因：`_handle_card_action` 返回空的 `P2CardActionTriggerResponse` 对象，经 Monkey Patch 序列化为 `data: "e30="` (Base64 of `{}`)，飞书服务端认为格式无效。
  - 修复方案：修改 `_handle_card_action` 返回 `None`，Patch 逻辑检测到 `None` 时不设置 `data` 字段，返回标准空响应。
  - 验证：新增单元测试 `tests/test_ws_client_patch.py` 验证 `None` 返回值及 Patch 序列化行为。

### 2026-01-22（卡片回调修复 + 流式卡片）
- **修复卡片按钮点击报错 200340 和 200671**
  - 200340 根本原因：飞书开放平台未订阅 `card.action.trigger` 回调
  - 200671 根本原因：SDK bug - `MessageType.CARD` 类型消息未被处理
  - 代码修复：
    1. `_handle_card_action` 返回 `P2CardActionTriggerResponse`
    2. 添加 `_patch_ws_client_for_card_callback` monkey patch 修复 SDK bug
  - SDK bug 详情：`lark_oapi/ws/client.py` 第 264-265 行对 `MessageType.CARD` 直接 return，未调用 `do_without_validation`
- 实现飞书流式卡片输出（打字机效果）
- 新增 src/card/streaming.py - StreamingCardManager
- 修复卡片 JSON 2.0 结构（schema: "2.0"）
- 修复 CardKit API 参数（type: "card_json"）
- 简化模式系统为两种：智能模式 + 编程模式
- 移除 Shell 模式（Shell 命令在智能模式下直接执行）
- 优化表情回复：智能模式 OK、编程模式 GET
- 修复 Coco 输出截断问题（30000 字符限制）
- 修复 Coco 模式退出命令识别（/exit、/end_coco）

### 2026-01-18（三种模式重构）
- 新增 src/mode/ 模块（manager.py）- 模式管理器
- 实现三种交互模式：智能模式、编程模式、Shell模式
- 新增 ENTER_SHELL、EXIT_SHELL、EXIT_MODE 意图类型
- 支持回复编程消息自动进入编程模式
- 优化消息卡片支持代码块渲染（markdown 组件）
- 重构工作目录为全局唯一，项目目录固定不变
- 优化创建项目逻辑：无名称时使用目录名
- 测试总数：175 个全部通过

### 2026-01-18（意图识别整合）
- 将项目管理整合到 ReAct 意图识别系统
- 扩展 IntentType 新增 5 种项目管理意图
- 支持自然语言创建/切换/关闭项目
- 支持「在当前目录创建项目然后开始编程」多步骤任务
- Coco 响应添加项目名和工作目录信息
- 移除硬编码的项目命令处理，统一走意图识别

### 2026-01-18（多项目架构）
- 新增多项目并行开发架构
- 新增 src/project/ 模块（context.py、manager.py、mapper.py）
- 新增 src/card/ 模块（builder.py、themes.py）- 飞书交互式卡片
- 新增 src/notification/ 模块（hub.py）- 异步通知系统
- 增强 src/coco/session.py 支持会话恢复（--resume）
- 增强 src/feishu/ws_client.py 集成多项目管理
- 新增 47 个多项目相关测试用例
- 测试总数：123 个全部通过

### 2026-01-18（工具链）
- 新增安全工具链（SafeShellTool、FileEditorTool、ToolManager）
- 完成 Claude Code SDK vs LangChain 技术调研
- 移除废弃的 HTTP 模式代码（server.py、client.py、handler.py）
- 清理无用配置（server_host、server_port）
- 新增 58 个工具链测试用例
- 重构 main.py 为 Application 类
- 更新项目文档

### 2026-01-09 23:12
- 引入 ReAct 推理模式进行意图识别
- 新增 TaskStep 类支持多任务拆解
- 新增消息过期丢弃机制（30秒阈值）
- 新增多任务执行逻辑（展示计划、逐步执行、进度反馈）
- Coco 模式下消息直接转发，不经过 ReAct

### 2026-01-09 22:55
- 新增表情回复功能
- 注册 im.message.reaction.created_v1 事件处理器消除错误日志

### 2026-01-09 22:29
- 新增智能意图识别功能
- 新增 src/agent/intent_recognizer.py 意图识别器
- 支持自然语言切换 Coco 模式
- 支持自然语言切换工作目录
- 每个聊天维护独立的工作目录状态

### 2026-01-09 22:22
- 修复消息重复处理问题
- 添加消息去重机制（message_id 缓存）

### 2026-01-09 22:13
- 新增 Coco 远程对话模式
- 新增 src/coco/session.py 会话管理器
- 支持 /coco、/end_coco、/coco_info 命令
- 会话隔离、上下文保持、超时控制

### 2026-01-09 22:00
- 修复飞书消息接收问题
- 从HTTP Webhook模式改为WebSocket长连接模式
- 新增 ws_client.py 长连接客户端
- 长连接测试成功

### 2026-01-09 21:34
- 项目创建并完成所有核心功能
- 通过所有单元测试
