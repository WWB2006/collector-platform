#!/usr/bin/env python
"""性能实测：写入吞吐与查询延迟。

用法::

    python scripts/benchmark.py                       # 10 万条，临时库
    python scripts/benchmark.py --rows 1000000        # 百万条
    python scripts/benchmark.py --db data/bench.db    # 指定库文件

输出直接可以贴进 README 的性能一节——数字都来自本机实测，不是估算。
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.storage import Database, Repository  # noqa: E402

BASE_TS = 1_700_000_000_000
#: 一次采集产生 response_ms + register_0 两条记录，按真实比例生成数据。
METRICS = ("response_ms", "register_0")


def human(n: float) -> str:
    return f"{n:,.0f}"


def generate_rows(device_id: int, rows: int, interval_ms: int = 500):
    """按固定周期生成样本；每 100 条掺 1 条超时（质量码 1），贴近真实分布。"""
    per_tick = len(METRICS)
    ticks = max(1, rows // per_tick)
    produced = 0
    for i in range(ticks):
        ts = BASE_TS + i * interval_ms
        failed = (i % 100) == 99
        quality = 1 if failed else 0
        for metric in METRICS:
            if produced >= rows:
                return
            value = 0.0 if failed else (8.0 + (i % 37) * 0.5 if metric == "response_ms" else 100.0 + i % 50)
            yield (device_id, ts, metric, value, quality)
            produced += 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmark.py", description="写入吞吐与查询延迟实测")
    parser.add_argument("--rows", type=int, default=100_000, help="样本条数（默认 100000）")
    parser.add_argument("--db", default=None, help="库文件路径（默认用临时库）")
    parser.add_argument("--batch-size", type=int, default=500, help="批量写入的分批大小")
    parser.add_argument("--keep", action="store_true", help="保留临时库文件")
    args = parser.parse_args()

    if args.db:
        db_path = Path(args.db)
    else:
        fd, tmp_name = tempfile.mkstemp(suffix=".db", prefix="bench-")
        os.close(fd)
        db_path = Path(tmp_name)

    db = Database(db_path)
    db.init_schema()
    repo = Repository(db)
    device_id = repo.upsert_device("bench-01", "tcp", "127.0.0.1:5020", 1, 500)

    print(f"库文件      : {db_path}")
    print(f"样本条数    : {human(args.rows)}")
    print(f"批大小      : {args.batch_size}")
    print(f"文件系统    : {sys.platform}")
    print()

    # ---- 写入 ----------------------------------------------------------
    started = time.perf_counter()
    inserted = repo.insert_samples(
        generate_rows(device_id, args.rows), batch_size=args.batch_size
    )
    write_seconds = time.perf_counter() - started
    print(f"写入        : {human(inserted)} 条 / {write_seconds:.2f} s"
          f"  ->  {human(inserted / write_seconds)} 条/秒")

    db_size = db_path.stat().st_size if db_path.exists() else 0
    print(f"库文件大小  : {db_size / 1024 / 1024:.1f} MB")

    # ---- 查询：按设备 + 时间范围 ----------------------------------------
    span_ms = 3600_000  # 最近一小时
    to_ts = BASE_TS + max(1, args.rows // len(METRICS)) * 500
    from_ts = to_ts - span_ms

    def timeit(label: str, fn, repeat: int = 20) -> float:
        samples: list[float] = []
        for _ in range(repeat):
            t0 = time.perf_counter()
            fn()
            samples.append((time.perf_counter() - t0) * 1000)
        p95 = statistics.quantiles(samples, n=20)[-1] if len(samples) >= 2 else samples[0]
        print(f"{label:<34}: 中位 {statistics.median(samples):7.2f} ms   P95 {p95:7.2f} ms")
        return p95

    count = repo.count_samples(device_id, from_ts=from_ts, to_ts=to_ts)
    print(f"\n一小时样本数: {human(count)}")
    print()

    timeit("COUNT(*) 设备+时间", lambda: repo.count_samples(device_id, from_ts=from_ts, to_ts=to_ts))
    timeit("分页查询 LIMIT 100",
           lambda: repo.query_samples(device_id, from_ts=from_ts, to_ts=to_ts, limit=100))
    timeit("取 quality/value 列表（统计用）",
           lambda: repo.quality_values(device_id, from_ts=from_ts, to_ts=to_ts))
    timeit("质量码分布 GROUP BY",
           lambda: repo.quality_distribution(device_id, from_ts=from_ts, to_ts=to_ts))
    timeit("分桶曲线 120 点",
           lambda: repo.series("response_ms", device_id=device_id, from_ts=from_ts, to_ts=to_ts,
                               bucket_ms=30_000))

    # ---- 索引验证 -------------------------------------------------------
    print("\n查询计划（应当出现 USING INDEX）：")
    plan = repo.query_plan(
        "SELECT * FROM samples WHERE device_id = ? AND ts >= ? AND ts < ?",
        (device_id, from_ts, to_ts),
    )
    for line in plan:
        print(f"  {line}")

    db.close_all()
    if not args.keep and not args.db and db_path.exists():
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(db_path) + suffix)
            if candidate.exists():
                candidate.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
