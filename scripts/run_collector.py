#!/usr/bin/env python
"""采集调度入口：按配置周期采集并写库，收到停止信号后优雅退出。

用法::

    python scripts/run_collector.py --config config/app.json
    python scripts/run_collector.py --config config/app.test.json --speed 60 --seconds 20
    python scripts/run_collector.py --stop-file /tmp/stop        # 出现该文件即停止

退出顺序（``CollectorScheduler.stop`` 里实现）：先停采集线程，
再把队列里剩下的样本写完，最后关数据库。反过来会丢掉最后几批数据。
"""

from __future__ import annotations

import argparse
import platform
import signal
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import __version__  # noqa: E402
from app.analytics.alarm_engine import AlarmEngine  # noqa: E402
from app.config import ConfigError, load_config  # noqa: E402
from app.logging_setup import get_logger, setup_logging  # noqa: E402
from app.scheduler import CollectorScheduler, resolve_replay_paths  # noqa: E402
from app.storage import Database, Repository  # noqa: E402
from app.storage.migrations import now_ms  # noqa: E402

logger = get_logger("run_collector")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_collector.py",
        description="按周期采集设备数据并写入 SQLite",
    )
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config" / "app.json"))
    parser.add_argument("--seconds", type=float, default=None,
                        help="运行指定秒数后退出（默认一直运行到收到停止信号）")
    parser.add_argument("--speed", type=float, default=1.0,
                        help="CSV 回放的倍速（60 表示一分钟的数据一秒放完）")
    parser.add_argument("--no-loop", action="store_true",
                        help="回放文件放完即停，不循环")
    parser.add_argument("--batch-size", type=int, default=500, help="批量写库的分批大小")
    parser.add_argument("--stop-file", default=None,
                        help="该文件出现时停止（便于在脚本/CI 里控制退出）")
    parser.add_argument("--quiet", action="store_true", help="控制台只输出警告")
    parser.add_argument("--dry-run", action="store_true",
                        help="只校验配置与采集器能否建起来，不写库、不采集")
    return parser


def _watch_stop_file(path: Path, stop_event: threading.Event) -> threading.Thread:
    def loop() -> None:
        while not stop_event.is_set():
            if path.exists():
                logger.info("检测到停止文件 %s，准备退出", path)
                stop_event.set()
                return
            stop_event.wait(0.2)

    thread = threading.Thread(target=loop, name="stop-file-watcher", daemon=True)
    thread.start()
    return thread


def _install_signal_handlers(stop_event: threading.Event) -> None:
    def handler(signum, _frame):  # type: ignore[no-untyped-def]
        logger.info("收到信号 %s，准备退出（会先把已采集的数据写完）", signum)
        stop_event.set()

    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):  # pragma: no cover - 非主线程/平台不支持
                pass


def _environment(config_path: Path, config) -> dict[str, object]:
    return {
        "collectorVersion": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "configPath": str(config_path),
        "database": str(config.database_path),
        "devices": [
            {
                "name": d.name,
                "transport": d.transport,
                "address": d.address,
                "pollIntervalMs": d.poll_interval_ms,
            }
            for d in config.devices
        ],
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 2

    setup_logging(
        config.log_level,
        config.log_dir_path,
        console_level="info" if not args.quiet else "warning",
    )
    logger.info("启动采集：配置 %s", config.source_path)

    replay_paths = resolve_replay_paths(config)
    for name, path in replay_paths.items():
        if not path.exists():
            print(f"[配置错误] 设备 {name} 的回放文件不存在：{path}", file=sys.stderr)
            return 2

    db = Database(config.database_path)
    db.init_schema()
    repo = Repository(db)

    # 告警引擎与采集共用同一个库；样本落库后由写入线程触发评估。
    try:
        alarm_engine = AlarmEngine(repo, config.alarms)
    except Exception as exc:  # noqa: BLE001 - 规则写错要立刻报出来
        print(f"[配置错误] 告警规则无法解析：{exc}", file=sys.stderr)
        db.close_all()
        return 2

    scheduler = CollectorScheduler(
        config,
        repo,
        speed=args.speed,
        loop_replay=not args.no_loop,
        batch_size=args.batch_size,
        alarm_engine=alarm_engine,
    )

    stop_event = threading.Event()
    _install_signal_handlers(stop_event)
    if args.stop_file:
        _watch_stop_file(Path(args.stop_file), stop_event)

    try:
        scheduler.setup()
    except Exception as exc:  # noqa: BLE001 - 采集器建不起来时给出可读信息
        print(f"[启动失败] 无法建立采集器：{exc}", file=sys.stderr)
        db.close_all()
        return 2

    if args.dry_run:
        print(f"配置校验通过：{len(scheduler.devices)} 台设备可采集")
        print(f"数据库：{config.database_path}")
        for name, path in replay_paths.items():
            print(f"回放文件：{name} -> {path}")
        db.close_all()
        return 0

    run_id = repo.start_run(_environment(Path(args.config), config))
    started = time.monotonic()
    exit_code = 0

    try:
        scheduler.start()
        scheduler.wait(args.seconds, stop_event=stop_event)
    except KeyboardInterrupt:  # pragma: no cover - 兜底，正常走信号处理器
        logger.info("收到中断")
    except Exception as exc:  # noqa: BLE001
        logger.exception("采集过程异常终止：%s", exc)
        exit_code = 1
    finally:
        scheduler.stop()
        elapsed = time.monotonic() - started
        stats = scheduler.stats_dict()
        summary = {
            "durationSeconds": round(elapsed, 3),
            "exitCode": exit_code,
            **stats,
        }
        repo.finish_run(run_id, summary, finished_at=now_ms())
        db.close_all()

    print()
    print(f"运行时长      : {elapsed:.1f} s")
    print(f"采集轮次      : {stats['cycles']}")
    print(f"样本条数      : {stats['samples']}（失败轮次 {stats['failures']}）")
    print(f"已落库        : {stats['written']} 条")
    if stats["writeErrors"]:
        print(f"写库错误      : {stats['writeErrors']}")
    if stats["alarmsOpened"] or stats["alarmsClosed"]:
        print(f"告警          : 触发 {stats['alarmsOpened']} 次、恢复 {stats['alarmsClosed']} 次")
    for name, device_stats in stats["devices"].items():
        print(f"  - {name:<12} 轮次 {device_stats['cycles']:<5} "
              f"样本 {device_stats['samples']:<6} 成功率 {device_stats['successRate']:.1%}")
    print(f"运行记录 id   : {run_id}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
