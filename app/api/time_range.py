"""时间范围与分页参数的解析与校验。

规格要求「所有查询接口必须有时间范围与分页上限，默认只返回最近一小时」，
并且「超过上限的时间范围被拒绝」。这两条都收在这个文件里，
保证每个路由的行为完全一致——否则某个接口漏了上限，就是一次慢查询事故。

区间语义沿用全项目的约定：**左闭右开** ``[from_ts, to_ts)``。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime

from app.api.errors import InvalidParameterError

#: ``1h`` / ``30m`` / ``7d`` / ``90s``
RANGE_PATTERN = re.compile(r"^(\d+)([smhd])$")

UNIT_SECONDS: dict[str, int] = {"s": 1, "m": 60, "h": 3600, "d": 86400}

#: 命令行/接口里接受的绝对时间写法（本地时区）。
TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
)


def parse_time_arg(text: str) -> int:
    """把 ``2026-09-19 09:00`` 这类写法转成 Unix 毫秒（本地时区）。

    纯数字直接当 Unix 毫秒。解析失败时给出可用格式清单，而不是抛裸 ValueError。
    """
    cleaned = (text or "").strip()
    if not cleaned:
        raise InvalidParameterError("时间不能为空", code="invalid_time", field="time")
    if cleaned.isdigit():
        return int(cleaned)
    for fmt in TIME_FORMATS:
        try:
            return int(datetime.strptime(cleaned, fmt).timestamp() * 1000)
        except ValueError:
            continue
    raise InvalidParameterError(
        f"无法解析时间 {text!r}；支持 '2026-09-19 09:00'、'2026-09-19' 或 Unix 毫秒",
        code="invalid_time",
        field="time",
    )

#: 默认时间范围（规格：默认只返回最近一小时）。
DEFAULT_RANGE = "1h"

#: 允许的最大跨度。超过就拒绝——「查一年」这种请求会把库和内存一起拖垮。
MAX_RANGE_SECONDS = 7 * 86400

#: 分页默认与上限。
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000

#: 曲线默认点数上限（图表用）。
DEFAULT_BUCKETS = 120
MAX_BUCKETS = 500


@dataclass(frozen=True)
class TimeRange:
    """解析好的时间区间，``label`` 保留原始写法便于回显。"""

    from_ts: int
    to_ts: int
    label: str

    @property
    def span_seconds(self) -> float:
        return (self.to_ts - self.from_ts) / 1000.0

    def as_dict(self) -> dict[str, int | str]:
        return {"range": self.label, "from": self.from_ts, "to": self.to_ts}


def parse_range_seconds(text: str) -> int:
    """把 ``1h`` 这类写法转成秒数。"""
    match = RANGE_PATTERN.match((text or "").strip())
    if not match:
        raise InvalidParameterError(
            f"时间范围格式不正确：{text!r}；应形如 30m / 1h / 24h / 7d",
            code="invalid_range",
            field="range",
        )
    amount = int(match.group(1))
    unit = match.group(2)
    if amount <= 0:
        raise InvalidParameterError(
            f"时间范围必须为正数：{text!r}", code="invalid_range", field="range"
        )
    return amount * UNIT_SECONDS[unit]


def resolve_time_range(
    range_text: str | None = None,
    *,
    from_ts: int | None = None,
    to_ts: int | None = None,
    now_ms: int | None = None,
) -> TimeRange:
    """解析时间范围。

    两种写法二选一：

    * ``range=1h``——相对当前时刻（默认）；
    * ``from``/``to``——显式 Unix 毫秒区间。

    无论哪种，跨度都必须落在 ``MAX_RANGE_SECONDS`` 之内。
    """
    current = int(now_ms if now_ms is not None else time.time() * 1000)

    if from_ts is not None or to_ts is not None:
        start = int(from_ts) if from_ts is not None else current - 3600_000
        end = int(to_ts) if to_ts is not None else current
        if end <= start:
            raise InvalidParameterError(
                "to 必须大于 from", code="invalid_range", field="to"
            )
        label = f"{start}-{end}"
    else:
        text = (range_text or DEFAULT_RANGE).strip()
        seconds = parse_range_seconds(text)
        end = current
        start = current - seconds * 1000
        label = text

    span_seconds = (end - start) / 1000.0
    if span_seconds > MAX_RANGE_SECONDS:
        raise InvalidParameterError(
            f"时间范围过大：{label}（{span_seconds / 86400:.1f} 天），"
            f"上限为 {MAX_RANGE_SECONDS // 86400} 天",
            code="range_too_large",
            field="range",
        )
    return TimeRange(from_ts=start, to_ts=end, label=label)


def validate_limit(limit: int, *, maximum: int = MAX_LIMIT, field: str = "limit") -> int:
    if limit <= 0:
        raise InvalidParameterError(
            f"{field} 必须为正数，实际是 {limit}", code="invalid_parameter", field=field
        )
    if limit > maximum:
        raise InvalidParameterError(
            f"{field} 超过上限 {maximum}，实际是 {limit}",
            code="limit_too_large",
            field=field,
        )
    return limit


def validate_offset(offset: int, *, field: str = "offset") -> int:
    if offset < 0:
        raise InvalidParameterError(
            f"{field} 不能为负数，实际是 {offset}", code="invalid_parameter", field=field
        )
    return offset


def validate_buckets(buckets: int) -> int:
    return validate_limit(buckets, maximum=MAX_BUCKETS, field="buckets")
