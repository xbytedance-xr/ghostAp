"""TTADK 工具模型列表动态获取模块

通过模拟终端交互获取每个 TTADK 工具支持的模型列表。
"""

import logging
import os
import re
import select
import struct
import time
import fcntl
import termios
import pty
from typing import Optional

from .models import TTADKModel

logger = logging.getLogger(__name__)

# ANSI 颜色码正则
ANSI_ESCAPE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b[()][AB012]')

# 模型选择界面提示
MODEL_SELECTION_PROMPT = "Select a model:"

# 模型名称提取正则（从工具界面）
MODEL_NAME_PATTERN = re.compile(r'model:\s*([^\s]+)')


class TTADKModelFetcher:
    """通过模拟终端交互获取 TTADK 工具的模型列表"""

    # 缓存 TTL（秒）
    CACHE_TTL = 300

    def __init__(self):
        self._cache: dict[str, list[TTADKModel]] = {}
        self._cache_time: dict[str, float] = {}

    def fetch_tool_models(self, tool_name: str, force_refresh: bool = False) -> list[TTADKModel]:
        """
        获取指定工具的模型列表

        Args:
            tool_name: 工具名称（如 codex, claude）
            force_refresh: 是否强制刷新缓存

        Returns:
            TTADKModel 列表
        """
        # 检查缓存
        if not force_refresh and self._is_cache_valid(tool_name):
            return self._cache[tool_name]

        try:
            models = self._fetch_models_from_terminal(tool_name)
            if models:
                self._cache[tool_name] = models
                self._cache_time[tool_name] = time.time()
                return models
        except Exception as e:
            logger.error("Failed to fetch models for tool %s: %s", tool_name, e)

        # 失败时返回空列表
        return []

    def _is_cache_valid(self, tool_name: str) -> bool:
        """检查缓存是否有效"""
        if tool_name not in self._cache:
            return False
        cache_time = self._cache_time.get(tool_name, 0)
        return (time.time() - cache_time) < self.CACHE_TTL

    def invalidate_cache(self, tool_name: Optional[str] = None) -> None:
        """使缓存失效"""
        if tool_name:
            self._cache.pop(tool_name, None)
            self._cache_time.pop(tool_name, None)
        else:
            self._cache.clear()
            self._cache_time.clear()

    def _fetch_models_from_terminal(self, tool_name: str) -> list[TTADKModel]:
        """通过模拟终端交互获取模型列表"""
        master, slave = None, None
        try:
            # 创建 pty 并设置终端大小
            master, slave = pty.openpty()
            winsize = struct.pack('HHHH', 40, 120, 0, 0)
            fcntl.ioctl(master, termios.TIOCSWINSZ, winsize)

            # 启动 ttadk code 进程
            pid = os.fork()
            if pid == 0:
                # 子进程
                os.setsid()
                os.dup2(slave, 0)
                os.dup2(slave, 1)
                os.dup2(slave, 2)
                if slave > 2:
                    os.close(slave)
                if master > 2:
                    os.close(master)
                os.execvp("ttadk", ["ttadk", "code", "-t", tool_name])

            # 父进程：关闭 slave 端
            os.close(slave)
            slave = None

            # 读取输出直到出现模型选择界面
            output = self._read_until_prompt(master, MODEL_SELECTION_PROMPT, timeout=10)

            if not output:
                logger.warning("No output from ttadk for tool %s", tool_name)
                return []

            # 解析模型选择界面
            friendly_names = self._parse_model_selection_menu(output)
            if not friendly_names:
                logger.warning("No models found in selection menu for tool %s", tool_name)
                return []

            logger.debug("Found %d models for tool %s: %s", len(friendly_names), tool_name, friendly_names)

            # 依次选择每个模型获取真实名称
            models: list[TTADKModel] = []
            for idx, friendly_name in enumerate(friendly_names):
                real_name = self._select_and_extract_model_name(
                    master, idx, len(friendly_names), friendly_name
                )
                if real_name:
                    models.append(
                        TTADKModel(
                            name=real_name,
                            description=friendly_name,
                            friendly_name=friendly_name,
                            is_default=(idx == 0),
                        )
                    )

            return models

        except Exception as e:
            logger.error("Terminal interaction failed for tool %s: %s", tool_name, e)
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

        输出格式示例:
        ? Select a model:  (Use arrow keys)
         ❯ GPT 5.2 Codex (Recommended)
           GPT 4.1 Codex
           o4-mini
        """
        names: list[str] = []

        # 清理 ANSI 颜色码
        clean_output = self._strip_ansi(output)

        # 查找 "Select a model:" 之后的内容
        lines = clean_output.split('\n')
        in_menu = False

        for line in lines:
            stripped = line.strip()
            if MODEL_SELECTION_PROMPT in stripped or "? Select a model:" in stripped:
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

        Args:
            fd: pty 文件描述符
            model_index: 模型索引（0-based）
            total_models: 总模型数
            friendly_name: 友好名称（用于日志）

        Returns:
            真实模型名称，如 gpt-5.2-codex-ttadk
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
                # 可能需要额外的操作来返回模型选择界面
                # 这里先简化处理，实际可能需要更复杂的交互

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

        输出示例:
        model:     gpt-5.2-codex-ttadk
        """
        # 清理 ANSI 颜色码
        clean_output = self._strip_ansi(output)

        # 查找 model: 行
        for line in clean_output.split('\n'):
            line = line.strip()
            match = MODEL_NAME_PATTERN.match(line)
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

    def _strip_ansi(self, text: str) -> str:
        """移除 ANSI 颜色码"""
        return ANSI_ESCAPE.sub('', text)
