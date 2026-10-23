"""Modbus TCP 采集器。

三条实现要点（每一条都对应一个容易踩的坑）：

1. **一次采集 = 一次 Modbus 事务**。需要 ``register_0``/``register_1`` 时
   读 ``[0, max_index]`` 一整段，而不是每个寄存器发一次请求——
   后者会让 ``response_ms`` 失去意义（同一次采集测出好几个不同的耗时）。
2. **响应时间从发送前量到收到响应后**，并且**超时也算一条记录**。
   只在成功时记录延迟，会让「设备慢到超时」在统计里变成「没有数据」。
3. **失败必须产出完整结果集**（``response_ms`` 与每个 ``register_N`` 各一条），
   质量码统一非 0。否则一次采集成功/失败产出的行数不一样，
   按条数核对数据完整性时会永远对不上。
"""

from __future__ import annotations

import time
from typing import Any, Callable

from app.collector.base import (
    LATENCY_METRIC,
    MODBUS_MAX_REGISTERS_PER_READ,
    Collector,
    CollectorError,
    CollectResult,
    now_ms,
    parse_tcp_address,
    register_indices,
)
from app.config import DeviceConfig, PollConfig
from app.quality import (
    QUALITY_EXCEPTION,
    QUALITY_OK,
    QUALITY_TIMEOUT,
)

#: 默认客户端工厂的签名：``() -> ModbusTcpClient``。
ClientFactory = Callable[[], Any]


class ModbusCollector(Collector):
    """按配置读取保持寄存器，产出 ``response_ms`` 与 ``register_N``。"""

    def __init__(
        self,
        device: DeviceConfig,
        poll: PollConfig,
        *,
        client_factory: ClientFactory | None = None,
        **_ignored: Any,
    ) -> None:
        super().__init__(device, poll)
        self._host, self._port = parse_tcp_address(device.address)
        self._indices = register_indices(device.metrics)

        if self._indices:
            highest = max(self._indices)
            if highest >= MODBUS_MAX_REGISTERS_PER_READ:
                raise CollectorError(
                    f"设备 {device.name} 要求读寄存器 {highest}，"
                    f"但 Modbus 单次最多读 {MODBUS_MAX_REGISTERS_PER_READ} 个（0-124）"
                )
            self._read_count = highest + 1
        else:
            # 只要 response_ms 的设备也要发一次最小请求，否则测不出链路是否活着。
            self._read_count = 1

        self._client: Any | None = None
        self._client_factory: ClientFactory = client_factory or self._default_client

    # ---- 客户端 ---------------------------------------------------------

    def _default_client(self) -> Any:
        # 延迟导入：pymodbus 只在真正用 TCP 采集时才需要，
        # 让纯 CSV 回放场景（CI 里跑端到端）不依赖它。
        from pymodbus.client import ModbusTcpClient

        return ModbusTcpClient(
            self._host,
            port=self._port,
            # 重试策略由我们自己控制（语义更清楚：一次 collect 内重试 N 次），
            # 所以把 pymodbus 自带的重试关掉，避免 1×3 变成 3×3 次。
            retries=0,
            timeout=self.poll.timeout_ms / 1000.0,
        )

    def _ensure_client(self) -> Any:
        from pymodbus.exceptions import ConnectionException

        if self._client is None:
            self._client = self._client_factory()
        if not _is_connected(self._client):
            if not self._client.connect():
                self._drop_client()
                raise ConnectionException(f"无法连接 {self._host}:{self._port}")
        return self._client

    def _drop_client(self) -> None:
        """连接坏了就丢掉，下一轮重新建——半死的 socket 会一直返回同样的错误。"""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响主流程
                pass
            self._client = None

    def close(self) -> None:
        self._drop_client()

    # ---- 采集 -----------------------------------------------------------

    def collect(self) -> list[CollectResult]:
        from pymodbus.exceptions import (
            ConnectionException,
            ModbusException,
            ModbusIOException,
        )

        started = time.perf_counter()
        registers: list[int] = []
        quality = QUALITY_OK
        detail = ""

        for _attempt in range(self.poll.retry + 1):
            try:
                registers = self._read_registers()
            except ConnectionException as exc:
                quality, detail = QUALITY_TIMEOUT, f"连接失败：{exc}"
                self._drop_client()
            except ModbusIOException as exc:
                quality, detail = QUALITY_TIMEOUT, f"无响应：{exc}"
            except ModbusException as exc:
                quality, detail = QUALITY_EXCEPTION, f"协议异常：{exc}"
            except OSError as exc:
                quality, detail = QUALITY_TIMEOUT, f"网络错误：{exc}"
                self._drop_client()
            else:
                quality, detail = QUALITY_OK, ""
                break

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        ts = now_ms()

        results = [CollectResult(self.device.name, LATENCY_METRIC, elapsed_ms, ts, quality)]
        for index in self._indices:
            value = 0.0
            if quality == QUALITY_OK and index < len(registers):
                value = float(registers[index])
            results.append(
                CollectResult(self.device.name, f"register_{index}", value, ts, quality)
            )

        if quality != QUALITY_OK:
            # 记在结果对象之外的地方会丢上下文，所以带在最后一条的 value 上不可行；
            # 这里直接打到日志，靠日志里的设备名定位是哪台。
            _log_failure(self.device.name, detail)
        return results

    def _read_registers(self) -> list[int]:
        from pymodbus.exceptions import ModbusException

        client = self._ensure_client()
        response = client.read_holding_registers(
            0, count=self._read_count, device_id=self.device.unit_id
        )
        if response is None:
            raise ModbusException("未收到响应对象")
        if response.isError():
            code = getattr(response, "exception_code", None)
            if code:
                raise ModbusException(f"从站返回异常码 {code}")
            raise ModbusException(f"响应错误：{response}")
        return [int(v) for v in getattr(response, "registers", [])]


def _is_connected(client: Any) -> bool:
    """``connected`` 在 pymodbus 不同版本里既有属性也有方法的写法，两种都兼容。"""
    value = getattr(client, "connected", None)
    if callable(value):
        value = value()
    return bool(value)


def _log_failure(device: str, detail: str) -> None:
    from app.logging_setup import get_logger

    get_logger("collector.modbus").warning("设备 %s 采集失败：%s", device, detail)
