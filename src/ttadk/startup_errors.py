"""TTADK 启动错误类型与预检查常量。

从 `manager.py` 提取，保持向后兼容（manager.py 会 re-export 所有符号）。
"""

from typing import Optional


class TTADKStartupError(RuntimeError):
    """TTADK 启动失败的可降级异常（携带上下文供上层做 fallback）。

    说明：该异常类型定义在 TTADK 层，避免在启动编排 SSOT 中依赖 `src.agent_session`。
    调用方可通过 `type(err).__name__ == "TTADKStartupError"` 进行稳定分类。
    """

    is_ghostap_error = True

    def __init__(
        self,
        message: str,
        *,
        tool_name: str = "",
        input_model: str = "",
        real_model: str = "",
        cause: Exception | None = None,
        # Startup failure diagnostics (best-effort; keep field names aligned with ACP diagnostics)
        agent_cmd: str = "",
        agent_args: Optional[list[str]] = None,
        returncode: Optional[int] = None,
        stdout_snippet: str = "",
        stderr_snippet: str = "",
        fail_reason: str = "",
    ) -> None:
        super().__init__(message)
        self.tool_name = tool_name
        self.input_model = input_model
        self.real_model = real_model
        self.__cause__ = cause

        # Keep names compatible with `src.acp.sync_adapter.build_startup_diagnostics`
        # so upper layers can extract cmd/args/rc/snippets without special-casing.
        try:
            self.agent_cmd = str(agent_cmd or "")
        except Exception:
            self.agent_cmd = ""
        try:
            self.agent_args = [str(x) for x in (agent_args or [])]
        except Exception:
            self.agent_args = []
        try:
            self.returncode = int(returncode) if returncode is not None else None
        except Exception:
            self.returncode = None
        try:
            self.stdout_snippet = str(stdout_snippet or "")
        except Exception:
            self.stdout_snippet = ""
        try:
            self.stderr_snippet = str(stderr_snippet or "")
        except Exception:
            self.stderr_snippet = ""
        try:
            self.fail_reason = str(fail_reason or "")
        except Exception:
            self.fail_reason = ""


# ---------------------------------------------------------------------------
# TTADK startup precheck contract
# ---------------------------------------------------------------------------
#
# 目标：收敛"启动阶段预校验/透传 model"的单一入口，避免 agent_session/acp/engine 多处实现漂移。
#
# 启动链路（SSOT）：
# - `precheck_ttadk_startup_model()`：决定"是否透传 -m"以及记录解析来源/告警
# - `coordinate_ttadk_startup()`：负责 start→invalid_model 闭环修复→retry→degrade，并产出 attempts
# - `src/acp/manager.py`：仅消费 coordinator 的稳定字段记录日志/诊断
#
# source 语义（稳定约定）：
# - 模型列表来源（ModelListResult.source）：cache / structured_sync / official_cli / probe / file_cache / local_config / defaults
# - 名称匹配来源（ResolvedModelResult.source）：exact / friendly / prefix / partial / unknown / fallback
#
# warnings 语义（稳定约定）：
# - models_untrusted：模型列表不可信，不允许用于 validated 透传 -m（例如 defaults 兜底、跨项目缓存、拉取失败等）
# - models_empty / models_error：模型列表为空/拉取失败，必须走 (auto)
# - low_confidence / source_cross_project：跨项目来源（例如 ~/.ttadk/models_cache.json），必须经更可信来源验证后才可透传
# - no_m_passthrough：显式标记"不应透传 -m"（用于上层日志/验收与 UI 提示）
#
# 输出字段（稳定契约）：
# - tool: str                 # ttadk tool 名（例如 codex）
# - input_model: str          # 用户输入/当前选择的 model 意图（可能是友好名/短名）
# - model: Optional[str]      # validated=True 时透传给 ttadk 的真实 model id；否则 None（表示 (auto)）
# - validated: bool           # 是否能确定 model 为真实可用 id
# - source: str               # 解析来源（cache/probe/structured/.../unknown/error）
# - decision: str             # precheck_validated / precheck_auto / precheck_error / non_ttadk
# - fail_phase: str           # precheck_error 或空字符串
# - warnings: list[str]       # 诊断提示

TTADK_PRECHECK_DECISIONS = {
    "precheck_validated",
    "precheck_auto",
    "precheck_error",
    "non_ttadk",
}

TTADK_PRECHECK_FAIL_PHASES = {
    "",
    "precheck_error",
}


# 统一日志模板：Engine/ACP 复用（保留关键前缀 `ttadk startup model` 以兼容 grep/监控）
TTADK_STARTUP_LOG_FMT = (
    "ttadk startup model: tool=%s input_model=%s model=%s validated=%s source=%s fail_phase=%s decision=%s warnings=%s"
)
TTADK_STARTUP_LOG_RESUME_FMT = "ttadk startup model(resume): tool=%s input_model=%s model=%s validated=%s source=%s fail_phase=%s decision=%s warnings=%s"
