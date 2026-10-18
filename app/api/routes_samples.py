"""样本接口：按时间范围分页查询。

三条硬性约束（规格「接口层」）：

* 必须有时间范围——默认最近一小时，不传就是 1h；
* 必须有分页上限——``limit`` 超过 :data:`MAX_LIMIT` 直接 400，而不是截断后装作没事；
* 时间范围有上限——``range=30d`` 会被拒绝（400），因为那是一次全表扫描。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_repo, resolve_device_id
from app.api.models import SampleOut, SamplesResponse
from app.api.time_range import (
    DEFAULT_LIMIT,
    DEFAULT_RANGE,
    MAX_LIMIT,
    resolve_time_range,
    validate_limit,
    validate_offset,
)
from app.quality import label as quality_label
from app.storage import Repository

router = APIRouter(prefix="/api", tags=["samples"])


@router.get(
    "/samples",
    response_model=SamplesResponse,
    summary="样本查询（分页 + 时间范围）",
)
def list_samples(
    device: str | None = Query(None, description="设备名；不传表示全部设备"),
    metric: str | None = Query(None, description="指标名，如 response_ms / register_0"),
    range_: str = Query(DEFAULT_RANGE, alias="range", description="时间范围，如 1h / 24h"),
    from_ts: int | None = Query(None, alias="from", description="起始 Unix 毫秒（左闭）"),
    to_ts: int | None = Query(None, alias="to", description="结束 Unix 毫秒（右开）"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="每页条数"),
    offset: int = Query(0, ge=0, description="偏移量"),
    newest: bool = Query(True, description="是否按时间倒序"),
    repo: Repository = Depends(get_repo),
) -> SamplesResponse:
    window = resolve_time_range(range_, from_ts=from_ts, to_ts=to_ts)
    validate_limit(limit)
    validate_offset(offset)
    device_id = resolve_device_id(repo, device)

    total = repo.count_samples(
        device_id, from_ts=window.from_ts, to_ts=window.to_ts, metric=metric
    )
    rows = repo.query_samples(
        device_id,
        from_ts=window.from_ts,
        to_ts=window.to_ts,
        metric=metric,
        limit=limit,
        offset=offset,
        newest_first=newest,
    )

    items = [
        SampleOut(
            id=int(row["id"]),
            device=str(row["device"]),
            ts=int(row["ts"]),
            metric=str(row["metric"]),
            value=float(row["value"]),
            quality=int(row["quality"]),
            qualityLabel=quality_label(int(row["quality"])),
        )
        for row in rows
    ]

    return SamplesResponse(
        range=window.label,
        from_ts=window.from_ts,
        to=window.to_ts,
        device=device,
        metric=metric,
        total=total,
        limit=limit,
        offset=offset,
        items=items,
    )
