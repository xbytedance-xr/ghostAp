"""ACP Provider 启动解析性能基准（P99）。

目标：验证“启动解析/命令组装”链路的开销是否满足 P99 <= 2000ms。

注意：
- 默认仅基准 `resolve_agent_spec()` 与 `ToolRegistry.get_serve_command()` 的耗时，不会真实拉起 ACP Server。
- 该脚本面向 CI/本地快速排查，避免依赖外部工具实际安装情况。
"""

from __future__ import annotations

import argparse
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

from src.acp.sync_adapter import resolve_agent_spec


def _p99_ms(samples_ms: list[float]) -> float:
    if not samples_ms:
        return 0.0
    xs = sorted(float(x) for x in samples_ms)
    # ceil(p*len) - 1
    idx = max(0, min(len(xs) - 1, int((len(xs) * 0.99) + 0.999999) - 1))
    return float(xs[idx])


def _run_once(agent_type: str, model: str | None) -> float:
    t0 = time.perf_counter()
    try:
        resolve_agent_spec(agent_type, model_name=model)
    except Exception:
        # 基准关注“解析/组装”开销；工具不存在时也应快速失败。
        pass
    return (time.perf_counter() - t0) * 1000.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--providers", default="coco,aiden", help="comma separated provider names")
    ap.add_argument("--model", default="test-model", help="model name for resolution")
    ap.add_argument("--iterations", type=int, default=50)
    ap.add_argument("--concurrency", type=int, default=10)
    ap.add_argument("--p99-ms", type=float, default=2000.0)
    args = ap.parse_args()

    providers = [p.strip() for p in str(args.providers or "").split(",") if p.strip()]
    if not providers:
        raise SystemExit("--providers 不能为空")

    iterations = max(1, int(args.iterations or 0))
    conc = max(1, int(args.concurrency or 0))
    model = str(args.model or "").strip() or None

    # warmup: trigger registry import + preheat thread once
    for p in providers:
        try:
            resolve_agent_spec(p, model_name=model)
        except Exception:
            # 只测解析路径，不要求工具真的可用
            pass

    lat_ms: list[float] = []
    with ThreadPoolExecutor(max_workers=conc) as ex:
        futs = []
        for _ in range(iterations):
            for p in providers:
                futs.append(ex.submit(_run_once, p, model))
        for f in futs:
            try:
                lat_ms.append(float(f.result()))
            except Exception:
                # 极端兜底：线程本身异常
                lat_ms.append(0.0)

    p99 = _p99_ms(lat_ms)
    avg = statistics.fmean(lat_ms) if lat_ms else 0.0
    mx = max(lat_ms) if lat_ms else 0.0

    print(
        "providers=%s iterations=%d concurrency=%d samples=%d avg_ms=%.2f p99_ms=%.2f max_ms=%.2f"
        % (",".join(providers), iterations, conc, len(lat_ms), avg, p99, mx)
    )

    if p99 > float(args.p99_ms or 2000.0):
        print("FAIL: p99_ms exceeds threshold")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
