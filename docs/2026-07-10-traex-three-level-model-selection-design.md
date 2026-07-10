# Traex 三级模型选择恢复设计

## 背景

Traex CLI 0.200.17 的 ACP adapter 不再返回约 90 个
`model/profile/effort` 扁平候选，而是在 `new_session.config_options` 中分别返回：

- `id=model`, `category=model`：26 个裸模型；
- `id=reasoning_effort`, `category=thought_level`：当前模型支持的 Effort。

GhostAP 的通用 Traex 探测仍只读取 `category=model`。2026-07-10 的 Codex
适配虽然已经支持分离的模型与 Effort，但被限制在 `tool_name == "codex"`。
因此 Traex 候选不再包含复合后缀，`has_cascade_variants()` 返回 false，普通
`/traex` 和 `/model` 退化为按钮选择，Workflow、Worktree、Spec 与 Slock 也只
能获得裸模型。

现有 Traex 启动边界还会调用 `normalize_acp_model_name()`，把历史复合值
`c_o_new_thinking/max/max` 提前裁成 `Test-O-New-Thinking`。这避免了把非法复合
字符串直接传给 `session/set_model`，但同时永久丢失 Profile 与 Effort。

## 目标

- 恢复 Traex 的模型、Profile、Effort 三级选择。
- `c_o_new_thinking/max/max` 必须真实运行在 max Profile 与 max Effort，而不只是
  卡片和持久化值显示正确。
- 普通编程、Deep、Spec、Worktree、Workflow 与 Slock 共享同一能力和执行语义。
- 保持 Codex 的模型/Effort 选择和其他 ACP provider 行为不变。
- 缓存、元数据或 adapter 能力异常时不静默降级到错误模型配置。

## 非目标

- 不开放 Traex 的 mode、permission、sandbox 等其他 ACP 配置项。
- 不把 Worktree、Spec 或 Slock 的既有按钮/分页界面改造成新的三级级联界面。
- 不修改 `ProjectContext` schema；模型选择继续保存在 `acp_model_name` 或现有
  工作流选择项中。
- 不为缺少可信 Profile 元数据的模型猜测 max Profile。

## 可行性证据

本机 Traex 0.200.17 的 `models_cache.json` 包含 26 个模型，其中
`business_metadata.variants` 提供 standard/max Profile key、每个 Profile 的默认
Effort 与支持矩阵。

协议级验证结果：

- Adapter 返回 26 个模型。
- 由 adapter 白名单和本地 metadata 组合出 36 个 Profile，36 个 Profile 的模型
  配置值全部被临时 ACP session 接受。
- 30 个带 Effort 的 Profile，其 adapter 实际返回 Effort 与 metadata 完全一致。
- `c_o_new_thinking` 的标准 Profile 接受裸值 `c_o_new_thinking`；max Profile 接受
  隐藏值 `c_o_new_thinking__max`；两者均支持 `low/medium/high/max`。

真实最小 turn 依次写入：

```text
model = c_o_new_thinking__max
reasoning_effort = max
```

Traex 最终 session 元数据记录：

```text
model = Test-O-New-Thinking
model_backend_variant = max
reasoning_effort = max
```

这证明推荐方案能够同时控制真实模型、上下文 Profile 和推理 Effort。

## 设计

### 1. 结构化能力模型

`ACPModelOption` 继续表示一个用户可识别的模型族，并新增不可变的 Profile 能力
集合。每个 Profile 能力至少包含：

- UI Profile 名：`standard` 或 `max`；
- ACP `model` 配置值：标准 Profile 使用裸 `config_name`，max Profile 使用
  metadata 的 `max_key`；
- 支持的 Effort；
- 默认 Effort；
- 对应的稳定选择值。

普通 Codex 的 `reasoning_efforts` 字段继续保留，不强制迁移到 Traex Profile
结构，避免扩大本轮变更范围。共享卡片渲染优先使用显式 Profile 能力；只有旧
provider 候选没有结构化能力时，才继续使用现有字符串后缀推断。

### 2. 能力发现与信任边界

Traex 探测使用同一个临时 ACP session：

1. 从 adapter 的 `model` 配置读取模型白名单、显示名和当前模型。
2. 从 `~/.trae/cli/models_cache.json` 读取与白名单模型匹配的 Profile 元数据。
3. 标准 Profile 使用裸 `config_name`；有 `max_key` 时增加 max Profile。
4. 对每个 Profile 设置一次 `config_id=model`，从响应读取实际
   `reasoning_effort` 选项与当前适配值。
5. 本地 metadata 与 adapter 都提供 Effort 时只保留交集；adapter 返回完整集合
   时，不接受 metadata 中 adapter 未声明的值。
6. Profile 配置被拒绝时不展示该 Profile；模型所有 Profile 都不可用时不展示该
   模型。

当本地 cache 缺失、损坏或与 adapter 白名单不匹配时，仍可通过裸模型切换读取
标准 Profile 的 Effort；此时不展示 max Profile。探测全部失败时沿用现有错误卡，
不得回退到历史复合值或虚构能力。

正缓存、负缓存与 single-flight 继续按 `(tool, cwd)` 工作。缓存保存 provider 原始
能力，项目当前选择只在返回副本上标记。新增显式失效函数，使“刷新模型”按钮真实
清除该 `(tool, cwd)` 的正缓存和负缓存，并递增该 key 的缓存 generation。Workflow
短缓存同时保存 generation，发现共享 generation 变化时丢弃旧条目；正常 TTL 仍为
五分钟。

### 3. 选择值与兼容性

Traex 新生成的带 Effort 选择使用无歧义的三段值：

```text
<model>/<profile>/<effort>
```

例如：

```text
c_o_new_thinking/max/max
c_o_new_thinking/standard/high
```

无 Effort 的 Profile 选择只保存 `<model>/<profile>`；只有标准 Profile、无额外
维度的模型继续保存裸 `<model>`。

解析器继续接受历史值：

- `<model>/<effort>` 解释为 standard Profile 的 Effort，但历史单后缀 `/max`
  保持原语义，解释为 max Profile 的默认 Effort；
- `<model>/max/<effort>` 解释为 max Profile 与指定 Effort；
- 裸 `<model>` 使用 standard Profile 和 adapter 的默认 Effort。

显式 Profile 能力为新卡片提供选择值，因此共享 renderer 不再需要从新 Traex
候选名称猜测维度。`resolve_default_selection()` 能从新旧值恢复默认下拉状态。

### 4. ACP 运行时应用

复合选择必须一直保留到 `SyncACPSession`：

- `ACPSessionManager`、`agent_session.factory` 和普通编程在线切模不再提前把 Traex
  选择规范化成 slug。
- `resolve_agent_spec()` 仍可用 provider 的裸模型规范化结果构建启动命令，避免
  把复合值直接写进 CLI 参数。
- `SyncACPSession._model_name` 保存原始选择值，用于会话复用、切模比较和状态显示。

Traex 在 `new_session` 成功后、session 对外标记 ready 前，按顺序执行：

1. 将选择解析为模型、Profile、Effort。
2. 根据可信能力映射得到 ACP model 配置值。
3. `set_config_option("model", backend_profile_key)`。
4. 有显式 Effort 时执行
   `set_config_option("reasoning_effort", effort)`。

在线 `set_model()` 使用相同函数和顺序。任一步失败均返回失败；首次启动失败时关闭
session 并进入现有启动失败卡，在线切模失败时走现有关闭旧 session 并完整重建的
路径。不得在 Profile/Effort 失败后报告“已进入编程模式”。

Deep、Spec、Review、Slock 与 Workflow 最终都经 `create_engine_session()` 或
`SyncACPSession` 创建 Traex session，因此不在各引擎增加后端特定分支。

### 5. 各选择界面

- 普通 `/traex`、`/model`：共享 `model_cascade` 使用显式 Profile 能力，恢复模型、
  Profile、Effort 三级级联。
- Workflow：`WorktreeToolDiscovery` 和 Workflow 缓存完整透传 Profile 能力，继续用
  现有级联卡片。
- Worktree、Spec、Slock：保持既有按钮/分页 UI，在进入这些 builder 前把结构化
  Profile 能力展开成合法的稳定复合选择值，恢复其原有完整候选覆盖。
- Deep：继续使用项目保存的 `acp_model_name`，运行时统一解析并应用。

在 `ux/acp-model-cascade.html` 更新 Traex 预览，使生产卡片标签、默认值和最终复合
值与预览一致。

## 错误处理

- 只展示 adapter 白名单中的模型。
- Profile key 必须来自当前 Traex metadata，并在探测 session 中被 adapter 接受。
- Effort 必须同时得到 metadata 与 adapter 支持；缺一侧时只使用 adapter 能证明的
  标准 Profile 能力。
- 直接 `/model <value>` 输入的 Profile/Effort 在运行时按同一能力映射校验；非法或
  过期值失败关闭，不裁掉后缀继续运行。
- 缓存中的结构化 Profile 使用不可变对象，复制模型候选时不会产生跨项目默认值
  污染。
- 日志只记录 model/profile/effort 和失败阶段，不打印模型缓存的其他业务字段。

## Grill-Me 审查与自动采纳

用户授权自动接受 `grill-me` 推荐建议。审查逐项解决了以下风险：

1. **能力矩阵被错误笛卡尔积**
   - 采纳：adapter 模型白名单、metadata Profile 与实时 Effort 三方校验。
2. **标准 Profile key 与 ACP 写入值混淆**
   - 发现：`standard_key=...__dev` 虽可被 RPC 接受，但响应不稳定返回 Effort。
   - 采纳：标准 Profile 写裸 `config_name`，仅 max Profile 写 `max_key`。
3. **单后缀 `/max` 歧义**
   - 采纳：新值显式写 `model/profile/effort`，历史值继续兼容解析。
4. **启动前规范化再次吞掉维度**
   - 采纳：原始选择保留到 Sync session；CLI 启动名和 ACP 配置值分开解析。
5. **只修普通模式导致执行策略漂移**
   - 采纳：能力发现、候选展开和运行时应用均设为共享边界，各引擎不单独实现。
6. **缓存更新后仍展示旧能力**
   - 采纳：刷新按钮显式失效共享探测缓存；Workflow 短缓存通过 generation 感知
     失效。
7. **卡片正确但后端仍跑 standard/default**
   - 采纳：真实 Traex turn 作为最终 smoke，检查落盘
     `model_backend_variant` 与 `reasoning_effort`。

## 测试与验证

测试按 TDD 增加以下回归：

- Traex cache 解析：standard/max Profile、backend key、各 Profile Effort 与默认值。
- Traex ACP 探测：adapter 白名单交集、逐 Profile 能力、缺失/损坏 cache 降级、
  Profile/Effort 被拒绝、缓存副本隔离和刷新失效。
- 选择编码：新三段值、历史两段/三段值、单后缀 `/max`、非法 Profile/Effort。
- 普通卡片：`c_o_new_thinking` 同时出现 Profile 和 Effort 下拉，确认值为
  `c_o_new_thinking/max/max`。
- Workflow：结构化 Profile 能力经过 Worktree discovery 和 Workflow cache 后仍
  显示三级选择。
- Worktree/Spec/Slock：按钮候选包含有效复合选择且分页预算不回退。
- 首次启动与在线切模：按顺序写 backend profile key 和 reasoning_effort；任一步
  失败 fail-close；`_model_name` 保留复合值；Codex 原有行为不变。
- Deep/Spec/Review 工厂：Traex 复合值不再提前规范化丢失。

验证层次：

1. 新增回归测试先在旧实现上得到预期失败，再最小实现转绿。
2. 运行 ACP probe、model cascade、session factory、switch model、Workflow、
   Worktree、Spec、Slock 的相关测试。
3. 运行全量 `uv run python -m pytest tests/ -q`、`uv run ruff check`、
   `uv run python -m src.main --validate` 与 `git diff --check`。
4. 使用真实 Traex 临时 session 选择 `c_o_new_thinking/max/max`，执行最小 turn，
   检查 session 元数据为 `model_backend_variant=max` 且
   `reasoning_effort=max`。
