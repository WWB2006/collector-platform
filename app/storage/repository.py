"""仓储层：所有 SQL 集中在这里。

为什么把 SQL 都收在一个文件（而不是散在各层）：

1. 优化时只需要看一个地方——加索引、改分页、调分桶粒度都在这里；
2. 上层的统计、接口、报告都只调用具名方法，读代码时能看出「业务要什么」，
   而不是「这串 SQL 在干嘛」；
3. 查询计划检查（``EXPLAIN QUERY PLAN``）有统一的落点。

两条贯穿全文件的约定：

* **时间区间一律左闭右开** ``[from_ts, to_ts)``。
  写死在一个地方，前后两次统计才不会因为边界差一条而对不上。
* **批量写入必须包在事务里**，每批 500 条（见 :meth:`Repository.insert_samples`）。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable, NamedTuple, Sequence

from app.quality import (
    QUALITY_CRC,
    QUALITY_EXCEPTION,
    QUALITY_LABELS,
    QUALITY_NAMES,
    QUALITY_OK,
    QUALITY_TIMEOUT,
)
from app.storage.db import Database
from app.storage.migrations import now_ms

#: 批量插入的分批大小。经验值：太小则提交次数多，太大则单次事务持有写锁过久。
DEFAULT_BATCH_SIZE = 500

#: 响应时间指标的固定名字。
LATENCY_METRIC = "response_ms"


class SampleRecord(NamedTuple):
    """一条待入库的样本。字段顺序与 ``samples`` 表一致。"""

    device_id: int
    ts: int
    metric: str
    value: float
    quality: int


class Repository:
    """设备、样本、告警、运行记录的读写入口。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ==================================================================
    # devices
    # ==================================================================

    def upsert_device(
        self,
        name: str,
        transport: str,
        address: str,
        unit_id: int = 1,
        poll_interval_ms: int = 1000,
        enabled: bool = True,
    ) -> int:
        """按名字插入或更新设备，返回其 id（名字是唯一键，重复调用幂等）。"""
        self.db.execute(
            """
            INSERT INTO devices(name, transport, address, unit_id, poll_interval_ms, enabled)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                transport        = excluded.transport,
                address          = excluded.address,
                unit_id          = excluded.unit_id,
                poll_interval_ms = excluded.poll_interval_ms,
                enabled          = excluded.enabled
            """,
            (name, transport, address, unit_id, poll_interval_ms, int(enabled)),
        )
        return self.device_id(name)

    def device_id(self, name: str) -> int:
        row = self.db.query_one("SELECT id FROM devices WHERE name = ?", (name,))
        if row is None:
            raise KeyError(f"设备不存在：{name}")
        return int(row["id"])

    def device_id_map(self) -> dict[str, int]:
        """名字 -> id 的全量映射，调度器启动时取一次，避免每轮查库。"""
        return {row["name"]: int(row["id"]) for row in self.db.query("SELECT id, name FROM devices")}

    def get_device(self, name: str) -> sqlite3.Row | None:
        return self.db.query_one("SELECT * FROM devices WHERE name = ?", (name,))

    def list_devices(self) -> list[sqlite3.Row]:
        return self.db.query("SELECT * FROM devices ORDER BY name")

    def set_device_enabled(self, name: str, enabled: bool) -> None:
        self.db.execute("UPDATE devices SET enabled = ? WHERE name = ?", (int(enabled), name))

    def sync_devices(self, devices: Sequence[Any]) -> dict[str, int]:
        """把配置里的设备同步进库，返回名字 -> id 映射。

        配置是设备清单的唯一来源：新增会插入，改名会新增（旧行保留，
        历史样本不丢），周期/地址变化会更新。
        """
        mapping: dict[str, int] = {}
        with self.db.transaction() as conn:
            for dev in devices:
                conn.execute(
                    """
                    INSERT INTO devices(name, transport, address, unit_id,
                                        poll_interval_ms, enabled)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        transport        = excluded.transport,
                        address          = excluded.address,
                        unit_id          = excluded.unit_id,
                        poll_interval_ms = excluded.poll_interval_ms,
                        enabled          = excluded.enabled
                    """,
                    (
                        dev.name,
                        dev.transport,
                        dev.address,
                        dev.unit_id,
                        dev.poll_interval_ms,
                        int(dev.enabled),
                    ),
                )
        mapping.update(self.device_id_map())
        return mapping

    # ==================================================================
    # samples
    # ==================================================================

    def insert_samples(
        self,
        rows: Iterable[Sequence[Any]],
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> int:
        """批量写入样本，返回写入条数。

        每 ``batch_size`` 条一次 ``executemany`` + 一次提交。
        逐条 commit 的写法在 10 万条量级上会慢一个数量级（见 README 性能实测）。

        接受的对象是普通序列 ``(device_id, ts, metric, value, quality)``——
        不绑定采集层的类型，采集器与回放器都能直接喂。
        """
        sql = "INSERT INTO samples(device_id, ts, metric, value, quality) VALUES (?, ?, ?, ?, ?)"
        total = 0
        batch: list[Sequence[Any]] = []

        for row in rows:
            batch.append(tuple(row))
            if len(batch) >= batch_size:
                total += self._flush(batch, sql)
                batch = []
        if batch:
            total += self._flush(batch, sql)
        return total

    def _flush(self, batch: list[Sequence[Any]], sql: str) -> int:
        with self.db.transaction() as conn:
            conn.executemany(sql, batch)
        return len(batch)

    def count_samples(
        self,
        device_id: int | None = None,
        *,
        from_ts: int | None = None,
        to_ts: int | None = None,
        metric: str | None = None,
    ) -> int:
        where, params = _sample_filters(device_id, from_ts, to_ts, metric)
        row = self.db.query_one(f"SELECT COUNT(*) AS n FROM samples s{where}", params)
        return int(row["n"]) if row else 0

    def last_sample_ts(self, device_id: int | None = None) -> int | None:
        """最近一条样本的时间戳（设备状态接口用；无数据返回 ``None``）。

        走 ``idx_samples_device_ts`` 的反向扫描，不是全表 ``MAX()``。
        """
        if device_id is None:
            row = self.db.query_one("SELECT MAX(ts) AS ts FROM samples")
        else:
            row = self.db.query_one(
                "SELECT MAX(ts) AS ts FROM samples WHERE device_id = ?", (int(device_id),)
            )
        return None if row is None or row["ts"] is None else int(row["ts"])

    def query_samples(
        self,
        device_id: int | None = None,
        *,
        from_ts: int | None = None,
        to_ts: int | None = None,
        metric: str | None = None,
        limit: int = 100,
        offset: int = 0,
        newest_first: bool = True,
    ) -> list[sqlite3.Row]:
        """按设备与时间范围分页查询，走 ``idx_samples_device_ts``。

        ``newest_first=True`` 时按时间倒序（页面默认看最新几条）。
        """
        where, params = _sample_filters(device_id, from_ts, to_ts, metric)
        order = "DESC" if newest_first else "ASC"
        sql = (
            "SELECT s.id, s.device_id, d.name AS device, s.ts, s.metric, s.value, s.quality "
            "FROM samples s JOIN devices d ON d.id = s.device_id"
            f"{where} ORDER BY s.ts {order}, s.id {order} LIMIT ? OFFSET ?"
        )
        return self.db.query(sql, (*params, int(limit), int(offset)))

    def failed_samples(
        self,
        device_id: int | None = None,
        *,
        from_ts: int | None = None,
        to_ts: int | None = None,
        metric: str | None = LATENCY_METRIC,
        limit: int = 50,
        newest_first: bool = True,
    ) -> list[sqlite3.Row]:
        """质量码非 0 的样本（报告里的「异常明细」）。

        默认只看 ``response_ms``——一次失败的采集会在每个指标上各写一条，
        不限定指标的话，明细里同一个故障会出现好几遍。
        """
        where, params = _sample_filters(device_id, from_ts, to_ts, metric)
        joiner = " AND " if where else " WHERE "
        order = "DESC" if newest_first else "ASC"
        sql = (
            "SELECT s.id, s.device_id, d.name AS device, s.ts, s.metric, s.value, s.quality "
            "FROM samples s JOIN devices d ON d.id = s.device_id"
            f"{where}{joiner}s.quality != 0 "
            f"ORDER BY s.ts {order}, s.id {order} LIMIT ?"
        )
        return self.db.query(sql, (*params, int(limit)))

    def quality_values(
        self,
        device_id: int | None = None,
        *,
        from_ts: int | None = None,
        to_ts: int | None = None,
        metric: str | None = LATENCY_METRIC,
    ) -> list[tuple[int, float]]:
        """取 ``(quality, value)`` 列表，供统计层计算成功率与分位数。

        返回原始对，而不是在 SQL 里算好——因为统计口径（例如「超时不计入延迟分位」）
        必须和报告、接口共用同一个 Python 实现，否则三处数字会对不上。
        """
        where, params = _sample_filters(device_id, from_ts, to_ts, metric)
        rows = self.db.query(f"SELECT s.quality, s.value FROM samples s{where}", params)
        return [(int(r["quality"]), float(r["value"])) for r in rows]

    def quality_distribution(
        self,
        device_id: int | None = None,
        *,
        from_ts: int | None = None,
        to_ts: int | None = None,
    ) -> dict[int, int]:
        """质量码分布 ``{quality: count}``，只统计一次采集（response_ms），避免重复计数。"""
        where, params = _sample_filters(device_id, from_ts, to_ts, LATENCY_METRIC)
        rows = self.db.query(
            f"SELECT s.quality, COUNT(*) AS n FROM samples s{where} GROUP BY s.quality", params
        )
        return {int(r["quality"]): int(r["n"]) for r in rows}

    def series(
        self,
        metric: str,
        *,
        device_id: int | None = None,
        from_ts: int,
        to_ts: int,
        bucket_ms: int,
    ) -> list[dict[str, Any]]:
        """把区间按 ``bucket_ms`` 分桶，返回每桶的 ``avg`` / ``max`` / ``count`` / ``failed``。

        用于响应时间曲线。分桶在 SQL 里做，避免把几十万行拉到 Python 再聚合。
        桶起点用 ``from_ts`` 对齐，保证同一区间多次查询桶边界一致。

        **``avg`` / ``max`` 只统计质量码为 0 的样本**，与统计层的口径一致
        （见 ``analytics.statistics.latency_values``）。这不是细节：
        超时样本的 ``value`` 是**配置里的超时上限**（例如 800 ms），
        不是一个被测量出来的响应时间。把它混进曲线，会让「P95 = 22 ms」
        与曲线上那条贴着 800 ms 的线摆在同一张页面上，读者只会以为哪里算错了。

        失败样本不是被丢掉，而是单独计入 ``failed``，前端据此在曲线上标出故障时段。
        """
        if bucket_ms <= 0:
            raise ValueError("bucket_ms 必须为正数")
        where, params = _sample_filters(device_id, from_ts, to_ts, metric)
        sql = (
            "SELECT ((s.ts - ?) / ?) AS bucket, "
            "       MIN(s.ts) AS ts, "
            "       AVG(CASE WHEN s.quality = 0 THEN s.value END) AS avg_value, "
            "       MAX(CASE WHEN s.quality = 0 THEN s.value END) AS max_value, "
            "       SUM(CASE WHEN s.quality = 0 THEN 1 ELSE 0 END) AS ok_count, "
            "       SUM(CASE WHEN s.quality != 0 THEN 1 ELSE 0 END) AS failed_count "
            f"FROM samples s{where} "
            "GROUP BY bucket ORDER BY bucket"
        )
        rows = self.db.query(sql, (int(from_ts), int(bucket_ms), *params))
        return [
            {
                "bucket": int(r["bucket"]),
                "ts": int(from_ts) + int(r["bucket"]) * int(bucket_ms),
                "avg": float(r["avg_value"]) if r["avg_value"] is not None else None,
                "max": float(r["max_value"]) if r["max_value"] is not None else None,
                "count": int(r["ok_count"] or 0),
                "failed": int(r["failed_count"] or 0),
            }
            for r in rows
        ]

    def delete_samples_before(self, ts: int, *, device_id: int | None = None) -> int:
        """删除某时刻之前的样本，返回删除条数（数据保留策略用）。"""
        if device_id is None:
            cursor = self.db.execute("DELETE FROM samples WHERE ts < ?", (int(ts),))
        else:
            cursor = self.db.execute(
                "DELETE FROM samples WHERE ts < ? AND device_id = ?", (int(ts), int(device_id))
            )
        return cursor.rowcount or 0

    # ==================================================================
    # alarms
    # ==================================================================

    def find_active_alarm(self, device_id: int, rule: str) -> sqlite3.Row | None:
        """这条规则当前是否还有未恢复的告警。

        告警引擎每轮先问这一句，才能做到「未恢复前只保留一条」，
        避免每个采集周期都插一条、把告警表刷爆。
        """
        return self.db.query_one(
            "SELECT * FROM alarms WHERE device_id = ? AND rule = ? AND ended_at IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (int(device_id), rule),
        )

    def open_alarm(
        self, device_id: int, rule: str, started_at: int, detail: str | None = None
    ) -> int:
        cursor = self.db.execute(
            "INSERT INTO alarms(device_id, rule, started_at, ended_at, detail) "
            "VALUES (?, ?, ?, NULL, ?)",
            (int(device_id), rule, int(started_at), detail),
        )
        return int(cursor.lastrowid or 0)

    def close_alarm(self, alarm_id: int, ended_at: int, detail: str | None = None) -> None:
        if detail is None:
            self.db.execute("UPDATE alarms SET ended_at = ? WHERE id = ?", (int(ended_at), alarm_id))
        else:
            self.db.execute(
                "UPDATE alarms SET ended_at = ?, detail = ? WHERE id = ?",
                (int(ended_at), detail, alarm_id),
            )

    def active_alarms(self, device_id: int | None = None) -> list[sqlite3.Row]:
        if device_id is None:
            return self.db.query(
                "SELECT a.*, d.name AS device FROM alarms a JOIN devices d ON d.id = a.device_id "
                "WHERE a.ended_at IS NULL ORDER BY a.started_at DESC"
            )
        return self.db.query(
            "SELECT a.*, d.name AS device FROM alarms a JOIN devices d ON d.id = a.device_id "
            "WHERE a.ended_at IS NULL AND a.device_id = ? ORDER BY a.started_at DESC",
            (int(device_id),),
        )

    def list_alarms(
        self,
        *,
        device_id: int | None = None,
        from_ts: int | None = None,
        to_ts: int | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list[Any] = []
        if device_id is not None:
            clauses.append("a.device_id = ?")
            params.append(int(device_id))
        if from_ts is not None:
            clauses.append("a.started_at >= ?")
            params.append(int(from_ts))
        if to_ts is not None:
            clauses.append("a.started_at < ?")
            params.append(int(to_ts))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            "SELECT a.*, d.name AS device FROM alarms a JOIN devices d ON d.id = a.device_id"
            f"{where} ORDER BY a.started_at DESC LIMIT ? OFFSET ?"
        )
        return self.db.query(sql, (*params, int(limit), int(offset)))

    def count_alarms(
        self,
        *,
        device_id: int | None = None,
        from_ts: int | None = None,
        to_ts: int | None = None,
    ) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if device_id is not None:
            clauses.append("device_id = ?")
            params.append(int(device_id))
        if from_ts is not None:
            clauses.append("started_at >= ?")
            params.append(int(from_ts))
        if to_ts is not None:
            clauses.append("started_at < ?")
            params.append(int(to_ts))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        row = self.db.query_one(f"SELECT COUNT(*) AS n FROM alarms{where}", params)
        return int(row["n"]) if row else 0

    # ==================================================================
    # runs
    # ==================================================================

    def start_run(self, env: dict[str, Any] | None = None, *, started_at: int | None = None) -> int:
        cursor = self.db.execute(
            "INSERT INTO runs(started_at, finished_at, env, summary) VALUES (?, NULL, ?, NULL)",
            (int(started_at if started_at is not None else now_ms()),
             json.dumps(env or {}, ensure_ascii=False)),
        )
        return int(cursor.lastrowid or 0)

    def finish_run(
        self, run_id: int, summary: dict[str, Any] | None = None, *, finished_at: int | None = None
    ) -> None:
        self.db.execute(
            "UPDATE runs SET finished_at = ?, summary = ? WHERE id = ?",
            (
                int(finished_at if finished_at is not None else now_ms()),
                json.dumps(summary or {}, ensure_ascii=False),
                int(run_id),
            ),
        )

    def get_run(self, run_id: int) -> sqlite3.Row | None:
        return self.db.query_one("SELECT * FROM runs WHERE id = ?", (int(run_id),))

    # ==================================================================
    # 运维辅助
    # ==================================================================

    def query_plan(self, sql: str, params: Sequence[Any] = ()) -> list[str]:
        """返回 SQLite 的查询计划，用于验证索引是否真的生效（README 性能一节）。"""
        rows = self.db.query(f"EXPLAIN QUERY PLAN {sql}", params)
        return [str(r["detail"]) for r in rows]


# ---------------------------------------------------------------------------
# 过滤条件构造：把「左闭右开」这条约定收进一个函数
# ---------------------------------------------------------------------------


def _sample_filters(
    device_id: int | None,
    from_ts: int | None,
    to_ts: int | None,
    metric: str | None,
) -> tuple[str, list[Any]]:
    """返回 ``(WHERE 子句, 参数)``；区间语义固定为 ``[from_ts, to_ts)``。

    **约定**：子句里的列一律带 ``s.`` 前缀，因此调用方的 SQL
    必须写成 ``FROM samples s``（可以再 JOIN devices）。统一前缀是为了让
    「按设备 + 时间过滤」这一条路径只有一个实现，改口径时不会漏掉某个查询。
    """
    clauses: list[str] = []
    params: list[Any] = []
    if device_id is not None:
        clauses.append("s.device_id = ?")
        params.append(int(device_id))
    if from_ts is not None:
        clauses.append("s.ts >= ?")
        params.append(int(from_ts))
    if to_ts is not None:
        clauses.append("s.ts < ?")
        params.append(int(to_ts))
    if metric is not None:
        clauses.append("s.metric = ?")
        params.append(metric)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params
