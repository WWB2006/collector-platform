"""报告构建：把某一时刻的数据**冻结**成一份自包含的 HTML。

规格里有一条很容易被忽略的要求：「报告基于生成时刻的数据快照，生成后不再依赖数据库」。
这不是性能优化，是**可复现性**要求——报告一旦发出去，
别人打开时看到的数字必须还是你发出去时的那一份。
所以这里一次性把需要的数字全查出来放进 :class:`ReportContext`，
渲染阶段（Jinja2 模板 + SVG 图表）不再碰数据库。

报告里同时写入**环境信息**（平台、Python 版本、采集器版本、数据库路径、
设备来源与采集周期）。没有这些，报告里的数字无法解释——
「P95 = 12ms」在本地回环和跨机房里是两个完全不同的结论。
"""

from __future__ import annotations

import json
import platform
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from app import __version__
from app.analytics.statistics import overall_summary, series_for_chart
from app.config import AppConfig, ConfigError, PROJECT_ROOT
from app.logging_setup import get_logger
from app.quality import QUALITY_LABELS, label as quality_label
from app.report import charts
from app.storage import Repository
from app.storage.migrations import now_ms
from app.storage.repository import LATENCY_METRIC

logger = get_logger("report")

#: 默认模板位置。
DEFAULT_TEMPLATE = PROJECT_ROOT / "app" / "report" / "templates" / "report.html.j2"

#: 默认报告配置位置。
DEFAULT_REPORT_CONFIG = PROJECT_ROOT / "config" / "report.json"

#: 报告里最多列出的异常明细条数。
DEFAULT_MAX_EXCEPTIONS = 50


@dataclass(frozen=True)
class ReportConfig:
    """``config/report.json`` 的解析结果。"""

    title: str = "设备数据采集测试报告"
    default_range: str = "1h"
    sections: tuple[str, ...] = (
        "overview", "success_rate", "latency", "quality", "alarms", "env"
    )
    chart_points: int = 120
    max_exceptions: int = DEFAULT_MAX_EXCEPTIONS


def load_report_config(path: str | Path | None = None) -> ReportConfig:
    """读取报告配置；文件不存在时用默认值（报告生成不该因为少个配置文件就失败）。"""
    config_path = Path(path) if path is not None else DEFAULT_REPORT_CONFIG
    if not config_path.exists():
        return ReportConfig()
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"JSON 语法错误（第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}）", path=config_path
        ) from exc
    if not isinstance(payload, dict):
        raise ConfigError("应为对象", path=config_path)

    sections = payload.get("sections")
    return ReportConfig(
        title=str(payload.get("title") or ReportConfig.title),
        default_range=str(payload.get("defaultRange") or ReportConfig.default_range),
        sections=tuple(sections) if isinstance(sections, list) and sections
        else ReportConfig.sections,
        chart_points=int(payload.get("chartPoints") or ReportConfig.chart_points),
        max_exceptions=int(payload.get("maxExceptions") or DEFAULT_MAX_EXCEPTIONS),
    )


@dataclass
class ReportContext:
    """报告的全部内容，渲染时不再访问数据库。"""

    title: str
    generated_at: int
    from_ts: int
    to_ts: int
    range_label: str
    conclusion: str
    summary: dict[str, Any]
    devices: list[dict[str, Any]]
    series: dict[str, Any]
    exceptions: list[dict[str, Any]]
    alarms: list[dict[str, Any]]
    environment: dict[str, Any]
    config_snapshot: dict[str, Any]
    charts: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "generatedAt": self.generated_at,
            "generatedAtText": format_time(self.generated_at),
            "from": self.from_ts,
            "to": self.to_ts,
            "fromText": format_time(self.from_ts),
            "toText": format_time(self.to_ts),
            "rangeLabel": self.range_label,
            "conclusion": self.conclusion,
            "summary": self.summary,
            "devices": self.devices,
            "series": self.series,
            "exceptions": self.exceptions,
            "alarms": self.alarms,
            "environment": self.environment,
            "configSnapshot": self.config_snapshot,
            "charts": self.charts,
        }


def format_time(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


def build_conclusion(summary: dict[str, Any], *, from_text: str, to_text: str) -> str:
    """把统计数字写成一句人话——报告的第一段应该直接回答「好不好」。"""
    total = int(summary.get("total") or 0)
    if total == 0:
        return (
            f"{from_text} 至 {to_text} 区间内没有任何采集记录。"
            "请确认采集进程是否在运行，或时间范围是否选错。"
        )

    rate = float(summary.get("successRate") or 0.0)
    failed = int(summary.get("failed") or 0)
    latency = summary.get("latency") or {}
    p95 = float(latency.get("p95") or 0.0)

    if rate >= 0.99:
        verdict = "链路稳定"
    elif rate >= 0.95:
        verdict = "基本可用，存在零星失败"
    else:
        verdict = "链路异常，失败占比偏高，需要排查"

    return (
        f"{from_text} 至 {to_text} 区间内共采集 {total} 条样本，"
        f"成功率 {rate:.2%}（失败 {failed} 条），"
        f"成功样本的响应时间 P50 / P95 / P99 分别为 "
        f"{float(latency.get('p50') or 0):.1f} / {p95:.1f} / "
        f"{float(latency.get('p99') or 0):.1f} ms，整体判定为「{verdict}」。"
    )


def _database_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def build_environment(
    config: AppConfig,
    *,
    from_ts: int,
    to_ts: int,
    generated_at: int,
    sample_total: int,
) -> dict[str, Any]:
    """报告要能解释自己的数字，所以环境信息必须完整。"""
    db_path = config.database_path
    size = _database_size(db_path)
    return {
        "collectorVersion": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "generatedAt": generated_at,
        "generatedAtText": format_time(generated_at),
        "from": from_ts,
        "to": to_ts,
        "fromText": format_time(from_ts),
        "toText": format_time(to_ts),
        "configPath": str(config.source_path),
        "database": str(db_path),
        "databaseSizeBytes": size,
        "databaseSizeText": f"{size / 1024:.1f} KB" if size is not None else "未知",
        "sampleTotal": sample_total,
        "devices": [
            {
                "name": d.name,
                "transport": d.transport,
                "address": d.address,
                "unitId": d.unit_id,
                "pollIntervalMs": d.poll_interval_ms,
                "enabled": d.enabled,
                "metrics": list(d.metrics),
            }
            for d in config.devices
        ],
        "poll": {
            "defaultIntervalMs": config.poll.default_interval_ms,
            "timeoutMs": config.poll.timeout_ms,
            "retry": config.poll.retry,
        },
    }


def build_config_snapshot(config: AppConfig) -> dict[str, Any]:
    return {
        "sourcePath": str(config.source_path),
        "database": config.database,
        "logLevel": config.log_level,
        "poll": {
            "defaultIntervalMs": config.poll.default_interval_ms,
            "timeoutMs": config.poll.timeout_ms,
            "retry": config.poll.retry,
        },
        "devices": [
            {
                "name": d.name,
                "transport": d.transport,
                "address": d.address,
                "unitId": d.unit_id,
                "pollIntervalMs": d.poll_interval_ms,
                "metrics": list(d.metrics),
                "enabled": d.enabled,
            }
            for d in config.devices
        ],
        "alarms": [
            {"name": a.name, "device": a.device, "rule": a.rule, "windowSec": a.window_sec}
            for a in config.alarms
        ],
    }


def build_context(
    config: AppConfig,
    repo: Repository,
    *,
    from_ts: int,
    to_ts: int,
    range_label: str = "",
    report_config: ReportConfig | None = None,
    title: str | None = None,
    generated_at: int | None = None,
) -> ReportContext:
    """一次性取齐所有数据并算出图表（之后渲染不再碰数据库）。"""
    settings = report_config or load_report_config()
    timestamp = int(generated_at if generated_at is not None else now_ms())

    summary = overall_summary(repo, from_ts=from_ts, to_ts=to_ts, metric=LATENCY_METRIC)
    per_device = summary.get("devices", [])

    series = series_for_chart(
        repo,
        from_ts=from_ts,
        to_ts=to_ts,
        buckets=settings.chart_points,
        metric=LATENCY_METRIC,
    )

    exceptions = [
        {
            "id": int(row["id"]),
            "device": str(row["device"]),
            "ts": int(row["ts"]),
            "tsText": format_time(int(row["ts"])),
            "metric": str(row["metric"]),
            "value": float(row["value"]),
            "quality": int(row["quality"]),
            "qualityLabel": quality_label(int(row["quality"])),
        }
        for row in repo.failed_samples(
            from_ts=from_ts, to_ts=to_ts, limit=settings.max_exceptions
        )
    ]

    alarms = [
        {
            "id": int(row["id"]),
            "device": str(row["device"]),
            "rule": str(row["rule"]),
            "startedAt": int(row["started_at"]),
            "startedAtText": format_time(int(row["started_at"])),
            "endedAt": None if row["ended_at"] is None else int(row["ended_at"]),
            "endedAtText": None if row["ended_at"] is None else format_time(int(row["ended_at"])),
            "detail": row["detail"],
            "active": row["ended_at"] is None,
        }
        for row in repo.list_alarms(from_ts=from_ts, to_ts=to_ts, limit=200)
    ]

    environment = build_environment(
        config,
        from_ts=from_ts,
        to_ts=to_ts,
        generated_at=timestamp,
        sample_total=repo.count_samples(),
    )

    from_text = format_time(from_ts)
    to_text = format_time(to_ts)
    conclusion = build_conclusion(summary, from_text=from_text, to_text=to_text)

    quality_items = [
        (
            QUALITY_LABELS.get(int(code), f"unknown({code})"),
            float(bucket["count"]),
            charts.QUALITY_COLORS.get(int(code), charts.MUTED),
        )
        for code, bucket in sorted(summary.get("quality", {}).items(), key=lambda kv: int(kv[0]))
    ]
    total = int(summary.get("total") or 0)
    ok = int(summary.get("ok") or 0)

    rendered_charts = {
        "latency": charts.line_chart(series["points"], label="响应时间 (ms)"),
        "quality": charts.bar_chart(quality_items, label="质量码分布"),
        "success": charts.donut_chart(
            [
                ("成功", float(ok), charts.OK_COLOR),
                ("失败", float(total - ok), charts.DANGER),
            ],
            center_label=f"{ok / total:.0%}" if total else "",
        ),
    }

    return ReportContext(
        title=title or settings.title,
        generated_at=timestamp,
        from_ts=from_ts,
        to_ts=to_ts,
        range_label=range_label,
        conclusion=conclusion,
        summary=summary,
        devices=per_device,
        series=series,
        exceptions=exceptions,
        alarms=alarms,
        environment=environment,
        config_snapshot=build_config_snapshot(config),
        charts=rendered_charts,
    )


def render_report(context: ReportContext, *, template_path: str | Path | None = None) -> str:
    """用 Jinja2 渲染报告（自动转义，防止设备名等字段里的字符破坏页面）。"""
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    path = Path(template_path) if template_path else DEFAULT_TEMPLATE
    if not path.exists():
        raise FileNotFoundError(f"报告模板不存在：{path}")

    environment = Environment(
        loader=FileSystemLoader(str(path.parent)),
        autoescape=select_autoescape(["html", "xml", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    environment.filters["fmt_ms"] = lambda value: f"{float(value):.2f}"
    environment.filters["fmt_bytes"] = lambda value: (
        "未知" if value is None else f"{value / 1024:.1f} KB"
    )
    environment.filters["tojson_pretty"] = lambda value: json.dumps(
        value, ensure_ascii=False, indent=2
    )
    template = environment.get_template(path.name)
    return template.render(**context.as_dict())


def generate_report(
    config: AppConfig,
    repo: Repository,
    *,
    from_ts: int,
    to_ts: int,
    range_label: str = "",
    output: str | Path | None = None,
    report_config: ReportConfig | None = None,
    title: str | None = None,
    template_path: str | Path | None = None,
    generated_at: int | None = None,
) -> tuple[str, Path | None]:
    """构建 + 渲染 + （可选）写文件。返回 ``(html, 写入路径或 None)``。"""
    context = build_context(
        config,
        repo,
        from_ts=from_ts,
        to_ts=to_ts,
        range_label=range_label,
        report_config=report_config,
        title=title,
        generated_at=generated_at,
    )
    html = render_report(context, template_path=template_path)
    if output is None:
        return html, None
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    logger.info("报告已写入 %s（%d 字节）", path, len(html.encode("utf-8")))
    return html, path


def default_output_path(reports_dir: str | Path, from_ts: int, to_ts: int) -> Path:
    """默认文件名：``report-<起>-<止>.html``，便于多份报告共存。"""
    stamp_from = datetime.fromtimestamp(from_ts / 1000).strftime("%Y%m%d-%H%M")
    stamp_to = datetime.fromtimestamp(to_ts / 1000).strftime("%H%M")
    return Path(reports_dir) / f"report-{stamp_from}-{stamp_to}.html"


def section_enabled(settings: ReportConfig, name: str) -> bool:
    return name in settings.sections


def quality_legend() -> Sequence[tuple[int, str]]:
    return tuple(sorted(QUALITY_LABELS.items()))
