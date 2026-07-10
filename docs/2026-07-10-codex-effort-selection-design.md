# Codex 模型与 Effort 级联选择设计

Date: 2026-07-10

## Goal Snapshot

- Goal: 官方 Codex ACP 迁移后，普通编程模式和 Workflow 继续使用模型与 Effort 下拉选择，并让所选 Effort 真正作用于运行会话。
- Success criteria:
  - `/codex` 显示模型族与 Effort 下拉，不退化为模型按钮列表。
  - `/wf` 选择 Codex 时复用同一份精确模型/Effort 能力数据。
  - 只展示每个模型真实支持的 Effort。
  - 选择值在项目状态和 Workflow 绑定中可持久化、可反解。
  - Codex 启动和在线切模分别设置 `model` 与 `reasoning_effort`。
  - 任一配置被 adapter 拒绝时 fail-close，不静默使用错误配置。
- Constraints:
  - 不新增项目持久化字段，继续使用 `acp_model_name` 和既有 Workflow `model_name`。
  - 不改变 Traex 现有 `模型/Profile/Effort` 复合值语义。
  - 只信任官方 Codex ACP adapter 返回的模型和配置能力，不恢复本机 Codex 缓存兜底。
  - 保持 ACP 探测 single-flight、正缓存和负缓存机制。
- Non-goals:
  - 本轮不开放 Codex `fast-mode`、sandbox 或 approval 配置。
  - 不重构所有 ACP provider 的通用配置 UI。

## Root Cause

官方 `@agentclientprotocol/codex-acp@1.1.2` 在 `new_session.config_options`
中分别暴露：

- `id=model`, `category=model`
- `id=reasoning_effort`, `category=thought_level`

GhostAP 的 `_extract_models_from_config_options()` 只读取
`category=model`。因此模型候选不带 `/high`、`/max` 等变体，级联判断
`has_cascade_variants()` 返回 false，普通模式退回按钮卡；启动链路也只发送
`config_id=model`，没有发送 `reasoning_effort`。

## Chosen Design

### 1. 探测精确的模型/Effort 组合

Codex ACP 只为当前模型返回 `reasoning_effort` 选项，不提供所有模型的能力矩阵。
模型探测使用同一个临时 ACP session：

1. 读取初始 `model` 与 `reasoning_effort` 配置。
2. 对每个模型调用 `set_config_option(config_id="model")`。
3. 从响应中的最新 `reasoning_effort` 配置读取该模型支持值和当前默认值。
4. 每个模型保留一个 `ACPModelOption`，并附带 `reasoning_efforts` 与
   `adapted_reasoning_effort` 能力元数据。后者表示从临时 session 初始 Effort
   切到该模型后 adapter 实际适配出的值，不等同于 Codex 模型元数据的固有默认。
5. 单个模型切换失败时跳过该模型并记录日志；全部无法形成能力数据时返回空列表，
   继续使用 Codex 既有 fail-close 错误卡。
6. 只保留 GhostAP 明确认识且能够在运行时拆分的 Effort；adapter 新增未知值时
   记录 warning 并过滤，防止 UI 可选但启动时把复合值误当模型 ID。

这避免把当前模型的 Effort 列表错误地应用到其他模型。

### 2. 候选携带显式级联维度

`ACPModelOption` 增加可选 UI 能力元数据：

- `reasoning_efforts`
- `adapted_reasoning_effort`

共享 `model_cascade` 将单个模型按 `reasoning_efforts` 展开成内部变体，只有旧候选
没有能力元数据时才按模型名后缀推断。这样：

- Codex 的 `gpt-5.6-sol/max` 明确表示 `profile=standard, effort=max`。
- Traex 原有单后缀 `/max` 仍保持 `profile=max, effort=default`。
- `ultra` 被纳入 Effort 排序，但不会改变其他 provider。
- Worktree/Slock 等仍使用按钮的模式每个模型只显示一次，不会出现几十个组合按钮。

### 3. 复合值只用于选择与持久化

GhostAP 继续将选择保存为 `model/effort`，无需修改 `ProjectContext` schema。
到官方 Codex ACP 边界时解析为：

```text
gpt-5.6-sol/max
  -> model = gpt-5.6-sol
  -> reasoning_effort = max
```

`SyncACPSession` 在首次启动和在线 `set_model()` 时复用同一个应用函数：

1. 设置 `model`。
2. 有 Effort 时设置 `reasoning_effort`。
3. 任一步失败返回失败；首次启动失败时关闭 session 并抛错。

底层 `ACPSession` 提供通用 `set_config_option(config_id, value)`，`set_model()`
继续兼容其他 ACP provider 和旧 `session/set_model` fallback。

### 4. 普通模式与 Workflow 共用能力数据

`WorktreeToolDiscovery.get_models_for_tool()` 保留候选的显式维度字段；
Workflow 的 `_get_workflow_models_for_tool()` 不再丢弃这些字段。
普通模式与 Workflow 都通过共享 `build_model_groups()` 获得一致分组。

默认选中优先级：

1. 用户当前正在操作的 pending 选择。
2. 与持久化复合值精确匹配的候选。
3. 同模型的 `is_variant_default` 候选，用于兼容旧的裸模型值。
4. adapter 报告的全局默认候选。
5. 列表第一项。

non-Coco 的 5 分钟共享缓存始终保存 provider 原始默认；项目当前选择只在返回副本
上重新标记，避免第一个访问项目污染 Workflow 或其他项目的默认值。失效的历史选择
回退到 adapter 当前默认模型及该模型的默认 Effort。

## Error Handling

- Codex 模型探测只接受 adapter 返回的数据。
- 某模型的 Effort 能力无法读取时不构造猜测组合。
- 启动时 model 或 effort 配置失败会关闭新 session 并进入已有启动失败卡。
- 在线切换部分成功时返回 false，既有编程 handler 会结束旧 session 并按完整复合值重建，避免继续使用不确定状态。
- 日志记录 model 与 effort 名称，不记录凭据或 adapter 原始敏感数据。

## Grill-Me Review

用户授权自动接受推荐建议。本设计经过多轮问题树审查并采纳：

1. **能力矩阵准确性**
   - 问题：直接把当前模型 Effort 与全部模型做笛卡尔积会展示非法组合。
   - 采纳：在临时 session 中逐模型切换并读取响应后的 Effort。
2. **`max` 语义冲突**
   - 问题：既有单后缀 `/max` 被解释为 Profile，而 Codex 的 `max` 是 Effort。
   - 采纳：候选增加显式 group/profile/effort 元数据，字符串推断仅作旧数据 fallback。
3. **跨模式一致性**
   - 问题：Workflow discovery 会重建字典并丢弃新增元数据。
   - 采纳：Worktree discovery 和 Workflow 缓存链路完整透传元数据。
4. **持久化兼容**
   - 问题：新增独立 effort 字段会修改项目 schema 并扩大所有引擎改动面。
   - 采纳：保留复合选择值，只在 Codex ACP 边界拆分。
5. **按钮模式爆炸半径**
   - 问题：全局返回模型/Effort 笛卡尔积会让 Worktree/Slock 的按钮列表膨胀。
   - 采纳：每个模型返回一次并携带 Effort 能力，只在级联渲染阶段展开。
6. **默认值与缓存隔离**
   - 问题：首次访问项目的 current model 可能污染共享缓存，失效历史值也可能清空
     adapter 默认。
   - 采纳：缓存只存 provider 默认；current model 只标记返回副本，失效值回退默认。
7. **协议前向兼容**
   - 问题：未来未知 Effort 若直接显示，运行时无法可靠拆分复合值。
   - 采纳：共享已知 Effort 集覆盖 `none/minimal/low/medium/high/xhigh/max/ultra`，
     未知值在 probe 边界过滤并告警。

## Verification

- 单元测试：
  - Codex config options 精确展开为模型/Effort 变体。
  - 不同模型只包含各自支持的 Effort。
  - `max`/`ultra` 映射到 Effort 而非 Profile。
  - 普通模型卡与 Workflow 都显示 Effort 下拉。
  - 首次启动和在线切模按顺序设置 model、reasoning_effort。
  - effort 设置失败时 fail-close。
  - 旧裸模型值默认到 adapter 报告的该模型默认 Effort。
- 定向回归：ACP probe、model cascade、Sync adapter、switch model、Workflow selection。
- 扩展回归：ACP 全量、Workflow 全量、配置校验、ruff、文档引用和 diff check。
