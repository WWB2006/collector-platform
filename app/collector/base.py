"""采集层的公共抽象。

这个项目最关键的一个抽象就在这个文件：:class:`CollectResult`。

无论数据来自 Modbus TCP、串口 RTU 还是 CSV 回放，采集器都只返回
「设备 + 指标 + 值 + 时间戳 + 质量码」这五元组。于是：

* 统计层不关心数据来源，同一份代码算所有设备的成功率与分位数；
* 告警引擎不关心数据来源；
* 报告不关心数据来源；
* 测试可以用假采集器注入任意序列，不必真连设备。

另一个约定：**采集失败也必须返回结果**（质量码非 0），而不是返回空列表或抛异常。
返回空列表会让「设备挂了」和「这个周期没轮到」变成同一件事。
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from app.config import DeviceConfig, PollConfig
from app.quality import QUALITY_OK

#: 响应时间指标的固定名字。所有采集器都必须产出它，否则统计层没有延迟数据。
LATENCY_METRIC = "response_ms"

#: 寄存器指标名的格式：``register_0`` / ``register_100``。
_REGISTER_PATTERN = re.compile(r"^register_(\d+)$")

#: Modbus 一次读保持寄存器的数量上限（协议规定 125）。
MODBUS_MAX_REGISTERS_PER_READ = 125


class CollectorError(Exception):
    """采集器无法建立（配置错、回放文件缺失等）。

    注意区分：**运行期采集失败不算异常**，那是质量码非 0 的结果；
    只有「这个采集器根本建不起来」才抛这个。
    """


@dataclass(frozen=True, slots=True)
class CollectResult:
    """一次采集产出的一条样本。"""

    device: str
    metric: str
    value: float
    ts_ms: int
    quality: int = QUALITY_OK

    @property
    def ok(self) -> bool:
        return self.quality == QUALITY_OK

    def as_row(self, device_id: int) -> tuple[int, int, str, float, int]:
        """转成仓储层要的元组，避免 storage 反向依赖 collector。"""
        return (device_id, self.ts_ms, self.metric, self.value, self.quality)

    @property
    def register_index(self) -> int | None:
        match = _REGISTER_PATTERN.match(self.metric)
        return int(match.group(1)) if match else None


def parse_tcp_address(address: str) -> tuple[str, int]:
    """把 ``127.0.0.1:5020`` 拆成 ``(host, port)``。

    出错时抛 :class:`CollectorError` 并说明期望格式——
    配置里写错地址是最常见的问题，报错必须直接指出。
    """
    text = address.strip()
    if ":" not in text:
        raise CollectorError(f"TCP 地址应形如 host:port，实际是 {address!r}")
    host, _, port_text = text.rpartition(":")
    if not host:
        raise CollectorError(f"TCP 地址缺少主机部分：{address!r}")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise CollectorError(f"TCP 端口不是整数：{address!r}") from exc
    if not (0 < port < 65536):
        raise CollectorError(f"TCP 端口超出范围：{address!r}")
    return host, port


def register_indices(metrics: tuple[str, ...] | list[str]) -> list[int]:
    """从指标名里解析出需要读的寄存器下标（保持顺序、去重）。"""
    indices: list[int] = []
    for metric in metrics:
        match = _REGISTER_PATTERN.match(metric)
        if match:
            index = int(match.group(1))
            if index not in indices:
                indices.append(index)
    return indices


def now_ms() -> int:
    import time

    return int(time.time() * 1000)


class Collector(ABC):
    """采集器基类。

    子类必须实现 :meth:`collect`，返回**这一次采集**产生的全部样本
    （包含 ``response_ms``）。调度器只管按周期调用，不关心实现细节。
    """

    def __init__(self, device: DeviceConfig, poll: PollConfig) -> None:
        self.device = device
        self.poll = poll

    @abstractmethod
    def collect(self) -> list[CollectResult]:
        """执行一次采集。失败时返回质量码非 0 的结果，而不是抛异常。"""

    def close(self) -> None:  # pragma: no cover - 默认无资源可释放
        """释放连接。默认什么都不做。"""

    @property
    def name(self) -> str:
        return self.device.name

    @property
    def pacing_ms(self) -> int:
        """调度器两次调用 :meth:`collect` 之间的间隔（毫秒）。

        默认就是设备配置里的 ``pollIntervalMs``。单独抽出来是因为**回放采集器
        需要按倍速改变节拍**：``speed=60`` 时它每 8 ms 采一次，
        于是「30 分钟的录制用 30 秒放完」。

        如果倍速只改时间戳、不改节拍，就会出现「墙钟跑了 45 秒、
        记下来的数据只跨 0.74 秒」这种自相矛盾的结果——报告上的时间轴也就没意义了。
        """
        return self.device.poll_interval_ms

    def __enter__(self) -> Collector:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<{type(self).__name__} device={self.device.name} transport={self.device.transport}>"
