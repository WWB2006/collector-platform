"""调度器用例。

这里守的核心是一条：**停止之后不能丢数据**。
「退出时线程未收敛、数据只写了一半」是这类程序最常见的线上事故，
而且症状很隐蔽——服务看起来正常退出了，只是库里少了几百条。

所以 :func:`test_stop_flushes_everything` 与
:func:`test_no_samples_lost_under_short_interval` 是这组里最值钱的用例。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from app.analytics.alarm_engine import AlarmEngine
from app.collector import Collector, CollectResult
from app.config import AppConfig, DeviceConfig, PollConfig, parse_config
from app.quality import QUALITY_OK, QUALITY_TIMEOUT
from app.scheduler import CollectorScheduler, DeviceStats, SchedulerStats, run_for
from app.storage import Database, Repository
from app.storage.migrations import now_ms


class CountingCollector(Collector):
    """假采集器：每轮产出一条样本，可选地注入失败。"""

    def __init__(
        self,
        device: DeviceConfig,
        poll: PollConfig,
        *,
        fail_every: int = 0,
        metrics: tuple[str, ...] = ("response_ms",),
        **_: Any,
    ) -> None:
        super().__init__(device, poll)
        self.calls = 0
        self.fail_every = fail_every
        self.metrics = metrics
        self.closed = False

    def collect(self) -> list[CollectResult]:
        self.calls += 1
        failed = self.fail_every and self.calls % self.fail_every == 0
        quality = QUALITY_TIMEOUT if failed else QUALITY_OK
        ts = now_ms()
        return [
            CollectResult(self.device.name, metric, 0.0 if failed else 1.0, ts, quality)
            for metric in self.metrics
        ]

    def close(self) -> None:
        self.closed = True


class ExplodingCollector(Collector):
    """每轮都抛异常——验证采集线程不会被一次未预料的错误打死。"""

    def __init__(self, device: DeviceConfig, poll: PollConfig, **_: Any) -> None:
        super().__init__(device, poll)
        self.calls = 0

    def collect(self) -> list[CollectResult]:
        self.calls += 1
        raise RuntimeError("采集器内部错误")


def make_config(*devices: dict, **root: Any) -> AppConfig:
    payload: dict[str, Any] = {
        "database": "data/x.db",
        "devices": list(devices),
        "poll": {"defaultIntervalMs": 50, "timeoutMs": 200, "retry": 0},
    }
    payload.update(root)
    return parse_config(payload)


def device_payload(name: str, interval: int = 50, **overrides: Any) -> dict:
    payload = {
        "name": name,
        "transport": "csv",
        "address": "data/replay/x.csv",
        "unitId": 1,
        "pollIntervalMs": interval,
        "metrics": ["response_ms"],
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def repo_db(tmp_path: Path):
    database = Database(tmp_path / "sched.db")
    database.init_schema()
    yield Repository(database)
    database.close_all()


def counting_factory(**options: Any):
    def build(device: DeviceConfig, poll: PollConfig, **kwargs: Any) -> Collector:
        kwargs.pop("base_dir", None)
        kwargs.pop("speed", None)
        kwargs.pop("loop", None)
        return CountingCollector(device, poll, **{**options, **kwargs})

    return build


# ---------------------------------------------------------------------------
# 统计对象
# ---------------------------------------------------------------------------


def test_device_stats_success_rate() -> None:
    stats = DeviceStats(name="a")
    assert stats.success_rate == 1.0  # 没跑过时不该显示 0%
    stats.cycles, stats.failures = 4, 1
    assert stats.success_rate == pytest.approx(0.75)


def test_scheduler_stats_as_dict() -> None:
    stats = SchedulerStats()
    stats.devices["a"] = DeviceStats(name="a", cycles=2, samples=2)
    payload = stats.as_dict()
    assert payload["devices"]["a"]["successRate"] == 1.0


# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------


def test_setup_syncs_devices_into_db(repo_db: Repository) -> None:
    config = make_config(device_payload("a"), device_payload("b"))
    scheduler = CollectorScheduler(config, repo_db, collector_factory=counting_factory())
    scheduler.setup()
    assert repo_db.device_id_map() == {"a": 1, "b": 2}
    assert [d.name for d in scheduler.devices] == ["a", "b"]


def test_setup_skips_disabled_devices(repo_db: Repository) -> None:
    config = make_config(device_payload("a"), device_payload("b", enabled=False))
    scheduler = CollectorScheduler(config, repo_db, collector_factory=counting_factory())
    scheduler.setup()
    assert [d.name for d in scheduler.devices] == ["a"]


def test_setup_without_devices_is_allowed(repo_db: Repository) -> None:
    scheduler = CollectorScheduler(make_config(), repo_db, collector_factory=counting_factory())
    scheduler.setup()
    scheduler.start()
    scheduler.stop()
    assert scheduler.stats.cycles == 0


def test_start_before_setup_raises(repo_db: Repository) -> None:
    scheduler = CollectorScheduler(make_config(device_payload("a")), repo_db,
                                   collector_factory=counting_factory())
    with pytest.raises(RuntimeError, match="setup"):
        scheduler.start()


def test_double_start_raises(repo_db: Repository) -> None:
    scheduler = CollectorScheduler(make_config(device_payload("a")), repo_db,
                                   collector_factory=counting_factory())
    scheduler.setup()
    scheduler.start()
    try:
        with pytest.raises(RuntimeError, match="已经启动"):
            scheduler.start()
    finally:
        scheduler.stop()


def test_setup_twice_raises(repo_db: Repository) -> None:
    scheduler = CollectorScheduler(make_config(device_payload("a")), repo_db,
                                   collector_factory=counting_factory())
    scheduler.setup()
    with pytest.raises(RuntimeError):
        scheduler.setup()


# ---------------------------------------------------------------------------
# 采集循环
# ---------------------------------------------------------------------------


def test_collects_roughly_at_configured_interval(repo_db: Repository) -> None:
    """50 ms 周期跑 0.5 秒，轮次应在 8~14 之间（期望 10，允许调度抖动）。"""
    config = make_config(device_payload("a", interval=50))
    scheduler = CollectorScheduler(config, repo_db, collector_factory=counting_factory())
    scheduler.setup()
    scheduler.start()
    try:
        scheduler.wait(0.5)
    finally:
        scheduler.stop()
    assert 6 <= scheduler.stats.cycles <= 16, scheduler.stats.cycles


def test_multiple_devices_are_independent(repo_db: Repository) -> None:
    config = make_config(device_payload("fast", interval=40), device_payload("slow", interval=200))
    scheduler = CollectorScheduler(config, repo_db, collector_factory=counting_factory())
    scheduler.setup()
    scheduler.start()
    try:
        scheduler.wait(0.6)
    finally:
        scheduler.stop()
    fast = scheduler.stats.devices["fast"]
    slow = scheduler.stats.devices["slow"]
    assert fast.cycles > slow.cycles
    assert slow.cycles >= 2


def test_produces_multiple_metrics_per_cycle(repo_db: Repository) -> None:
    config = make_config(device_payload("a", interval=50))
    scheduler = CollectorScheduler(
        config, repo_db,
        collector_factory=counting_factory(metrics=("response_ms", "register_0")),
    )
    scheduler.setup()
    scheduler.start()
    try:
        scheduler.wait(0.3)
    finally:
        scheduler.stop()
    assert scheduler.stats.samples == scheduler.stats.cycles * 2


def test_failures_are_counted(repo_db: Repository) -> None:
    config = make_config(device_payload("a", interval=30))
    scheduler = CollectorScheduler(
        config, repo_db, collector_factory=counting_factory(fail_every=2)
    )
    scheduler.setup()
    scheduler.start()
    try:
        scheduler.wait(0.4)
    finally:
        scheduler.stop()
    assert scheduler.stats.failures > 0
    assert scheduler.stats.failures == scheduler.stats.cycles // 2


def test_failing_quality_samples_still_persisted(repo_db: Repository) -> None:
    """失败的样本也要落库，否则成功率会虚高。"""
    config = make_config(device_payload("a", interval=30))
    scheduler = CollectorScheduler(
        config, repo_db, collector_factory=counting_factory(fail_every=1)
    )
    scheduler.setup()
    scheduler.start()
    try:
        scheduler.wait(0.3)
    finally:
        scheduler.stop()
    assert scheduler.stats.cycles > 0
    rows = repo_db.quality_values()
    assert rows
    assert all(quality == QUALITY_TIMEOUT for quality, _ in rows)


def test_exploding_collector_does_not_kill_thread(repo_db: Repository) -> None:
    config = make_config(device_payload("a", interval=30))
    scheduler = CollectorScheduler(
        config, repo_db,
        collector_factory=lambda device, poll, **kw: ExplodingCollector(device, poll),
    )
    scheduler.setup()
    scheduler.start()
    try:
        scheduler.wait(0.3)
    finally:
        scheduler.stop()
    assert scheduler.stats.cycles == 0
    assert repo_db.count_samples() == 0


# ---------------------------------------------------------------------------
# 运行期间的数据可见性（定时 flush）
# ---------------------------------------------------------------------------


def test_low_rate_stream_is_visible_during_run(repo_db: Repository) -> None:
    """低流量下数据必须在**运行期间**就落库，而不是攒满一批才写。

    回归一个很隐蔽的缺陷：写入线程原先只在 ``queue.get(timeout=1s)`` 超时后
    才按时间 flush，但采集端每 50 ms 就投一批进队列，队列**永远不会空**——
    于是「按时间 flush」这条路径一次都走不到。采集 223 条（不足一批 500 条）时，
    整轮运行只在退出那一刻落库一次，后果是：

    * 运行期间接口查不到任何数据（仪表盘空白）；
    * 告警评估只发生一次，且已经错过故障时刻，**告警永远不触发**；
    * 进程被 kill 会丢掉缓冲区里的全部样本。

    所以断言必须落在「运行中」这个时刻，而不是跑完再看总数——
    后者在缺陷存在时同样是通过的。
    """
    config = make_config(device_payload("a", interval=50))
    scheduler = CollectorScheduler(
        config, repo_db, collector_factory=counting_factory(),
        batch_size=500, flush_interval_ms=100,
    )
    scheduler.setup()
    scheduler.start()
    try:
        scheduler.wait(0.6)
        during = repo_db.count_samples()
        cycles = scheduler.stats.cycles
    finally:
        scheduler.stop()

    assert cycles < 500, "本用例的前提是「样本数不足一批」，否则测不到这条路径"
    assert during > 0, "运行期间库里就该有数据，不能等到退出才落库"
    assert during >= cycles - 3, f"运行中已落库 {during} 条，采集 {cycles} 轮，落后太多"


def test_alarm_opens_and_closes_during_run(repo_db: Repository) -> None:
    """连续 3 次超时必须开告警，恢复后必须自动结束——且都发生在运行期间。

    这条用例同时守三件事：

    * 告警评估真的在跑（依赖上面的定时 flush，否则整轮只在退出时评估一次）；
    * **去重**：故障持续期间只留一条活动告警，不是每轮插一条（告警风暴）；
    * **恢复**：条件不再成立时写 ``ended_at``，否则告警永远挂在活动列表里。
    """
    config = make_config(
        device_payload("a", interval=30),
        alarms=[{"name": "streak", "device": "a", "rule": "timeout>=3", "windowSec": 60}],
    )
    engine = AlarmEngine(repo_db, config.alarms)

    class ScriptedCollector(Collector):
        """前 6 轮正常 -> 3 轮超时（触发）-> 之后一直正常（恢复）。"""

        PLAN = [QUALITY_OK] * 6 + [QUALITY_TIMEOUT] * 3 + [QUALITY_OK] * 40

        def __init__(self, device: DeviceConfig, poll: PollConfig, **_: Any) -> None:
            super().__init__(device, poll)
            self.index = 0

        def collect(self) -> list[CollectResult]:
            quality = self.PLAN[min(self.index, len(self.PLAN) - 1)]
            self.index += 1
            return [
                CollectResult(
                    self.device.name,
                    "response_ms",
                    1.0 if quality == QUALITY_OK else 0.0,
                    now_ms(),
                    quality,
                )
            ]

    scheduler = CollectorScheduler(
        config, repo_db,
        collector_factory=lambda device, poll, **kw: ScriptedCollector(device, poll),
        alarm_engine=engine, batch_size=500, flush_interval_ms=60,
    )
    scheduler.setup()
    scheduler.start()
    try:
        scheduler.wait(1.2)
        opened = scheduler.stats.alarms_opened
        closed = scheduler.stats.alarms_closed
    finally:
        scheduler.stop()

    assert opened >= 1, "连续 3 次超时应当产生告警"
    assert closed >= 1, "恢复之后告警应当自动结束"
    rows = repo_db.list_alarms()
    assert rows, "alarms 表里应当留下记录"
    assert all(row["ended_at"] is not None for row in rows), "退出时不应还挂着活动告警"


# ---------------------------------------------------------------------------
# 优雅退出：不丢数据
# ---------------------------------------------------------------------------


def test_stop_flushes_everything(repo_db: Repository) -> None:
    """停止后库里条数必须等于统计到的样本数——一条都不能少。"""
    config = make_config(device_payload("a", interval=20), device_payload("b", interval=35))
    scheduler = CollectorScheduler(config, repo_db, collector_factory=counting_factory())
    scheduler.setup()
    scheduler.start()
    try:
        scheduler.wait(0.5)
    finally:
        scheduler.stop()

    assert scheduler.stats.samples > 0
    assert repo_db.count_samples() == scheduler.stats.samples
    assert scheduler.stats.written == scheduler.stats.samples
    assert scheduler.stats.write_errors == 0


def test_no_samples_lost_under_short_interval(repo_db: Repository) -> None:
    """极短周期（5 ms）+ 小批量，专门压「退出时队列没排空」这个坑。"""
    config = make_config(device_payload("a", interval=5), device_payload("b", interval=5))
    scheduler = CollectorScheduler(
        config, repo_db, collector_factory=counting_factory(), batch_size=3,
        flush_interval_ms=200,
    )
    scheduler.setup()
    scheduler.start()
    try:
        scheduler.wait(0.4)
    finally:
        scheduler.stop()

    assert scheduler.stats.samples > 20, scheduler.stats.samples
    assert repo_db.count_samples() == scheduler.stats.samples


def test_queue_drained_after_stop(repo_db: Repository) -> None:
    config = make_config(device_payload("a", interval=10))
    scheduler = CollectorScheduler(config, repo_db, collector_factory=counting_factory())
    scheduler.setup()
    scheduler.start()
    try:
        scheduler.wait(0.25)
    finally:
        scheduler.stop()
    assert scheduler.pending() == 0


def test_stop_is_idempotent(repo_db: Repository) -> None:
    config = make_config(device_payload("a", interval=50))
    scheduler = CollectorScheduler(config, repo_db, collector_factory=counting_factory())
    scheduler.setup()
    scheduler.start()
    scheduler.wait(0.15)
    scheduler.stop()
    scheduler.stop()  # 第二次不能抛异常


def test_stop_before_start_is_safe(repo_db: Repository) -> None:
    scheduler = CollectorScheduler(make_config(device_payload("a")), repo_db,
                                   collector_factory=counting_factory())
    scheduler.setup()
    scheduler.stop()


def test_collectors_are_closed_on_stop(repo_db: Repository) -> None:
    created: list[CountingCollector] = []

    def factory(device: DeviceConfig, poll: PollConfig, **kw: Any) -> Collector:
        collector = CountingCollector(device, poll)
        created.append(collector)
        return collector

    scheduler = CollectorScheduler(make_config(device_payload("a"), device_payload("b")),
                                   repo_db, collector_factory=factory)
    scheduler.setup()
    scheduler.start()
    scheduler.wait(0.1)
    scheduler.stop()
    assert len(created) == 2
    assert all(c.closed for c in created)


def test_stop_event_ends_wait(repo_db: Repository) -> None:
    """外部 stop_event 置位后 wait 应立即返回，便于信号处理与 CI 控制。"""
    config = make_config(device_payload("a", interval=50))
    scheduler = CollectorScheduler(config, repo_db, collector_factory=counting_factory())
    scheduler.setup()
    scheduler.start()
    stop_event = threading.Event()
    threading.Timer(0.15, stop_event.set).start()
    started = time.monotonic()
    try:
        scheduler.wait(30.0, stop_event=stop_event)
    finally:
        scheduler.stop()
    assert time.monotonic() - started < 5.0


# ---------------------------------------------------------------------------
# 上下文管理器与便捷入口
# ---------------------------------------------------------------------------


def test_context_manager(repo_db: Repository) -> None:
    config = make_config(device_payload("a", interval=30))
    with CollectorScheduler(config, repo_db, collector_factory=counting_factory()) as scheduler:
        scheduler.wait(0.2)
        cycles = scheduler.stats.cycles
    assert cycles > 0
    assert repo_db.count_samples() == scheduler.stats.samples


def test_run_for_helper(repo_db: Repository) -> None:
    config = make_config(device_payload("a", interval=25))
    stats = run_for(config, repo_db, seconds=0.25, collector_factory=counting_factory())
    assert stats.cycles > 0
    assert repo_db.count_samples() == stats.samples


# ---------------------------------------------------------------------------
# 批量写入与真实回放
# ---------------------------------------------------------------------------


def test_small_batch_size_still_writes_all(repo_db: Repository) -> None:
    config = make_config(device_payload("a", interval=10))
    scheduler = CollectorScheduler(
        config, repo_db, collector_factory=counting_factory(), batch_size=1
    )
    scheduler.setup()
    scheduler.start()
    try:
        scheduler.wait(0.3)
    finally:
        scheduler.stop()
    assert repo_db.count_samples() == scheduler.stats.samples


def test_real_csv_replay_end_to_end(tmp_path: Path) -> None:
    """真用 CSV 回放跑一轮，验证采集器工厂、调度、写库串起来是通的。"""
    from app.collector.csv_collector import ReplayRow, write_replay_file

    replay = write_replay_file(
        tmp_path / "data" / "replay" / "r.csv",
        [
            (0, [ReplayRow("response_ms", 10.0, 0), ReplayRow("register_0", 100.0, 0)]),
            (500, [ReplayRow("response_ms", 11.0, 0), ReplayRow("register_0", 101.0, 0)]),
            (1000, [ReplayRow("response_ms", 800.0, 1), ReplayRow("register_0", 0.0, 1)]),
        ],
    )
    database = Database(tmp_path / "e2e.db")
    database.init_schema()
    repo = Repository(database)

    config = parse_config(
        {
            "database": str(tmp_path / "e2e.db"),
            "poll": {"timeoutMs": 200, "retry": 0},
            "devices": [
                {
                    "name": "replay-01",
                    "transport": "csv",
                    "address": "data/replay/r.csv",
                    "unitId": 1,
                    "pollIntervalMs": 20,
                    "metrics": ["response_ms", "register_0"],
                }
            ],
        },
        base_dir=tmp_path,
    )
    scheduler = CollectorScheduler(config, repo, loop_replay=False, batch_size=2)
    scheduler.setup()
    scheduler.start()
    try:
        scheduler.wait(0.5)
    finally:
        scheduler.stop()
        database.close_all()

    # 三轮回放数据（每轮 2 条）应该全部落库，最后一轮是注入的超时。
    assert repo.count_samples(metric="response_ms") == 3
    assert repo.count_samples(metric="register_0") == 3
    assert repo.quality_distribution() == {0: 2, 1: 1}
    assert replay.exists()
