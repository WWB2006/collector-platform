"""分析层：统计口径与告警规则判定。

依赖方向：``analytics`` 只**读** ``storage``，不写、不碰采集。

* :mod:`app.analytics.statistics`——成功率、分位数、质量码分布；
* :mod:`app.analytics.alarm_engine`——告警规则的解析与判定。
"""

from app.analytics.statistics import (
    DEFAULT_QUANTILES,
    LatencyStats,
    Summary,
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

__all__ = [
    "DEFAULT_QUANTILES",
    "LatencyStats",
    "Summary",
    "latency_stats",
    "latency_values",
    "mean",
    "ok_count",
    "overall_summary",
    "percentile",
    "quality_distribution",
    "quantiles",
    "series_for_chart",
    "success_rate",
    "summarize",
    "summarize_by_device",
    "summarize_samples",
]
