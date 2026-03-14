import abc
import logging
import re
import subprocess
import os
import pty
import struct
import fcntl
import termios
import select
import time
import json
from pathlib import Path
from typing import Optional

from .env_sandbox import build_ttadk_subprocess_env

from .models import TTADKModel
from .models import is_invalid_model_error, extract_available_models
from .models import truncate_snippet, strip_ansi, is_model_token
from .models import parse_ttadk_models_from_output
from .models import parse_ttadk_models_from_output_to_models

logger = logging.getLogger(__name__)


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


_TTY_ERROR_RE = re.compile(r"could\s+not\s+open\s+a\s+new\s+tty|/dev/tty|no\s+such\s+device\s+or\s+address", re.IGNORECASE)
_PANIC_RE = re.compile(r"program\s+experienced\s+a\s+panic|\bpanic\b", re.IGNORECASE)

class ModelFetchStrategy(abc.ABC):
    """模型获取策略基类"""
    
    @abc.abstractmethod
    def fetch(self, tool_name: str, cwd: Optional[str] = None) -> list[TTADKModel]:
        """获取指定工具的模型列表"""
        pass

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """策略名称"""
        pass


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
                pass
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
                pass

        buf = bytearray()
        start = time.time()
        try:
            while True:
                if timeout and (time.time() - start) > float(timeout):
                    try:
                        p.terminate()
                    except Exception:
                        pass
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
                        pass
                    break
        finally:
            try:
                os.close(master_fd)
            except OSError:
                pass

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
                pass
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
                    pass
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
            logger.warning(f"ProbeStrategy timed out for tool {tool_name}: {e}")
            raise
        except TTADKProbeError:
            # 将可诊断错误上抛给 fetcher，便于记录 stderr/stdout 片段
            raise
        except Exception as e:
            # 统一上抛可诊断错误，避免 fetcher 侧出现“ok=False 但无 error_type/rc/snippet”的静默回退。
            logger.warning(f"ProbeStrategy failed for tool {tool_name}: {e}")
            raise TTADKProbeError(
                f"probe_exception:{type(e).__name__}: tool={tool_name}",
                returncode=rc,
                stdout=truncate_snippet(out),
                stderr=truncate_snippet(err or str(e) or ""),
            )


    # 旧的正则/ANSI 处理已下沉到 models.py（extract_available_models / is_invalid_model_error）


class InteractiveStrategy(ModelFetchStrategy):
    """
    交互解析策略 (Strategy B)
    使用 PTY 模拟终端交互，解析菜单项并逐一进入详情页获取真实模型名称。
    作为 ProbeStrategy 失败后的兜底方案。
    """

    # 模型选择界面提示
    MODEL_SELECTION_PROMPT = "Select a model:"
    
    # 模型名称提取正则（从工具界面）
    MODEL_NAME_PATTERN = re.compile(r'model:\s*([^\s]+)')

    # 移除 ANSI 颜色码
    ANSI_ESCAPE = re.compile(r'\x1b\[[\?0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b[()][AB012]')

    @property
    def name(self) -> str:
        return "interactive"

    def fetch(self, tool_name: str, cwd: Optional[str] = None) -> list[TTADKModel]:
        # Default OFF: interactive mode (pty + fork) is risky in multi-threaded services.
        # Enable only when explicitly set.
        try:
            from ..config import get_settings

            if not bool(getattr(get_settings(), "ttadk_interactive_enabled", False)):
                return []
        except Exception:
            return []

        logger.info(f"InteractiveStrategy starting for tool {tool_name}")
        master, slave = None, None
        proc: Optional[subprocess.Popen] = None
        try:
            # 创建 pty 并设置终端大小
            master, slave = pty.openpty()
            winsize = struct.pack('HHHH', 40, 120, 0, 0)
            fcntl.ioctl(master, termios.TIOCSWINSZ, winsize)

            # 启动 ttadk code 进程（使用 subprocess + PTY，避免 fork 在多线程服务中的风险）
            env, _ = build_ttadk_subprocess_env(cwd=cwd or ".", agent_type="ttadk", tool_name=tool_name)
            try:
                env.setdefault("TERM", env.get("TERM") or "xterm-256color")
            except Exception:
                pass
            cmd = ["ttadk", "code", "-t", tool_name]
            proc = subprocess.Popen(
                cmd,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                cwd=cwd or None,
                env=env,
                close_fds=True,
                start_new_session=True,  # 便于 kill 进程组，避免遗留子进程
            )
            os.close(slave)
            slave = None

            # 读取输出直到出现模型选择界面
            output = self._read_until_prompt(master, self.MODEL_SELECTION_PROMPT, timeout=10)

            if not output:
                logger.warning(f"InteractiveStrategy: No output from ttadk for tool {tool_name}")
                return []

            # 解析模型选择界面
            friendly_names = self._parse_model_selection_menu(output)
            if not friendly_names:
                logger.warning(f"InteractiveStrategy: No models found in selection menu for tool {tool_name}")
                return []

            logger.debug(f"InteractiveStrategy found friendly names for {tool_name}: {friendly_names}")

            # 依次选择每个模型获取真实名称（避免模型过多导致 O(n) 过慢/不稳定）
            max_models = int(os.getenv("TTADK_INTERACTIVE_MAX_MODELS", "12") or 12)
            max_models = max(1, min(max_models, 50))
            if len(friendly_names) > max_models:
                logger.warning(
                    "InteractiveStrategy: too many models (%d), will only probe first %d",
                    len(friendly_names),
                    max_models,
                )
                friendly_names = friendly_names[:max_models]

            total_timeout = float(os.getenv("TTADK_INTERACTIVE_TOTAL_TIMEOUT", "20") or 20)
            total_timeout = max(5.0, min(total_timeout, 120.0))
            started_at = time.time()

            models: list[TTADKModel] = []
            # 交互选择的“位置移动”容易漂移（不同版本 UI 行为不同）。
            # 这里采用稳健策略：每次选择当前高亮项，返回后仅下移 1 行进入下一项。
            for idx, friendly_name in enumerate(friendly_names):
                if (time.time() - started_at) > total_timeout:
                    logger.warning(
                        "InteractiveStrategy: total timeout reached (%.1fs), stop probing remaining models",
                        total_timeout,
                    )
                    break

                # 选择当前项
                real_name = self._select_and_extract_current_model(master, timeout=6)
                if real_name:
                    models.append(
                        TTADKModel(
                            name=real_name,
                            description=friendly_name,
                            friendly_name=friendly_name,
                            is_default=(idx == 0),
                        )
                    )

                # 回到菜单后，下移到下一项（最后一项无需移动）
                if idx < len(friendly_names) - 1:
                    try:
                        os.write(master, b"\x1b[B")  # Down arrow
                        time.sleep(0.05)
                    except Exception:
                        pass

            logger.info(f"InteractiveStrategy successfully fetched {len(models)} models for {tool_name}")
            return models

        except Exception as e:
            logger.warning(f"InteractiveStrategy failed for tool {tool_name}: {e}")
            return []
        finally:
            # 清理
            if master is not None:
                try:
                    os.close(master)
                except Exception:
                    pass
            if slave is not None:
                try:
                    os.close(slave)
                except Exception:
                    pass

            # 确保子进程被正确清理
            if proc is not None:
                try:
                    import signal

                    # 先尝试温和退出
                    try:
                        proc.terminate()
                    except Exception:
                        pass

                    # 再杀整个进程组，避免遗留子进程
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except Exception:
                        pass

                    try:
                        proc.wait(timeout=1.2)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except Exception:
                            pass
                        try:
                            proc.wait(timeout=1.2)
                        except Exception:
                            pass
                except Exception as e:
                    logger.warning(f"Failed to cleanup ttadk interactive process {getattr(proc, 'pid', None)}: {e}")


    def _select_and_extract_current_model(self, fd: int, timeout: float = 6) -> Optional[str]:
        """选择当前高亮项并提取 real model id，然后返回菜单。"""
        try:
            # Enter 选择
            os.write(fd, b"\r")
            time.sleep(0.1)

            output = self._read_until_model_display(fd, timeout=timeout)
            real_name = self._extract_real_model_name(output or "")

            # Escape 返回菜单
            os.write(fd, b"\x1b")
            time.sleep(0.1)
            return real_name
        except Exception:
            return None

    def _read_until_prompt(self, fd: int, prompt: str, timeout: float = 10) -> str:
        """读取输出直到出现指定提示或超时"""
        output = ""
        start_time = time.time()

        while time.time() - start_time < timeout:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(fd, 4096)
                    if chunk:
                        output += chunk.decode("utf-8", errors="ignore")
                        if prompt in output:
                            return output
                except Exception:
                    break
        return output

    def _strip_ansi(self, text: str) -> str:
        """移除 ANSI 颜色码"""
        return self.ANSI_ESCAPE.sub('', text)

    def _parse_model_selection_menu(self, output: str) -> list[str]:
        """
        解析模型选择界面的友好名称
        """
        names: list[str] = []

        # 清理 ANSI 颜色码
        clean_output = self._strip_ansi(output)

        # 查找 "Select a model:" 之后的内容
        lines = clean_output.split('\n')
        in_menu = False

        for line in lines:
            stripped = line.strip()
            if self.MODEL_SELECTION_PROMPT in stripped or "? Select a model:" in stripped:
                in_menu = True
                continue

            if in_menu:
                # 检查是否已经离开菜单区域（遇到空行或其他提示）
                if not stripped or stripped.startswith('?') or stripped.startswith('Press'):
                    # 跳过空行和提示行，但不结束菜单解析
                    if not stripped:
                        continue
                    # 遇到新的提示符，结束菜单解析
                    if stripped.startswith('?'):
                        break

                # 匹配菜单项：以 ❯ 或空格开头
                # 格式: "❯ GPT 5.2 Codex (Recommended)" 或 "  GPT 4.1 Codex"
                if stripped.startswith('❯'):
                    # 提取模型名称
                    name = stripped.lstrip('❯').strip()
                    if name and not name.startswith('('):
                        names.append(name)
                elif stripped and not stripped.startswith('('):
                    # 普通菜单项
                    if stripped: # 确保不是空字符串
                         names.append(stripped)

        return names

    def _select_and_extract_model_name(
        self,
        fd: int,
        model_index: int,
        total_models: int,
        friendly_name: str,
    ) -> Optional[str]:
        """
        选择指定模型并提取真实模型名称
        """
        try:
            # 移动到目标模型位置
            # 第一个模型已经是选中状态，不需要移动
            for _ in range(model_index):
                os.write(fd, b'\x1b[B')  # 下箭头
                time.sleep(0.05)

            # 按 Enter 选择
            os.write(fd, b'\r')
            time.sleep(0.1)

            # 读取输出直到进入工具界面
            output = self._read_until_model_display(fd, timeout=5)

            if not output:
                return None

            # 提取真实模型名称
            real_name = self._extract_real_model_name(output)
            if real_name:
                logger.debug("Model '%s' -> '%s'", friendly_name, real_name)

            # 按 Escape 返回模型选择界面（如果还有更多模型需要获取）
            if model_index < total_models - 1:
                os.write(fd, b'\x1b')  # Escape
                time.sleep(0.1)

            return real_name

        except Exception as e:
            logger.debug("Failed to extract model name for index %d: %s", model_index, e)
            return None

    def _read_until_model_display(self, fd: int, timeout: float = 5) -> str:
        """读取输出直到出现模型名称显示或超时"""
        output = ""
        start_time = time.time()

        while time.time() - start_time < timeout:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(fd, 4096)
                    if chunk:
                        output += chunk.decode("utf-8", errors="ignore")
                        # 检查是否出现模型名称行
                        if "model:" in output.lower():
                            return output
                except Exception:
                    break
        return output

    def _extract_real_model_name(self, output: str) -> Optional[str]:
        """
        从工具界面提取真正的模型名称
        """
        # 清理 ANSI 颜色码
        clean_output = self._strip_ansi(output)

        # 查找 model: 行
        for line in clean_output.split('\n'):
            line = line.strip()
            match = self.MODEL_NAME_PATTERN.match(line)
            if match:
                return match.group(1)

        # 备用模式：查找包含 model 的行
        for line in clean_output.split('\n'):
            lower_line = line.lower()
            if 'model' in lower_line and ':' in lower_line:
                # 提取冒号后的内容
                parts = line.split(':', 1)
                if len(parts) == 2:
                    name = parts[1].strip()
                    # 验证是否像模型名称
                    if name and re.match(r'^[a-zA-Z0-9_\-.]+$', name):
                        return name
        return None


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
            pass
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
            msg = "official_cli_nonzero_exit" if (last_rc is not None and int(last_rc) != 0) else "official_cli_no_models"
            try:
                phase = "json" if ("-f" in [str(x) for x in (last_cmd or [])] and "json" in [str(x) for x in (last_cmd or [])]) else "text"
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
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=float(self.timeout_s or 3.0), cwd=cwd, env=env)
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
                stderr=truncate_snippet(str(e) or ""),
            )

        text = (raw or "").strip()
        if not text:
            return []

        # JSON 解析
        if text.lstrip().startswith(('{', '[')):
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
            except TTADKLocalConfigError as e:
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
