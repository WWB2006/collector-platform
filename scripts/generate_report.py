#!/usr/bin/env python
"""生成 HTML 测试报告。

用法::

    # 按相对时间范围（默认取 config/report.json 的 defaultRange）
    python scripts/generate_report.py --config config/app.json --range 1h

    # 指定绝对时间区间（本地时区，支持到分钟）
    python scripts/generate_report.py --config config/app.json \\
        --from "2026-09-19 09:00" --to "2026-09-19 10:00" \\
        --out reports/plc-01-20260919.html

时间参数三种写法都接受：``2026-09-19 09:00``、``2026-09-19 09:00:00``、
``2026-09-19``，以及纯 Unix 毫秒。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.api.time_range import (  # noqa: E402
    InvalidParameterError,
    parse_time_arg,
    resolve_time_range,
)
from app.config import ConfigError, load_config  # noqa: E402
from app.logging_setup import get_logger, setup_logging  # noqa: E402
from app.report import default_output_path, generate_report, load_report_config  # noqa: E402
from app.storage import Database, Repository  # noqa: E402

logger = get_logger("generate_report")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="generate_report.py", description="生成 HTML 测试报告")
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config" / "app.json"))
    parser.add_argument("--range", dest="range_text", default=None,
                        help="相对时间范围，如 1h / 24h（默认取 config/report.json）")
    parser.add_argument("--from", dest="from_text", default=None, help="起始时间")
    parser.add_argument("--to", dest="to_text", default=None, help="结束时间")
    parser.add_argument("--out", default=None, help="输出文件（默认 reports/report-*.html）")
    parser.add_argument("--title", default=None, help="覆盖报告标题")
    parser.add_argument("--report-config", default=None, help="报告配置（默认 config/report.json）")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = load_config(args.config)
        report_config = load_report_config(args.report_config)
    except ConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 2

    if not args.quiet:
        setup_logging(config.log_level, config.log_dir_path, console_level="warning")

    try:
        from_ts = parse_time_arg(args.from_text) if args.from_text else None
        to_ts = parse_time_arg(args.to_text) if args.to_text else None
        window = resolve_time_range(
            args.range_text or report_config.default_range,
            from_ts=from_ts,
            to_ts=to_ts,
        )
    except (ValueError, InvalidParameterError) as exc:
        print(f"[参数错误] {exc}", file=sys.stderr)
        return 2

    db = Database(config.database_path)
    try:
        db.init_schema()
        repo = Repository(db)

        total = repo.count_samples(from_ts=window.from_ts, to_ts=window.to_ts)
        if total == 0:
            print(
                f"[提示] 区间 {window.label} 内没有任何样本，报告仍会生成（会说明无数据）。",
                file=sys.stderr,
            )

        output = (
            Path(args.out)
            if args.out
            else default_output_path(PROJECT_ROOT / "reports", window.from_ts, window.to_ts)
        )
        html, path = generate_report(
            config,
            repo,
            from_ts=window.from_ts,
            to_ts=window.to_ts,
            range_label=window.label,
            output=output,
            report_config=report_config,
            title=args.title,
        )
    finally:
        db.close_all()

    print(f"报告已生成：{path}")
    print(f"  时间范围 : {window.label}")
    print(f"  样本条数 : {total}")
    print(f"  文件大小 : {len(html.encode('utf-8')) / 1024:.1f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
