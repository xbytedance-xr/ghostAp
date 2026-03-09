"""TTADK 启动期探测（startup_probe）。

该模块只包含“快速探测/能力探测”类逻辑，避免与启动编排（precheck→start→repair→retry→degrade）耦合。

设计约束：
- 仅依赖标准库与 `resolve_agent_spec_fn` 注入函数（避免循环依赖）
- best-effort：永不抛异常（返回 bool）
"""

from __future__ import annotations

import os
import select
import subprocess
import time
from typing import Callable, Optional

from .env_sandbox import build_ttadk_subprocess_env

__all__ = [
    "ttadk_acp_ready_quickcheck",
]


def ttadk_acp_ready_quickcheck(
    *,
    agent_type: str,
    cwd: str,
    model_name: Optional[str],
    resolve_agent_spec_fn: Callable[..., tuple[str, list[str]]],
    time_fn: Callable[[], float] = time.time,
    timeout_s: float = 2.0,
) -> bool:
    """快速探测 ttadk wrapper 是否会产出 ACP JSON-RPC（best-effort）。

    目标：避免 `ttadk code -t <tool> -a "acp serve"` 在某些 tool 上阻塞且不输出 JSON，
    导致 ACP handshake 等待超时。

    判定规则：在短超时内 stdout 出现以 '{' 开头的行（wrapper 过滤 banner 后的 JSON 起始）。

    Contract:
    - never raises
    - returns bool
    """

    try:
        cmd, args = resolve_agent_spec_fn(agent_type, model_name=model_name)
    except Exception:
        return False

    full_cmd = [cmd] + list(args)
    try:
        env, _ = build_ttadk_subprocess_env(
            cwd=cwd or ".",
            agent_type=str(agent_type or ""),
            tool_name=str(agent_type or "").replace("ttadk_", "", 1) if (agent_type or "").startswith("ttadk_") else "",
        )
        p = subprocess.Popen(
            full_cmd,
            cwd=cwd or ".",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            # IMPORTANT: use binary + non-blocking select to avoid hanging on readline()
            text=False,
            env=env,
        )
    except Exception:
        return False

    try:
        start = float(time_fn())
        deadline_s = max(0.1, float(timeout_s or 0.0))
        if p.stdout is None:
            return False

        fd = p.stdout.fileno()
        pending = bytearray()
        buf = bytearray()

        while (float(time_fn()) - start) < deadline_s:
            alive = p.poll() is None
            try:
                now = float(time_fn())
                remain = max(0.0, deadline_s - (now - start))
            except Exception:
                remain = 0.05
            wait_s = min(0.05, remain)
            if wait_s <= 0:
                break

            try:
                r, _, _ = select.select([fd], [], [], wait_s)
            except Exception:
                break
            if not r:
                if not alive:
                    break
                continue

            try:
                chunk = os.read(fd, 4096)
            except Exception:
                break
            if not chunk:
                if not alive:
                    break
                continue

            # 只保留最近 64KB，避免极端 banner 导致内存增长
            buf.extend(chunk)
            if len(buf) > 65536:
                buf = buf[-65536:]

            pending.extend(chunk)
            while True:
                idx = pending.find(b"\n")
                if idx < 0:
                    break
                line = pending[: idx + 1]
                del pending[: idx + 1]
                if line.lstrip().startswith(b"{"):
                    return True

        # 若最后一段没有换行，也做一次判定
        try:
            if pending.lstrip().startswith(b"{"):
                return True
        except Exception:
            pass
        return False
    finally:
        try:
            p.terminate()
        except Exception:
            pass
        try:
            p.wait(timeout=1)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
