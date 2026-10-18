"""报告模块用例。

最关键的一条是 :func:`test_report_is_self_contained`：
报告是要当附件发出去的东西，一旦引用了 CDN 上的 CSS/JS，
同事断网打开就是一片空白。所以这里直接断言 HTML 里**没有任何外部资源引用**。

第二条是 :func:`test_report_is_a_snapshot`：生成之后再往库里写数据，
已经生成的 HTML 不能变——否则「报告数字与页面不一致」这个经典问题会重现。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.api.time_range import parse_time_arg
from app.api.errors import InvalidParameterError
from app.report import (
    ReportConfig,
    build_conclusion,
    build_context,
    default_output_path,
    generate_report,
    load_report_config,
    render_report,
)
from app.report import charts
from app.quality import QUALITY_OK, QUALITY_TIMEOUT
from app.storage.repository import LATENCY_METRIC

BASE = 1_700_000_000_000


@pytest.fixture
def seeded(repo, app_config):
    """造一台设备 + 一批包含成功/超时/CRC 的样本。"""
    device_id = repo.upsert_device("plc-01", "tcp", "127.0.0.1:5020", 1, 500)
    repo.sync_devices(app_config.devices)
    rows = []
    for i in range(60):
        ts = BASE + i * 1000
        if i % 20 == 19:
            rows.append((device_id, ts, LATENCY_METRIC, 800.0, QUALITY_TIMEOUT))
        elif i % 17 == 16:
            rows.append((device_id, ts, LATENCY_METRIC, 0.0, 2))
        else:
            rows.append((device_id, ts, LATENCY_METRIC, 10.0 + i % 10, QUALITY_OK))
    repo.insert_samples(rows)
    repo.open_alarm(device_id, "timeout-streak", BASE + 19_000, "连续 3 次 timeout")
    return repo


# ---------------------------------------------------------------------------
# 图表（服务端 SVG）
# ---------------------------------------------------------------------------


def test_line_chart_renders_svg() -> None:
    svg = charts.line_chart(
        [{"ts": BASE + i * 1000, "avg": 10.0 + i, "max": 20.0 + i, "count": 5}
         for i in range(10)]
    )
    assert svg.startswith("<svg")
    assert svg.endswith("</svg>")
    assert "polyline" in svg
    # 唯一的 http 出现处应当是 SVG 命名空间，不是外部资源
    assert svg.count("http") == 1
    assert 'xmlns="http://www.w3.org/2000/svg"' in svg
    assert "src=" not in svg and "href=" not in svg


def test_line_chart_handles_empty() -> None:
    svg = charts.line_chart([])
    assert "<svg" in svg
    assert "没有可绘制的数据" in svg


def test_line_chart_single_point() -> None:
    svg = charts.line_chart([{"ts": BASE, "avg": 12.0, "max": 12.0, "count": 1}])
    assert "<circle" in svg


def test_bar_chart_renders() -> None:
    svg = charts.bar_chart([("ok", 90, charts.OK_COLOR), ("timeout", 10, charts.DANGER)])
    assert "<rect" in svg
    assert "ok" in svg
    assert "timeout" in svg


def test_bar_chart_handles_empty() -> None:
    assert "没有可统计的样本" in charts.bar_chart([])


def test_donut_chart_renders() -> None:
    svg = charts.donut_chart([("成功", 90, charts.OK_COLOR), ("失败", 10, charts.DANGER)],
                             center_label="90%")
    assert "stroke-dasharray" in svg
    assert "90%" in svg


def test_donut_chart_handles_empty() -> None:
    assert "无数据" in charts.donut_chart([])


# ---------------------------------------------------------------------------
# 报告配置
# ---------------------------------------------------------------------------


def test_report_config_defaults_when_file_missing(tmp_path: Path) -> None:
    config = load_report_config(tmp_path / "nope.json")
    assert config == ReportConfig()


def test_report_config_real_file(project_root: Path) -> None:
    config = load_report_config(project_root / "config" / "report.json")
    assert config.title
    assert config.chart_points == 120
    assert "overview" in config.sections


def test_report_config_bad_json(tmp_path: Path) -> None:
    from app.config import ConfigError

    path = tmp_path / "report.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="JSON 语法错误"):
        load_report_config(path)


# ---------------------------------------------------------------------------
# 结论
# ---------------------------------------------------------------------------


def test_conclusion_no_data() -> None:
    text = build_conclusion({"total": 0}, from_text="a", to_text="b")
    assert "没有任何采集记录" in text


def test_conclusion_stable() -> None:
    text = build_conclusion(
        {"total": 100, "ok": 100, "failed": 0, "successRate": 1.0,
         "latency": {"p50": 10.0, "p95": 20.0, "p99": 30.0}},
        from_text="a", to_text="b",
    )
    assert "链路稳定" in text
    assert "100 条样本" in text


def test_conclusion_degraded() -> None:
    text = build_conclusion(
        {"total": 100, "ok": 90, "failed": 10, "successRate": 0.9,
         "latency": {"p50": 10.0, "p95": 20.0, "p99": 30.0}},
        from_text="a", to_text="b",
    )
    assert "需要排查" in text


# ---------------------------------------------------------------------------
# 上下文（数据快照）
# ---------------------------------------------------------------------------


def test_context_contains_expected_keys(seeded, app_config) -> None:
    context = build_context(app_config, seeded, from_ts=BASE, to_ts=BASE + 60_000)
    payload = context.as_dict()
    for key in ("title", "conclusion", "summary", "devices", "series", "exceptions",
                "alarms", "environment", "configSnapshot", "charts"):
        assert key in payload, key
    assert payload["summary"]["total"] == 60
    assert set(payload["charts"]) == {"latency", "quality", "success"}


def test_context_exceptions_only_failures(seeded, app_config) -> None:
    context = build_context(app_config, seeded, from_ts=BASE, to_ts=BASE + 60_000)
    assert context.exceptions
    assert all(item["quality"] != 0 for item in context.exceptions)
    assert {item["qualityLabel"] for item in context.exceptions} <= {"timeout", "crc"}


def test_context_includes_alarms(seeded, app_config) -> None:
    context = build_context(app_config, seeded, from_ts=BASE, to_ts=BASE + 60_000)
    assert len(context.alarms) == 1
    assert context.alarms[0]["active"] is True


def test_context_environment_is_complete(seeded, app_config) -> None:
    env = build_context(app_config, seeded, from_ts=BASE, to_ts=BASE + 60_000).environment
    for key in ("collectorVersion", "python", "platform", "configPath", "database",
                "databaseSizeText", "sampleTotal", "devices", "poll", "generatedAtText"):
        assert key in env, key
    assert env["sampleTotal"] == 60
    assert {d["name"] for d in env["devices"]} == {"plc-01", "meter-02"}


def test_context_devices_sorted_by_name(seeded, app_config) -> None:
    context = build_context(app_config, seeded, from_ts=BASE, to_ts=BASE + 60_000)
    names = [d["device"] for d in context.devices]
    assert names == sorted(names)


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def test_rendered_html_contains_key_facts(seeded, app_config) -> None:
    html = generate_report(
        app_config, seeded, from_ts=BASE, to_ts=BASE + 60_000, range_label="1h"
    )[0]
    assert "设备数据采集测试报告" in html
    assert "总览" in html
    assert "质量码分布" in html
    assert "分设备统计" in html
    assert "告警记录" in html
    assert "异常明细" in html
    assert "环境信息" in html
    assert "配置快照" in html
    assert "plc-01" in html


def test_report_is_self_contained(seeded, app_config) -> None:
    """报告不能引用任何外部资源——断网打开必须照常显示。"""
    html = generate_report(app_config, seeded, from_ts=BASE, to_ts=BASE + 60_000)[0]
    for pattern in (r'<script[^>]+src=', r'<link[^>]+href="http', r'@import\s+url\(',
                    r'src="//', r'href="//'):
        assert not re.search(pattern, html, re.IGNORECASE), f"发现外部引用：{pattern}"
    # 允许 svg 里的 xmlns 命名空间（它不是网络请求）
    assert "cdn." not in html
    assert "unpkg.com" not in html
    assert "jsdelivr" not in html


def test_report_numbers_match_database(seeded, app_config) -> None:
    from app.analytics.statistics import summarize

    summary = summarize(seeded, from_ts=BASE, to_ts=BASE + 60_000, device="plc-01")
    html = generate_report(app_config, seeded, from_ts=BASE, to_ts=BASE + 60_000)[0]
    assert f"{summary.success_rate * 100:.2f}%" in html
    assert f"{summary.latency.p95:.1f}" in html


def test_report_is_a_snapshot(seeded, app_config) -> None:
    """快照的本质：渲染阶段不查库。

    所以先建 context（此时取数），再往库里写数据，
    然后用**同一个 context** 再渲染一次——两次 HTML 必须完全相同，
    且总览里的数字仍是旧的那一份。
    """
    context = build_context(
        app_config, seeded, from_ts=BASE, to_ts=BASE + 60_000, generated_at=BASE
    )
    html_first = render_report(context)
    assert context.summary["total"] == 60

    device_id = seeded.device_id("plc-01")
    seeded.insert_samples([(device_id, BASE + 30_000, LATENCY_METRIC, 999.0, QUALITY_OK)])
    assert seeded.count_samples() == 61

    html_second = render_report(context)
    assert html_first == html_second, "渲染阶段又去查了数据库，报告不再是快照"
    assert context.summary["total"] == 60, "快照里的数字被后续写入改变了"


def test_report_without_data_renders(seeded, app_config) -> None:
    html = generate_report(app_config, seeded, from_ts=BASE + 10**9, to_ts=BASE + 10**9 + 3600_000)[0]
    assert "没有任何采集记录" in html
    assert "没有可绘制的数据" in html
    assert "没有异常样本" in html


def test_render_requires_template(seeded, app_config, tmp_path: Path) -> None:
    context = build_context(app_config, seeded, from_ts=BASE, to_ts=BASE + 60_000)
    with pytest.raises(FileNotFoundError):
        render_report(context, template_path=tmp_path / "nope.j2")


def test_generate_report_writes_file(seeded, app_config, tmp_path: Path) -> None:
    out = tmp_path / "reports" / "r.html"
    html, path = generate_report(
        app_config, seeded, from_ts=BASE, to_ts=BASE + 60_000, output=out
    )
    assert path == out
    assert out.exists()
    assert out.read_text(encoding="utf-8") == html


def test_html_escapes_device_names(repo, app_config, tmp_path: Path) -> None:
    """设备名里的尖括号必须被转义，否则会破坏页面结构。"""
    repo.sync_devices(app_config.devices)
    repo.upsert_device("<script>alert(1)</script>", "tcp", "x")
    html = generate_report(app_config, repo, from_ts=BASE, to_ts=BASE + 1000)[0]
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_default_output_path_format() -> None:
    path = default_output_path("reports", BASE, BASE + 3600_000)
    assert path.parent == Path("reports")
    assert path.name.startswith("report-")
    assert path.suffix == ".html"


# ---------------------------------------------------------------------------
# 时间参数解析
# ---------------------------------------------------------------------------


def test_parse_time_arg_iso() -> None:
    ts = parse_time_arg("2026-09-19 09:00")
    assert ts > 0
    assert parse_time_arg("2026-09-19 09:00:00") == ts
    assert parse_time_arg("2026-09-19") < ts


def test_parse_time_arg_unix_ms() -> None:
    assert parse_time_arg("1700000000000") == 1_700_000_000_000


def test_parse_time_arg_slashes() -> None:
    assert parse_time_arg("2026/09/19 09:00") == parse_time_arg("2026-09-19 09:00")


@pytest.mark.parametrize("bad", ["", "not-a-time", "2026-13-45"])
def test_parse_time_arg_rejects_bad(bad: str) -> None:
    with pytest.raises(InvalidParameterError):
        parse_time_arg(bad)
