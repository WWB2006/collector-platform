"""采集调度：把「按周期采集、批量写库、优雅退出」这三件事做成可测试的对象。

放在 ``app/`` 而不是 ``scripts/``，是因为它包含真实的业务规则
（周期怎么算、失败怎么记、退出时怎么保证不丢数据），必须能被单元测试直接驱动；
``scripts/run_collector.py`` 只负责解析命令行参数。

线程模型（生产者—消费者）::

    采集线程 × N  --(队列)-->  写入线程  -->  SQLite

为什么不是「每个设备线程自己写库」：

* SQLite 同一时刻只允许一个写事务，多线程各写各的会互相等锁，
  采集线程被写库拖住，周期就不准了；
* 攒批写库（默认 500 条一次提交）比逐条提交快一个数量级，
  而攒批需要一个集中的写入点；
* 采集与写入解耦之后，写库慢不会影响采集节拍，队列顶多堆积。

为什么退出顺序是「先停采集、再关数据库」：
反过来的话，采集线程会在库已关闭之后写数据，那些样本静默丢失。
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.analytics.alarm_engine import AlarmEngine
from app.collector import Collector, CollectResult, build_collector
from app.collector.base import LATENCY_METRIC
from app.config import AppConfig, DeviceConfig
from app.logging_setup import get_logger
from app.quality import QUALITY_OK
from app.storage import Repository

logger = get_logger("scheduler")

#: 队列容量。写库明显慢于采集时队列会涨到这里，然后采集线程被阻塞——
#: 这是有意的背压，好过无限堆积把内存吃光。
DEFAULT_QUEUE_SIZE = 4096

#: 写入线程在攒不够一批时的最长等待，保证低流量下数据也能及时落库。
DEFAULT_FLUSH_INTERVAL_MS = 1000


@dataclass
class DeviceStats:
    """单台设备的运行计数。"""

    name: str
    cycles: int = 0
    samples: int = 0
    failures: int = 0
    last_ts: int = 0

    @property
    def success_rate(self) -> float:
        return 1.0 if self.cycles == 0 else (self.cycles - self.failures) / self.cycles


@dataclass
class SchedulerStats:
    """整轮运行的汇总。"""

    cycles: int = 0
    samples: int = 0
    failures: int = 0
    written: int = 0
    write_errors: int = 0
    alarms_opened: int = 0
    alarms_closed: int = 0
    devices: dict[str, DeviceStats] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "cycles": self.cycles,
            "samples": self.samples,
            "failures": self.failures,
            "written": self.written,
            "writeErrors": self.write_errors,
            "alarmsOpened": self.alarms_opened,
            "alarmsClosed": self.alarms_closed,
            "devices": {
                name: {
                    "cycles": d.cycles,
                    "samples": d.samples,
                    "failures": d.failures,
                    "successRate": round(d.success_rate, 4),
                }
                for name, d in sorted(self.devices.items())
            },
        }


class CollectorScheduler:
    """按配置驱动多台设备采集并写入数据库。

    典型用法::

        scheduler = CollectorScheduler(config, repo)
        scheduler.setup()
        scheduler.start()
        scheduler.wait(seconds=60)   # 或 wait_until_stopped()
        scheduler.stop()             # 先停采集、排空队列、再关库
    """

    def __init__(
        self,
        config: AppConfig,
        repo: Repository,
        *,
        speed: float = 1.0,
        loop_replay: bool = True,
        batch_size: int = 500,
        flush_interval_ms: int = DEFAULT_FLUSH_INTERVAL_MS,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        collector_factory: Callable[..., Collector] = build_collector,
        alarm_engine: AlarmEngine | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.repo = repo
        self.speed = speed
        self.loop_replay = loop_replay
        self.batch_size = batch_size
        self.flush_interval_ms = flush_interval_ms
        self._collector_factory = collector_factory
        self._alarm_engine = alarm_engine
        self._clock = clock

        self._queue: queue.Queue[list[CollectResult] | None] = queue.Queue(maxsize=queue_size)
        self._stop = threading.Event()
        self._collect_threads: list[threading.Thread] = []
        self._writer_thread: threading.Thread | None = None
        self._devices: list[DeviceConfig] = []
        self._device_ids: dict[str, int] = {}
        self._stats = SchedulerStats()
        self._stats_lock = threading.Lock()
        self._started = False
        self._setup_done = False

    # ==================================================================
    # 准备
    # ==================================================================

    def setup(self) -> None:
        """同步设备清单并构造采集器。必须在 :meth:`start` 之前调用，且只能调一次。

        重复调用会重建采集器并清空统计，还会把上一个采集器的连接丢掉，
        所以这里直接拒绝，而不是「默默再来一遍」。
        """
        if self._setup_done:
            raise RuntimeError("调度器已经 setup 过，不能重复调用（如需重来请新建实例）")
        self._setup_done = True

        self._device_ids = self.repo.sync_devices(self.config.devices)
        self._devices = list(self.config.enabled_devices())
        if not self._devices:
            logger.warning("配置里没有启用的设备，调度器不会采集任何数据")

        for device in self._devices:
            self._stats.devices[device.name] = DeviceStats(name=device.name)

        # 采集器在这里一次性建好：CSV 回放文件的格式错误、
        # TCP 地址写错这类问题要在启动阶段就报出来，而不是跑到一半才发现。
        self._collectors: dict[str, Collector] = {}
        for device in self._devices:
            self._collectors[device.name] = self._collector_factory(
                device,
                self.config.poll,
                base_dir=self.config.base_dir,
                speed=self.speed,
                loop=self.loop_replay,
            )
        logger.info(
            "调度器就绪：%d 台设备，轮询周期 %s",
            len(self._devices),
            ", ".join(f"{d.name}={d.poll_interval_ms}ms" for d in self._devices),
        )

    # ==================================================================
    # 生命周期
    # ==================================================================

    def start(self) -> None:
        if self._started:
            raise RuntimeError("调度器已经启动")
        if not hasattr(self, "_collectors"):
            raise RuntimeError("请先调用 setup()")

        self._stop.clear()
        self._started = True

        self._writer_thread = threading.Thread(
            target=self._writer_loop, name="collector-writer", daemon=True
        )
        self._writer_thread.start()

        for device in self._devices:
            thread = threading.Thread(
                target=self._device_loop,
                args=(device,),
                name=f"collector-{device.name}",
                daemon=True,
            )
            thread.start()
            self._collect_threads.append(thread)

        logger.info("采集已启动")

    def wait(self, seconds: float | None = None, *, stop_event: threading.Event | None = None) -> None:
        """等待若干秒，或等到 ``stop_event`` 被置位（两者都给了就谁先到算谁）。"""
        deadline = None if seconds is None else self._clock() + seconds
        while not self._stop.is_set():
            if stop_event is not None and stop_event.is_set():
                return
            if deadline is not None:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return
                self._stop.wait(min(remaining, 0.2))
            else:
                self._stop.wait(0.2)

    def wait_until_stopped(self) -> None:
        self.wait(None)

    def stop(self, *, timeout: float = 30.0) -> None:
        """先停采集线程，再排空队列，最后关闭写入线程。"""
        if not self._started:
            return
        logger.info("正在停止采集…")
        self._stop.set()

        for thread in self._collect_threads:
            thread.join(timeout=timeout)
            if thread.is_alive():  # pragma: no cover - 采集器卡在阻塞 IO 上
                logger.warning("采集线程 %s 未在 %.0f 秒内退出", thread.name, timeout)

        # 哨兵：让写入线程把剩余数据 flush 掉再退出。
        self._queue.put(None)
        if self._writer_thread is not None:
            self._writer_thread.join(timeout=timeout)
            if self._writer_thread.is_alive():  # pragma: no cover
                logger.warning("写入线程未在 %.0f 秒内退出", timeout)

        for collector in getattr(self, "_collectors", {}).values():
            try:
                collector.close()
            except Exception as exc:  # noqa: BLE001 - 关闭失败不应影响退出
                logger.warning("关闭采集器失败：%s", exc)

        self._started = False
        logger.info(
            "采集已停止：%d 轮、%d 条样本（失败 %d 轮）、已落库 %d 条",
            self._stats.cycles,
            self._stats.samples,
            self._stats.failures,
            self._stats.written,
        )

    def __enter__(self) -> CollectorScheduler:
        self.setup()
        self.start()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.stop()

    # ==================================================================
    # 线程主体
    # ==================================================================

    def _device_loop(self, device: DeviceConfig) -> None:
        """单台设备的采集循环：立即采一次，之后按周期采。"""
        collector = self._collectors[device.name]
        # 用采集器自己的节拍，而不是设备配置里的周期：
        # 回放采集器会按倍速把节拍压缩，配置里的 500 ms 在 speed=60 时其实是 8 ms。
        interval_s = max(0.001, collector.pacing_ms / 1000.0)

        while not self._stop.is_set():
            started = self._clock()
            try:
                results = collector.collect()
            except Exception as exc:  # noqa: BLE001 - 采集器不该把线程打死
                # 采集器内部已经把「设备失败」变成质量码；能走到这里的是
                # 我们没预料到的错误（例如回放文件被删），必须记下来而不是静默吞掉。
                logger.exception("设备 %s 采集线程异常：%s", device.name, exc)
                results = []

            if results:
                self._record(device.name, results)
                self._enqueue(results)

            elapsed = self._clock() - started
            self._stop.wait(max(0.0, interval_s - elapsed))

    def _enqueue(self, results: list[CollectResult]) -> None:
        try:
            self._queue.put(results, timeout=5.0)
        except queue.Full:  # pragma: no cover - 需要写库长期阻塞才能触发
            logger.error("写入队列已满，丢弃 %d 条样本", len(results))
            with self._stats_lock:
                self._stats.write_errors += 1

    def _record(self, device_name: str, results: list[CollectResult]) -> None:
        """更新运行计数（采集线程调用，需要加锁）。"""
        failed = any(r.quality != QUALITY_OK for r in results)
        with self._stats_lock:
            self._stats.cycles += 1
            self._stats.samples += len(results)
            if failed:
                self._stats.failures += 1
            device_stats = self._stats.devices.setdefault(device_name, DeviceStats(name=device_name))
            device_stats.cycles += 1
            device_stats.samples += len(results)
            device_stats.last_ts = results[0].ts_ms
            if failed:
                device_stats.failures += 1

    def _writer_loop(self) -> None:
        """写入线程：攒够一批**或等够时间**就落库，落库后顺带评估告警。

        两个触发条件缺一不可：

        * **攒够一批**——高流量时减少事务次数（500 条一次提交比逐条快一个数量级）；
        * **等够时间**——低流量时保证数据可见性与告警时效。

        只做前者会踩一个很隐蔽的坑：采集端每 500 ms 就投一批进队列，
        ``queue.get(timeout=1.0)`` 于是**永远不超时**，按时间 flush 这条路径
        一次都走不到。后果是采集 223 条（不足一批 500 条）时，
        整轮运行只在退出时落库一次——运行期间接口查不到数据、
        **告警评估只发生一次且已错过故障时刻**、进程被 kill 会丢掉全部样本。

        告警评估放在写入线程（而不是采集线程）是必须的：
        采集结果先进队列，样本落库之后才谈得上「窗口内末尾连续失败几次」。
        在采集线程里评估会读到还没写进去的数据，判定结果永远慢一拍。
        """
        buffer: list[tuple[int, int, str, float, int]] = []
        interval_s = max(0.05, self.flush_interval_ms / 1000.0)
        deadline: float | None = None

        while True:
            # 缓冲区非空时，等到 flush 截止时间就必须落库，不能一直等下一批。
            timeout = interval_s if deadline is None else max(0.0, deadline - self._clock())
            try:
                item = self._queue.get(timeout=timeout)
            except queue.Empty:
                if buffer:
                    self._flush(buffer)
                    buffer = []
                    deadline = None
                if self._stop.is_set() and self._queue.empty():
                    # 停止时再确认一次队列是否真的空了，避免丢掉最后几批。
                    break
                continue

            if item is None:
                break

            for result in item:
                device_id = self._device_ids.get(result.device)
                if device_id is None:
                    logger.warning("样本引用了未知设备 %s，已跳过", result.device)
                    continue
                buffer.append(result.as_row(device_id))

            if deadline is None:
                deadline = self._clock() + interval_s

            if len(buffer) >= self.batch_size:
                self._flush(buffer)
                buffer = []
                deadline = None

        if buffer:
            self._flush(buffer)

    def _flush(self, buffer: list[tuple[int, int, str, float, int]]) -> None:
        try:
            written = self.repo.insert_samples(buffer, batch_size=self.batch_size)
        except Exception as exc:  # noqa: BLE001 - 写失败不能让整个服务挂掉
            logger.exception("批量写库失败（%d 条）：%s", len(buffer), exc)
            with self._stats_lock:
                self._stats.write_errors += 1
            return
        with self._stats_lock:
            self._stats.written += written

        if self._alarm_engine is not None:
            self._evaluate_alarms(buffer)

    def _evaluation_points(self, rows: list[tuple[int, int, str, float, int]]) -> dict[str, list[int]]:
        """算出每台设备需要评估告警的时刻（毫秒）。

        **只在质量码发生变化的位置 + 批次末尾评估**，而不是只评估批次末尾。

        连续次数类规则（``timeout>=3``）看的是「窗口内末尾连续同码样本」，
        它只可能在「一段同码样本刚刚结束」或「刚好落在末尾」时达到最大值。
        如果一批里恰好包含「3 次超时 + 紧随其后的恢复样本」，
        只评估批次末尾就会得到「末尾连续 0 次」，这次故障被整批吞掉——
        批次越大越容易漏：真实场景一批 500 条时，短促故障几乎必然与恢复样本同批，
        告警功能等于形同虚设。

        只在这些位置评估也让开销可控：一批里的质量码变化通常不超过几次。
        """
        id_to_name = {device_id: name for name, device_id in self._device_ids.items()}
        last: dict[str, tuple[int, int]] = {}  # 设备 -> (最近一条延迟样本的 ts, 质量码)
        points: dict[str, list[int]] = {}

        for device_id, ts, metric, _value, quality in rows:
            if metric != LATENCY_METRIC:
                continue  # 与 _window_samples 口径一致：一次采集只算 response_ms
            name = id_to_name.get(device_id)
            if name is None:
                continue
            previous = last.get(name)
            if previous is not None and previous[1] != quality:
                # 上一段同码样本到此结束，在它的最后一刻评估一次。
                points.setdefault(name, []).append(previous[0])
            last[name] = (ts, quality)

        for name, (ts, _quality) in last.items():
            points.setdefault(name, []).append(ts)
        return points

    def _evaluate_alarms(self, rows: list[tuple[int, int, str, float, int]]) -> None:
        """样本落库后评估告警；任何异常都只记日志，不影响采集。"""
        for name, timestamps in sorted(self._evaluation_points(rows).items()):
            for ts in timestamps:
                try:
                    transitions = self._alarm_engine.evaluate_device(name, now=ts)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("评估设备 %s 的告警时出错：%s", name, exc)
                    continue
                if not transitions:
                    continue
                with self._stats_lock:
                    for transition in transitions:
                        if transition.action == "opened":
                            self._stats.alarms_opened += 1
                        else:
                            self._stats.alarms_closed += 1

    # ==================================================================
    # 查询
    # ==================================================================

    @property
    def stats(self) -> SchedulerStats:
        return self._stats

    @property
    def devices(self) -> list[DeviceConfig]:
        """本次调度实际会采集的设备（setup 之后才有效）。"""
        return list(self._devices)

    def stats_dict(self) -> dict[str, Any]:
        return self._stats.as_dict()

    def pending(self) -> int:
        """队列里还没写库的批次数（用于测试断言「退出时已排空」）。"""
        return self._queue.qsize()


def run_for(
    config: AppConfig,
    repo: Repository,
    *,
    seconds: float,
    stop_event: threading.Event | None = None,
    **options: Any,
) -> SchedulerStats:
    """便捷入口：跑固定时长然后干净退出（脚本与测试共用）。"""
    scheduler = CollectorScheduler(config, repo, **options)
    scheduler.setup()
    scheduler.start()
    try:
        scheduler.wait(seconds, stop_event=stop_event)
    finally:
        scheduler.stop()
    return scheduler.stats


def resolve_replay_paths(config: AppConfig) -> dict[str, Path]:
    """返回每台 CSV 设备回放文件的绝对路径（诊断用）。"""
    paths: dict[str, Path] = {}
    for device in config.devices:
        if device.transport == "csv":
            paths[device.name] = config.resolve(device.address)
    return paths
