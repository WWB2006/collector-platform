#!/usr/bin/env python
"""初始化数据库：建表 + 把配置里的设备同步进去。

用法::

    python scripts/init_db.py --config config/app.json

幂等：重复执行不会重复插设备（``name`` 是唯一键），也不会重复建表。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import ConfigError, load_config  # noqa: E402
from app.logging_setup import setup_logging  # noqa: E402
from app.storage import Database, Repository  # noqa: E402
from app.storage.migrations import current_version  # noqa: E402

EXPECTED_TABLES = ("alarms", "devices", "runs", "samples", "schema_version")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="init_db.py",
        description="初始化 SQLite 数据库并同步设备清单",
    )
    parser.add_argument(
        "--config",
        default=str(PROJECT_ROOT / "config" / "app.json"),
        help="配置文件路径（默认 config/app.json）",
    )
    parser.add_argument("--quiet", action="store_true", help="只输出结果摘要")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        # 配置错误是使用者最常见的问题，直接给一句人话，不打印栈。
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 2

    if not args.quiet:
        setup_logging(config.log_level, config.log_dir_path, console_level="info")

    db_path = config.database_path
    db = Database(db_path)
    try:
        db.init_schema()
        repo = Repository(db)
        mapping = repo.sync_devices(config.devices)

        tables = sorted(
            row["name"]
            for row in db.query(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        )
        missing = [t for t in EXPECTED_TABLES if t not in tables]

        print(f"数据库        : {db_path}")
        print(f"schema 版本   : {current_version(db)}")
        print(f"表            : {', '.join(tables)}")
        print(f"设备          : {len(mapping)} 台")
        for name, device_id in sorted(mapping.items()):
            row = repo.get_device(name)
            flag = "启用" if row and row["enabled"] else "停用"
            print(f"  - id={device_id:<3} {name:<12} {row['transport']:<4} "
                  f"{row['address']:<24} 周期={row['poll_interval_ms']}ms {flag}")
        print(f"样本行数      : {repo.count_samples()}")

        if missing:
            print(f"[失败] 缺少表：{', '.join(missing)}", file=sys.stderr)
            return 1
        print("初始化完成。")
        return 0
    finally:
        db.close_all()


if __name__ == "__main__":
    raise SystemExit(main())
