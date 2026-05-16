import subprocess
from pathlib import Path
from typing import Optional

from ..env_sandbox import build_ttadk_subprocess_env
from ..models import (
    TTADKModel,
    parse_ttadk_models_from_output_to_models,
    truncate_snippet,
)
from .base import ModelFetchStrategy


class TTADKProjectMetaError(RuntimeError):
    """ProjectMetaModelsStrategy 失败时携带 stdout/stderr/rc/cmd 供上层 diagnostics 记录。"""

    def __init__(
        self,
        message: str,
        *,
        returncode: Optional[int] = None,
        stdout: str = "",
        stderr: str = "",
        cmd: Optional[list[str]] = None,
    ):
        super().__init__(message)
        self.returncode = returncode
        try:
            self.stdout = str(stdout or "")
        except Exception:
            self.stdout = ""
        try:
            self.stderr = str(stderr or "")
        except Exception:
            self.stderr = ""
        try:
            self.cmd = list(cmd or [])
        except Exception:
            self.cmd = []


class ProjectMetaModelsStrategy(ModelFetchStrategy):
    """从项目元数据（skills/plugin 等）中尝试提取真实模型列表（best-effort）。

    背景：TTADK 0.3.8 缺少 `models/model` 子命令，且部分环境 `ttadk sync` 需要先 init。
    该策略仅在检测到“项目已 init 的迹象”时启用，避免对未 init 项目产生额外子进程噪声。

    说明：该策略不保证一定能产出模型列表；失败时抛出可诊断异常，让上层记录 attempts。
    """

    def __init__(self, runner=None, timeout_s: float = 3.0):
        self._runner = runner
        try:
            self.timeout_s = float(timeout_s or 0) or 3.0
        except Exception:
            self.timeout_s = 3.0
        self._detail: dict = {}

    @property
    def name(self) -> str:
        return "project_meta"

    def get_warnings(self) -> list[str]:
        # 项目侧来源（若项目未 init，则 fetcher 会额外标记 ttadk_config_missing）
        return ["source_project"]

    def get_attempt_detail(self) -> dict:
        return dict(self._detail or {})

    def _project_initialized_hint(self, cwd: str) -> bool:
        try:
            base = Path(cwd)
            if (base / ".ttadk").exists():
                return True
            if (base / "ttadk.json").exists():
                return True
        except Exception:
            return False
        return False

    def fetch(self, tool_name: str, cwd: Optional[str] = None) -> list[TTADKModel]:
        # 没有 cwd / 未 init 项目：跳过该策略（不执行外部命令）
        if not cwd:
            return []
        if not self._project_initialized_hint(cwd):
            return []

        tool = (tool_name or "").strip().lower()
        if not tool:
            raise TTADKProjectMetaError("project_meta_missing_tool")

        # 当前最稳妥的可解析入口仍是 `ttadk skills read`（如果项目已 init）
        # 注意：该命令输出可能包含大量文本；只做 token 提取并依赖 is_model_token 过滤。
        cmd = ["ttadk", "skills", "read", "ttadk/common"]
        try:
            self._detail = {"raw_cmd": list(cmd), "cwd": str(cwd or ""), "scope": "project"}
        except Exception:
            self._detail = {}

        if self._runner:
            rc, out, err = self._runner(cmd, cwd, float(self.timeout_s or 3.0))
        else:
            env, _ = build_ttadk_subprocess_env(cwd=cwd or ".", agent_type="ttadk", tool_name=tool)
            p = subprocess.run(
                cmd, capture_output=True, text=True, timeout=float(self.timeout_s or 3.0), cwd=cwd, env=env
            )
            rc, out, err = int(getattr(p, "returncode", 0) or 0), (p.stdout or ""), (p.stderr or "")

        if int(rc or 0) != 0:
            raise TTADKProjectMetaError(
                f"project_meta_nonzero_exit: tool={tool}",
                returncode=int(rc or 0),
                stdout=truncate_snippet(out),
                stderr=truncate_snippet(err),
                cmd=list(cmd),
            )

        payload = ((out or "") + "\n" + (err or "")).strip()
        models = parse_ttadk_models_from_output_to_models(payload)
        if models:
            return list(models)

        raise TTADKProjectMetaError(
            f"project_meta_no_models: tool={tool}",
            returncode=int(rc or 0),
            stdout=truncate_snippet(out),
            stderr=truncate_snippet(err),
            cmd=list(cmd),
        )
