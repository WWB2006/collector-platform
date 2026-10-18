"""统计层：把样本算成人能看懂的数字。

本文件里的**统计口径**是全项目唯一的一份，接口、报告、告警引擎都必须调用这里的函数。
口径散落各处的后果很具体：报告说成功率 92%，接口说 96%，面试官一问就露馅。

三条口径（README「统计口径」一节有同样一份说明）：

1. **成功率 = 质量码为 0 的样本数 / 全部样本数**。
   超时样本留在**分母**里——把它们剔掉的话，设备越坏成功率越高。
   验收用例：``success_rate([(0, 10.0), (0, 12.0), (1, 0.0), (0, 11.0)]) == 0.75``。
2. **延迟分位只统计质量码为 0 的样本**。
   超时样本的 value 是「等了多少毫秒才放弃」（通常等于超时阈值），
   把它算进 P95 会让延迟曲线完全被超时次数主导，看不出真实性能。
   注意这与口径 1 并不矛盾：分母口径不同，因为回答的是两个不同的问题。
3. **分位数用最近秩法**（nearest-rank），不用线性插值。
   ``percentile([10,20,30,40,50], 95) == 50``；插值法会给出 48，
   而插值出来的「响应时间」在实际系统里并不存在。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from app.quality import QUALITY_LABELS, QUALITY_OK
from app.storage import Repository
from app.storage.repository import LATENCY_METRIC

#: 默认报告的分位数。
DEFAULT_QUANTILES = (50, 95, 99)

#: ``(quality, value)`` 样本对。
QualityValue = tuple[int, float]


# ---------------------------------------------------------------------------
# 纯函数：不碰数据库，最好测
# ---------------------------------------------------------------------------


def success_rate(samples: Sequence[QualityValue]) -> float:
    """成功率 = 质量码为 0 的条数 / 总条数。

    空输入返回 ``0.0``——「没有数据」不该被显示成「100% 正常」。
    """
    if not samples:
        return 0.0
    ok = sum(1 for quality, _ in samples if quality == QUALITY_OK)
    return ok / len(samples)


def ok_count(samples: Sequence[QualityValue]) -> int:
    return sum(1 for quality, _ in samples if quality == QUALITY_OK)


def percentile(values: Sequence[float], p: float) -> float:
    """最近秩法分位数。

    ``rank = ceil(p/100 * n)``，取排序后第 ``rank`` 个（下标从 1 起）。
    这保证结果**一定是数据里真实存在的值**，而不是插值出来的中间数。
    """
    if not 0 < p <= 100:
        raise ValueError(f"分位数必须在 (0, 100] 之间，实际是 {p}")
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100.0 * len(ordered)))
    return float(ordered[min(rank, len(ordered)) - 1])


def mean(values: Sequence[float]) -> float:
    return float(sum(values) / len(values)) if values else 0.0


def quantiles(values: Sequence[float], ps: Iterable[float] = DEFAULT_QUANTILES) -> dict[int, float]:
    return {int(p): percentile(values, p) for p in ps}


def quality_distribution(samples: Sequence[QualityValue]) -> dict[int, int]:
    """质量码分布 ``{码: 条数}``。只含出现过的码，便于前端按需渲染。"""
    distribution: dict[int, int] = {}
    for quality, _ in samples:
        distribution[int(quality)] = distribution.get(int(quality), 0) + 1
    return dict(sorted(distribution.items()))


def latency_values(samples: Sequence[QualityValue]) -> list[float]:
    """挑出参与延迟统计的值：只有质量码为 0 的样本。"""
    return [float(value) for quality, value in samples if quality == QUALITY_OK]


# ---------------------------------------------------------------------------
# 结果对象
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LatencyStats:
    """响应时间统计。``count == 0`` 表示区间内没有成功样本，此时各分位为 0。"""

    count: int = 0
    mean: float = 0.0
    p50: float = 0.0
    p95: float = 0.0
    p99: float = 0.0
    minimum: float = 0.0
    maximum: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "mean": round(self.mean, 3),
            "p50": round(self.p50, 3),
            "p95": round(self.p95, 3),
            "p99": round(self.p99, 3),
            "min": round(self.minimum, 3),
            "max": round(self.maximum, 3),
        }


@dataclass(frozen=True)
class Summary:
    """一个设备（或全部设备）在某个时间区间内的统计结果。"""

    from_ts: int
    to_ts: int
    total: int
    ok: int
    failed: int
    success_rate: float
    latency: LatencyStats
    quality: dict[int, int] = field(default_factory=dict)
    device: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "device": self.device,
            "from": self.from_ts,
            "to": self.to_ts,
            "total": self.total,
            "ok": self.ok,
            "failed": self.failed,
            "successRate": round(self.success_rate, 4),
            "latency": self.latency.as_dict(),
            "quality": {
                str(code): {
                    "count": count,
                    "label": QUALITY_LABELS.get(code, f"unknown({code})"),
                }
                for code, count in self.quality.items()
            },
        }
        return payload


def latency_stats(values: Sequence[float], ps: Iterable[float] = DEFAULT_QUANTILES) -> LatencyStats:
    """对一组延迟值算统计量。输入应当是已过滤掉失败样本的值。"""
    if not values:
        return LatencyStats()
    quantile_map = quantiles(values, ps)
    return LatencyStats(
        count=len(values),
        mean=mean(values),
        p50=quantile_map.get(50, 0.0),
        p95=quantile_map.get(95, 0.0),
        p99=quantile_map.get(99, 0.0),
        minimum=float(min(values)),
        maximum=float(max(values)),
    )


def summarize_samples(
    samples: Sequence[QualityValue],
    *,
    from_ts: int,
    to_ts: int,
    device: str | None = None,
    ps: Iterable[float] = DEFAULT_QUANTILES,
) -> Summary:
    """纯函数版本的汇总：给定样本列表，算出 :class:`Summary`。"""
    return Summary(
        from_ts=from_ts,
        to_ts=to_ts,
        total=len(samples),
        ok=ok_count(samples),
        failed=len(samples) - ok_count(samples),
        success_rate=success_rate(samples),
        latency=latency_stats(latency_values(samples), ps),
        quality=quality_distribution(samples),
        device=device,
    )


# ---------------------------------------------------------------------------
# 与存储层结合
# ---------------------------------------------------------------------------


def summarize(
    repo: Repository,
    *,
    from_ts: int,
    to_ts: int,
    device: str | None = None,
    metric: str = LATENCY_METRIC,
    ps: Iterable[float] = DEFAULT_QUANTILES,
) -> Summary:
    """从库里取样本并汇总。

    :param device: 设备名；``None`` 表示全部设备合并统计。
    :param metric: 参与统计的指标，默认 ``response_ms``。
        必须指定指标——否则 ``register_0`` 的值会被当成延迟算进分位数。
    """
    device_id = repo.device_id(device) if device is not None else None
    samples = repo.quality_values(device_id, from_ts=from_ts, to_ts=to_ts, metric=metric)
    return summarize_samples(samples, from_ts=from_ts, to_ts=to_ts, device=device, ps=ps)


def summarize_by_device(
    repo: Repository,
    *,
    from_ts: int,
    to_ts: int,
    metric: str = LATENCY_METRIC,
    ps: Iterable[float] = DEFAULT_QUANTILES,
) -> list[Summary]:
    """每台设备各算一份，按设备名排序。"""
    summaries: list[Summary] = []
    for row in repo.list_devices():
        name = str(row["name"])
        summaries.append(
            summarize(repo, from_ts=from_ts, to_ts=to_ts, device=name, metric=metric, ps=ps)
        )
    return summaries


def overall_summary(
    repo: Repository,
    *,
    from_ts: int,
    to_ts: int,
    metric: str = LATENCY_METRIC,
    ps: Iterable[float] = DEFAULT_QUANTILES,
) -> dict[str, Any]:
    """整体 + 分设备的完整汇总（接口与报告共用）。"""
    combined = summarize(repo, from_ts=from_ts, to_ts=to_ts, metric=metric, ps=ps)
    per_device = summarize_by_device(repo, from_ts=from_ts, to_ts=to_ts, metric=metric, ps=ps)
    payload = combined.as_dict()
    payload["devices"] = [s.as_dict() for s in per_device]
    return payload


def series_for_chart(
    repo: Repository,
    *,
    from_ts: int,
    to_ts: int,
    buckets: int = 120,
    device: str | None = None,
    metric: str = LATENCY_METRIC,
) -> dict[str, Any]:
    """按 ``buckets`` 个等宽桶取曲线数据。

    桶宽由区间长度和桶数算出，保证图表点数恒定——
    否则「查一小时」和「查一天」会把不同数量的点丢给前端，
    数据点一多页面就卡住（规格排错表里的「页面卡顿」）。
    """
    if buckets <= 0:
        raise ValueError("buckets 必须为正数")
    span = max(1, to_ts - from_ts)
    bucket_ms = max(1, span // buckets)
    device_id = repo.device_id(device) if device is not None else None
    points = repo.series(
        metric, device_id=device_id, from_ts=from_ts, to_ts=to_ts, bucket_ms=bucket_ms
    )
    return {
        "device": device,
        "metric": metric,
        "from": from_ts,
        "to": to_ts,
        "bucketMs": bucket_ms,
        "points": points,
    }
