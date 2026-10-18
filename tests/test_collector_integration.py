"""与项目二从站模拟器的集成测试。

与 ``test_collector.py`` 的分工：

* ``test_collector.py`` 用假客户端，覆盖所有失败路径——**不依赖任何外部进程**；
* 本文件真起一个 Modbus 从站进程，验证「协议细节没理解错」：
  单元号怎么写、一次读几个寄存器、寄存器值确实对得上。

没有模拟器时整组跳过（CI 上不装 C++ 工具链也能绿），
但本地开发跑一次就能确认协议实现是对的——这正是规格里
「对同一个模拟器分别用 Modbus 与 CSV 采集，输出的结果对象字段一致」那条验证。
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path

import pytest

from app.collector import CollectResult, ModbusCollector
from app.config import DeviceConfig, PollConfig

pytestmark = pytest.mark.integration

#: 模拟器里 HR[0..3] 的配置值（见项目二 config/slave-simulator.json）。
EXPECTED_REGISTERS = (100, 250, 380, 7)


def find_simulator() -> Path | None:
    """定位项目二的从站模拟器；找不到返回 ``None``（用例会跳过）。"""
    override = os.environ.get("MODBUS_SIMULATOR")
    if override and Path(override).exists():
        return Path(override)

    gateway = Path(__file__).resolve().parent.parent.parent / "MyselfModbusGateway"
    for relative in (
        "build_portable/slave_simulator.exe",
        "build_portable/slave_simulator",
        "build/slave_simulator.exe",
        "build/slave_simulator",
    ):
        candidate = gateway / relative
        if candidate.exists():
            return candidate
    return None


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_port(port: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.3)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.2)
    return False


@pytest.fixture(scope="module")
def simulator():
    """起一个从站模拟器；没有可执行文件就跳过整组用例。"""
    binary = find_simulator()
    if binary is None:
        pytest.skip("未找到项目二的从站模拟器（可用 MODBUS_SIMULATOR 环境变量指定）")

    port = free_port()
    # 模拟器默认读它自己目录下的 config/slave-simulator.json，工作目录必须在仓库根。
    log_path = binary.parent.parent / "_simulator_test.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [str(binary), "--port", str(port), "--quiet"],
            cwd=str(binary.parent.parent),
            stdout=log,
            stderr=log,
        )
    try:
        if not wait_for_port(port):
            process.terminate()
            pytest.fail(f"模拟器未在 15 秒内监听端口 {port}，日志见 {log_path}")
        yield port
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()


def tcp_device(port: int, **overrides) -> DeviceConfig:
    payload = {
        "name": "plc-01",
        "transport": "tcp",
        "address": f"127.0.0.1:{port}",
        "unit_id": 1,
        "poll_interval_ms": 500,
        "metrics": ("response_ms", "register_0"),
    }
    payload.update(overrides)
    return DeviceConfig(**payload)


POLL = PollConfig(default_interval_ms=1000, timeout_ms=800, retry=1)


def test_reads_real_register_value(simulator: int) -> None:
    with ModbusCollector(tcp_device(simulator), POLL) as collector:
        results = collector.collect()
    assert [r.metric for r in results] == ["response_ms", "register_0"]
    assert all(r.quality == 0 for r in results), [r.quality for r in results]
    assert results[1].value == float(EXPECTED_REGISTERS[0])


def test_repeated_reads_are_stable(simulator: int) -> None:
    with ModbusCollector(tcp_device(simulator), POLL) as collector:
        values = [collector.collect()[1].value for _ in range(5)]
    assert values == [float(EXPECTED_REGISTERS[0])] * 5


def test_contiguous_block_reads_all_requested_registers(simulator: int) -> None:
    """一次请求读 0..3，四个寄存器的值与模拟器配置逐一对上。"""
    device = tcp_device(
        simulator,
        metrics=("response_ms", "register_0", "register_1", "register_2", "register_3"),
    )
    with ModbusCollector(device, POLL) as collector:
        results = collector.collect()
    assert [r.value for r in results[1:]] == [float(v) for v in EXPECTED_REGISTERS]


def test_latency_is_measured_and_positive(simulator: int) -> None:
    with ModbusCollector(tcp_device(simulator), POLL) as collector:
        results = collector.collect()
    assert results[0].value > 0.0


def test_dead_port_produces_timeout_quality() -> None:
    """连不上的端口也必须产出完整记录，质量码为超时。"""
    port = free_port()
    device = tcp_device(port, name="ghost")
    with ModbusCollector(device, POLL) as collector:
        results = collector.collect()
    assert len(results) == 2
    assert all(r.quality == 1 for r in results)


def test_result_shape_matches_csv_source(simulator: int, tmp_path: Path) -> None:
    """规格要求的验证：两种来源产出的结果对象字段与类型完全一致。"""
    from app.collector import CsvReplayCollector, write_replay_file
    from app.collector.csv_collector import ReplayRow

    path = write_replay_file(
        tmp_path / "r.csv",
        [(0, [ReplayRow("response_ms", 9.0, 0), ReplayRow("register_0", 100.0, 0)])],
    )
    csv_device = tcp_device(simulator, transport="csv", address=str(path))

    with ModbusCollector(tcp_device(simulator), POLL) as modbus:
        modbus_results = modbus.collect()
    csv_results = CsvReplayCollector(csv_device, POLL, path=path, loop=False).collect()

    def shape(results: list[CollectResult]):
        return [
            (r.metric, type(r.value).__name__, type(r.ts_ms).__name__,
             type(r.quality).__name__, type(r.device).__name__)
            for r in results
        ]

    assert shape(modbus_results) == shape(csv_results)
    assert [r.metric for r in modbus_results] == [r.metric for r in csv_results]
