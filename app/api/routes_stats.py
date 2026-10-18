"""统计接口：汇总与曲线。

两者都直接调用 :mod:`app.analytics.statistics` 里的函数，
**不在路由里重算任何口径**。报告模块也调同一批函数——
这样「页面上显示 92%，报告里写 96%」这类问题在结构上就不可能出现。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.analytics.statistics import overall_summary, series_for_chart, summarize
from app.api.deps import get_repo, resolve_device_id
from app.api.models import (
    LatencyOut,
    QualityBucketOut,
    SeriesPointOut,
    SeriesResponse,
    SummaryOut,
    SummaryResponse,
)
from app.api.time_range import (
    DEFAULT_BUCKETS,
    DEFAULT_RANGE,
    MAX_BUCKETS,
    resolve_time_range,
    validate_buckets,
)
from app.storage import Repository
from app.storage.repository import LATENCY_METRIC

router = APIRouter(prefix="/api/stats", tags=["stats"])


def _to_summary_out(payload: dict) -> SummaryOut:
    return SummaryOut(
        device=payload["device"],
        from_ts=payload["from"],
        to=payload["to"],
        total=payload["total"],
        ok=payload["ok"],
        failed=payload["failed"],
        successRate=payload["successRate"],
        latency=LatencyOut(**payload["latency"]),
        quality={
            code: QualityBucketOut(**bucket) for code, bucket in payload["quality"].items()
        },
    )


@router.get(
    "/summary",
    response_model=SummaryResponse,
    summary="成功率、延迟分位与质量码分布",
)
def stats_summary(
    device: str | None = Query(None, description="设备名；不传表示全部设备合并统计"),
    metric: str = Query(LATENCY_METRIC, description="参与延迟统计的指标"),
    range_: str = Query(DEFAULT_RANGE, alias="range", description="时间范围，如 1h / 24h"),
    from_ts: int | None = Query(None, alias="from"),
    to_ts: int | None = Query(None, alias="to"),
    repo: Repository = Depends(get_repo),
) -> SummaryResponse:
    window = resolve_time_range(range_, from_ts=from_ts, to_ts=to_ts)
    resolve_device_id(repo, device)  # 设备不存在时给 404，而不是返回一份空统计

    if device is None:
        payload = overall_summary(
            repo, from_ts=window.from_ts, to_ts=window.to_ts, metric=metric
        )
        return SummaryResponse(
            range=window.label,
            **_to_summary_out(payload).model_dump(),
            devices=[_to_summary_out(item) for item in payload["devices"]],
        )

    summary = summarize(
        repo, from_ts=window.from_ts, to_ts=window.to_ts, device=device, metric=metric
    )
    payload = summary.as_dict()
    return SummaryResponse(
        range=window.label,
        **_to_summary_out(payload).model_dump(),
        devices=[],
    )


@router.get(
    "/series",
    response_model=SeriesResponse,
    summary="响应时间曲线（分桶，点数固定）",
)
def stats_series(
    device: str | None = Query(None, description="设备名；不传表示全部设备"),
    metric: str = Query(LATENCY_METRIC, description="指标名"),
    range_: str = Query(DEFAULT_RANGE, alias="range", description="时间范围"),
    from_ts: int | None = Query(None, alias="from"),
    to_ts: int | None = Query(None, alias="to"),
    buckets: int = Query(DEFAULT_BUCKETS, ge=1, le=MAX_BUCKETS, description="桶数量"),
    repo: Repository = Depends(get_repo),
) -> SeriesResponse:
    window = resolve_time_range(range_, from_ts=from_ts, to_ts=to_ts)
    validate_buckets(buckets)
    resolve_device_id(repo, device)

    payload = series_for_chart(
        repo,
        from_ts=window.from_ts,
        to_ts=window.to_ts,
        buckets=buckets,
        device=device,
        metric=metric,
    )
    return SeriesResponse(
        device=payload["device"],
        metric=payload["metric"],
        from_ts=payload["from"],
        to=payload["to"],
        bucketMs=payload["bucketMs"],
        points=[SeriesPointOut(**point) for point in payload["points"]],
    )
