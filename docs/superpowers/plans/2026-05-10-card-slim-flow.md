# 卡片精简 FLOW 实施计划

- **Spec**: `2026-05-10-card-slim-flow.md`
- **Date**: 2026-05-10

## Phase 0: 基础设施（atoms + renderer 注册）

1. `atoms.py` — AtomKind 新增 `"activity_digest"`
2. `renderer.py` — `_ATOM_RENDERERS` 新增 `activity_digest` renderer
3. `renderer.py` — `_BODY_ATOM_KINDS` 新增 `activity_digest` + `tool_panel`
4. 运行 `pytest -x -q` 确认 AtomKind ↔ _ATOM_RENDERERS 校验通过

## Phase 1: 实现 activity_digest 渲染 + flatten 改造

1. `tools.py` — 新增 `render_activity_digest_line(blocks)` 一行统计函数
2. `tools.py` — 新增 `render_active_tool_line(block)` 运行中工具一行
3. `atoms.py` — 重写 `flatten_to_atoms` 的 tool_call 处理：
   - completed/failed → 累积到 pending buffer
   - active → flush pending 为 activity_digest + 当前 active 为 tool_panel
   - 非 tool block → flush pending 为 activity_digest + dispatch
4. 运行测试

## Phase 2: 去除冗余

1. `acp/renderer.py` — 删除 `_format_tool_run_line` 在 text 中的注入
2. 运行测试确认不影响其他功能

## Phase 3: 新增测试 + 验证

1. 新增 `tests/test_activity_digest.py` 覆盖 spec §9 的 5 个测试用例
2. 运行全量测试
