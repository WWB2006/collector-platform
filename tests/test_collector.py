"""采集层用例。

全部用**假客户端**驱动，不连真实设备也不连网络（规格「测试夹具」的要求）：
真实网络的失败模式（超时、连接被拒、半死 socket）在 CI 上不可复现，
而这里要断言的恰恰是这些失败路径的质量码。

最值钱的两条：

* :func:`test_failure_produces_same_row_count_as_success`——
  失败时也必须产出与成功时**同样条数**的记录，
  否则「按条数核对数据完整性」永远对不上；
* :func:`test_timeout_is_recorded_not_skipped`——
  超时要落库，不能因为「没采到」就不写。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pymodbus.exceptions import (
    ConnectionException,
    ModbusException,
    ModbusIOException,
)

from app.collector import (
    CollectorError,
    CollectResult,
    CsvReplayCollector,
    ModbusCollector,
    build_collector,
    load_replay_file,
    parse_tcp_address,
    register_indices,
    write_replay_file,
)
from app.collector.csv_collector import ReplayRow
from app.config import DeviceConfig, PollConfig
from app.quality import (
    QUALITY_EXCEPTION,
    QUALITY_OK,
    QUALITY_TIMEOUT,
)

BASE_TS = 1_700_000_000_000


# ---------------------------------------------------------------------------
# 假客户端
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, registers: list[int] | None = None, exception_code: int | None = None):
        self.registers = list(registers or [])
        self.exception_code = exception_code

    def isError(self) -> bool:  # noqa: N802 - 对齐 pymodbus 的命名
        return self.exception_code is not None

    def __repr__(self) -> str:
        return f"<FakeResponse registers={self.registers} code={self.exception_code}>"


class FakeClient:
    """可编排的假 Modbus 客户端。

    ``script`` 是一串「每次调用发生什么」：
    ``FakeResponse`` 正常返回，``Exception`` 实例则抛出。
    """

    def __init__(self, script: list[Any] | None = None, *, connect_ok: bool = True):
        self._script = list(script or [FakeResponse([100, 250, 380, 7])])
        self._connect_ok = connect_ok
        self.connected = False
        self.connect_calls = 0
        self.close_calls = 0
        self.read_calls: list[tuple[int, int, int]] = []

    def connect(self) -> bool:
        self.connect_calls += 1
        if not self._connect_ok:
            return False
        self.connected = True
        return True

    def close(self) -> None:
        self.close_calls += 1
        self.connected = False

    def read_holding_registers(self, address: int, *, count: int = 1, device_id: int = 1):
        self.read_calls.append((address, count, device_id))
        if not self._script:
            return FakeResponse([0] * count)
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def make_device(**overrides: Any) -> DeviceConfig:
    """构造 DeviceConfig。

    注意用 snake_case：配置 JSON 是 camelCase，但 dataclass 字段是 snake_case，
    两者只在 ``app/config.py`` 的解析层转换一次。
    """
    payload: dict[str, Any] = {
        "name": "plc-01",
        "transport": "tcp",
        "address": "127.0.0.1:5020",
        "unit_id": 1,
        "poll_interval_ms": 500,
        "metrics": ("response_ms", "register_0"),
    }
    payload.update(overrides)
    return DeviceConfig(**payload)


POLL = PollConfig(default_interval_ms=1000, timeout_ms=800, retry=2)


# ---------------------------------------------------------------------------
# CollectResult
# ---------------------------------------------------------------------------


def test_collect_result_fields() -> None:
    result = CollectResult("plc-01", "response_ms", 12.5, BASE_TS, QUALITY_OK)
    assert result.ok is True
    assert result.as_row(7) == (7, BASE_TS, "response_ms", 12.5, 0)


def test_collect_result_register_index() -> None:
    assert CollectResult("d", "register_12", 1.0, BASE_TS).register_index == 12
    assert CollectResult("d", "response_ms", 1.0, BASE_TS).register_index is None


def test_collect_result_is_frozen() -> None:
    result = CollectResult("d", "m", 1.0, BASE_TS)
    with pytest.raises(Exception):
        result.value = 2.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 地址与指标解析
# ---------------------------------------------------------------------------


def test_parse_tcp_address() -> None:
    assert parse_tcp_address("127.0.0.1:5020") == ("127.0.0.1", 5020)
    assert parse_tcp_address("plc.local:502") == ("plc.local", 502)


@pytest.mark.parametrize("bad", ["127.0.0.1", "127.0.0.1:", ":5020", "127.0.0.1:abc", "127.0.0.1:0"])
def test_parse_tcp_address_errors(bad: str) -> None:
    with pytest.raises(CollectorError):
        parse_tcp_address(bad)


def test_register_indices() -> None:
    assert register_indices(("response_ms", "register_0")) == [0]
    assert register_indices(("register_3", "register_1", "register_3")) == [3, 1]
    assert register_indices(("response_ms",)) == []


# ---------------------------------------------------------------------------
# ModbusCollector
# ---------------------------------------------------------------------------


def test_modbus_success_produces_latency_and_registers() -> None:
    client = FakeClient([FakeResponse([100, 250, 380, 7])])
    collector = ModbusCollector(make_device(metrics=("response_ms", "register_0", "register_2")),
                                POLL, client_factory=lambda: client)
    results = collector.collect()

    assert [r.metric for r in results] == ["response_ms", "register_0", "register_2"]
    assert all(r.quality == QUALITY_OK for r in results)
    assert results[1].value == 100.0
    assert results[2].value == 380.0
    assert results[0].value >= 0.0
    assert len({r.ts_ms for r in results}) == 1  # 同一次采集共用一个时间戳


def test_modbus_reads_contiguous_block_in_one_request() -> None:
    """要 register_0 和 register_2 时只发一次请求，读 0..2。"""
    client = FakeClient([FakeResponse([10, 20, 30])])
    collector = ModbusCollector(
        make_device(metrics=("response_ms", "register_0", "register_2")), POLL,
        client_factory=lambda: client,
    )
    collector.collect()
    assert client.read_calls == [(0, 3, 1)]


def test_modbus_uses_configured_unit_id() -> None:
    client = FakeClient([FakeResponse([1])])
    collector = ModbusCollector(make_device(unit_id=9, metrics=("response_ms", "register_0")),
                                POLL, client_factory=lambda: client)
    collector.collect()
    assert client.read_calls[0][2] == 9


def test_modbus_only_latency_still_sends_request() -> None:
    """只要 response_ms 的设备也要真发一次请求，否则测不出链路死活。"""
    client = FakeClient([FakeResponse([1])])
    collector = ModbusCollector(make_device(metrics=("response_ms",)), POLL,
                                client_factory=lambda: client)
    results = collector.collect()
    assert [r.metric for r in results] == ["response_ms"]
    assert client.read_calls == [(0, 1, 1)]


def test_timeout_is_recorded_not_skipped() -> None:
    client = FakeClient([ModbusIOException("no response")] * 3)
    collector = ModbusCollector(make_device(), POLL, client_factory=lambda: client)
    results = collector.collect()
    assert results[0].metric == "response_ms"
    assert results[0].quality == QUALITY_TIMEOUT
    assert results[0].value > 0  # 超时耗时也要记下来


def test_failure_produces_same_row_count_as_success() -> None:
    """失败与成功的记录条数必须一致，否则按条数核对完整性会永远对不上。"""
    ok_collector = ModbusCollector(make_device(), POLL,
                                   client_factory=lambda: FakeClient([FakeResponse([1, 2])]))
    bad_collector = ModbusCollector(make_device(), POLL,
                                    client_factory=lambda: FakeClient([ModbusIOException("x")] * 3))
    ok_results = ok_collector.collect()
    bad_results = bad_collector.collect()
    assert len(ok_results) == len(bad_results) == 2
    assert [r.metric for r in ok_results] == [r.metric for r in bad_results]
    assert all(r.quality == QUALITY_OK for r in ok_results)
    assert all(r.quality == QUALITY_TIMEOUT for r in bad_results)
    assert bad_results[1].value == 0.0


def test_connection_failure_maps_to_timeout() -> None:
    client = FakeClient(connect_ok=False)
    collector = ModbusCollector(make_device(), POLL, client_factory=lambda: client)
    results = collector.collect()
    assert results[0].quality == QUALITY_TIMEOUT
    assert client.connect_calls > 1  # 重试过


def test_connection_exception_maps_to_timeout() -> None:
    client = FakeClient([ConnectionException("refused")] * 3)
    collector = ModbusCollector(make_device(), POLL, client_factory=lambda: client)
    assert collector.collect()[0].quality == QUALITY_TIMEOUT


def test_exception_code_maps_to_exception_quality() -> None:
    client = FakeClient([FakeResponse(exception_code=2)] * 3)
    collector = ModbusCollector(make_device(), POLL, client_factory=lambda: client)
    assert collector.collect()[0].quality == QUALITY_EXCEPTION


def test_generic_modbus_exception_maps_to_exception_quality() -> None:
    client = FakeClient([ModbusException("weird")] * 3)
    collector = ModbusCollector(make_device(), POLL, client_factory=lambda: client)
    assert collector.collect()[0].quality == QUALITY_EXCEPTION


def test_os_error_maps_to_timeout() -> None:
    client = FakeClient([OSError("network down")] * 3)
    collector = ModbusCollector(make_device(), POLL, client_factory=lambda: client)
    assert collector.collect()[0].quality == QUALITY_TIMEOUT


def test_retry_succeeds_on_second_attempt() -> None:
    client = FakeClient([ModbusIOException("first"), FakeResponse([42, 0])])
    collector = ModbusCollector(make_device(), POLL, client_factory=lambda: client)
    results = collector.collect()
    assert results[0].quality == QUALITY_OK
    assert results[1].value == 42.0


def test_retry_count_matches_config() -> None:
    """retry=0 时只尝试一次，retry=2 时最多三次。"""
    for retry in (0, 1, 2):
        client = FakeClient([ModbusIOException("x")] * 10)
        poll = PollConfig(retry=retry, timeout_ms=800)
        collector = ModbusCollector(make_device(), poll, client_factory=lambda c=client: c)
        collector.collect()
        assert len(client.read_calls) == retry + 1, f"retry={retry}"


def test_bad_connection_is_dropped_for_reconnect() -> None:
    """连接失败后要丢掉客户端，下一轮重建——半死的 socket 会一直返回同样的错。"""
    client = FakeClient(connect_ok=False)
    collector = ModbusCollector(make_device(), POLL, client_factory=lambda: client)
    collector.collect()
    assert client.close_calls >= 1


def test_close_is_idempotent() -> None:
    client = FakeClient()
    collector = ModbusCollector(make_device(), POLL, client_factory=lambda: client)
    collector.collect()
    collector.close()
    collector.close()


def test_register_index_out_of_range_rejected() -> None:
    with pytest.raises(CollectorError, match="最多读"):
        ModbusCollector(make_device(metrics=("register_200",)), POLL)


def test_collector_context_manager() -> None:
    client = FakeClient()
    with ModbusCollector(make_device(), POLL, client_factory=lambda: client) as collector:
        collector.collect()
    assert client.close_calls >= 1


# ---------------------------------------------------------------------------
# CSV 回放
# ---------------------------------------------------------------------------


def _write_csv(path: Path, rows: list[tuple[int, str, float, int]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["offset_ms,metric,value,quality"]
    lines += [f"{o},{m},{v},{q}" for o, m, v, q in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_load_replay_groups_by_offset(tmp_path: Path) -> None:
    path = _write_csv(tmp_path / "r.csv", [
        (0, "response_ms", 10.0, 0),
        (0, "register_0", 100.0, 0),
        (1000, "response_ms", 11.0, 0),
        (1000, "register_0", 101.0, 0),
    ])
    groups = load_replay_file(path)
    assert [g.offset_ms for g in groups] == [0, 1000]
    assert [r.metric for r in groups[0].rows] == ["response_ms", "register_0"]


def test_replay_produces_unified_results(tmp_path: Path) -> None:
    path = _write_csv(tmp_path / "r.csv", [
        (0, "response_ms", 10.0, 0),
        (0, "register_0", 100.0, 0),
    ])
    device = make_device(transport="csv", address=str(path), metrics=("response_ms", "register_0"))
    collector = CsvReplayCollector(device, POLL, path=path, start_ts=BASE_TS, loop=False)
    results = collector.collect()
    assert [r.metric for r in results] == ["response_ms", "register_0"]
    assert [r.value for r in results] == [10.0, 100.0]
    assert all(r.device == "plc-01" for r in results)
    assert all(r.ts_ms == BASE_TS for r in results)


def test_replay_timestamps_follow_offset(tmp_path: Path) -> None:
    path = _write_csv(tmp_path / "r.csv", [
        (0, "response_ms", 1.0, 0),
        (1000, "response_ms", 2.0, 0),
        (2500, "response_ms", 3.0, 0),
    ])
    device = make_device(transport="csv", address=str(path), metrics=("response_ms",))
    collector = CsvReplayCollector(device, POLL, path=path, start_ts=BASE_TS, loop=False)
    stamps = [collector.collect()[0].ts_ms for _ in range(3)]
    assert stamps == [BASE_TS, BASE_TS + 1000, BASE_TS + 2500]


def test_replay_speed_compresses_timeline(tmp_path: Path) -> None:
    path = _write_csv(tmp_path / "r.csv", [
        (0, "response_ms", 1.0, 0),
        (60_000, "response_ms", 2.0, 0),
    ])
    device = make_device(transport="csv", address=str(path), metrics=("response_ms",))
    collector = CsvReplayCollector(device, POLL, path=path, speed=60.0,
                                   start_ts=BASE_TS, loop=False)
    first = collector.collect()[0].ts_ms
    second = collector.collect()[0].ts_ms
    assert first == BASE_TS
    assert second == BASE_TS + 1000  # 60000 / 60


def test_replay_speed_compresses_pacing_not_only_timestamps(tmp_path: Path) -> None:
    """倍速必须**同时**压缩节拍与时间戳，否则时间轴自相矛盾。

    只压缩时间戳（不压缩节拍）会出现「墙钟跑了 45 秒、记下来的数据只跨 0.74 秒」：
    45 秒里每 500 ms 采一次共 89 轮，时间戳却按 500/60 ≈ 8 ms 前进。
    报告上的时间轴与「最近一小时」查询都会因此失去意义。

    判据很干脆：**时间轴跨度必须等于墙钟耗时**（轮次 × 节拍）。
    """
    path = _write_csv(tmp_path / "r.csv", [
        (i * 500, "response_ms", 10.0, 0) for i in range(20)
    ])
    device = make_device(transport="csv", address=str(path), metrics=("response_ms",),
                         poll_interval_ms=500)
    collector = CsvReplayCollector(device, POLL, path=path, speed=10.0,
                                   start_ts=BASE_TS, loop=False)

    assert collector.pacing_ms == 50, "500 ms 的周期在 10 倍速下应当是 50 ms"
    stamps = [collector.collect()[0].ts_ms for _ in range(20)]
    span = stamps[-1] - stamps[0]
    assert span == 19 * collector.pacing_ms, "时间轴跨度应当等于墙钟耗时（19 个节拍）"
    assert span == 950  # 20 组 × 500 ms 源数据 ÷ 10 倍速，减去第一组的偏移


def test_default_pacing_is_device_interval() -> None:
    """非回放采集器的节拍就是配置里的周期，倍速不影响它。"""
    device = make_device(poll_interval_ms=250)
    collector = ModbusCollector(device, POLL)
    assert collector.pacing_ms == 250


def test_replay_stops_when_not_looping(tmp_path: Path) -> None:
    path = _write_csv(tmp_path / "r.csv", [(0, "response_ms", 1.0, 0)])
    device = make_device(transport="csv", address=str(path), metrics=("response_ms",))
    collector = CsvReplayCollector(device, POLL, path=path, start_ts=BASE_TS, loop=False)
    assert len(collector.collect()) == 1
    assert collector.collect() == []
    assert collector.finished is True


def test_replay_loop_advances_timeline(tmp_path: Path) -> None:
    """循环回放时第二轮时间戳必须往后走，否则两轮数据会挤在同一个时间窗里。"""
    path = _write_csv(tmp_path / "r.csv", [
        (0, "response_ms", 1.0, 0),
        (1000, "response_ms", 2.0, 0),
    ])
    device = make_device(transport="csv", address=str(path), metrics=("response_ms",),
                         poll_interval_ms=500)
    collector = CsvReplayCollector(device, POLL, path=path, start_ts=BASE_TS, loop=True)
    stamps = [collector.collect()[0].ts_ms for _ in range(4)]
    assert stamps == [BASE_TS, BASE_TS + 1000, BASE_TS + 1500, BASE_TS + 2500]


def test_replay_injects_timeout_quality(tmp_path: Path) -> None:
    """回放文件里直接写 quality=1 就能注入超时，不需要故障注入框架。"""
    path = _write_csv(tmp_path / "r.csv", [
        (0, "response_ms", 800.0, 1),
        (1000, "response_ms", 12.0, 0),
    ])
    device = make_device(transport="csv", address=str(path), metrics=("response_ms",))
    collector = CsvReplayCollector(device, POLL, path=path, start_ts=BASE_TS, loop=False)
    assert collector.collect()[0].quality == QUALITY_TIMEOUT
    assert collector.collect()[0].quality == QUALITY_OK


def test_replay_reset(tmp_path: Path) -> None:
    path = _write_csv(tmp_path / "r.csv", [(0, "response_ms", 1.0, 0)])
    device = make_device(transport="csv", address=str(path), metrics=("response_ms",))
    collector = CsvReplayCollector(device, POLL, path=path, start_ts=BASE_TS, loop=False)
    collector.collect()
    collector.reset()
    assert collector.finished is False
    assert len(collector.collect()) == 1


def test_replay_group_count(tmp_path: Path) -> None:
    path = _write_csv(tmp_path / "r.csv", [
        (0, "response_ms", 1.0, 0),
        (1000, "response_ms", 2.0, 0),
        (2000, "response_ms", 3.0, 0),
    ])
    device = make_device(transport="csv", address=str(path), metrics=("response_ms",))
    assert CsvReplayCollector(device, POLL, path=path).group_count == 3


def test_replay_rejects_zero_speed(tmp_path: Path) -> None:
    path = _write_csv(tmp_path / "r.csv", [(0, "response_ms", 1.0, 0)])
    device = make_device(transport="csv", address=str(path), metrics=("response_ms",))
    with pytest.raises(CollectorError, match="倍速"):
        CsvReplayCollector(device, POLL, path=path, speed=0)


# ---- 回放文件的坏输入 -------------------------------------------------------


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(CollectorError, match="不存在"):
        load_replay_file(tmp_path / "nope.csv")


def test_missing_header_column(tmp_path: Path) -> None:
    path = tmp_path / "r.csv"
    path.write_text("offset,metric,value,quality\n0,response_ms,1,0\n", encoding="utf-8")
    with pytest.raises(CollectorError, match="表头缺少列"):
        load_replay_file(path)


def test_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "r.csv"
    path.write_text("offset_ms,metric,value,quality\n", encoding="utf-8")
    with pytest.raises(CollectorError, match="没有数据行"):
        load_replay_file(path)


def test_non_increasing_offset_rejected(tmp_path: Path) -> None:
    path = _write_csv(tmp_path / "r.csv", [
        (1000, "response_ms", 1.0, 0),
        (500, "response_ms", 2.0, 0),
    ])
    with pytest.raises(CollectorError, match="递增"):
        load_replay_file(path)


@pytest.mark.parametrize(
    ("row", "match"),
    [
        ("0,response_ms,abc,0", "不是数字"),
        ("abc,response_ms,1,0", "不是整数"),
        ("0,response_ms,1,abc", "不是整数"),
        ("0,,1,0", "metric 为空"),
        (",response_ms,1,0", "offset_ms 为空"),
    ],
)
def test_bad_rows_report_line_number(tmp_path: Path, row: str, match: str) -> None:
    path = tmp_path / "r.csv"
    path.write_text(f"offset_ms,metric,value,quality\n{row}\n", encoding="utf-8")
    with pytest.raises(CollectorError) as exc:
        load_replay_file(path)
    assert match in str(exc.value)
    assert "第 2 行" in str(exc.value)


def test_write_and_read_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "out" / "r.csv"
    write_replay_file(path, [
        (0, [ReplayRow("response_ms", 10.5, 0), ReplayRow("register_0", 100.0, 0)]),
        (1000, [ReplayRow("response_ms", 11.0, 1)]),
    ])
    groups = load_replay_file(path)
    assert [g.offset_ms for g in groups] == [0, 1000]
    assert groups[1].rows[0].quality == 1


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


def test_build_collector_tcp() -> None:
    collector = build_collector(make_device(), POLL)
    assert isinstance(collector, ModbusCollector)
    collector.close()


def test_build_collector_csv_resolves_relative_path(tmp_path: Path) -> None:
    _write_csv(tmp_path / "data" / "replay" / "x.csv", [(0, "response_ms", 1.0, 0)])
    device = make_device(transport="csv", address="data/replay/x.csv", metrics=("response_ms",))
    collector = build_collector(device, POLL, base_dir=tmp_path)
    assert isinstance(collector, CsvReplayCollector)
    assert collector.collect()[0].value == 1.0


def test_build_collector_csv_without_base_dir_uses_address(tmp_path: Path) -> None:
    path = _write_csv(tmp_path / "x.csv", [(0, "response_ms", 1.0, 0)])
    device = make_device(transport="csv", address=str(path), metrics=("response_ms",))
    assert isinstance(build_collector(device, POLL), CsvReplayCollector)


def test_build_collector_rtu_gives_actionable_error() -> None:
    device = make_device(transport="rtu", address="COM11")
    with pytest.raises(CollectorError, match="CSV 回放"):
        build_collector(device, POLL)


def test_build_collector_passes_options(tmp_path: Path) -> None:
    _write_csv(tmp_path / "x.csv", [(0, "response_ms", 1.0, 0), (60_000, "response_ms", 2.0, 0)])
    device = make_device(transport="csv", address=str(tmp_path / "x.csv"),
                         metrics=("response_ms",))
    collector = build_collector(device, POLL, speed=60.0, loop=False, start_ts=BASE_TS)
    assert collector.collect()[0].ts_ms == BASE_TS
    assert collector.collect()[0].ts_ms == BASE_TS + 1000


def test_default_speed_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.collector.csv_collector import default_speed_from_env

    monkeypatch.delenv("COLLECTOR_REPLAY_SPEED", raising=False)
    assert default_speed_from_env(3.0) == 3.0
    monkeypatch.setenv("COLLECTOR_REPLAY_SPEED", "60")
    assert default_speed_from_env() == 60.0
    monkeypatch.setenv("COLLECTOR_REPLAY_SPEED", "junk")
    assert default_speed_from_env(2.0) == 2.0
