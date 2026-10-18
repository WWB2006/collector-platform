"""接口层用例。

规格要求「检查分页与边界参数（例如超过上限的时间范围被拒绝）」
与「对非法参数返回 400 而不是 500」，所以这组用例的重心在**边界与错误结构**：

* 每个错误响应都断言 ``{"error": {...}}`` 这个统一形状——
  否则前端要写两套解析逻辑；
* 未知设备名必须 404，而不是静默返回空列表（那是两件不同的事）；
* ``range=30d`` 必须被拒绝，否则一次请求就是一次全表扫描。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.time_range import (
    DEFAULT_LIMIT,
    DEFAULT_RANGE,
    MAX_LIMIT,
    MAX_RANGE_SECONDS,
    parse_range_seconds,
    resolve_time_range,
    validate_limit,
)
from app.api.errors import InvalidParameterError
from app.main import create_app
from app.quality import QUALITY_OK, QUALITY_TIMEOUT
from app.storage.repository import LATENCY_METRIC

#: 一个固定的历史时刻，只用于「纯函数」用例（时间范围解析等）。
BASE = 1_700_000_000_000


def now_ms() -> int:
    return int(time.time() * 1000)


def seed(
    api,
    samples=None,
    *,
    device: str = "plc-01",
    metric: str = LATENCY_METRIC,
    start: int | None = None,
    step: int = 1000,
) -> int:
    """往库里插样本，返回实际使用的起始时间戳。

    ``start`` 缺省时**自动把整批数据放进默认的最近一小时窗口内**：
    从「当前时间往前 span 秒」开始，最后一条落在 5 秒前。
    这样用例不必关心造了多少条数据——本文件最初就是因为
    「固定历史时间戳 + 接口按当前时间开窗」而整片查不到数据。
    """
    rows = samples if samples is not None else [(0, 10.0)] * 5
    if start is None:
        span = max(0, len(rows) - 1) * step
        start = now_ms() - span - 5000
    device_id = api.repo.device_id(device)
    payload = [
        (device_id, start + i * step, metric, float(value), int(quality))
        for i, (quality, value) in enumerate(rows)
    ]
    api.repo.insert_samples(payload)
    return start


# ---------------------------------------------------------------------------
# 健康检查与文档
# ---------------------------------------------------------------------------


def test_health(api) -> None:
    response = api.client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_openapi_lists_all_endpoints(api) -> None:
    paths = api.client.get("/openapi.json").json()["paths"]
    for path in ("/health", "/api/devices", "/api/samples", "/api/stats/summary",
                 "/api/stats/series", "/api/alarms", "/api/alarm-rules"):
        assert path in paths


# ---------------------------------------------------------------------------
# /api/devices
# ---------------------------------------------------------------------------


def test_devices_list(api) -> None:
    response = api.client.get("/api/devices")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    names = {d["name"] for d in body["devices"]}
    assert names == {"plc-01", "meter-02"}


def test_devices_status_fields(api) -> None:
    start = seed(api, [(0, 10.0), (1, 0.0)])
    device = next(d for d in api.client.get("/api/devices").json()["devices"]
                  if d["name"] == "plc-01")
    assert device["totalSamples"] == 2
    assert device["lastSampleAt"] == start + 1000
    assert device["successRate"] == pytest.approx(0.5)
    assert device["pollIntervalMs"] == 500
    assert device["enabled"] is True


def test_devices_without_data_have_null_status(api) -> None:
    device = next(d for d in api.client.get("/api/devices").json()["devices"]
                  if d["name"] == "meter-02")
    assert device["totalSamples"] == 0
    assert device["lastSampleAt"] is None
    assert device["successRate"] is None


# ---------------------------------------------------------------------------
# /api/samples
# ---------------------------------------------------------------------------


def test_default_range_is_one_hour(api) -> None:
    """规格验收用例。"""
    seed(api)
    response = api.client.get("/api/samples?device=plc-01")
    assert response.status_code == 200
    assert response.json()["range"] == "1h"


def test_samples_pagination(api) -> None:
    seed(api, [(0, float(i)) for i in range(250)])
    first = api.client.get("/api/samples?device=plc-01&limit=100&offset=0").json()
    assert first["total"] == 250
    assert len(first["items"]) == 100
    assert first["limit"] == DEFAULT_LIMIT or first["limit"] == 100

    third = api.client.get("/api/samples?device=plc-01&limit=100&offset=200").json()
    assert len(third["items"]) == 50


def test_samples_newest_first_by_default(api) -> None:
    seed(api, [(0, float(i)) for i in range(5)])
    items = api.client.get("/api/samples?device=plc-01").json()["items"]
    assert items[0]["ts"] > items[-1]["ts"]


def test_samples_oldest_first_when_asked(api) -> None:
    seed(api, [(0, float(i)) for i in range(5)])
    items = api.client.get("/api/samples?device=plc-01&newest=false").json()["items"]
    assert items[0]["ts"] < items[-1]["ts"]


def test_samples_quality_label(api) -> None:
    seed(api, [(0, 10.0), (1, 0.0)])
    items = api.client.get("/api/samples?device=plc-01&newest=false").json()["items"]
    assert items[0]["qualityLabel"] == "ok"
    assert items[1]["qualityLabel"] == "timeout"


def test_samples_metric_filter(api) -> None:
    seed(api, [(0, 10.0)], metric="response_ms")
    seed(api, [(0, 100.0)], metric="register_0")
    body = api.client.get("/api/samples?device=plc-01&metric=register_0").json()
    assert body["total"] == 1
    assert body["items"][0]["metric"] == "register_0"


def test_samples_limit_over_maximum_rejected(api) -> None:
    response = api.client.get(f"/api/samples?device=plc-01&limit={MAX_LIMIT + 1}")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_samples_limit_zero_rejected(api) -> None:
    assert api.client.get("/api/samples?device=plc-01&limit=0").status_code == 422


def test_samples_unknown_device_404(api) -> None:
    response = api.client.get("/api/samples?device=ghost")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert response.json()["error"]["field"] == "device"


def test_samples_range_too_large_rejected(api) -> None:
    """规格验收：30 天必须被拒绝。"""
    response = api.client.get("/api/samples?device=plc-01&range=30d")
    assert response.status_code in (400, 422)
    assert "时间范围过大" in response.json()["error"]["message"]


def test_samples_bad_range_format_rejected(api) -> None:
    response = api.client.get("/api/samples?range=soon")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_range"


def test_samples_explicit_from_to(api) -> None:
    start = seed(api, [(0, float(i)) for i in range(10)])
    body = api.client.get(
        f"/api/samples?device=plc-01&from={start}&to={start + 3000}"
    ).json()
    assert body["total"] == 3
    assert body["from"] == start
    assert body["to"] == start + 3000


def test_samples_from_after_to_rejected(api) -> None:
    response = api.client.get(f"/api/samples?from={BASE + 1000}&to={BASE}")
    assert response.status_code == 400
    assert response.json()["error"]["field"] == "to"


def test_samples_response_shape(api) -> None:
    seed(api)
    body = api.client.get("/api/samples?device=plc-01").json()
    assert set(body) >= {
        "range", "from", "to", "device", "metric", "total", "limit", "offset", "items"
    }
    assert set(body["items"][0]) == {
        "id", "device", "ts", "metric", "value", "quality", "qualityLabel"
    }


# ---------------------------------------------------------------------------
# /api/stats/summary
# ---------------------------------------------------------------------------


def test_summary_all_devices(api) -> None:
    seed(api, [(0, 10.0), (0, 12.0), (1, 0.0), (0, 11.0)])
    body = api.client.get("/api/stats/summary").json()
    assert body["range"] == "1h"
    assert body["total"] == 4
    assert body["successRate"] == pytest.approx(0.75)
    assert len(body["devices"]) == 2


def test_summary_single_device(api) -> None:
    seed(api, [(0, 10.0), (0, 12.0), (1, 0.0), (0, 11.0)])
    body = api.client.get("/api/stats/summary?device=plc-01&range=1h").json()
    assert body["device"] == "plc-01"
    assert body["successRate"] == pytest.approx(0.75)
    assert body["devices"] == []


def test_summary_latency_fields(api) -> None:
    seed(api, [(0, float(v)) for v in (10, 20, 30, 40, 50)])
    body = api.client.get("/api/stats/summary?device=plc-01").json()
    assert body["latency"]["count"] == 5
    assert body["latency"]["p50"] == 30.0
    assert body["latency"]["p95"] == 50.0
    assert body["latency"]["max"] == 50.0


def test_summary_quality_labels(api) -> None:
    seed(api, [(0, 10.0), (1, 0.0), (2, 0.0)])
    body = api.client.get("/api/stats/summary?device=plc-01").json()
    assert body["quality"]["0"] == {"count": 1, "label": "ok"}
    assert body["quality"]["1"] == {"count": 1, "label": "timeout"}
    assert body["quality"]["2"] == {"count": 1, "label": "crc"}


def test_summary_range_over_limit_is_rejected(api) -> None:
    """规格验收用例：``range=30d`` 必须被拒绝。"""
    response = api.client.get("/api/stats/summary?device=plc-01&range=30d")
    assert response.status_code in (400, 422)
    assert response.json()["error"]["code"] == "range_too_large"


def test_summary_unknown_device_404(api) -> None:
    assert api.client.get("/api/stats/summary?device=ghost").status_code == 404


def test_summary_empty_range_returns_zeros(api) -> None:
    body = api.client.get("/api/stats/summary?device=plc-01").json()
    assert body["total"] == 0
    assert body["successRate"] == 0.0
    assert body["latency"]["count"] == 0


# ---------------------------------------------------------------------------
# /api/stats/series
# ---------------------------------------------------------------------------


def test_series_bounded_points(api) -> None:
    seed(api, [(0, float(i % 30)) for i in range(600)])
    body = api.client.get("/api/stats/series?device=plc-01&buckets=30").json()
    assert len(body["points"]) <= 30
    assert body["bucketMs"] > 0
    assert body["metric"] == LATENCY_METRIC


def test_series_buckets_over_limit_rejected(api) -> None:
    assert api.client.get("/api/stats/series?buckets=9999").status_code == 422


def test_series_point_shape(api) -> None:
    seed(api, [(0, float(i)) for i in range(100)])
    point = api.client.get("/api/stats/series?device=plc-01&buckets=10").json()["points"][0]
    assert set(point) == {"bucket", "ts", "avg", "max", "count", "failed"}


def test_series_reports_failures_separately(api) -> None:
    """接口也要守住「失败样本不进 avg/max」这条口径，否则前端拿到的是错的。"""
    seed(api, [(0, 10.0), (0, 20.0), (1, 800.0), (0, 30.0)])
    body = api.client.get("/api/stats/series?device=plc-01&buckets=1").json()
    point = body["points"][0]
    assert point["avg"] == pytest.approx(20.0)
    assert point["max"] == pytest.approx(30.0)
    assert point["count"] == 3
    assert point["failed"] == 1


# ---------------------------------------------------------------------------
# /api/alarms
# ---------------------------------------------------------------------------


def test_alarms_empty(api) -> None:
    body = api.client.get("/api/alarms").json()
    assert body["active"] == 0
    assert body["items"] == []


def test_alarms_active_only(api) -> None:
    device_id = api.repo.device_id("plc-01")
    alarm_id = api.repo.open_alarm(device_id, "timeout-streak", now_ms(), "连续 3 次超时")
    body = api.client.get("/api/alarms?active_only=true").json()
    assert body["active"] == 1
    assert body["items"][0]["id"] == alarm_id
    assert body["items"][0]["active"] is True
    assert body["items"][0]["endedAt"] is None


def test_alarms_include_closed(api) -> None:
    device_id = api.repo.device_id("plc-01")
    started = now_ms() - 10_000
    alarm_id = api.repo.open_alarm(device_id, "r", started)
    api.repo.close_alarm(alarm_id, started + 5000, "已恢复")
    body = api.client.get("/api/alarms").json()
    assert body["active"] == 0
    assert body["items"][0]["active"] is False
    assert body["items"][0]["endedAt"] == started + 5000
    assert body["items"][0]["durationMs"] == 5000


def test_alarms_active_outside_window_still_listed(api) -> None:
    """活动告警即使开始时间早于窗口，也必须出现在列表里。

    否则页面会显示「无告警」，而设备其实正在告警中。
    """
    device_id = api.repo.device_id("plc-01")
    api.repo.open_alarm(device_id, "r", now_ms() - 10 * 86400_000)
    body = api.client.get("/api/alarms?range=1h").json()
    assert body["active"] == 1
    assert any(item["active"] for item in body["items"])


def test_alarms_unknown_device_404(api) -> None:
    assert api.client.get("/api/alarms?device=ghost").status_code == 404


def test_alarm_rules_endpoint(api) -> None:
    body = api.client.get("/api/alarm-rules").json()
    assert body["count"] == 2
    by_name = {rule["name"]: rule for rule in body["rules"]}
    assert by_name["timeout-streak"]["kind"] == "streak"
    assert by_name["timeout-streak"]["subject"] == "timeout"
    assert by_name["timeout-streak"]["threshold"] == 3.0
    assert by_name["p95-latency"]["kind"] == "threshold"
    assert by_name["p95-latency"]["operator"] == ">"


# ---------------------------------------------------------------------------
# 统一错误结构
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/samples?range=30d",
        "/api/samples?range=junk",
        "/api/samples?device=ghost",
        "/api/stats/summary?range=365d",
        "/api/stats/summary?device=ghost",
        "/api/alarms?device=ghost",
        "/api/does-not-exist",
    ],
)
def test_errors_share_one_shape(api, path: str) -> None:
    response = api.client.get(path)
    assert response.status_code >= 400
    body = response.json()
    assert "error" in body, body
    assert isinstance(body["error"]["code"], str)
    assert isinstance(body["error"]["message"], str)


def test_internal_error_is_structured(api, monkeypatch: pytest.MonkeyPatch) -> None:
    """未预料的异常也要返回结构化 JSON，而不是 HTML 错误页。

    ``TestClient`` 默认会把服务端异常重新抛给测试（方便调试），
    这里必须显式关掉，才能真正看到返回给客户端的响应。
    """
    def boom(*_args, **_kwargs):
        raise RuntimeError("模拟内部错误")

    monkeypatch.setattr("app.api.routes_devices.summarize", boom)
    device_id = api.repo.device_id("plc-01")
    api.repo.insert_samples([(device_id, now_ms(), LATENCY_METRIC, 1.0, QUALITY_OK)])

    with TestClient(api.client.app, raise_server_exceptions=False) as client:
        response = client.get("/api/devices")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"


# ---------------------------------------------------------------------------
# 时间范围解析（纯函数）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("30s", 30), ("5m", 300), ("1h", 3600), ("24h", 86400), ("7d", 604800)],
)
def test_parse_range_seconds(text: str, seconds: int) -> None:
    assert parse_range_seconds(text) == seconds


@pytest.mark.parametrize("bad", ["", "h", "1", "1x", "-1h", "0h", "1 h"])
def test_parse_range_rejects_bad_input(bad: str) -> None:
    with pytest.raises(InvalidParameterError):
        parse_range_seconds(bad)


def test_resolve_range_default_is_one_hour() -> None:
    window = resolve_time_range(None, now_ms=BASE)
    assert window.label == DEFAULT_RANGE
    assert window.to_ts == BASE
    assert window.to_ts - window.from_ts == 3600_000


def test_resolve_range_at_limit_is_allowed() -> None:
    window = resolve_time_range("7d", now_ms=BASE)
    assert window.span_seconds == MAX_RANGE_SECONDS


def test_resolve_range_over_limit_rejected() -> None:
    with pytest.raises(InvalidParameterError, match="时间范围过大"):
        resolve_time_range("8d", now_ms=BASE)


def test_resolve_range_from_to() -> None:
    window = resolve_time_range(None, from_ts=BASE, to_ts=BASE + 60_000)
    assert window.from_ts == BASE
    assert window.to_ts == BASE + 60_000


def test_resolve_range_from_to_over_limit() -> None:
    with pytest.raises(InvalidParameterError):
        resolve_time_range(None, from_ts=BASE, to_ts=BASE + 30 * 86400_000)


def test_validate_limit_boundaries() -> None:
    assert validate_limit(1) == 1
    assert validate_limit(MAX_LIMIT) == MAX_LIMIT
    with pytest.raises(InvalidParameterError):
        validate_limit(MAX_LIMIT + 1)
    with pytest.raises(InvalidParameterError):
        validate_limit(0)
