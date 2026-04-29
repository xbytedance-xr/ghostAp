import logging
import subprocess
import time
from typing import Optional

from .base import ModelFetchStrategy
from ..env_sandbox import build_ttadk_subprocess_env
from ..models import (
    TTADKModel,
    parse_ttadk_models_from_output_to_models,
    strip_ansi,
    truncate_snippet,
)

logger = logging.getLogger(__name__)


class TTADKOfficialCLIError(RuntimeError):
    """OfficialCLIModelsStrategy 失败时携带 stdout/stderr/rc 供上层 diagnostics 记录。"""

    def __init__(
        self,
        message: str,
        *,
        returncode: Optional[int] = None,
        stdout: str = "",
        stderr: str = "",
        phase: str = "",
        cmd: Optional[list[str]] = None,
    ):
        super().__init__(message)
        self.returncode = returncode
        # 诊断扩展字段：best-effort（不参与错误语义判断）
        try:
            self.phase = str(phase or "")
        except Exception:
            self.phase = ""
        try:
            self.cmd = list(cmd or [])
        except Exception:
            self.cmd = []
        try:
            self.stdout = str(stdout or "")
        except Exception:
            self.stdout = ""
        try:
            self.stderr = str(stderr or "")
        except Exception:
            self.stderr = ""


class TTADKModelsListStrategy(ModelFetchStrategy):
    """通过 ttadk 的“官方子命令”直接获取模型列表（优先 JSON，失败降级）。

    设计目标：减少对 `Invalid model` 错误输出解析的依赖。
    - 先做 capability probe（--help），命中后才执行实际 list 命令
    - 支持 JSON / 文本两类输出
    - best-effort：失败返回空列表，让 fetcher 继续后续策略
    """

    def __init__(self, runner=None, timeout_s: float = 4.0, probe_ttl_s: float = 300.0):
        # runner: Callable[[list[str], Optional[str], float], tuple[int,str,str]]
        self._runner = runner
        try:
            self.timeout_s = float(timeout_s or 0) or 4.0
        except Exception:
            self.timeout_s = 4.0
        try:
            self._probe_ttl_s = float(probe_ttl_s or 0) or 300.0
        except Exception:
            self._probe_ttl_s = 300.0

        # 缓存：tool -> (ok, chosen_cmd_prefix, ts)
        self._probe_cache: dict[str, tuple[bool, list[str], float]] = {}

        # best-effort：供上层 diagnostics 采集的 detail
        self._detail: dict = {}

    @property
    def name(self) -> str:
        return "official_cli"

    def get_attempt_detail(self) -> dict:
        return dict(self._detail or {})

    def _reset_detail(self) -> None:
        self._detail = {}

    def _run(self, args: list[str], cwd: Optional[str], timeout: float) -> tuple[int, str, str]:
        # best-effort：为 diagnostics 记录最后一次 raw_cmd/cwd
        try:
            self._detail["raw_cmd"] = list(args)
            self._detail["cwd"] = str(cwd or "")
        except Exception:
            logger.debug("_run: convert to list", exc_info=True)
        if self._runner:
            return self._runner(args, cwd, timeout)
        env, _ = build_ttadk_subprocess_env(cwd=cwd or ".", agent_type="ttadk", tool_name="")
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=env)
        return int(getattr(p, "returncode", 0) or 0), (p.stdout or ""), (p.stderr or "")

    def _probe_candidates(self) -> list[list[str]]:
        # NOTE: 候选集保持保守，避免误触发交互。只探测 --help。
        return [
            ["ttadk", "models", "--help"],
            ["ttadk", "models", "list", "--help"],
            ["ttadk", "model", "--help"],
            ["ttadk", "model", "list", "--help"],
        ]

    def _list_candidates(self, tool_name: str, *, cmd_prefix: Optional[list[str]] = None) -> list[list[str]]:
        """生成 list 命令候选。

        - 若 probe 已确认某个子命令前缀（models 或 model），优先只尝试该分支，减少无效调用。
        - 优先 JSON（-f json），失败回退文本解析。
        """
        tool = (tool_name or "").strip()

        preferred_subcmd: str | None = None
        try:
            if cmd_prefix and len(cmd_prefix) >= 2 and str(cmd_prefix[0]).strip().lower() == "ttadk":
                preferred_subcmd = str(cmd_prefix[1]).strip().lower() or None
        except Exception:
            preferred_subcmd = None

        subcmds: list[str] = []
        if preferred_subcmd in ("models", "model"):
            subcmds = [preferred_subcmd]
        else:
            subcmds = ["models", "model"]

        cands: list[list[str]] = []
        for sub in subcmds:
            # 优先 JSON，其次文本；均尽量避免触发下游 tool。
            # 覆盖常见参数差异：-t / --tool
            cands.append(["ttadk", sub, "list", "-t", tool, "-f", "json"])
            cands.append(["ttadk", sub, "list", "--tool", tool, "-f", "json"])
            cands.append(["ttadk", sub, "list", "-t", tool])
            cands.append(["ttadk", sub, "list", "--tool", tool])

            # 某些版本可能没有 list 子命令，或用 `ttadk models -t ...` 直接列出
            cands.append(["ttadk", sub, "-t", tool, "-f", "json"])
            cands.append(["ttadk", sub, "--tool", tool, "-f", "json"])
            cands.append(["ttadk", sub, "-t", tool])
            cands.append(["ttadk", sub, "--tool", tool])

            # fallback: 某些版本可能不需要 tool 参数或参数不同；保留无 tool 版本（低优先级）
            cands.append(["ttadk", sub, "list", "-f", "json"])
            cands.append(["ttadk", sub, "list"])
            cands.append(["ttadk", sub, "-f", "json"])
            cands.append(["ttadk", sub])

        # 去重（保序）
        seen: set[str] = set()
        uniq: list[list[str]] = []
        for cmd in cands:
            key = "\x00".join([str(x) for x in cmd])
            if key in seen:
                continue
            seen.add(key)
            uniq.append(cmd)
        return uniq

    def _probe(self, tool_name: str, cwd: Optional[str]) -> tuple[bool, list[str], list[str], dict]:
        tool = (tool_name or "").strip().lower()
        now = time.time()
        cached = self._probe_cache.get(tool)
        if cached:
            ok, cmd_prefix, ts = cached
            if (now - ts) < float(self._probe_ttl_s or 0):
                return bool(ok), list(cmd_prefix), [], {}

        warnings: list[str] = []
        last: dict = {}
        for cmd in self._probe_candidates():
            try:
                rc, out, err = self._run(cmd, cwd=cwd, timeout=min(2.0, float(self.timeout_s or 4.0)))
                last = {
                    "cmd": list(cmd),
                    "rc": int(rc or 0),
                    "stdout": truncate_snippet(out),
                    "stderr": truncate_snippet(err),
                }
                blob = strip_ansi((out or "") + "\n" + (err or "")).lower()

                # 注意：部分版本在未知子命令时也会返回 rc=0 并输出顶层 help。
                # 因此这里必须校验“usage 行是否包含目标子命令前缀”，避免把顶层 help 误判为 capability。
                if rc == 0 and ("usage" in blob or "help" in blob or "commands" in blob):
                    # 期望：Usage: ttadk <subcmd> ...（或 ttadk <subcmd> <subsubcmd> ...）
                    # cmd 至少包含 [ttadk, <subcmd>, ...]
                    expected = "usage: ttadk " + (cmd[1] if len(cmd) >= 2 else "")
                    expected2 = "usage: ttadk " + " ".join(cmd[1:3]) if len(cmd) >= 3 else ""
                    if expected.strip() and expected in blob:
                        self._probe_cache[tool] = (True, cmd[:2], now)
                        return True, cmd[:2], warnings, last
                    if expected2.strip() and expected2 in blob:
                        self._probe_cache[tool] = (True, cmd[:2], now)
                        return True, cmd[:2], warnings, last
            except Exception as e:
                warnings.append(f"probe_error:{type(e).__name__}")
                continue

        self._probe_cache[tool] = (False, [], now)
        return False, [], warnings, last

    def fetch(self, tool_name: str, cwd: Optional[str] = None) -> list[TTADKModel]:
        self._reset_detail()
        tool = (tool_name or "").strip().lower()
        if not tool:
            return []

        ok, cmd_prefix, probe_warnings, probe_last = self._probe(tool, cwd)
        if not ok:
            # 该策略“不可用/不支持”也应在 diagnostics 中可见，避免 fetcher 侧出现无 error_type 的静默回退。
            raise TTADKOfficialCLIError(
                f"official_cli_probe_failed: tool={tool}",
                returncode=probe_last.get("rc"),
                stdout=str(probe_last.get("stdout") or ""),
                stderr=truncate_snippet(";".join(probe_warnings) or str(probe_last.get("stderr") or "")),
                phase="probe",
                cmd=list(probe_last.get("cmd") or []),
            )

        last_stdout = ""
        last_stderr = ""
        last_rc: Optional[int] = None
        last_cmd: list[str] = []
        timeouts = 0
        errors = 0
        ran = 0

        for cmd in self._list_candidates(tool, cmd_prefix=cmd_prefix):
            try:
                ran += 1
                last_cmd = list(cmd)
                rc, out, err = self._run(cmd, cwd=cwd, timeout=float(self.timeout_s or 4.0))
            except subprocess.TimeoutExpired:
                timeouts += 1
                continue
            except Exception:
                errors += 1
                continue

            last_rc, last_stdout, last_stderr = int(rc or 0), (out or ""), (err or "")
            if int(rc or 0) != 0:
                continue

            payload = (out or "").strip() or (err or "").strip()
            if not payload:
                continue

            argv: list[str] = []
            is_json_cmd = False
            try:
                argv = [str(x) for x in (cmd or [])]
                is_json_cmd = ("-f" in argv) and ("json" in argv)
            except Exception:
                argv = []
                is_json_cmd = False

            # 统一解析入口（SSOT）：优先 Invalid-model，其次 JSON，再次文本 token 提取。
            models = parse_ttadk_models_from_output_to_models(payload)
            if models:
                phase = "json" if is_json_cmd else "text"
                try:
                    self._detail = {"phase": phase, "cmd": list(cmd)}
                except Exception:
                    self._detail = {}
                return list(models)

        # 失败：优先抛出可诊断异常供 fetcher 记录（避免“ok=False 但无 error_type/snippet”的静默回退）
        blob = (last_stdout or "") + "\n" + (last_stderr or "")
        blob = blob.strip()
        if blob:
            # 1) 命令确实运行且有输出：要么解析失败，要么非 0 退出
            msg = (
                "official_cli_nonzero_exit" if (last_rc is not None and int(last_rc) != 0) else "official_cli_no_models"
            )
            try:
                phase = (
                    "json"
                    if ("-f" in [str(x) for x in (last_cmd or [])] and "json" in [str(x) for x in (last_cmd or [])])
                    else "text"
                )
            except Exception:
                phase = ""
            raise TTADKOfficialCLIError(
                f"{msg}: tool={tool}",
                returncode=last_rc,
                stdout=truncate_snippet(last_stdout),
                stderr=truncate_snippet(last_stderr),
                phase=phase,
                cmd=list(last_cmd or []),
            )
        if ran and (timeouts or errors):
            # 2) 全部超时/异常：也应可诊断
            raise TTADKOfficialCLIError(
                f"official_cli_unstable: tool={tool} timeouts={timeouts} errors={errors}",
                returncode=None,
                stdout="",
                stderr="",
                phase="list",
                cmd=list(last_cmd or []),
            )
        # 3) 极端情况：没有任何输出也没有异常（或 runner 返回空）— 也应可诊断，避免静默回退
        raise TTADKOfficialCLIError(
            f"official_cli_empty_output: tool={tool}",
            returncode=last_rc,
            stdout=truncate_snippet(last_stdout),
            stderr=truncate_snippet(last_stderr),
            phase="list",
            cmd=list(last_cmd or []),
        )


# Backward-compatible alias (historical name)
OfficialCLIModelsStrategy = TTADKModelsListStrategy
