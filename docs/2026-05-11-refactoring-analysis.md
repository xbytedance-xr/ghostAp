# GhostAP 代码库重构分析报告

> **日期**: 2026-05-11
> **项目规模**: 308 个源文件，81,826 行代码，19 个模块
> **分析范围**: src/ 全目录

---

## 目录

- [模块概览](#模块概览)
- [高优先级问题](#-高优先级影响可维护性和可测试性)
- [中优先级问题](#-中优先级代码质量和架构改进)
- [低优先级问题](#-低优先级整洁度和可维护性)
- [量化汇总](#-量化汇总)
- [架构亮点](#-架构亮点保持不变)

---

## 模块概览

| 模块 | 文件数 | 代码行数 | 平均行/文件 |
|---|---|---|---|
| `card/` | 113 | 22,189 | 196 |
| `feishu/` | 41 | 15,134 | 369 |
| `ttadk/` | 21 | 9,191 | 437 |
| `spec_engine/` | 27 | 7,785 | 288 |
| `acp/` | 13 | 7,520 | 578 |
| `utils/` | 34 | 5,183 | 152 |
| `project/` | 6 | 2,509 | 418 |
| `worktree_engine/` | 9 | 2,463 | 273 |
| `agent_session/` | 7 | 2,403 | 343 |
| 其他 | 37 | 7,449 | 201 |
| **合计** | **308** | **81,826** | **265** |

**超过 1000 行的文件（12 个）**:

| 文件 | 行数 | 核心问题 |
|---|---|---|
| `feishu/ws_client.py` | 2,155 | God Class，306 行 `__init__` |
| `acp/sync_adapter.py` | 1,636 | 职责混杂，54 处宽泛异常捕获 |
| `spec_engine/engine.py` | 1,576 | God Class，19 个初始化参数 |
| `ttadk/manager.py` | 1,507 | 16 个兼容性 re-export，230 行 shim |
| `ttadk/models.py` | 1,330 | 数据类、正则解析、模型解析混杂 |
| `project/unified_context.py` | 1,200 | 上下文管理复杂度 |
| `feishu/handlers/programming.py` | 1,196 | `enter_mode` 235 行 |
| `acp/manager.py` | 1,153 | `_start_session_inner` 315 行 |
| `card/builders/system.py` | 1,119 | 5 个选择卡片方法 90% 结构重复 |
| `feishu/handlers/system.py` | 1,097 | 二级 God Class，5+ 职责 |
| `card/orchestrator.py` | 1,044 | 复杂但合理 |
| `ttadk/model_fetcher.py` | 1,042 | 5 个类混在一个文件 |

---

## 🔴 高优先级（影响可维护性和可测试性）

### 1. God Class：`FeishuWSClient`

- **位置**: `src/feishu/ws_client.py:148-2155`（2,008 行类体）
- **问题**:
  - `__init__` 长达 306 行，内联初始化 20+ 个协作对象
  - 单类承担 WebSocket 生命周期、消息路由、卡片动作处理、项目管理、线程调度、emoji 反馈等职责
  - 通过 `router.py` 的 `setattr` 动态绑定暴露 ~160 个转发方法
  - 从 14 个不同包导入，全项目耦合度最高
- **建议**:
  - 将 `__init__` 提取为应用 Bootstrap 工厂/DI 容器
  - 拆分为 `WSEventRouter`（消息路由）、`WSCardActionHandler`（卡片动作）、`WSResourceManager`（资源管理）

### 2. God Function：`build_startup_diagnostics`

- **位置**: `src/acp/sync_adapter.py:124-447`
- **问题**: 324 行，91 个分支，全项目复杂度最高的函数。54 个 `except Exception:` 密集排列。
- **建议**:
  - 拆解为 `DiagnosticsBuilder` 类：`_collect_environment()`、`_collect_agent_info()`、`_collect_session_state()`
  - 引入 `@safe_extract(default=None, log_msg="...")` 装饰器消除 try/except 样板

### 3. God Function：`coordinate_ttadk_startup`

- **位置**: `src/ttadk/startup.py:253-662`
- **问题**: 410 行，68 个分支。内联回退逻辑、探测处理、冷却管理、错误分类。
- **建议**: 用状态机或 Strategy 模式拆分为离散启动阶段

### 4. God Function：`_start_session_inner`

- **位置**: `src/acp/manager.py:493-807`
- **问题**: 315 行，61 个分支。覆盖 ACP/CLI/TTADK/Coco 回退、模型解析、PTY 重试。
- **建议**: 按会话类型（ACP/CLI/TTADK/Coco）实现 Strategy 模式

### 5. Dispatcher 与 Client 强耦合

- **位置**: `src/feishu/dispatcher.py`
- **问题**: ~80+ 处 `self.client._*` 私有成员访问。Dispatcher 本质上是 `FeishuWSClient` 的"友元类"，无法独立测试或复用。`action_registry.py` 也通过 lambda 直接引用 `client._*`。
- **建议**: 定义 `DispatchContext` 协议接口，仅暴露 dispatcher 实际需要的方法

### 6. 泛滥的宽泛异常捕获 — 全项目 193 处

| 文件 | 数量 | 严重度 |
|---|---|---|
| `acp/sync_adapter.py` | 54 | 🔴 |
| `agent_session/wrappers.py` | 25 | 🔴 |
| `feishu/handlers/diagnostics.py` | 21 | 🔴 |
| `card/orchestrator.py` | 15 | 🟡 |
| `acp/provider.py` | 11 | 🟡 |
| `acp/session.py` | 11 | 🟡 |
| 其他 40+ 文件 | 56 | 🟡 |

- **建议**: 捕获具体异常类型；对诊断类场景引入 `@safe_extract` 装饰器统一处理

---

## 🟡 中优先级（代码质量和架构改进）

### 7. 7 种不一致的单例实现

| 位置 | 模式 |
|---|---|
| `ttadk/manager.py:1284` | `_manager` + `_manager_lock` + 双重检查锁 |
| `chat_lock.py:388` | `_instance` 模式 |
| `repo_lock.py:568` | `_instance` 模式 |
| `coco_model/manager.py:253` | `_manager` 模式 |
| `thread/manager.py:202` | `_manager` 模式 |
| `card/timers/scheduler.py:138` | `_global_scheduler` 模式 |
| `utils/gc_monitor.py:73` | `_global_gc_monitor` 模式 |

- **建议**: 统一使用 `ServiceRegistry` DI 容器或创建泛型 `Singleton[T]` 描述符

### 8. 模型解析逻辑分散 — 4 个重叠函数共 675 行

| 函数 | 位置 | 行数 |
|---|---|---|
| `resolve_model_id` | `ttadk/models.py` | 229 |
| `resolve_startup_model_with_diagnostics` | `ttadk/manager.py` | 212 |
| `resolve_model_intent_ssot` | `ttadk/manager.py` | 133 |
| `resolve_real_model_name` | `ttadk/manager.py` | 101 |

- **建议**: 抽象为统一的 `ModelResolver` 类

### 9. `sync_adapter.py` 职责混杂

- **位置**: `src/acp/sync_adapter.py`（1,636 行，全项目最大文件）
- **问题**: 前 ~500 行是模块级工具函数（`classify_startup_fail_phase`、`resolve_agent_spec`、`build_startup_diagnostics`），后面才是 `SyncACPSession` 类
- **建议**: 提取启动工具到 `acp/startup_utils.py`

### 10. `ttadk/models.py` 膨胀

- **位置**: `src/ttadk/models.py`（1,330 行）
- **问题**: 混合了正则常量、解析函数、10+ 个 dataclass、模型 ID 解析逻辑
- **建议**: 拆分为 `ttadk/parsing.py`（正则/解析）+ `ttadk/models.py`（纯数据类）+ `ttadk/resolution.py`（模型 ID 解析）

### 11. `CardBuilder` 纯代理外观

- **位置**: `src/card/builder.py`（529 行）
- **问题**: 40+ 个静态方法全部委托到 `CoreBuilder`、`ProjectBuilder`、`SystemBuilder` 等，自身零逻辑
- **建议**: 废弃 `CardBuilder`，让消费方直接引用具体 builder，消除 529 行无用中间层

### 12. Handler 间重复模式

- **涉及文件**: `handlers/deep.py`、`handlers/spec.py`、`handlers/engine_base.py`、`handlers/lock_helper.py`
- **问题**: `_acquire_repo_lock`、`_release_repo_lock`、`_create_callbacks`、`_show_status` 等 31 个方法在多个 handler 间结构性重复
- **建议**: 将更多共享逻辑下沉到 `BaseEngineHandler`，使用 Template Method 模式

### 13. 魔法数字硬编码 — 20+ 处重复

| 值 | 出现次数 | 位置 |
|---|---|---|
| `startup_timeout=60` | 4 次 | `acp/manager.py` 的 4 个方法签名 |
| `snippet_limit=240` | 6 次 | `acp/diagnostics.py`、`acp/sync_adapter.py` |
| `total_limit=2000` | 3 次 | `acp/sync_adapter.py`、`acp/diagnostics.py` |
| `TTL=3600` | 多处 | `acp/client.py`、`card/delivery/engine.py` |

- **建议**: 提取到 `Settings` 配置或模块级 `DiagnosticsConfig` 常量类

### 14. 参数列表过长 — 27+ 个函数

- **重灾区**: `feishu/dispatcher.py` 和 `feishu/renderers/` 反复传递 `(message_id, chat_id, project, engine, state)` 元组
- **示例**: `_dispatch_shell(self, data, message_id, chat_id, original_text, project, shell_fast_tracked)` — 7 参数
- **建议**: 创建 `FeishuRequestContext` dataclass 封装请求上下文

### 15. 意图识别巨型 if-elif

- **位置**: `src/agent/intent_recognizer.py:377` 的 `_quick_match()`
- **问题**: 313 行，35 个分支的 if-elif 链
- **建议**: 重构为 `IntentMatcher` 注册表（策略模式）

### 16. Renderer 不对称组合

- **问题**: `SpecRenderer` 使用了 `RotatingRendererMixin`，但 `DeepRenderer` 没有，尽管两者行为相似
- **位置**: `feishu/renderers/deep_renderer.py` vs `feishu/renderers/spec_renderer.py`
- **建议**: 统一 `DeepRenderer` 也使用 `RotatingRendererMixin`，消除卡片分割/提示逻辑的重复

### 17. `SystemHandler` 成为二级 God Class

- **位置**: `src/feishu/handlers/system.py`（1,097 行）
- **问题**: 混合了帮助、退出模式、Shell 命令、目录切换、ACP 工具选择、模型选择、菜单命令、TTADK 命令、锁命令等 5+ 个不同领域
- **建议**: 拆分为 `ShellHandler`、`ACPToolSelectionHandler`、`HelpHandler`

### 18. `HandlerContext` 服务定位器反模式

- **位置**: `src/feishu/handler_context.py`（40+ 字段的 dataclass）
- **问题**: 每个 handler 接收完整 `HandlerContext`，可访问任意服务。测试 `ProjectHandler` 需要构造全部 20+ 个协作对象。
- **建议**: 让 handler 声明具体依赖接口，而非接收全部服务

### 19. Router FORWARDING_MAP 缺乏类型安全

- **位置**: `src/feishu/router.py:7-160`
- **问题**: 160 条字符串映射，通过 `setattr`/`getattr` 绑定，无编译期验证。其中 54 条（6 种模式 × 9 方法）可以从模式列表自动生成。
- **建议**: 从模式注册表程序化生成 `FORWARDING_MAP`

### 20. `type: ignore` 注释 — 39 处

| 文件 | 数量 | 主要原因 |
|---|---|---|
| `spec_engine/review_retry.py` | 8 | `union-attr` — `circuit` 对象类型约束不足 |
| `acp/providers/__init__.py` | 4 | `attr-defined` — 猴子补丁 `.cache_clear` |
| `acp/manager.py` | 4 | `attr-defined` / `arg-type` |

---

## 🟢 低优先级（整洁度和可维护性）

### 21. ~100 处废弃代码待清理

- `card/events/factories.py` — 6 个废弃的 worktree 事件代理方法
- `card/session/core.py` — 废弃的 `on_first_deliver` 回调
- `card/render/pagination.py` — 废弃的 `paginate_atoms`
- `ttadk/manager.py` — 16 个兼容性 re-export
- `feishu/handlers/base.py` — 3 个 raise `NotImplementedError` 的废弃方法
- 部分标注移除日期为 `2026-06-01`，建议届时集中清理

### 22. `TYPE_CHECKING` 守卫泛滥 — 59 个文件

- `card/` 子系统占 30+，`feishu/` 占 15，`acp/` 占 5
- 另有 PEP 562 `__getattr__` 延迟导入（`acp/__init__.py`、`card/styles.py`）和运行时 `importlib.import_module`（`acp/sync_adapter.py:91`）
- 架构层面可接受，但暗示模块边界可能过度拆分

### 23. 工具类错放 — `utils/` 中的领域专属代码

| 文件 | 行数 | 应归属 |
|---|---|---|
| `utils/ttadk_wrapper.py` | 553 | `src/ttadk/` |
| `utils/spec_utils.py` | 506 | `src/spec_engine/` |
| `utils/review_helpers.py` | 492 | `src/spec_engine/` |
| `utils/review_diagnostics.py` | 380 | `src/spec_engine/` |

### 24. `ui_text.py` 单体 UI 字符串注册表

- **位置**: `src/card/ui_text.py`（929 行）
- **问题**: 500+ 个 key 的单一字典，覆盖所有模块 UI 文案。运行时碰撞检查（行 904-915）是脆弱的断言。
- **建议**: 按领域拆分为 `card_ui_text.py`、`worktree_ui_text.py`、`deep_ui_text.py` 等

### 25. `builders/system.py` 选择卡片构建重复

- **位置**: `src/card/builders/system.py:302-716`
- **问题**: `build_ttadk_tool_select_card`、`build_ttadk_model_select_card`、`build_acp_tool_select_card` 等 5 个方法遵循完全相同的模式（构建选项 → 包装布局 → 序列化），90% 结构重复
- **建议**: 提取泛型 `_build_select_card(title, prompt, options, *, refresh_action=None)` 辅助方法

### 26. 三种不一致的错误展示路径

| 路径 | 触发场景 | 展示方式 |
|---|---|---|
| `handlers/base.py:send_error_card` | 系统错误 | CardBuilder 错误卡 + QuickActions |
| `handlers/spec.py:_on_engine_error` | Spec 引擎错误 | renderer 构建的富错误卡 |
| `engine_base.py:_on_engine_error` | Deep 引擎错误 | session pipeline dispatch |

- 三种不同卡片样式会呈现给用户，体验不一致

### 27. `card/styles.py` 通配符 re-export

```python
from .themes import *      # noqa: F401, F403
from .thresholds import *   # noqa: F401
from .buttons_config import *  # noqa: F401
from .terminal import *     # noqa: F401
```

- 5 个通配符导入，存在命名空间污染和隐式依赖风险

### 28. `model_fetcher.py` 多类混合

- **位置**: `src/ttadk/model_fetcher.py`（1,042 行）
- **问题**: `TTADKModelFetcher`、`FileCacheStrategy`、`StructuredSyncStrategy`、`TTADKRunner`、`TTADKRunResult` 5 个类在同一文件
- **建议**: 拆分为独立的策略文件

---

## 📊 量化汇总

| 分类 | 数量 | 影响 |
|---|---|---|
| God Class/Function（>1000 行或 >60 分支） | **5 个** | 可维护性严重下降 |
| 宽泛异常捕获 `except Exception:` | **193 处** | 隐藏真实错误 |
| 不一致的单例模式 | **7 种** | 认知负担 |
| 硬编码魔法数字（重复出现） | **20+ 处** | 配置不灵活 |
| 废弃代码待清理 | **~100 处** | 代码膨胀 |
| `TYPE_CHECKING` 循环依赖守卫 | **59 个文件** | 耦合信号 |
| `type: ignore` 类型绕过 | **39 处** | 类型安全缺口 |
| `# noqa` 规则抑制 | **26 处** | 静态分析盲区 |
| 过长参数列表（6+ 参数） | **27+ 个函数** | 接口复杂 |
| TODO/FIXME/HACK | **0** | ✅ 清洁 |

---

## ✅ 架构亮点（保持不变）

以下设计模式应在重构中保留：

1. **Card Pipeline 单向数据流**: `dispatch → reduce → render → deliver`，层级清晰，禁止反向 import
2. **Engine 基类模式**: `BaseEngine` 提供良好的代码复用（Deep/Spec/Worktree）
3. **编程模式 Handler 的模板方法**: `ProgrammingModeHandler` 的配置驱动子类化（Coco/Claude/Aiden/Codex/Gemini），极少代码实现新模式
4. **Worktree 引擎**: 全项目最干净的模块，model/service 分离清晰
5. **事件驱动渲染**: `ACPEventRenderer` 处理 `ACPEvent` 流，驱动实时卡片更新
6. **SessionHook 协议**: 将 emoji/上下文持久化等副作用从渲染闭包中解耦
7. **多层锁排序**: 6 级层次结构 + 运行时死锁检测
8. **Circuit Breaker**: 滑动窗口故障计数，三态转换用于故障隔离
9. **UI 文案外部化**: 虽然当前是单体字典，但已具备 i18n 就绪基础
