"""采集层：把不同来源的数据统一成 :class:`CollectResult`。

对外只需要两个东西：

* :func:`build_collector`——按设备配置造出合适的采集器；
* :class:`CollectResult`——统计、告警、报告都只认这个类型。

上层不应直接 ``import`` 具体的 ``ModbusCollector`` / ``CsvReplayCollector``，
这样将来加一种来源（串口、OPC UA、MQTT）时只改这个文件。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.collector.base import (
    LATENCY_METRIC,
    Collector,
    CollectorError,
    CollectResult,
    parse_tcp_address,
    register_indices,
)
from app.collector.csv_collector import (
    CsvReplayCollector,
    ReplayGroup,
    ReplayRow,
    default_speed_from_env,
    load_replay_file,
    write_replay_file,
)
from app.collector.modbus_collector import ModbusCollector
from app.config import DeviceConfig, PollConfig

__all__ = [
    "Collector",
    "CollectorError",
    "CollectResult",
    "LATENCY_METRIC",
    "ModbusCollector",
    "CsvReplayCollector",
    "ReplayGroup",
    "ReplayRow",
    "build_collector",
    "load_replay_file",
    "parse_tcp_address",
    "register_indices",
    "write_replay_file",
]


def build_collector(
    device: DeviceConfig,
    poll: PollConfig,
    *,
    base_dir: str | Path | None = None,
    **options: Any,
) -> Collector:
    """按 ``device.transport`` 造采集器。

    :param base_dir: 解析 CSV 回放相对路径的基准目录（通常是项目根）。
    :param options: 透传给具体采集器的额外参数（如回放的 ``speed``/``loop``）。
    """
    transport = device.transport

    if transport == "tcp":
        tcp_options = {k: v for k, v in options.items() if k not in ("speed", "loop")}
        return ModbusCollector(device, poll, **tcp_options)

    if transport == "csv":
        path = Path(device.address)
        if not path.is_absolute() and base_dir is not None:
            path = Path(base_dir) / path
        options.setdefault("speed", default_speed_from_env())
        return CsvReplayCollector(device, poll, path=path, **options)

    if transport == "rtu":
        raise CollectorError(
            f"设备 {device.name} 使用串口采集（rtu），需要 pyserial 与真实串口。"
            "无串口环境请改用 CSV 回放：把 transport 设为 csv 并指向 data/replay/*.csv。"
        )

    raise CollectorError(f"设备 {device.name} 的传输类型无法识别：{transport!r}")
