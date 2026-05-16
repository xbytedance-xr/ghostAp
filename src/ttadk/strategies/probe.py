import logging
import os
import pty
import re
import select
import subprocess
import time
from typing import Optional

from ...utils.errors import get_error_detail
from ..env_sandbox import build_ttadk_subprocess_env
from ..models import (
    TTADKModel,
    extract_available_models,
    is_invalid_model_error,
    truncate_snippet,
)
from .base import ModelFetchStrategy

logger = logging.getLogger(__name__)

_TTY_ERROR_RE = re.compile(
    r"could\s+not\s+open\s+a\s+new\s+tty|/dev/tty|no\s+such\s+device\s+or\s+address", re.IGNORECASE
)
_PANIC_RE = re.compile(r"program\s+experienced\s+a\s+panic|\bpanic\b", re.IGNORECASE)


class TTADKProbeError(RuntimeError):
    """Probe 失败时携带 stdout/stderr/rc，便于上层 diagnostics 记录。"""

    def __init__(self, message: str, *, returncode: Optional[int] = None, stdout: str = "", stderr: str = ""):
        super().__init__(message)
        self.returncode = returncode
        # 防御性：单测里经常用 MagicMock 模拟 subprocess 结果，stdout/stderr 可能不是 str
        try:
            self.stdout = str(stdout or "")
        except Exception:
            self.stdout = ""
        try:
            self.stderr = str(stderr or "")
        except Exception:
            self.stderr = ""


class ProbeStrategy(ModelFetchStrategy):
    """
    异常探测策略 (Strategy A)
    通过执行带无效模型参数的命令 (ttadk code -t <tool> -m INVALID_PROBE)
    捕获错误输出中暴露的 Available models 列表。
    """

    def __init__(self, runner=None, timeout_s: float = 10.0):
        # runner: Callable[[list[str], Optional[str], float], tuple[int,str,str]]
        self._runner = runner
        try:
            self.timeout_s = float(timeout_s or 0) or 10.0
        except Exception:
            self.timeout_s = 10.0

        # best-effort：供上层 diagnostics 采集的 detail
        self._detail: dict = {}

    @property
    def name(self) -> str:
        return "probe"

    def get_attempt_detail(self) -> dict:
        return dict(self._detail or {})

    def _reset_detail(self) -> None:
        self._detail = {}

    def _run(self, args: list[str], cwd: Optional[str], timeout: float) -> tuple[int, str, str]:
        if self._runner:
            return self._runner(args, cwd, timeout)
        env, _ = build_ttadk_subprocess_env(cwd=cwd or ".", agent_type="ttadk", tool_name="")
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=env)
        return p.returncode, p.stdout or "", p.stderr or ""

    def _run_with_pty(self, args: list[str], cwd: Optional[str], timeout: float) -> tuple[int, str, str]:
        """用 PTY 启动子进程，避免部分下游 tool 依赖 /dev/tty 导致失败。

        注意：PTY 模式 stdout/stderr 会合并输出到同一 stream（master fd）。
        """
        master_fd, slave_fd = pty.openpty()
        try:
            env, _ = build_ttadk_subprocess_env(cwd=cwd or ".", agent_type="ttadk", tool_name="")
            try:
                env.setdefault("TERM", env.get("TERM") or "xterm-256color")
            except Exception:
                logger.debug("_run_with_pty: set default value", exc_info=True)
            p = subprocess.Popen(
                args,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=cwd,
                close_fds=True,
                env=env,
            )
        finally:
            try:
                os.close(slave_fd)
            except OSError:
                logger.debug("_run_with_pty: close fd", exc_info=True)

        buf = bytearray()
        start = time.time()
        try:
            while True:
                if timeout and (time.time() - start) > float(timeout):
                    try:
                        p.terminate()
                    except Exception:
                        logger.debug("_run_with_pty: p.terminate()", exc_info=True)
                    raise subprocess.TimeoutExpired(args=args, timeout=timeout)

                r, _, _ = select.select([master_fd], [], [], 0.2)
                if master_fd in r:
                    try:
                        chunk = os.read(master_fd, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    buf.extend(chunk)

                if p.poll() is not None:
                    # drain remaining
                    try:
                        while True:
                            r, _, _ = select.select([master_fd], [], [], 0)
                            if master_fd not in r:
                                break
                            chunk = os.read(master_fd, 4096)
                            if not chunk:
                                break
                            buf.extend(chunk)
                    except Exception:
                        logger.debug("while True:", exc_info=True)
                    break
        finally:
            try:
                os.close(master_fd)
            except OSError:
                logger.debug("close fd", exc_info=True)

        text = buf.decode(errors="ignore")
        # PTY 下 stdout/stderr 无法区分，统一放入 stderr 以便错误侧解析
        return p.returncode if p.returncode is not None else 0, "", text

    def _run_auto(self, args: list[str], cwd: Optional[str], timeout: float) -> tuple[int, str, str]:
        """优先非 PTY，遇到 TTY 相关失败或明显 panic 时回退 PTY。"""
        rc, out, err = self._run(args, cwd=cwd, timeout=timeout)
        # 防御：stdout/stderr 可能是 MagicMock（单测）或其他类型，必须强制转为 str
        output = f"{out or ''}\n{err or ''}"
        if self._runner:
            return rc, out, err
        if _TTY_ERROR_RE.search(output) or _PANIC_RE.search(output):
            logger.debug("ProbeStrategy detected tty/panic output, retry with PTY: %s", " ".join(args))
            try:
                self._detail["pty"] = True
            except Exception:
                logger.debug("_run_auto: True", exc_info=True)
            return self._run_with_pty(args, cwd=cwd, timeout=timeout)
        return rc, out, err

    def fetch(self, tool_name: str, cwd: Optional[str] = None) -> list[TTADKModel]:
        out: str = ""
        err: str = ""
        rc: Optional[int] = None
        try:
            self._reset_detail()
            # 构造命令：使用不存在的模型名称触发错误提示
            # 说明：probe 目标是触发 TTADK 自身的 Invalid model 校验（通常在启动下游 tool 前就发生）。
            # 因此这里不强依赖 "-a acp serve"，避免对不支持 ACP 的 tool 造成额外噪声。
            cmd = [
                "ttadk",
                "code",
                "-t",
                tool_name,
                "-m",
                "INVALID_PROBE_FOR_DISCOVERY",
            ]

            # detail：尽量记录 raw_cmd/cwd/pty（供上层 attempts 结构化）
            try:
                self._detail = {
                    "raw_cmd": list(cmd),
                    "cwd": str(cwd or ""),
                    "pty": False,
                }
            except Exception:
                self._detail = {}

            logger.debug(f"Executing probe command: {' '.join(cmd)}")

            # 执行命令并捕获输出
            # 注意：ttadk 的错误信息通常输出到 stderr，但也可能在 stdout。
            # 该 probe 的目标是触发 TTADK 自身的 Invalid model 校验（通常在启动下游 tool 前发生）。
            # 因此这里不再追加 `-a acp serve`，避免对部分 tool 引入额外交互/阻塞风险。
            try:
                rc, out, err = self._run_auto(cmd, cwd=cwd, timeout=self.timeout_s)
                output = (out or "") + "\n" + (err or "")
            except subprocess.TimeoutExpired:
                # 超时兜底：在某些环境中 subprocess 可能因无 TTY/IO 阻塞而超时，尝试一次强制 PTY。
                if self._runner:
                    raise
                logger.debug("ProbeStrategy timeout, retry with PTY: %s", " ".join(cmd))
                try:
                    self._detail["pty"] = True
                except Exception:
                    logger.debug("fetch: True", exc_info=True)
                rc, out, err = self._run_with_pty(cmd, cwd=cwd, timeout=self.timeout_s)
                output = (out or "") + "\n" + (err or "")

            logger.debug("Probe output length: %d", len(output or ""))

            # 仅在识别到 Invalid model 错误时解析 Available models
            if not is_invalid_model_error(output):
                logger.warning(
                    "ProbeStrategy: output is not an invalid model error for tool=%s (rc=%s)",
                    tool_name,
                    rc,
                )
                raise TTADKProbeError(
                    f"probe_output_not_invalid_model: tool={tool_name}",
                    returncode=rc,
                    stdout=out,
                    stderr=err,
                )

            names = extract_available_models(output)
            models = [TTADKModel(name=n, description=n, friendly_name=n) for n in names]

            if models:
                logger.info(f"ProbeStrategy found {len(models)} models for {tool_name}")
                return models

            # Invalid model 已命中，但 Available models 为空：记录为可诊断失败
            raise TTADKProbeError(
                f"available_models_empty: tool={tool_name}",
                returncode=rc,
                stdout=out,
                stderr=err,
            )

        except subprocess.TimeoutExpired as e:
            # 让上层 diagnostics 记录 timeout，而不是静默吞掉
            logger.info(f"ProbeStrategy timed out for tool {tool_name}: {get_error_detail(e)}")
            raise
        except TTADKProbeError:
            # 将可诊断错误上抛给 fetcher，便于记录 stderr/stdout 片段
            raise
        except Exception as e:
            # 统一上抛可诊断错误，避免 fetcher 侧出现“ok=False 但无 error_type/rc/snippet”的静默回退。
            logger.warning(f"ProbeStrategy failed for tool {tool_name}: {get_error_detail(e)}")
            raise TTADKProbeError(
                f"probe_exception:{type(e).__name__}: tool={tool_name}",
                returncode=rc,
                stdout=truncate_snippet(out),
                stderr=truncate_snippet(err or get_error_detail(e)),
            )
