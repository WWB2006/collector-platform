"""接口的 Pydantic 响应模型。

用模型而不是裸 dict 的价值在 ``/docs``：接口文档里能看到每个字段的类型与含义，
前端不用来问「这个字段有没有可能为 null」。

字段名用 camelCase（与配置 JSON 一致），个别需要保留 ``from``/``to``
这类 Python 关键字的名字时用 alias。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _CamelModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True)


# ---------------------------------------------------------------------------
# 系统
# ---------------------------------------------------------------------------


class HealthOut(_CamelModel):
    status: str
    version: str
    uptimeSeconds: float
    devices: int
    enabledDevices: int
    alarmRules: int
    database: str
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 设备
# ---------------------------------------------------------------------------


class DeviceOut(_CamelModel):
    id: int
    name: str
    transport: str
    address: str
    unitId: int
    pollIntervalMs: int
    enabled: bool
    totalSamples: int = Field(description="库中该设备的样本总条数")
    lastSampleAt: int | None = Field(default=None, description="最近一条样本的时间戳（毫秒）")
    successRate: float | None = Field(
        default=None, description="最近一小时成功率；无数据时为 null"
    )


class DevicesResponse(_CamelModel):
    count: int
    devices: list[DeviceOut]


# ---------------------------------------------------------------------------
# 样本
# ---------------------------------------------------------------------------


class SampleOut(_CamelModel):
    id: int
    device: str
    ts: int = Field(description="Unix 毫秒")
    metric: str
    value: float
    quality: int = Field(description="0 ok / 1 timeout / 2 crc / 3 exception")
    qualityLabel: str


class SamplesResponse(_CamelModel):
    range: str = Field(description="回显请求的时间范围写法")
    from_ts: int = Field(alias="from", serialization_alias="from")
    to: int
    device: str | None = None
    metric: str | None = None
    total: int = Field(description="区间内总条数（不只是当前页）")
    limit: int
    offset: int
    items: list[SampleOut]


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------


class LatencyOut(_CamelModel):
    count: int
    mean: float
    p50: float
    p95: float
    p99: float
    min: float
    max: float


class QualityBucketOut(_CamelModel):
    count: int
    label: str


class SummaryOut(_CamelModel):
    device: str | None = None
    from_ts: int = Field(alias="from", serialization_alias="from")
    to: int
    total: int
    ok: int
    failed: int
    successRate: float
    latency: LatencyOut
    quality: dict[str, QualityBucketOut]


class SummaryResponse(SummaryOut):
    range: str
    devices: list[SummaryOut] = Field(default_factory=list)


class SeriesPointOut(_CamelModel):
    bucket: int
    ts: int
    avg: float | None = Field(default=None, description="桶内成功样本的平均响应时间（毫秒）")
    max: float | None = Field(default=None, description="桶内成功样本的最大响应时间（毫秒）")
    count: int = Field(description="桶内成功样本条数")
    failed: int = Field(
        default=0,
        description="桶内失败样本条数；失败样本不计入 avg/max（超时值不是测得的响应时间）",
    )


class SeriesResponse(_CamelModel):
    device: str | None = None
    metric: str
    from_ts: int = Field(alias="from", serialization_alias="from")
    to: int
    bucketMs: int
    points: list[SeriesPointOut]


# ---------------------------------------------------------------------------
# 告警
# ---------------------------------------------------------------------------


class AlarmOut(_CamelModel):
    id: int
    device: str
    rule: str
    startedAt: int
    endedAt: int | None = None
    detail: str | None = None
    active: bool = True
    durationMs: int | None = None


class AlarmsResponse(_CamelModel):
    active: int
    items: list[AlarmOut]


class AlarmRuleOut(_CamelModel):
    name: str
    device: str
    rule: str
    windowSec: int
    kind: str = Field(description="streak：连续次数；threshold：窗口阈值")
    subject: str
    operator: str
    threshold: float


class AlarmRulesResponse(_CamelModel):
    count: int
    rules: list[AlarmRuleOut]
