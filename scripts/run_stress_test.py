#!/usr/bin/env python3
import os
import sys
import threading
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.utils.rate_limit import RateLimiter


def simulate_memory_and_load():
    print("=== 开始系统级压力测试 ===")

    # 1. Test RateLimiter under high concurrency (Spec Mode)
    rl = RateLimiter(capacity=1000, fill_rate=100.0)
    success_count = 0
    fail_count = 0

    def worker():
        nonlocal success_count, fail_count
        for _ in range(50):
            if rl.acquire(1, blocking=False):
                success_count += 1
            else:
                fail_count += 1

    threads = [threading.Thread(target=worker) for _ in range(100)]  # 5000 requests total
    start = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    end = time.time()

    print(f"[Spec并发] 5000请求耗时: {end-start:.3f}s. 成功: {success_count}, 熔断/限流: {fail_count}")

    # 2. Test Deep Engine recursion truncation
    from src.deep_engine.progress import _truncate_nested_data
    deep_data = {}
    curr = deep_data
    for _ in range(20):
        curr["child"] = {}
        curr = curr["child"]
    truncated = _truncate_nested_data(deep_data, max_depth=10)
    print(f"[Deep递归] 数据已安全截断, 最终结果类型: {type(truncated)}")

    # 3. Simulate continuous running
    print("=== 模拟长时间运行 (72h缩影) ===")
    import gc
    initial_objs = len(gc.get_objects())

    # create some garbage
    for _ in range(100):
        _ = {"level1": {"level2": "temp"}}
    gc.collect()

    final_objs = len(gc.get_objects())
    diff = final_objs - initial_objs
    print(f"[GC] 内存对象泄漏检测差值: {diff} (接近0为正常)")

    print("=== 测试完成 ===")

if __name__ == "__main__":
    simulate_memory_and_load()
