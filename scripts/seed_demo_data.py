#!/usr/bin/env python
"""生成演示与测试数据。

两种用途：

1. **生成 CSV 回放文件**（默认）——让整条链路在没有模拟器、没有网络的机器上也能跑，
   CI 的端到端验收就靠它。
2. ``--into-db`` 直接把样本灌进数据库，用于做截图或压测前的准备。

用法::

    python scripts/seed_demo_data.py                     # 生成 data/replay/*.csv
    python scripts/seed_demo_data.py --minutes 60        # 生成一小时的回放
    python scripts/seed_demo_data.py --into-db --rows 50000
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.collector.csv_collector import ReplayRow, write_replay_file  # noqa: E402
from app.config import ConfigError, load_config  # noqa: E402
from app.quality import (  # noqa: E402
    QUALITY_OK,
    QUALITY_TIMEOUT,
)
from app.storage import Database, Repository  # noqa: E402

#: 回放文件里的超时耗时，与 config/app.json 的 poll.timeoutMs 一致。
TIMEOUT_MS = 800.0

#: plc-01 每隔多少次采集安排一段「连续 3 次超时」。
#:
#: 取 40（500 ms 周期 -> 20 秒一次）是刻意的：既让失败率落在真实工业场景的
#: 量级（3/40 = 7.5%），又能在跑二十几秒的演示里**真的看到一次完整的
#: 「告警产生 -> 恢复」**。周期取太大（比如 200 组）时短演示跑不到，
#: 报告里就只有一条平直的成功曲线，看不出告警机制在工作。
#:
#: 注意这是**源数据里的组间隔**。回放倍速会同比压缩墙钟节奏：
#: ``--speed 60`` 时每 40 组只需 40 × 500/60 ≈ 0.33 秒，告警会变得非常密集。
#: 想看贴近真实的告警节奏，就用默认的 ``--speed 1``。
PLC_STREAK_PERIOD = 40
PLC_STREAK_LENGTH = 3

#: meter-02 每隔多少次采集出现一次单发超时（1000 ms 周期 -> 约 50 秒一次）。
METER_TIMEOUT_PERIOD = 50


def _plc_groups(minutes: int, rng: random.Random) -> list[tuple[int, list[ReplayRow]]]:
    """plc-01：每 500 ms 一次采集，产出 response_ms + register_0。

    每 :data:`PLC_STREAK_PERIOD` 次安排一段**连续 3 次超时**——
    正好是 ``timeout>=3`` 告警规则的触发条件，跑一段就能看到完整的一次
    告警产生 -> 恢复，而不是只能看到一条孤零零的失败记录。
    """
    groups: list[tuple[int, list[ReplayRow]]] = []
    ticks = minutes * 60 * 2  # 500ms 一次
    register_value = 100
    for i in range(ticks):
        offset = i * 500
        in_streak = (i % PLC_STREAK_PERIOD) >= (PLC_STREAK_PERIOD - PLC_STREAK_LENGTH)
        if in_streak:
            rows = [
                ReplayRow("response_ms", TIMEOUT_MS, QUALITY_TIMEOUT),
                ReplayRow("register_0", 0.0, QUALITY_TIMEOUT),
            ]
        else:
            latency = round(rng.gauss(12.0, 2.5), 2)
            latency = max(1.0, min(latency, 60.0))
            register_value = 100 + (i // 20) % 7
            rows = [
                ReplayRow("response_ms", latency, QUALITY_OK),
                ReplayRow("register_0", float(register_value), QUALITY_OK),
            ]
        groups.append((offset, rows))
    return groups


def _meter_groups(minutes: int, rng: random.Random) -> list[tuple[int, list[ReplayRow]]]:
    """meter-02：每 1000 ms 一次采集，只产出 response_ms，偶尔单次超时。"""
    groups: list[tuple[int, list[ReplayRow]]] = []
    for i in range(minutes * 60):
        offset = i * 1000
        if i % METER_TIMEOUT_PERIOD == METER_TIMEOUT_PERIOD - 1:
            groups.append((offset, [ReplayRow("response_ms", TIMEOUT_MS, QUALITY_TIMEOUT)]))
            continue
        latency = round(rng.gauss(18.0, 4.0), 2)
        groups.append((offset, [ReplayRow("response_ms", max(1.0, latency), QUALITY_OK)]))
    return groups


def generate_replay_files(minutes: int, out_dir: Path, seed: int) -> list[Path]:
    rng = random.Random(seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = [
        write_replay_file(out_dir / "plc-01.csv", _plc_groups(minutes, rng)),
        write_replay_file(out_dir / "meter-02.csv", _meter_groups(minutes, rng)),
    ]
    return written


def seed_database(config_path: str, rows: int, seed: int) -> int:
    """把合成样本直接灌进数据库（不做真实采集）。
    覆盖配置中的全部已启用设备（plc-01、meter-02），包含正常采样、偶发弱网、超时与历史告警事件。
    """
    config = load_config(config_path)
    db = Database(config.database_path)
    db.init_schema()
    repo = Repository(db)
    mapping = repo.sync_devices(config.devices)

    rng = random.Random(seed)
    now_ts = int(time.time() * 1000)
    # 生成覆盖最近时间窗口的样本（确保默认 10m / 1h 窗口打开即有丰富数据）
    base_ts = now_ts - rows * 500

    plc_id = mapping.get("plc-01") or next(iter(mapping.values()))
    meter_id = mapping.get("meter-02")

    samples_to_insert = []
    for i in range(rows):
        ts = base_ts + i * 500
        # plc-01 (500ms 一次采样)
        failed_plc = (i % 80 in (77, 78, 79))  # 制造偶发连续3次超时
        quality_plc = QUALITY_TIMEOUT if failed_plc else QUALITY_OK
        lat_plc = 800.0 if failed_plc else max(1.0, round(rng.gauss(14.0, 3.5), 2) + (120 if i % 50 == 0 else 0))
        samples_to_insert.append((plc_id, ts, "response_ms", lat_plc, quality_plc))
        samples_to_insert.append((plc_id, ts, "register_0", 220.0 if not failed_plc else 0.0, quality_plc))

        # meter-02 (每 1000ms 一次采样)
        if meter_id and (i % 2 == 0):
            failed_meter = (i % 90 == 89)
            quality_meter = QUALITY_TIMEOUT if failed_meter else QUALITY_OK
            lat_meter = 800.0 if failed_meter else max(2.0, round(rng.gauss(18.0, 4.0), 2))
            samples_to_insert.append((meter_id, ts, "response_ms", lat_meter, quality_meter))

    inserted = repo.insert_samples(samples_to_insert)

    # 写入一条逼真的已恢复历史告警
    try:
        alarm_start = now_ts - 12 * 60 * 1000  # 12分钟前触发
        alarm_end = now_ts - 10 * 60 * 1000    # 10分钟前恢复
        aid = repo.open_alarm(plc_id, "timeout>=3", alarm_start, detail='{"reason": "连续3次请求超时", "count": 3}')
        if aid:
            repo.close_alarm(aid, alarm_end, detail='{"reason": "通信链路已自动恢复", "status": "resolved"}')
    except Exception:
        pass

    db.close_all()
    return inserted


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="seed_demo_data.py", description="生成演示与测试数据")
    parser.add_argument("--minutes", type=int, default=30, help="回放数据覆盖的分钟数（默认 30）")
    parser.add_argument("--out", default=None, help="回放文件输出目录（默认 data/replay）")
    parser.add_argument("--into-db", action="store_true", help="改为直接往数据库灌样本")
    parser.add_argument("--rows", type=int, default=20_000, help="--into-db 时的样本条数")
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config" / "app.json"))
    parser.add_argument("--seed", type=int, default=20260919, help="随机种子（保证可复现）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.into_db:
        try:
            inserted = seed_database(args.config, args.rows, args.seed)
        except ConfigError as exc:
            print(f"[配置错误] {exc}", file=sys.stderr)
            return 2
        print(f"已写入 {inserted} 条样本。")
        return 0

    out_dir = Path(args.out) if args.out else PROJECT_ROOT / "data" / "replay"
    files = generate_replay_files(args.minutes, out_dir, args.seed)
    print(f"已生成 {len(files)} 个回放文件（覆盖 {args.minutes} 分钟）：")
    for path in files:
        lines = sum(1 for _ in path.open(encoding="utf-8")) - 1
        print(f"  - {path}  ({lines} 行)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
