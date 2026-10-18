"""统计层用例。

规格要求「用一批已知数据手工算出结果，与函数的输出逐项比对」，
所以 :data:`HAND_CHECKED` 那组数据的每个数字都是手算出来的，
断言写的是具体数值而不是「大于 0」——后者等于没测。

口径类用例（成功率分母、延迟分位只算成功样本）是这组里最关键的：
它们一旦被改错，报告与接口的数字会同时虚高，而且**不会报错**。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.analytics.statistics import (
    LatencyStats,
    latency_stats,
    latency_values,
    mean,
    ok_count,
    overall_summary,
    percentile,
    quality_distribution,
    quantiles,
    series_for_chart,
    success_rate,
    summarize,
    summarize_by_device,
    summarize_samples,
)
from app.quality import QUALITY_OK, QUALITY_TIMEOUT
from app.storage import Database, Repository
from app.storage.repository import LATENCY_METRIC

BASE = 1_700_000_000_000

#: 一组手工核算过的样本：(quality, response_ms)
#:
#: 5 条成功（10/20/30/40/50 ms）、2 条超时、1 条 CRC。
#: 手算结果：
#:   总数 8，成功 5，失败 3，成功率 5/8 = 0.625
#:   延迟样本 [10,20,30,40,50] -> P50=30, P95=50, P99=50, 均值=30, 最小=10, 最大=50
#:   质量码分布 {0: 5, 1: 2, 2: 1}
HAND_CHECKED: list[tuple[int, float]] = [
    (0, 10.0),
    (0, 20.0),
    (0, 30.0),
    (0, 40.0),
    (0, 50.0),
    (1, 800.0),
    (1, 800.0),
    (2, 0.0),
]


# ---------------------------------------------------------------------------
# 规格验收用例（逐字对齐）
# ---------------------------------------------------------------------------


def test_success_rate_excludes_timeouts_from_numerator_only() -> None:
    """规格验收：超时留在分母，只从分子里去掉。"""
    samples = [(0, 10.0), (0, 12.0), (1, 0.0), (0, 11.0)]
    assert success_rate(samples) == pytest.approx(0.75)


def test_percentile_matches_hand_calculation() -> None:
    """规格验收：最近秩法。"""
    values = [10, 20, 30, 40, 50]
    assert percentile(values, 50) == 30
    assert percentile(values, 95) == 50


def test_percentile_is_nearest_rank_not_interpolated() -> None:
    """插值法会给 48，最近秩法给 50——我们选后者，因为 48 这个值并不存在。"""
    assert percentile([10, 20, 30, 40, 50], 95) == 50
    assert percentile([10, 20, 30, 40, 50], 99) == 50
    assert percentile([1, 2, 3, 4], 25) == 1
    assert percentile([1, 2, 3, 4], 50) == 2
    assert percentile([1, 2, 3, 4], 75) == 3
    assert percentile([1, 2, 3, 4], 100) == 4


# ---------------------------------------------------------------------------
# 手工核算的整组比对
# ---------------------------------------------------------------------------


def test_hand_checked_sample_set() -> None:
    summary = summarize_samples(HAND_CHECKED, from_ts=BASE, to_ts=BASE + 60_000, device="plc-01")

    assert summary.total == 8
    assert summary.ok == 5
    assert summary.failed == 3
    assert summary.success_rate == pytest.approx(0.625)
    assert summary.quality == {0: 5, 1: 2, 2: 1}

    assert summary.latency.count == 5
    assert summary.latency.p50 == 30.0
    assert summary.latency.p95 == 50.0
    assert summary.latency.p99 == 50.0
    assert summary.latency.mean == 30.0
    assert summary.latency.minimum == 10.0
    assert summary.latency.maximum == 50.0


def test_timeout_values_do_not_pollute_latency() -> None:
    """两条 800ms 的超时如果混进延迟统计，P95 会从 50 跳到 800。"""
    values = latency_values(HAND_CHECKED)
    assert values == [10.0, 20.0, 30.0, 40.0, 50.0]
    assert latency_stats(values).p95 == 50.0


# ---------------------------------------------------------------------------
# 成功率口径的边界
# ---------------------------------------------------------------------------


def test_success_rate_empty_is_zero_not_one() -> None:
    """没有数据不能显示成 100% 正常。"""
    assert success_rate([]) == 0.0


def test_success_rate_all_ok() -> None:
    assert success_rate([(0, 1.0), (0, 2.0)]) == 1.0


def test_success_rate_all_failed() -> None:
    assert success_rate([(1, 0.0), (2, 0.0), (3, 0.0)]) == 0.0


def test_success_rate_degrades_as_failures_grow() -> None:
    """设备越坏成功率必须越低——这是「超时剔除分母」那个 bug 的回归用例。"""
    rates = [
        success_rate([(0, 1.0)] * ok + [(1, 0.0)] * bad)
        for ok, bad in ((10, 0), (9, 1), (5, 5), (1, 9), (0, 10))
    ]
    assert rates == [1.0, 0.9, 0.5, 0.1, 0.0]
    assert rates == sorted(rates, reverse=True)


def test_ok_count() -> None:
    assert ok_count(HAND_CHECKED) == 5
    assert ok_count([]) == 0


# ---------------------------------------------------------------------------
# 分位数与基础统计
# ---------------------------------------------------------------------------


def test_percentile_single_value() -> None:
    assert percentile([42.0], 50) == 42.0
    assert percentile([42.0], 99) == 42.0


def test_percentile_empty() -> None:
    assert percentile([], 95) == 0.0


@pytest.mark.parametrize("bad", [0, -1, 101, 1000])
def test_percentile_rejects_out_of_range(bad: float) -> None:
    with pytest.raises(ValueError):
        percentile([1.0, 2.0], bad)


def test_percentile_does_not_mutate_input() -> None:
    values = [30.0, 10.0, 20.0]
    percentile(values, 50)
    assert values == [30.0, 10.0, 20.0]


def test_mean() -> None:
    assert mean([10.0, 20.0, 30.0]) == 20.0
    assert mean([]) == 0.0


def test_quantiles_map() -> None:
    assert quantiles([10, 20, 30, 40, 50]) == {50: 30.0, 95: 50.0, 99: 50.0}


def test_latency_stats_empty() -> None:
    stats = latency_stats([])
    assert stats == LatencyStats()
    assert stats.count == 0
    assert stats.p95 == 0.0


def test_latency_stats_as_dict_rounds() -> None:
    stats = latency_stats([10.123456, 20.987654])
    payload = stats.as_dict()
    assert payload["p50"] == round(10.123456, 3)
    assert payload["max"] == round(20.987654, 3)
    assert payload["count"] == 2


# ---------------------------------------------------------------------------
# 质量码分布
# ---------------------------------------------------------------------------


def test_quality_distribution() -> None:
    assert quality_distribution(HAND_CHECKED) == {0: 5, 1: 2, 2: 1}


def test_quality_distribution_empty() -> None:
    assert quality_distribution([]) == {}


def test_quality_distribution_unknown_code_is_kept() -> None:
    """脏数据也要能显示出来，不能因为码值不认识就丢掉。"""
    assert quality_distribution([(0, 1.0), (9, 1.0)]) == {0: 1, 9: 1}


# ---------------------------------------------------------------------------
# 与存储层结合
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_repo(repo: Repository) -> Repository:
    device_id = repo.upsert_device("plc-01", "tcp", "x", 1, 500)
    rows = [
        (device_id, BASE + i * 1000, LATENCY_METRIC, value, quality)
        for i, (quality, value) in enumerate(HAND_CHECKED)
    ]
    # 混入非延迟指标，验证统计不会把它算进分位数
    rows.append((device_id, BASE, "register_0", 100.0, 0))
    rows.append((device_id, BASE, "register_1", 250.0, 0))
    repo.insert_samples(rows)
    return repo


def test_summarize_from_repository(seeded_repo: Repository) -> None:
    summary = summarize(seeded_repo, from_ts=BASE, to_ts=BASE + 60_000, device="plc-01")
    assert summary.total == 8
    assert summary.success_rate == pytest.approx(0.625)
    assert summary.latency.p95 == 50.0


def test_summarize_defaults_to_latency_metric(seeded_repo: Repository) -> None:
    """register_0=100 / register_1=250 不能被算成延迟。"""
    summary = summarize(seeded_repo, from_ts=BASE, to_ts=BASE + 60_000, device="plc-01")
    assert summary.latency.maximum == 50.0
    assert summary.latency.count == 5


def test_summarize_all_devices(seeded_repo: Repository) -> None:
    other = seeded_repo.upsert_device("meter-02", "csv", "y")
    seeded_repo.insert_samples([(other, BASE, LATENCY_METRIC, 15.0, 0)])
    combined = summarize(seeded_repo, from_ts=BASE, to_ts=BASE + 60_000)
    assert combined.total == 9
    assert combined.device is None


def test_summarize_by_device(seeded_repo: Repository) -> None:
    other = seeded_repo.upsert_device("meter-02", "csv", "y")
    seeded_repo.insert_samples([(other, BASE, LATENCY_METRIC, 15.0, 0)])
    summaries = summarize_by_device(seeded_repo, from_ts=BASE, to_ts=BASE + 60_000)
    by_name = {s.device: s for s in summaries}
    assert by_name["plc-01"].total == 8
    assert by_name["meter-02"].total == 1
    assert by_name["meter-02"].success_rate == 1.0


def test_summarize_respects_time_range(seeded_repo: Repository) -> None:
    """左闭右开：前 3 条落在 [BASE, BASE+3000) 里。"""
    summary = summarize(seeded_repo, from_ts=BASE, to_ts=BASE + 3000, device="plc-01")
    assert summary.total == 3


def test_summarize_empty_range(seeded_repo: Repository) -> None:
    summary = summarize(seeded_repo, from_ts=BASE + 10_000_000, to_ts=BASE + 20_000_000,
                        device="plc-01")
    assert summary.total == 0
    assert summary.success_rate == 0.0
    assert summary.latency.count == 0


def test_summarize_unknown_device_raises(seeded_repo: Repository) -> None:
    with pytest.raises(KeyError):
        summarize(seeded_repo, from_ts=BASE, to_ts=BASE + 1000, device="ghost")


def test_overall_summary_shape(seeded_repo: Repository) -> None:
    payload = overall_summary(seeded_repo, from_ts=BASE, to_ts=BASE + 60_000)
    assert payload["total"] == 8
    assert payload["device"] is None
    assert "devices" in payload
    assert payload["devices"][0]["device"] == "plc-01"
    assert payload["quality"]["0"]["label"] == "ok"
    assert payload["quality"]["1"]["label"] == "timeout"


def test_summary_as_dict_has_stable_keys(seeded_repo: Repository) -> None:
    payload = summarize(seeded_repo, from_ts=BASE, to_ts=BASE + 60_000, device="plc-01").as_dict()
    assert set(payload) == {
        "device", "from", "to", "total", "ok", "failed", "successRate", "latency", "quality"
    }
    assert set(payload["latency"]) == {"count", "mean", "p50", "p95", "p99", "min", "max"}


# ---------------------------------------------------------------------------
# 曲线数据
# ---------------------------------------------------------------------------


def test_series_point_count_is_bounded(repo: Repository) -> None:
    """无论区间多长，返回的点数都由 buckets 决定——防止页面被数据点拖垮。"""
    device_id = repo.upsert_device("plc-01", "tcp", "x", 1, 500)
    rows = [
        (device_id, BASE + i * 1000, LATENCY_METRIC, float(i % 50), 0) for i in range(3600)
    ]
    repo.insert_samples(rows)

    for buckets in (10, 60, 120):
        payload = series_for_chart(
            repo, from_ts=BASE, to_ts=BASE + 3_600_000, buckets=buckets, device="plc-01"
        )
        assert len(payload["points"]) <= buckets
        assert payload["bucketMs"] == 3_600_000 // buckets


def test_series_for_chart_rejects_bad_buckets(repo: Repository) -> None:
    with pytest.raises(ValueError):
        series_for_chart(repo, from_ts=BASE, to_ts=BASE + 1000, buckets=0)


def test_series_for_chart_metadata(repo: Repository) -> None:
    payload = series_for_chart(repo, from_ts=BASE, to_ts=BASE + 60_000, buckets=60)
    assert payload["metric"] == LATENCY_METRIC
    assert payload["device"] is None
    assert payload["from"] == BASE
    assert payload["to"] == BASE + 60_000
