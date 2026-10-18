"""CSV 回放采集器。

存在意义：**让整条链路在没有网络、没有串口、没有模拟器的机器上也能跑**。
CI、同事的笔记本、离线演示都靠它——这也是规格里「端到端测试用 CSV 回放驱动」的原因。

文件格式（表头必须存在，列名固定）::

    offset_ms,metric,value,quality
    0,response_ms,12.5,0
    0,register_0,100,0
    1000,response_ms,13.1,0
    1000,register_0,101,0

* ``offset_ms``：相对回放起点的毫秒偏移。**同一偏移的多行属于同一次采集**
  （一次采集产生 ``response_ms`` + 若干 ``register_N``），必须连续写在一起。
* ``quality``：直接写质量码，于是「注入超时」只需要写一行 ``1000,response_ms,800.0,1``，
  不需要任何故障注入框架。

回放语义（``speed`` 同时作用于**节拍**与**时间戳**）::

    时间戳 = 回放起点 + offset_ms / speed
    节拍   = pollIntervalMs / speed

于是 ``speed=60`` 表示「30 分钟的录制用 30 秒放完」，而且**记下来的数据
时间跨度恰好等于真实运行时长**——墙钟跑了 45 秒，时间轴就跨 45 秒。

这一点必须成对改：如果只压缩时间戳、不压缩节拍，就会出现
「墙钟跑 45 秒、数据只跨 0.74 秒」的自相矛盾结果，
报告上的时间轴与「最近一小时」查询都会失去意义。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from app.collector.base import Collector, CollectorError, CollectResult, now_ms
from app.config import DeviceConfig, PollConfig

#: 必须出现的表头列。
REQUIRED_COLUMNS = ("offset_ms", "metric", "value", "quality")


@dataclass(frozen=True, slots=True)
class ReplayRow:
    metric: str
    value: float
    quality: int


@dataclass(frozen=True, slots=True)
class ReplayGroup:
    """同一次采集产出的若干行。"""

    offset_ms: int
    rows: tuple[ReplayRow, ...]


def load_replay_file(path: str | Path) -> list[ReplayGroup]:
    """读取回放文件并按 ``offset_ms`` 分组。

    所有格式问题都在这里一次性报清楚（带行号），
    而不是等回放到一半才发现某一行是坏的。
    """
    file_path = Path(path)
    if not file_path.exists():
        raise CollectorError(f"回放文件不存在：{file_path}")

    groups: list[ReplayGroup] = []
    current_offset: int | None = None
    current_rows: list[ReplayRow] = []
    last_offset = -1

    with file_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        missing = [c for c in REQUIRED_COLUMNS if c not in fieldnames]
        if missing:
            raise CollectorError(
                f"回放文件表头缺少列 {missing}；实际表头为 {fieldnames}（文件 {file_path}）"
            )

        for line_no, raw in enumerate(reader, start=2):  # 第 1 行是表头
            offset_text = (raw.get("offset_ms") or "").strip()
            if not offset_text:
                raise CollectorError(f"第 {line_no} 行 offset_ms 为空（文件 {file_path}）")
            try:
                offset = int(offset_text)
            except ValueError as exc:
                raise CollectorError(
                    f"第 {line_no} 行 offset_ms 不是整数：{offset_text!r}（文件 {file_path}）"
                ) from exc
            if offset < last_offset:
                raise CollectorError(
                    f"第 {line_no} 行 offset_ms={offset} 小于上一行 {last_offset}；"
                    f"回放要求按时间递增（文件 {file_path}）"
                )

            metric = (raw.get("metric") or "").strip()
            if not metric:
                raise CollectorError(f"第 {line_no} 行 metric 为空（文件 {file_path}）")
            try:
                value = float((raw.get("value") or "").strip() or 0.0)
            except ValueError as exc:
                raise CollectorError(
                    f"第 {line_no} 行 value 不是数字：{raw.get('value')!r}（文件 {file_path}）"
                ) from exc
            try:
                quality = int((raw.get("quality") or "0").strip() or 0)
            except ValueError as exc:
                raise CollectorError(
                    f"第 {line_no} 行 quality 不是整数：{raw.get('quality')!r}（文件 {file_path}）"
                ) from exc

            if offset != current_offset:
                if current_rows:
                    groups.append(ReplayGroup(current_offset or 0, tuple(current_rows)))
                current_rows = []
                current_offset = offset
            current_rows.append(ReplayRow(metric, value, quality))
            last_offset = offset

    if current_rows and current_offset is not None:
        groups.append(ReplayGroup(current_offset, tuple(current_rows)))

    if not groups:
        raise CollectorError(f"回放文件没有数据行：{file_path}")
    return groups


def write_replay_file(
    path: str | Path, groups: Iterable[tuple[int, Sequence[ReplayRow]]]
) -> Path:
    """写一份回放文件（``scripts/seed_demo_data.py`` 与测试用）。"""
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with file_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(REQUIRED_COLUMNS)
        for offset, rows in groups:
            for row in rows:
                writer.writerow([offset, row.metric, f"{row.value:g}", row.quality])
    return file_path


class CsvReplayCollector(Collector):
    """按文件顺序回放采集结果，支持倍速与循环。"""

    def __init__(
        self,
        device: DeviceConfig,
        poll: PollConfig,
        *,
        path: str | Path | None = None,
        speed: float = 1.0,
        loop: bool = True,
        start_ts: int | None = None,
    ) -> None:
        super().__init__(device, poll)
        if speed <= 0:
            raise CollectorError(f"回放倍速必须为正数，实际是 {speed}")

        self.path = Path(path) if path is not None else Path(device.address)
        self._groups = load_replay_file(self.path)
        self._speed = float(speed)
        self._loop = loop
        self._epoch_ms = int(start_ts) if start_ts is not None else now_ms()
        self._cursor = 0
        self._last_ts = self._epoch_ms

    @property
    def group_count(self) -> int:
        return len(self._groups)

    @property
    def pacing_ms(self) -> int:
        """按倍速压缩节拍；至少 1 ms，避免除出 0 让调度器空转。"""
        return max(1, int(self.device.poll_interval_ms / self._speed))

    @property
    def finished(self) -> bool:
        """非循环模式下是否已经放完。"""
        return not self._loop and self._cursor >= len(self._groups)

    def reset(self) -> None:
        self._cursor = 0
        self._last_ts = self._epoch_ms

    def collect(self) -> list[CollectResult]:
        if self._cursor >= len(self._groups):
            if not self._loop:
                return []
            # 循环时把时间轴整体往前推**一个节拍**，而不是原地重来：
            # 原地重来会让第二轮的时间戳落回第一轮的区间里，
            # 「按时间范围查询」会把两轮数据混在一起。
            self._epoch_ms = self._last_ts + self.pacing_ms
            self._cursor = 0

        group = self._groups[self._cursor]
        self._cursor += 1
        ts = int(self._epoch_ms + group.offset_ms / self._speed)
        self._last_ts = ts

        return [
            CollectResult(self.device.name, row.metric, row.value, ts, row.quality)
            for row in group.rows
        ]


def default_speed_from_env(default: float = 1.0) -> float:
    """允许用环境变量统一调节回放速度，方便 CI 里加速。"""
    import os

    raw = os.environ.get("COLLECTOR_REPLAY_SPEED")
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


__all__ = [
    "CsvReplayCollector",
    "ReplayGroup",
    "ReplayRow",
    "REQUIRED_COLUMNS",
    "default_speed_from_env",
    "load_replay_file",
    "write_replay_file",
]
