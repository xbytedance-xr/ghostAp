import json
import re
from pathlib import Path
from typing import Optional

from .base import ModelFetchStrategy
from ...utils.errors import get_error_detail
from ..models import (
    TTADKModel,
    is_model_token,
    truncate_snippet,
)


class TTADKLocalConfigError(RuntimeError):
    """LocalConfigModelsStrategy 失败时携带上下文，便于上层 diagnostics 记录。"""

    def __init__(
        self,
        message: str,
        *,
        file_path: str = "",
        returncode: Optional[int] = None,
        stdout: str = "",
        stderr: str = "",
    ):
        super().__init__(message)
        self.file_path = str(file_path or "")
        self.returncode = returncode
        try:
            self.stdout = str(stdout or "")
        except Exception:
            self.stdout = ""
        try:
            self.stderr = str(stderr or "")
        except Exception:
            self.stderr = ""


class LocalConfigModelsStrategy(ModelFetchStrategy):
    """从本地文件/配置中提取真实模型列表（不执行外部命令）。

    设计目标：在 `Available models` 为空且无 `models/model` 子命令的版本中，提供一个可落地的真实来源。
    - 优先项目侧（cwd）候选文件
    - 再尝试用户侧（~/.ttadk）候选文件（跨项目，低可信）
    """

    def __init__(self, *, max_bytes: int = 256 * 1024):
        self._max_bytes = int(max_bytes or 0) if int(max_bytes or 0) > 0 else 256 * 1024
        self._warnings: list[str] = []
        self._detail: dict = {}

        # 候选文件（项目侧）
        self._project_candidates = [
            ".ttadk/setting.json",
            ".ttadk/settings.json",
            ".ttadk/config.json",
            ".ttadk/project.json",
            "ttadk.json",
        ]

        # 候选文件（用户侧）
        self._home_candidates = [
            ".ttadk/setting.json",
            ".ttadk/config.json",
            ".ttadk/project.json",
        ]

    @property
    def name(self) -> str:
        return "local_config"

    def get_warnings(self) -> list[str]:
        return list(self._warnings)

    def get_attempt_detail(self) -> dict:
        return dict(self._detail)

    def _reset_diag(self) -> None:
        self._warnings = []
        self._detail = {}

    def _safe_path_hint(self, p: Path) -> str:
        # 仅输出文件名，避免泄露绝对路径
        try:
            return str(p.name)
        except Exception:
            return "(unknown)"

    def _dedupe(self, items: list[str]) -> list[str]:
        seen = set()
        out: list[str] = []
        for x in items:
            s = str(x or "").strip()
            if not s:
                continue
            if s in seen:
                continue
            seen.add(s)
            out.append(s)
        return out

    def _extract_tokens_from_text(self, text: str) -> list[str]:
        # 从文本中提取疑似 token，并用 is_model_token 过滤
        tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9_.:\-]{2,128}", text or "")
        return self._dedupe([t for t in tokens if is_model_token(t)])

    def _try_one_file(self, path: Path) -> list[str]:
        try:
            if not path.exists():
                return []
            st = path.stat()
            if st.st_size and int(st.st_size) > int(self._max_bytes):
                raise TTADKLocalConfigError("local_config_too_large", file_path=self._safe_path_hint(path))
            raw = path.read_text(encoding="utf-8", errors="ignore")
        except TTADKLocalConfigError:
            raise
        except Exception as e:
            raise TTADKLocalConfigError(
                f"local_config_read_failed:{type(e).__name__}",
                file_path=self._safe_path_hint(path),
                stderr=truncate_snippet(get_error_detail(e)),
            )

        text = (raw or "").strip()
        if not text:
            return []

        # JSON 解析
        if text.lstrip().startswith(("{", "[")):
            try:
                json.loads(text)
                # 当前策略不解析 models_cache.json（由 FileCacheStrategy 统一处理）。
                return self._extract_tokens_from_text(text)
            except Exception:
                # 回退文本提取
                return self._extract_tokens_from_text(text)

        return self._extract_tokens_from_text(text)

    def fetch(self, tool_name: str, cwd: Optional[str] = None) -> list[TTADKModel]:
        self._reset_diag()
        tool = (tool_name or "").strip().lower()
        if not tool:
            raise TTADKLocalConfigError("local_config_missing_tool")

        # best-effort：记录 cwd（由上层决定是否输出/脱敏）
        try:
            self._detail["cwd"] = str(cwd or "")
        except Exception:
            pass

        candidates: list[tuple[Path, str]] = []
        if cwd:
            base = Path(cwd)
            for rel in self._project_candidates:
                candidates.append((base / rel, "project"))
        home = Path.home()
        for rel in self._home_candidates:
            candidates.append((home / rel, "home"))

        file_tried = 0
        for p, scope in candidates:
            file_tried += 1
            try:
                names = self._try_one_file(p)
            except TTADKLocalConfigError:
                # 继续下一个候选
                continue
            if not names:
                continue

            # 标注可信度
            if scope == "home":
                self._warnings.extend(["source_cross_project", "low_confidence"])
            else:
                self._warnings.append("source_project")

            self._detail = {
                "file_hit": self._safe_path_hint(p),
                "scope": scope,
                "count": len(names),
            }
            models = [TTADKModel(name=n, description=n, friendly_name=n) for n in names]
            if models:
                return models

        self._detail = {"files_tried": min(file_tried, 32)}
        raise TTADKLocalConfigError("local_config_all_candidates_failed")
