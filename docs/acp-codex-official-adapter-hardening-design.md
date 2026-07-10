# Codex 官方 ACP 迁移收口设计

日期：2026-07-10

## 目标

修复 `/codex` 选择 `gpt-5.6-sol` 后在首个 prompt 返回
`Model metadata ... not found` 和 `Internal error` 的问题，并保证：

- 模型选择列表与实际运行的 Codex ACP adapter 能力一致；
- 用户显式选择的模型在普通、Deep、Spec、Worktree、Workflow 和 Slock
  共用的 ACP 启动链路中真实生效；
- 升级到 ACP Python SDK 0.11 后，权限、文件和终端回调仍符合新接口；
- 重启预热不会因为把 ACP stdio server 当成普通 `--help` CLI 而阻塞；
- 已完成的异步激活、single-flight、负缓存和终态卡片逻辑继续复用。

## 已有实现

`98b8e0b fix(acp): migrate codex to official adapter` 已完成以下主体迁移：

- Codex fallback 从 `@zed-industries/codex-acp@0.14.0` 切换为
  `@agentclientprotocol/codex-acp@1.1.2`；
- `agent-client-protocol` 最低版本提升到 `0.11.0`；
- Codex 模型发现不再信任本机 `~/.codex/models_cache.json`，只消费
  adapter 的 `config_options`/探测结果，探测失败时返回空列表；
- 活跃会话模型切换优先调用
  `session/set_config_option(configId=model)`，新协议失败时不误降级；
- 相关依赖、模型探测、切换协议和重启脚本测试已更新。

这些实现保持不变，本轮只收口协议迁移遗漏。

## 根因与剩余缺口

### 1. 首次启动没有真正应用显式模型

当前 `CodexACPProvider.get_fallback_command()` 仍沿用旧 Zed adapter 的
`-c model=...` 参数。官方 adapter 不解析该参数；它只识别环境变量
`CODEX_CONFIG`，或会话建立后的 `session/set_config_option`。

同时 `SyncACPSession._start_session()` 在 `new_session` 后直接返回，没有把
`self._model_name` 写入新会话。因此模型卡选中值可能被保存并显示为已就绪，
实际首个 prompt 却继续使用用户配置中的默认模型。

### 2. ACP 0.11 Client 回调签名未迁移

ACP 0.11 调整了以下 Client 方法的参数顺序和字段：

- `request_permission(session_id, tool_call, options)`；
- `read_text_file(session_id, path, line=None, limit=None)`；
- `write_text_file(session_id, path, content)`；
- `create_terminal(session_id, command, args=None, env=None, cwd=None,
  output_byte_limit=None)`。

`GhostAPClient` 仍保留旧签名。纯文本 prompt 不会触发这些入口，所以现有 smoke
测试能够通过，但真实工具调用存在参数错位或路由异常风险。

### 3. 重启预热命令不会退出

官方 adapter 仅显式处理 `--version`、`login` 和 `cli`；其他参数会进入 ACP
stdio server。`restart.sh` 当前执行 `npx ... --help`，因此可能一直等待 stdin，
阻塞重启流程。

## 方案比较

### A. 收口官方 adapter + ACP 0.11 协议（采用）

保持已有官方 adapter 和 SDK 升级，补齐首次选模、Client 新签名和预热命令。
优点是模型能力、运行时和协议来自同一官方实现，且保留 ACP 会话能力；缺点是
需要覆盖权限、文件和终端回调的兼容测试。

### B. 过滤新模型并继续旧 adapter

改动较少，但会隐藏本机已经可用的模型，且下次模型目录与内嵌 Codex 版本漂移
时还会复发。该方案已被 0.14/0.16 对照实测否决。

### C. 新模型改走 Codex CLI bridge

可以绕过 adapter 元数据，但会让同一工具按模型拆分 ACP/CLI 两套传输，增加会话、
权限、事件和恢复语义分支，不符合 GhostAP 的工具抽象原则。

## 详细设计

### Provider 命令

当本机 Codex 不支持原生 `acp serve` 时，provider 只启动：

```text
npx --yes @agentclientprotocol/codex-acp@1.1.2
```

不再给官方 adapter 传递旧 `-c model=...` 参数。其他 provider 的模型启动参数
保持原样。

### 首次模型应用

`SyncACPSession` 继续持有 `agent_type` 和 `model_name`。Codex 会话完成
`initialize + new_session` 后、返回 ready 之前：

1. 若用户没有显式模型，保留 adapter/用户配置的默认值；
2. 若存在显式模型，调用 `ACPSession.set_model()`；
3. `set_model()` 通过 ACP 0.11 的
   `set_config_option(config_id="model", value=<model>)` 应用选择；
4. 设置成功后才允许 session startup 成功；
5. 设置失败时关闭会话并让现有 startup retry/失败卡链路接管，禁止假 ready。

这样普通模式与所有复用 `SyncACPSession`/`create_engine_session` 的引擎共享
同一正确性保证，同时活跃会话的模型切换继续保留上下文。

### 模型发现

保留现有 adapter-first 逻辑：

- 读取 `NewSessionResponse.config_options` 中 `category=model` 的选项；
- positive cache、negative cache 和 single-flight 继续按 `(tool, cwd)` 工作；
- Codex 探测失败返回空列表和重试入口，不回退到本机模型缓存；
- 不增加 `gpt-5.6-*` 或其他模型名硬编码。

### ACP Client 兼容

`GhostAPClient` 方法签名与 ACP 0.11 `Client` protocol 对齐。已有业务语义不变：

- 权限请求继续执行工具过滤、危险命令检查和自动批准策略；
- 文件读写继续限制在项目根目录并应用字符上限；
- 终端创建继续走 `SandboxExecutor`，同时正确接收 `args`、`env`、`cwd` 和
  `output_byte_limit`；
- 不支持或未使用的新增字段采用安全默认值，不扩大文件系统或执行权限。

### 重启预热

`restart.sh` 改用 `npx --yes <package> --version` 拉取并验证 adapter。该命令由
官方入口显式处理并立即退出。原生 `codex acp serve` 探测和环境覆盖变量保持不变。

## 错误处理

- 显式模型应用失败视为启动失败，不发送“编程模式已就绪”；
- adapter 模型探测失败继续显示可重试的空模型错误态；
- config-option RPC 可用但拒绝模型时 fail-close，不调用语义已经移除的旧 RPC；
- 只有连接对象完全不提供新 config-option 能力时，其他旧 ACP provider 才允许
  使用已有 legacy fallback；Codex 官方 adapter 不走该路径；
- 完整异常进入日志，飞书卡片继续使用现有安全化错误摘要。

## 测试设计

按 TDD 增加以下回归：

1. provider fallback 对官方 adapter 不再携带 `-c model=...`；
2. Codex 显式模型在 `new_session` 后、startup 返回前调用 config option；
3. config option 失败会使 startup 失败并触发清理，不产生 ready；
4. 默认模型启动不发送多余的模型切换 RPC；
5. `GhostAPClient` 四个迁移方法与 SDK 0.11 签名一致，并验证关键参数落入原业务字段；
6. permission、文件读写和终端创建至少各有一个行为测试；
7. restart 预热使用 `--version`，不再使用 `--help`；
8. 真实官方 adapter smoke：`gpt-5.6-sol` 建会话、显式设模、prompt 返回成功。

验证范围：先运行 ACP/provider/model/restart 定向测试，再扩大到普通编程、Deep、
Spec、Worktree、Workflow 和 Slock 的共享会话测试，最后运行全量 pytest、Ruff、
配置校验、shell 语法检查和 `git diff --check`。

## 非目标

- 不修改 Coco、Claude、Aiden、Gemini、Traex 或 TTADK 的传输选择；
- 不为单个模型增加白名单、别名或强制降级；
- 不改变飞书模型卡片布局和异步激活交互；
- 不自动追踪 npm `latest`，继续固定经过验证的 adapter 版本；
- 不重构无关 ACP session、card 或 engine 代码。

## 完成条件

- `/codex` 选择 `gpt-5.6-sol` 后，实际 session 使用该模型且首个 prompt 成功；
- 选择任意 adapter 返回的其他模型时，不会悄悄回落到用户默认模型；
- Codex 工具调用的权限、文件和终端回调在 ACP 0.11 下正常；
- `restart.sh` 的 Codex 依赖预热可以确定性退出；
- 所有定向、共享边界和全量验证通过；
- `.Memory/2026-07-10.md` 与 `.Memory/Abstract.md` 记录最终根因、补充修复和验证；
- 修复提交推送到 `origin/dev`。
