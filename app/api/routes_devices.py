"""设备接口：清单与状态。

``/api/devices`` 不只是把 ``devices`` 表读出来——它还带上每台设备的
样本总数、最近采集时间与最近一小时成功率。这三个字段是「设备活着吗」
这个问题的答案，缺了它们运维还得自己去查库。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.analytics.statistics import summarize
from app.api.deps import get_repo
from app.api.models import DeviceOut, DevicesResponse
from app.storage import Repository
from app.storage.repository import LATENCY_METRIC

router = APIRouter(prefix="/api", tags=["devices"])

#: 设备状态里的成功率窗口：最近一小时（与接口默认时间范围一致）。
STATUS_WINDOW_MS = 3600_000


@router.get(
    "/devices",
    response_model=DevicesResponse,
    summary="设备清单与状态",
)
def list_devices(repo: Repository = Depends(get_repo)) -> DevicesResponse:
    devices: list[DeviceOut] = []
    for row in repo.list_devices():
        device_id = int(row["id"])
        last_ts = repo.last_sample_ts(device_id)
        success: float | None = None
        if last_ts is not None:
            summary = summarize(
                repo,
                from_ts=last_ts - STATUS_WINDOW_MS,
                to_ts=last_ts + 1,
                device=str(row["name"]),
                metric=LATENCY_METRIC,
            )
            success = round(summary.success_rate, 4) if summary.total else None

        devices.append(
            DeviceOut(
                id=device_id,
                name=str(row["name"]),
                transport=str(row["transport"]),
                address=str(row["address"]),
                unitId=int(row["unit_id"]),
                pollIntervalMs=int(row["poll_interval_ms"]),
                enabled=bool(row["enabled"]),
                totalSamples=repo.count_samples(device_id),
                lastSampleAt=last_ts,
                successRate=success,
            )
        )

    return DevicesResponse(count=len(devices), devices=devices)
