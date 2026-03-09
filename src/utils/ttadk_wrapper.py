import sys
import subprocess
import threading
import shutil
import os
import re
import select
from dataclasses import dataclass, field
from typing import BinaryIO, Optional

# PTY support (best-effort): allow downstream tools that require a real TTY
try:
    import pty
except Exception:  # pragma: no cover
    pty = None

import argparse


# ANSI/control sequences may appear in PTY mode. We strip them before checking JSON start.
_ANSI_ESCAPE_RE = re.compile(rb"\x1b\[[\?0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b[()][AB012]")
"""TTADK wrapper runtime.

IMPORTANT:
- This module is intended to be launched via module mode:
  `python -m src.utils.ttadk_wrapper [--pty] <command> [args...]`
- Do NOT add `sys.path` runtime injection here. Import/package correctness
  must be guaranteed by the caller (SSOT: `src.acp.sync_adapter.resolve_agent_spec`).

The cwd normalization SSOT lives in `src.utils.path.normalize_ttadk_cwd`.
"""

from .path import normalize_ttadk_cwd  # deprecated re-export (compat)


@dataclass
class WrapperState:
    """Wrapper 运行态（避免 module-level 可变全局变量）。"""

    json_started: bool = False
    banner_tail: list[bytes] = field(default_factory=list)
    banner_tail_max: int = 8

    def append_banner_line(self, line: bytes) -> None:
        try:
            self.banner_tail.append(line)
            if self.banner_tail_max > 0 and len(self.banner_tail) > int(self.banner_tail_max):
                self.banner_tail.pop(0)
        except Exception:
            pass


def _parse_args(argv: list[str]) -> tuple[bool, list[str]]:
    """解析 wrapper 参数。

    约定：
    - wrapper 自身支持 `--pty`
    - 其余参数全部透传给下游命令
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--pty", action="store_true")
    # 其余参数透传
    ns, rest = parser.parse_known_args(argv)
    return bool(getattr(ns, "pty", False)), list(rest)


def _close_fd_quietly(fd: int) -> None:
    try:
        if fd is not None and fd >= 0:
            os.close(fd)
    except Exception:
        pass


def _strip_ansi_for_probe(data: bytes) -> bytes:
    try:
        return _ANSI_ESCAPE_RE.sub(b"", data or b"")
    except Exception:
        return data or b""


def _spawn_no_pty(cmd: list[str]) -> subprocess.Popen:
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=sys.stderr,  # Pass stderr through directly
        bufsize=0,
    )


def _spawn_with_pty(cmd: list[str]) -> tuple[subprocess.Popen, int]:
    """Spawn a subprocess with a PTY.

    Returns (proc, master_fd). Caller must close master_fd.
    """
    if pty is None:
        raise RuntimeError("pty_not_available")
    master_fd, slave_fd = pty.openpty()
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=slave_fd,
            stdout=slave_fd,
            # PTY 模式下将 stderr 也接到 slave，避免部分下游（如 node）对 stdio 类型不一致产生断言崩溃。
            stderr=slave_fd,
            bufsize=0,
            close_fds=True,
        )
        return proc, master_fd
    finally:
        # Child owns slave; wrapper closes it.
        _close_fd_quietly(slave_fd)


class _FDReader:
    """A minimal buffered binary reader backed by an OS fd.

    目标：避免逐字节 `os.read(fd, 1)` 带来的高 CPU/低吞吐。
    - `readline()`：按行读取（包含换行），支持跨 chunk
    - `read(n)`：按块读取，优先消耗内部缓冲
    """

    def __init__(
        self,
        fd: int,
        *,
        chunk_size: int = 4096,
        stop_event: Optional[threading.Event] = None,
        poll_interval: float = 0.1,
    ):
        self._fd = int(fd)
        try:
            chunk_size = int(chunk_size or 4096)
        except Exception:
            chunk_size = 4096
        # 保护：避免 chunk_size 过大导致内存异常
        self._chunk_size = max(256, min(chunk_size, 64 * 1024))
        self._buf = bytearray()
        self._stop_event = stop_event
        try:
            poll_interval = float(poll_interval or 0.1)
        except Exception:
            poll_interval = 0.1
        self._poll_interval = max(0.01, min(poll_interval, 1.0))

    def _wait_readable(self) -> bool:
        """等待 fd 可读。

        - 无 stop_event：直接返回 True（保持老行为：阻塞由 os.read 控制）
        - 有 stop_event：用 select 做短轮询，确保 stop_event set 后不会长期阻塞
        """
        if self._stop_event is None:
            return True
        try:
            r, _, _ = select.select([self._fd], [], [], self._poll_interval)
            if r:
                return True
        except Exception:
            # select 不可用时退化为直接读
            return True
        return not self._stop_event.is_set()

    def _read_more(self) -> bytes:
        if not self._wait_readable():
            return b""
        try:
            return os.read(self._fd, self._chunk_size)
        except OSError:
            return b""

    def readline(self) -> bytes:
        while True:
            try:
                idx = self._buf.find(b"\n")
            except Exception:
                idx = -1
            if idx >= 0:
                out = bytes(self._buf[: idx + 1])
                del self._buf[: idx + 1]
                return out

            chunk = self._read_more()
            if not chunk:
                if self._buf:
                    out = bytes(self._buf)
                    self._buf.clear()
                    return out
                return b""
            self._buf.extend(chunk)

    def read(self, n: int) -> bytes:
        try:
            n = int(n or 0)
        except Exception:
            n = 0
        if n <= 0:
            return b""

        if len(self._buf) >= n:
            out = bytes(self._buf[:n])
            del self._buf[:n]
            return out

        # 不足：先吐出缓冲，再读剩余
        out = bytes(self._buf)
        self._buf.clear()
        if not self._wait_readable():
            return out
        try:
            rest = os.read(self._fd, n - len(out))
        except OSError:
            rest = b""
        return out + (rest or b"")


def pump_filtered_stream(reader: object, writer: BinaryIO, state: WrapperState, *, chunk_size: int = 4096) -> None:
    """将 reader 的输出透传到 writer。

    规则：
    - JSON 起始前：按行读取，过滤 banner，并记录 banner_tail
    - JSON 起始后：按块读取并原样透传（不修改字节）
    """
    try:
        chunk_size = int(chunk_size or 4096)
    except Exception:
        chunk_size = 4096
    chunk_size = max(1, min(chunk_size, 1024 * 1024))

    while True:
        if not state.json_started:
            line = b""
            try:
                line = getattr(reader, "readline")()
            except Exception:
                line = b""
            if not line:
                break

            probe = _strip_ansi_for_probe(line)
            if probe.strip().startswith(b"{"):
                state.json_started = True
                try:
                    writer.write(line)
                    writer.flush()
                except Exception:
                    pass
                continue

            state.append_banner_line(line)
            continue

        # JSON started: pipe bytes
        try:
            chunk = getattr(reader, "read")(chunk_size)
        except Exception:
            chunk = b""
        if not chunk:
            break
        try:
            writer.write(chunk)
            writer.flush()
        except Exception:
            pass


def emit_failure_diagnostics(returncode: int, cmd: list[str], state: WrapperState, *, stderr: Optional[object] = None) -> None:
    """在失败且未开始输出 JSON 时打印 banner_tail 诊断信息。"""
    err = stderr if stderr is not None else sys.stderr
    try:
        if int(returncode or 0) == 0:
            return
    except Exception:
        return

    try:
        if state.json_started:
            return
    except Exception:
        return

    try:
        if state.banner_tail:
            tail = b"".join(state.banner_tail[-int(state.banner_tail_max or 8):])
            s = tail.decode("utf-8", errors="ignore").strip("\n")
            if s:
                err.write(f"[ttadk_wrapper] banner_tail:\n{s}\n")
    except Exception:
        pass
    try:
        err.write(f"[ttadk_wrapper] child exited rc={returncode} cmd={cmd}\n")
    except Exception:
        pass

def forward_stdin(proc):
    """Forward wrapper's stdin to subprocess's stdin."""
    try:
        while True:
            # Blocking read is fine in a thread
            chunk = sys.stdin.buffer.read(4096)
            if not chunk:
                break
            proc.stdin.write(chunk)
            proc.stdin.flush()
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            proc.stdin.close()
        except:
            pass

def forward_stdout(proc, state: WrapperState):
    """兼容旧调用：透传 stdout 并更新 state。"""
    try:
        pump_filtered_stream(getattr(proc, "stdout", None), sys.stdout.buffer, state)
    except Exception:
        return


def _pump_stdin_to_fd(
    *,
    fd: int,
    chunk_size: int = 4096,
    stop_event: Optional[threading.Event] = None,
    poll_interval: float = 0.1,
) -> None:
    """PTY 模式：将 wrapper stdin 转发到 master_fd。

    说明：
    - 子进程 stdin/stdout/stderr 都连接到 PTY slave
    - wrapper 侧通过 master_fd 同时读写
    - EOF 时退出；写入错误（EIO/EBADF/BrokenPipe）视为子进程已退出或不再接收输入
    """
    try:
        fd = int(fd)
    except Exception:
        return
    try:
        chunk_size = int(chunk_size or 4096)
    except Exception:
        chunk_size = 4096
    chunk_size = max(256, min(chunk_size, 64 * 1024))

    # 优先：可中断的 select 轮询（避免 stop_event set 后 stdin 长期阻塞）
    stdin_fd: Optional[int] = None
    try:
        stdin_fd = int(sys.stdin.fileno())
    except Exception:
        stdin_fd = None
    try:
        poll_interval = float(poll_interval or 0.1)
    except Exception:
        poll_interval = 0.1
    poll_interval = max(0.01, min(poll_interval, 1.0))

    try:
        if stop_event is not None and stdin_fd is not None:
            while not stop_event.is_set():
                try:
                    r, _, _ = select.select([stdin_fd], [], [], poll_interval)
                except Exception:
                    r = [stdin_fd]
                if not r:
                    continue
                try:
                    chunk = os.read(stdin_fd, chunk_size)
                except Exception:
                    break
                if not chunk:
                    break
                try:
                    os.write(fd, chunk)
                except (BrokenPipeError, OSError):
                    break
        else:
            # 退化路径：阻塞 read（线程为 daemon，不应阻塞进程退出）
            while True:
                if stop_event is not None and stop_event.is_set():
                    break
                try:
                    chunk = sys.stdin.buffer.read(chunk_size)
                except KeyboardInterrupt:
                    break
                except Exception:
                    break
                if not chunk:
                    break
                try:
                    os.write(fd, chunk)
                except (BrokenPipeError, OSError):
                    break
    finally:
        # best-effort: 不主动关闭 fd（由主流程统一收尾），避免与 stdout reader 竞争关闭
        return

def main():
    # `python3 ttadk_wrapper.py [--pty] <command> [args...]`
    use_pty, cmd = _parse_args(sys.argv[1:])
    if len(cmd) < 1:
        sys.stderr.write("Usage: ttadk_wrapper.py [--pty] <command> [args...]\n")
        sys.exit(1)
    
    # Resolve executable path if it's just a name
    if os.path.sep not in cmd[0]:
        resolved = shutil.which(cmd[0])
        if resolved:
            cmd[0] = resolved

    state = WrapperState()
    master_fd: Optional[int] = None

    try:
        # Start the actual process
        # bufsize=0 -> Unbuffered to minimize latency
        if use_pty:
            proc, master_fd = _spawn_with_pty(cmd)
        else:
            proc = _spawn_no_pty(cmd)
    except Exception as e:
        sys.stderr.write(f"Failed to start subprocess {cmd}: {e}\n")
        sys.exit(1)

    # Start forwarding threads
    if use_pty and master_fd is not None:
        stop_event = threading.Event()
        # PTY 模式：stdin/stdout 都挂在 master_fd；需要双向转发（stdin->master_fd / master_fd->stdout）。
        t_in = threading.Thread(
            target=_pump_stdin_to_fd,
            kwargs={"fd": int(master_fd), "stop_event": stop_event},
            daemon=True,
            name="ttadk-wrapper-pty-stdin",
        )

        def _pump_master(fd: int):
            try:
                try:
                    pump_filtered_stream(_FDReader(fd, stop_event=stop_event), sys.stdout.buffer, state)
                except Exception as e:
                    # 若 pump 线程异常退出，仍尽量留下可诊断线索（仅在 JSON 未开始时会输出）。
                    try:
                        state.append_banner_line(
                            f"[ttadk_wrapper] pump_error:{type(e).__name__}:{e}\n".encode("utf-8", errors="ignore")
                        )
                    except Exception:
                        pass
            finally:
                # 关闭职责：master_fd 由主线程统一关闭，避免双重 close 导致 EBADF/EIO 影响稳定性。
                return

        # 非 daemon：确保退出前能尽量 drain 完输出（否则可能丢 JSON 导致 ACP 握手失败）
        t_out = threading.Thread(
            target=_pump_master,
            args=(int(master_fd),),
            daemon=False,
            name="ttadk-wrapper-pty-stdout",
        )
    else:
        t_in = threading.Thread(target=forward_stdin, args=(proc,), daemon=True)
        t_out = threading.Thread(target=forward_stdout, args=(proc, state), daemon=True)
    
    if t_in is not None:
        try:
            t_in.start()
        except Exception:
            t_in = None
    t_out.start()
    
    # Wait for process to exit
    try:
        proc.wait()
    except KeyboardInterrupt:
        # Pass signal to child
        proc.terminate()
        proc.wait()
    
    # Ensure pump thread had a chance to drain outputs.
    # In PTY mode, proactively close master_fd after process exit to unblock reader.
    if use_pty and master_fd is not None:
        try:
            try:
                stop_event.set()
            except Exception:
                pass
            _close_fd_quietly(int(master_fd))
        except Exception:
            pass
    try:
        t_out.join(timeout=2.0)
    except Exception:
        pass

    # If process exits non-zero before any JSON, emit a minimal diagnostic.
    # This helps upstream identify failures where stderr may be empty.
    try:
        emit_failure_diagnostics(int(proc.returncode or 0), cmd, state)
    except Exception:
        pass

    sys.exit(proc.returncode)
if __name__ == "__main__":
    main()
