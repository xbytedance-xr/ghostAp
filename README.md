# GhostAP

GhostAP 是一个飞书/Lark 机器人服务，用聊天界面驱动本地项目中的 Shell、AI 编程工具和多 Agent 编排。它通过飞书长连接接收消息，不要求本机暴露公网地址，适合把日常研发命令、代码修改和长任务执行接入到团队聊天里。

## 核心能力

- **远程 Shell**：在当前项目目录执行命令，带超时、输出截断、黑名单和可选白名单。
- **多工具编程会话**：支持 Coco、Claude、Aiden、Codex、Gemini、Traex、TTADK 和 TUI2ACP 等后端，普通会话可持续多轮对话。
- **长任务引擎**：Deep、Spec、Worktree、Workflow 和 Slock 覆盖从单次自主执行到结构化闭环、并行 worktree 和群内多 Agent 协作。
- **自主工作系统（v5）**：基于持久化 Journal 的数字员工自主执行平台，支持目标管理、计划编排、效果追踪、安全门禁和飞书交互式员工创建。
- **飞书卡片进度**：任务状态、计划、工具调用、模型选择和错误诊断通过卡片持续更新。
- **多项目隔离**：每个聊天可绑定不同项目目录；会话、线程上下文、锁和持久化状态按项目隔离。
- **并发保护**：包含 chat 锁、repo 锁、任务调度队列和锁顺序检查，避免多个聊天同时改同一个仓库。

## 运行模型

GhostAP 把“执行策略”和“工具传输”拆开：

| 维度 | 说明 |
| --- | --- |
| 执行策略 | Smart、Shell、普通编程、Deep、Spec、Worktree、Workflow、Slock、Autonomous |
| 工具传输 | ACP 直接模式、Shell CLI 桥接、TTADK CLI 桥接 |

普通工具入口会设置聊天 + 项目的持续模式，直到 `/exit`。Deep、Spec、Worktree 和 Workflow 是作用在话题/根线程上的任务引擎，不会替换普通编程模式。Smart 是默认模式；当 `DEFAULT_ACP_TOOL` 留空时，未匹配的自由文本会按 Shell 命令处理。

## 快速开始

### 环境要求

- Python 3.11+
- `uv`
- 飞书/Lark 企业自建应用，开启长连接接收事件
- 如需使用 `/wf`，需要 Node.js 20+
- 需要使用的 AI 工具或 ACP Provider 已在本机安装并完成各自认证

### 安装依赖

```bash
uv sync --group dev
```

### 配置飞书应用

在飞书开放平台创建企业自建应用后，至少配置：

1. 获取 `APP_ID` 和 `APP_SECRET`。
2. 在“事件与回调”中启用“使用长连接接收事件”。
3. 订阅 `im.message.receive_v1`。
4. 授权消息接收、消息发送和卡片更新相关权限。

### 配置环境变量

```bash
cp .env.example .env
vim .env
```

最小配置：

```env
APP_ID=your_app_id
APP_SECRET=your_app_secret
DEFAULT_ACP_TOOL=coco
ADMIN_USER_IDS=
```

常用配置：

```env
SANDBOX_TIMEOUT=30
SANDBOX_MAX_OUTPUT_LENGTH=4000
SANDBOX_COMMAND_BLACKLIST=

ACP_PERMISSION_AUTO_APPROVE=true
ACP_MODEL_PROBE_TIMEOUT=15

WORKFLOW_TOTAL_TIMEOUT_S=3600
WORKFLOW_AGENT_CALL_TIMEOUT_S=600
WORKFLOW_SCRIPT_GEN_TIMEOUT_S=180

TTADK_DEFAULT_TOOL=claude
TTADK_DEFAULT_MODEL=

SLOCK_DEFAULT_ROLES=planner:claude,coder:codex,reviewer:claude,tester:codex
```

更多参数见 `.env.example` 和 `src/config/settings.py`。各 AI 后端所需的密钥、登录态或 CLI 配置应按对应工具自己的方式准备，GhostAP 只读取必要的环境变量和本地命令。

### 校验并启动

```bash
uv run python -m src.main --validate
uv run python -m src.main
```

首次启动后，可在飞书私聊机器人发送 `/setadmin` 设置管理员。`ADMIN_USER_IDS` 为空时允许首次设置；设置后只有管理员可以替换管理员配置。

## 常用命令

### 模式与模型

| 命令 | 作用 |
| --- | --- |
| `/help` | 查看完整帮助 |
| `/coco`、`/claude`、`/aiden`、`/codex`、`/gemini`、`/traex` | 进入对应编程模式 |
| `/ttadk` | 进入 TTADK 多工具编程模式 |
| `/tui2acp` | 进入 TUI2ACP 桥接模式 |
| `/model`、`/model list`、`/model <name>` | 查看或切换当前 ACP 工具模型 |
| `/acp` | 查看 ACP 工具选择入口 |
| `/exit` | 退出当前模式，回到 Smart |

Shell 不需要单独入口；在 Smart 模式中直接发送 `ls`、`git status`、`uv run ...` 等命令即可执行。`DEFAULT_ACP_TOOL` 留空时，未匹配文本也会回退到 Shell。

### 项目

| 命令 | 作用 |
| --- | --- |
| `/projects` | 查看项目面板 |
| `/new <名称> [目录]` | 创建项目 |
| `/switch <名称>` | 切换项目 |
| `/close <名称>` | 关闭项目 |
| `/status` | 查看当前项目、模式、锁和任务状态 |

### 长任务引擎

| 命令 | 作用 |
| --- | --- |
| `/deep <需求>` | 单次规划并自主执行 |
| `/deep_status`、`/deep_update <补充>`、`/stop_deep` | 查看、补充或停止 Deep |
| `/spec <需求>` | 按 Spec → Plan → Task → Build → Review 闭环推进 |
| `/spec_status`、`/spec_guide <引导>`、`/spec_pause`、`/spec_resume`、`/stop_spec` | 管理 Spec 任务 |
| `/worktree <需求>` 或 `/wt <需求>` | 多工具并行执行，使用 Git worktree 隔离分支 |
| `/wf <需求>` | 生成并执行 JS Workflow 编排脚本 |
| `/wf_status`、`/wf_help`、`/wf_save`、`/wf_list`、`/wf_history`、`/stop_wf` | 管理 Workflow |
| `/slock`、`/new-team <名称>` | 启用或创建 Slock 多 Agent 团队 |
| `/slock status`、`/task status`、`/new-role <名称>`、`/team dissolve <名称>` | 管理 Slock 团队 |

### 自主工作系统（Autonomous）

| 命令 | 作用 |
| --- | --- |
| `/goal <描述>` | 创建新的自主目标并启动执行 |
| `/goals` | 列出当前租户的所有目标 |
| `/run <goal_id>` | 启动目标的执行 Run |
| `/runs` | 列出所有运行中的 Run |
| `/status <run_id>` | 查看 Run 进度 |
| `/approve <id>` | 批准待审批操作 |
| `/employee create` | 通过交互式卡片创建新的数字员工 |
| `/employee list` | 列出所有活跃员工 |
| `/kill` | 激活紧急停止（管理员） |
| `/pause <goal_id>` | 暂停目标 |
| `/resume <goal_id>` | 恢复目标 |
| `/cancel <goal_id>` | 取消目标 |

**创建数字员工流程：**

1. 发送 `/employee create`，机器人弹出交互式卡片
2. 在卡片中选择：角色（coder/reviewer/planner/tester/researcher）、工具（coco/claude/codex/aiden/gemini/ttadk）、模型
3. 点击「Create Employee」确认
4. 系统创建员工并返回确认卡片，员工立即可接受工作分配

**安全等级：**

自主系统根据配置的 `AUTONOMOUS_DEPLOYMENT_MODE` 决定实际能力：

| 模式 | 行为 |
| --- | --- |
| `off` | 自主系统不加载 |
| `assist` | 只读，不执行写操作 |
| `manager_only` | 单 Manager 控制，需审批高风险操作 |
| `supervised` | 多员工协作，人工在环审批 |
| `bounded_autonomous` | 有限自主，满足全部安全门禁后生效 |

Workflow 使用三步流程：选择主编排 Agent、选择评审 Agent 或 Auto、确认后自动生成并执行脚本。内置原语包括 `agent()`、`sequence()`、`fanout()`、`verify()`、`generate()`、`tournament()`、`loop()` 和 `race()`，并由运行时限制总 agent 数、嵌套深度和危险脚本能力。

## 架构入口

| 路径 | 说明 |
| --- | --- |
| `src/main.py` | 应用启动、配置校验和生命周期 |
| `src/feishu/ws_client.py` | 飞书 WebSocket 入口、消息校验、去重和调度 |
| `src/feishu/handlers/` | 命令处理器 |
| `src/mode/` | 聊天/项目交互模式状态 |
| `src/acp/` | ACP 会话、Provider、模型发现、诊断和事件渲染 |
| `src/agent_session/` | ACP 与 CLI 后端的统一会话抽象 |
| `src/ttadk/` | TTADK 工具、模型和启动策略 |
| `src/deep_engine/` | Deep 单次自主执行 |
| `src/spec_engine/` | Spec 结构化闭环和多视角审查 |
| `src/worktree_engine/` | Git worktree 并行执行 |
| `src/workflow_engine/` | JS Workflow 生成、验证、运行时和卡片渲染 |
| `src/slock_engine/` | 群内多 Agent 团队、角色、任务队列和记忆 |
| `src/autonomous/` | v5 自主工作系统（详见下方） |
| `src/card/` | CardSession 事件管线、纯渲染和卡片投递 |
| `src/project/`、`src/project_chat/`、`src/thread/` | 项目、群绑定和线程上下文 |
| `src/chat_lock.py`、`src/repo_lock.py`、`src/utils/lock_order.py` | 聊天锁、仓库锁和锁顺序约束 |
| `src/config/` | Pydantic Settings 和 `.env` 配置 |

卡片管线遵循单向依赖：

```text
handler -> session -> render
                  -> delivery
```

渲染层保持纯函数；投递层不反向依赖会话层。跨层共享类型放在 `src/card/protocols.py` 或 `src/card/events/`。

## 自主工作系统架构（src/autonomous/）

v5 自主工作系统使用 Journal-backed 持久化架构，所有状态变更通过事务帧记录，支持崩溃恢复和重放。

```text
src/autonomous/
├── bootstrap.py              # 生产组装根，初始化 lark-oapi 客户端
├── coordinator.py            # 目标/Run 生命周期编排
├── planner.py                # 计划编译（模型辅助或默认单步）
├── employees.py              # 员工生命周期和协作规划
├── config.py                 # 部署模式和有效自治等级
├── domain/                   # 冻结聚合体和纯状态机
├── journal/                  # 事务帧、写入者、锚点、Blob、投影
├── policy/                   # 默认拒绝授权、预算 CAS、Kill Switch
├── broker/                   # 能力注册、线性化派发门、模型/工具代理
├── scheduler/                # 持久队列、租约围栏、触发器
├── runtime/                  # 结构化轮次协议、沙箱运行器
├── verifier/                 # 准则编译器和 Oracle 验证
├── reporter/                 # 持久发件箱和效果处置 Saga
├── supervisor/               # 启动/恢复/关闭和对账
├── manager/                  # 命令处理、飞书卡片、lark-oapi 适配器
├── migration/                # Slock 幂等导入和兼容层
├── acceptance/               # 77 门禁清单、统计度量、证据存储
└── feishu/                   # 能力探测和功能可见性门控
```

**关键依赖：**
- `lark-oapi==1.6.5`：REST API 消息发送、卡片更新、机器人管理
- `lark-channel-sdk==1.1.0`：WebSocket 事件订阅（持久收件箱）

**测试：**

```bash
uv run pytest tests/autonomous/ -q        # 577+ 测试
uv run ruff check src/autonomous/         # 0 错误
```

## 安全与运维

- Shell 执行经过沙箱检查，支持黑名单、白名单、超时和输出截断。
- 飞书消息有过期检查和去重缓存，避免重复执行。
- ACP 工具调用通过权限钩子处理，可配置自动批准或默认拒绝。
- 仓库操作受 repo 锁保护，群聊访问可由管理员锁定。
- 卡片按钮带签名校验，错误详情会脱敏和截断。
- Workflow 脚本会做结构化验证，禁止危险模块和明显逃逸。
- 日志优先查看 `logs.log`；重启或启动问题同时检查 `[RESTART]` 标记和 `uv run python -m src.main --validate` 输出。

## 开发

本仓库只使用 `uv`：

```bash
uv sync --group dev
uv run python -m src.main --validate
uv run python -m pytest tests/ -q
uv run python -m pytest tests/test_acp_client.py -q
uv run ruff check .
```

针对性修改时先跑最相关测试；涉及共享路由、卡片渲染、锁、配置或会话代码时扩大测试范围。项目约定见 `AGENTS.md`，提交信息规范见 `docs/commit-message-guidelines.md`。

## 目录

```text
ghostAp/
├── src/                 # 应用代码
│   ├── autonomous/      # v5 自主工作系统
│   ├── card/            # 飞书卡片事件、渲染、投递和状态管线
│   │   ├── actions/
│   │   ├── delivery/
│   │   ├── events/
│   │   ├── render/
│   │   ├── session/
│   │   ├── state/
│   │   ├── timers/
├── tests/               # 测试
│   ├── autonomous/      # 自主系统测试（unit/integration/chaos/security/contract）
├── docs/                # 架构记录和接入指南
├── scripts/             # 辅助脚本
├── ux/                  # UI 预览和验证资产
├── .Memory/             # 近期决策、验证和风险记录
├── AGENTS.md            # AI 编码代理项目指令
├── .env.example         # 环境变量模板
├── pyproject.toml       # Python 项目配置
└── README.md
```

## License

MIT License
