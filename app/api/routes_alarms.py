"""告警接口：当前活动告警、历史告警、以及规则清单。

规则清单单独开一个接口，是为了让面试/演示时能直接回答
「你这条 ``timeout>=3`` 到底是怎么解析的」——返回里带 ``kind``、
``subject``、``operator``、``threshold``，不用去翻配置文件。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.analytics.alarm_engine import AlarmEngine
from app.api.deps import get_alarm_engine, get_repo, resolve_device_id
from app.api.models import (
    AlarmOut,
    AlarmRulesResponse,
    AlarmRuleOut,
    AlarmsResponse,
)
from app.api.time_range import DEFAULT_RANGE, MAX_LIMIT, resolve_time_range
from app.storage import Repository

router = APIRouter(prefix="/api", tags=["alarms"])


def _to_alarm_out(row, *, now: int | None = None) -> AlarmOut:
    started_at = int(row["started_at"])
    ended_at = None if row["ended_at"] is None else int(row["ended_at"])
    return AlarmOut(
        id=int(row["id"]),
        device=str(row["device"]),
        rule=str(row["rule"]),
        startedAt=started_at,
        endedAt=ended_at,
        detail=row["detail"],
        active=ended_at is None,
        durationMs=(ended_at if ended_at is not None else now) - started_at
        if (ended_at is not None or now is not None)
        else None,
    )


@router.get("/alarms", response_model=AlarmsResponse, summary="活动告警与历史告警")
def list_alarms(
    device: str | None = Query(None, description="设备名；不传表示全部设备"),
    active_only: bool = Query(False, description="只看未恢复的告警"),
    range_: str = Query(DEFAULT_RANGE, alias="range", description="历史告警的时间范围"),
    limit: int = Query(100, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    engine: AlarmEngine = Depends(get_alarm_engine),
    repo: Repository = Depends(get_repo),
) -> AlarmsResponse:
    device_id = resolve_device_id(repo, device)
    active_rows = repo.active_alarms(device_id)

    if active_only:
        return AlarmsResponse(
            active=len(active_rows),
            items=[_to_alarm_out(row) for row in active_rows[:limit]],
        )

    window = resolve_time_range(range_)
    rows = repo.list_alarms(
        device_id=device_id,
        from_ts=window.from_ts,
        to_ts=window.to_ts,
        limit=limit,
        offset=offset,
    )
    active_ids = {int(row["id"]) for row in active_rows}
    items = [_to_alarm_out(row) for row in rows]

    # 活动告警可能早于时间窗口，但必须出现在列表里——否则页面会显示「无告警」
    # 而实际上设备正在告警中。
    listed_ids = {item.id for item in items}
    for row in active_rows:
        if int(row["id"]) not in listed_ids and int(row["id"]) in active_ids:
            items.insert(0, _to_alarm_out(row))

    return AlarmsResponse(active=len(active_rows), items=items)


@router.get(
    "/alarm-rules",
    response_model=AlarmRulesResponse,
    summary="告警规则清单（含解析结果）",
)
def list_alarm_rules(engine: AlarmEngine = Depends(get_alarm_engine)) -> AlarmRulesResponse:
    rules = [AlarmRuleOut(**item) for item in engine.describe_rules()]
    return AlarmRulesResponse(count=len(rules), rules=rules)
