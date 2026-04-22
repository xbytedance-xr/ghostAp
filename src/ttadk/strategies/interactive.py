import fcntl
import logging
import os
import pty
import re
import select
import struct
import subprocess
import sys
import termios
import time
from typing import Optional

from .base import ModelFetchStrategy, _env_truthy, _in_ci_environment
from ..env_sandbox import build_ttadk_subprocess_env
from ...utils.errors import get_error_detail
from ..models import TTADKModel, strip_ansi

logger = logging.getLogger(__name__)


class InteractiveStrategy(ModelFetchStrategy):
    """
    交互解析策略 (Strategy B)
    使用 PTY 模拟终端交互，解析菜单项并逐一进入详情页获取真实模型名称。
    作为 ProbeStrategy 失败后的兜底方案。
    """

    # 模型选择界面提示
    MODEL_SELECTION_PROMPT = "Select a model:"

    # 模型名称提取正则（从工具界面）
    MODEL_NAME_PATTERN = re.compile(r"model:\s*([^\s]+)")

    # 移除 ANSI 颜色码
    ANSI_ESCAPE = re.compile(r"\x1b\[[\?0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b[()][AB012]")

    @property
    def name(self) -> str:
        return "interactive"

    def fetch(self, tool_name: str, cwd: Optional[str] = None) -> list[TTADKModel]:
        # Default OFF: interactive mode (pty + fork) is risky in multi-threaded services.
        # Enable only when explicitly set.
        try:
            from ...config import get_settings

            if not bool(getattr(get_settings(), "ttadk_interactive_enabled", False)):
                return []
        except Exception:
            return []

        # 环境预检：非交互式环境直接跳过，避免无效等待。
        # 允许通过环境变量强制开启：TTADK_FORCE_INTERACTIVE=1
        if not _env_truthy("TTADK_FORCE_INTERACTIVE"):
            # 1) stdin TTY 检测（某些 runner 会将 sys.stdin 替换成非标准对象）
            is_tty = False
            try:
                stdin = getattr(sys, "stdin", None)
                isatty = getattr(stdin, "isatty", None)
                is_tty = bool(isatty()) if callable(isatty) else False
            except Exception:
                is_tty = False
            if not is_tty:
                logger.debug("InteractiveStrategy skipped: not a TTY")
                return []

            # 2) CI / 非交互环境检测
            try:
                if _in_ci_environment() or (os.getenv("DEBIAN_FRONTEND") == "noninteractive"):
                    logger.debug("InteractiveStrategy skipped: detected CI/noninteractive environment")
                    return []
            except Exception:
                # 若环境变量读取异常，保持保守：跳过交互策略
                logger.debug("InteractiveStrategy skipped: environment detection failed")
                return []

        logger.info(f"InteractiveStrategy starting for tool {tool_name}")
        master, slave = None, None
        proc: Optional[subprocess.Popen] = None
        try:
            # 创建 pty 并设置终端大小
            master, slave = pty.openpty()
            winsize = struct.pack("HHHH", 40, 120, 0, 0)
            fcntl.ioctl(master, termios.TIOCSWINSZ, winsize)

            # 启动 ttadk code 进程（使用 subprocess + PTY，避免 fork 在多线程服务中的风险）
            env, _ = build_ttadk_subprocess_env(cwd=cwd or ".", agent_type="ttadk", tool_name=tool_name)
            try:
                env.setdefault("TERM", env.get("TERM") or "xterm-256color")
            except Exception:
                pass
            cmd = ["ttadk", "code", "-t", tool_name]
            try:
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
            except Exception:
                # Popen failed — close slave immediately (master cleaned up in outer finally)
                if slave is not None:
                    try:
                        os.close(slave)
                    except OSError:
                        pass
                    slave = None
                raise
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
            logger.warning(f"InteractiveStrategy failed for tool {tool_name}: {get_error_detail(e)}")
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
                    logger.warning(f"Failed to cleanup ttadk interactive process {getattr(proc, 'pid', None)}: {get_error_detail(e)}")

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

    def _parse_model_selection_menu(self, output: str) -> list[str]:
        """
        解析模型选择界面的友好名称
        """
        names: list[str] = []

        clean_output = strip_ansi(output)

        # 查找 "Select a model:" 之后的内容
        lines = clean_output.split("\n")
        in_menu = False

        for line in lines:
            stripped = line.strip()
            if self.MODEL_SELECTION_PROMPT in stripped or "? Select a model:" in stripped:
                in_menu = True
                continue

            if in_menu:
                # 检查是否已经离开菜单区域（遇到空行或其他提示）
                if not stripped or stripped.startswith("?") or stripped.startswith("Press"):
                    # 跳过空行和提示行，但不结束菜单解析
                    if not stripped:
                        continue
                    # 遇到新的提示符，结束菜单解析
                    if stripped.startswith("?"):
                        break

                # 匹配菜单项：以 ❯ 或空格开头
                # 格式: "❯ GPT 5.2 Codex (Recommended)" 或 "  GPT 4.1 Codex"
                if stripped.startswith("❯"):
                    # 提取模型名称
                    name = stripped.lstrip("❯").strip()
                    if name and not name.startswith("("):
                        names.append(name)
                elif stripped and not stripped.startswith("("):
                    # 普通菜单项
                    if stripped:  # 确保不是空字符串
                        names.append(stripped)

        return names

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
        clean_output = strip_ansi(output)

        for line in clean_output.split("\n"):
            line = line.strip()
            match = self.MODEL_NAME_PATTERN.match(line)
            if match:
                return match.group(1)

        # 备用模式：查找包含 model 的行
        for line in clean_output.split("\n"):
            lower_line = line.lower()
            if "model" in lower_line and ":" in lower_line:
                # 提取冒号后的内容
                parts = line.split(":", 1)
                if len(parts) == 2:
                    name = parts[1].strip()
                    # 验证是否像模型名称
                    if name and re.match(r"^[a-zA-Z0-9_\-.]+$", name):
                        return name
        return None
