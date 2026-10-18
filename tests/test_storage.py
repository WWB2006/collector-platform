"""存储层用例：建表、批量写入、分页、区间边界、索引生效。

这里最值钱的一条是 :func:`test_range_is_left_closed_right_open`——
「左闭右开」如果每个调用点各写各的，统计数字迟早对不上，
所以把它钉成用例，改坏了立刻红。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from app.storage import Database, Repository
from app.storage.migrations import SCHEMA_VERSION, current_version

# 固定基准时间，避免用例依赖「当前时间」（CI 与本地跑出来的结果才一致）。
BASE = 1_700_000_000_000


# ---------------------------------------------------------------------------
# 建表
# ---------------------------------------------------------------------------


def test_all_tables_created(db: Database) -> None:
    names = {
        row["name"]
        for row in db.query("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {"devices", "samples", "alarms", "runs", "schema_version"} <= names


def test_indexes_created(db: Database) -> None:
    names = {
        row["name"]
        for row in db.query("SELECT name FROM sqlite_master WHERE type = 'index'")
    }
    assert "idx_samples_device_ts" in names
    assert "idx_samples_device_metric_ts" in names
    assert "idx_alarms_device_rule" in names


def test_schema_version_recorded(db: Database) -> None:
    assert current_version(db) == SCHEMA_VERSION


def test_init_schema_is_idempotent(db: Database) -> None:
    db.init_schema()
    db.init_schema()
    assert current_version(db) == SCHEMA_VERSION
    rows = db.query("SELECT COUNT(*) AS n FROM schema_version")
    assert rows[0]["n"] == 1


def test_wal_mode_enabled(db: Database) -> None:
    row = db.query_one("PRAGMA journal_mode")
    assert str(row[0]).lower() == "wal"


def test_schema_path_missing_raises(tmp_path: Path) -> None:
    database = Database(tmp_path / "x.db")
    with pytest.raises(FileNotFoundError):
        database.init_schema(tmp_path / "nope.sql")
    database.close_all()


# ---------------------------------------------------------------------------
# devices
# ---------------------------------------------------------------------------


def test_upsert_device_is_idempotent(repo: Repository) -> None:
    first = repo.upsert_device("plc-01", "tcp", "127.0.0.1:5020", 1, 500)
    second = repo.upsert_device("plc-01", "tcp", "127.0.0.1:5021", 2, 800)
    assert first == second
    device = repo.get_device("plc-01")
    assert device is not None
    assert device["address"] == "127.0.0.1:5021"
    assert device["poll_interval_ms"] == 800
    assert len(repo.list_devices()) == 1


def test_unknown_device_id_raises(repo: Repository) -> None:
    with pytest.raises(KeyError):
        repo.device_id("ghost")


def test_sync_devices_returns_mapping(repo: Repository, app_config) -> None:
    mapping = repo.sync_devices(app_config.devices)
    assert set(mapping) == {"plc-01", "meter-02"}
    assert repo.device_id("plc-01") == mapping["plc-01"]


def test_set_device_enabled(repo: Repository) -> None:
    repo.upsert_device("a", "tcp", "x")
    repo.set_device_enabled("a", False)
    assert repo.get_device("a")["enabled"] == 0


# ---------------------------------------------------------------------------
# samples：批量写入
# ---------------------------------------------------------------------------


def _rows(device_id: int, count: int, *, start: int = BASE, metric: str = "response_ms",
          quality: int = 0, value: float = 10.0):
    return [
        (device_id, start + i * 1000, metric, value + i, quality) for i in range(count)
    ]


def test_insert_returns_row_count(repo: Repository) -> None:
    device_id = repo.upsert_device("plc-01", "tcp", "x")
    assert repo.insert_samples(_rows(device_id, 1200)) == 1200
    assert repo.count_samples(device_id) == 1200


def test_insert_empty_returns_zero(repo: Repository) -> None:
    device_id = repo.upsert_device("plc-01", "tcp", "x")
    assert repo.insert_samples([]) == 0
    assert repo.count_samples(device_id) == 0


def test_batch_boundaries(repo: Repository) -> None:
    """批大小边界：0、恰好一批、多一条，都不能丢数据。"""
    device_id = repo.upsert_device("plc-01", "tcp", "x")
    for count in (0, 1, 499, 500, 501, 1000):
        repo.db.execute("DELETE FROM samples")
        inserted = repo.insert_samples(_rows(device_id, count), batch_size=500)
        assert inserted == count
        assert repo.count_samples(device_id) == count


def test_insert_rolls_back_on_bad_row(repo: Repository) -> None:
    """一批里有非法行时整批回滚，不能写一半留一半。

    注意 SQLite 是动态类型：往 INTEGER 列塞字符串**不会**报错，
    所以这里用 ``metric = NULL`` 触发 NOT NULL 约束，才是真正的失败路径。
    """
    device_id = repo.upsert_device("plc-01", "tcp", "x")
    bad = [
        (device_id, BASE, "response_ms", 1.0, 0),
        (device_id, BASE + 1000, None, 1.0, 0),
    ]
    with pytest.raises(sqlite3.IntegrityError):
        repo.insert_samples(bad)
    assert repo.count_samples(device_id) == 0


def test_quality_values_kept(repo: Repository) -> None:
    """非 0 质量码的样本必须落库——这是成功率口径的前提。"""
    device_id = repo.upsert_device("plc-01", "tcp", "x")
    repo.insert_samples(
        [
            (device_id, BASE, "response_ms", 10.0, 0),
            (device_id, BASE + 1000, "response_ms", 0.0, 1),
            (device_id, BASE + 2000, "response_ms", 0.0, 2),
            (device_id, BASE + 3000, "response_ms", 0.0, 3),
        ]
    )
    assert repo.quality_values(device_id, from_ts=BASE, to_ts=BASE + 10_000) == [
        (0, 10.0),
        (1, 0.0),
        (2, 0.0),
        (3, 0.0),
    ]


# ---------------------------------------------------------------------------
# samples：区间与分页
# ---------------------------------------------------------------------------


def test_range_is_left_closed_right_open(repo: Repository) -> None:
    device_id = repo.upsert_device("plc-01", "tcp", "x")
    repo.insert_samples(
        [
            (device_id, BASE, "response_ms", 1.0, 0),
            (device_id, BASE + 1000, "response_ms", 2.0, 0),
            (device_id, BASE + 2000, "response_ms", 3.0, 0),
        ]
    )
    rows = repo.query_samples(device_id, from_ts=BASE, to_ts=BASE + 2000, newest_first=False)
    assert [r["ts"] for r in rows] == [BASE, BASE + 1000]


def test_adjacent_windows_do_not_overlap_or_gap(repo: Repository) -> None:
    """两个相邻区间拼起来必须等于大区间——边界只数一次。"""
    device_id = repo.upsert_device("plc-01", "tcp", "x")
    repo.insert_samples(_rows(device_id, 10, start=BASE))
    left = repo.count_samples(device_id, from_ts=BASE, to_ts=BASE + 5000)
    right = repo.count_samples(device_id, from_ts=BASE + 5000, to_ts=BASE + 10_000)
    total = repo.count_samples(device_id, from_ts=BASE, to_ts=BASE + 10_000)
    assert left + right == total == 10


def test_pagination(repo: Repository) -> None:
    device_id = repo.upsert_device("plc-01", "tcp", "x")
    repo.insert_samples(_rows(device_id, 250))
    page1 = repo.query_samples(device_id, limit=100, offset=0, newest_first=False)
    page2 = repo.query_samples(device_id, limit=100, offset=100, newest_first=False)
    page3 = repo.query_samples(device_id, limit=100, offset=200, newest_first=False)
    assert (len(page1), len(page2), len(page3)) == (100, 100, 50)
    assert page1[0]["ts"] < page2[0]["ts"] < page3[0]["ts"]
    assert len({r["id"] for r in page1 + page2 + page3}) == 250


def test_newest_first_ordering(repo: Repository) -> None:
    device_id = repo.upsert_device("plc-01", "tcp", "x")
    repo.insert_samples(_rows(device_id, 5))
    rows = repo.query_samples(device_id, limit=5, newest_first=True)
    assert rows[0]["ts"] > rows[-1]["ts"]


def test_metric_filter(repo: Repository) -> None:
    device_id = repo.upsert_device("plc-01", "tcp", "x")
    repo.insert_samples(
        [
            (device_id, BASE, "response_ms", 10.0, 0),
            (device_id, BASE, "register_0", 100.0, 0),
            (device_id, BASE + 1000, "register_0", 101.0, 0),
        ]
    )
    assert repo.count_samples(device_id, metric="register_0") == 2
    assert repo.count_samples(device_id, metric="response_ms") == 1


def test_query_samples_joins_device_name(repo: Repository) -> None:
    device_id = repo.upsert_device("plc-01", "tcp", "x")
    repo.insert_samples(_rows(device_id, 1))
    row = repo.query_samples(device_id, limit=1)[0]
    assert row["device"] == "plc-01"


def test_count_across_all_devices(repo: Repository) -> None:
    a = repo.upsert_device("a", "tcp", "x")
    b = repo.upsert_device("b", "tcp", "y")
    repo.insert_samples(_rows(a, 3) + _rows(b, 5, start=BASE + 100_000))
    assert repo.count_samples() == 8
    assert repo.count_samples(from_ts=BASE, to_ts=BASE + 50_000) == 3


def test_quality_distribution(repo: Repository) -> None:
    device_id = repo.upsert_device("plc-01", "tcp", "x")
    repo.insert_samples(
        [
            (device_id, BASE, "response_ms", 10.0, 0),
            (device_id, BASE + 1, "response_ms", 11.0, 0),
            (device_id, BASE + 2, "response_ms", 0.0, 1),
            (device_id, BASE + 3, "response_ms", 0.0, 2),
        ]
    )
    assert repo.quality_distribution(device_id, from_ts=BASE, to_ts=BASE + 100) == {0: 2, 1: 1, 2: 1}


def test_quality_distribution_ignores_non_latency_metrics(repo: Repository) -> None:
    """一次采集会产生多条记录，分布只按 response_ms 数，否则总数会翻倍。"""
    device_id = repo.upsert_device("plc-01", "tcp", "x")
    repo.insert_samples(
        [
            (device_id, BASE, "response_ms", 10.0, 0),
            (device_id, BASE, "register_0", 100.0, 0),
            (device_id, BASE, "register_1", 200.0, 0),
        ]
    )
    assert repo.quality_distribution(device_id, from_ts=BASE, to_ts=BASE + 100) == {0: 1}


# ---------------------------------------------------------------------------
# 分桶
# ---------------------------------------------------------------------------


def test_series_buckets_align_to_from_ts(repo: Repository) -> None:
    device_id = repo.upsert_device("plc-01", "tcp", "x")
    rows = []
    for i in range(10):
        rows.append((device_id, BASE + i * 1000, "response_ms", float(i), 0))
    repo.insert_samples(rows)

    buckets = repo.series("response_ms", device_id=device_id, from_ts=BASE,
                          to_ts=BASE + 10_000, bucket_ms=5000)
    assert len(buckets) == 2
    assert buckets[0]["ts"] == BASE
    assert buckets[1]["ts"] == BASE + 5000
    assert buckets[0]["count"] == 5
    assert buckets[0]["avg"] == pytest.approx(2.0)   # (0+1+2+3+4)/5
    assert buckets[0]["max"] == pytest.approx(4.0)
    assert buckets[1]["avg"] == pytest.approx(7.0)   # (5+6+7+8+9)/5


def test_series_empty_range(repo: Repository) -> None:
    device_id = repo.upsert_device("plc-01", "tcp", "x")
    assert repo.series("response_ms", device_id=device_id, from_ts=BASE,
                       to_ts=BASE + 1000, bucket_ms=100) == []


def test_series_excludes_failed_samples_from_latency(repo: Repository) -> None:
    """失败样本不能进 avg/max，否则曲线与分位数口径打架。

    超时样本的 value 是**配置里的超时上限**（这里是 800 ms），
    不是测出来的响应时间。把它算进平均与最大值，会出现
    「卡片上写 P95 = 20 ms，曲线却贴着 800 ms」这种自相矛盾的页面。
    失败样本单独计入 ``failed``，供前端把故障时段标出来。
    """
    device_id = repo.upsert_device("plc-01", "tcp", "x")
    rows = [
        (device_id, BASE + 0, "response_ms", 10.0, 0),
        (device_id, BASE + 100, "response_ms", 20.0, 0),
        (device_id, BASE + 200, "response_ms", 800.0, 1),  # 超时
        (device_id, BASE + 300, "response_ms", 30.0, 0),
    ]
    repo.insert_samples(rows)

    bucket = repo.series("response_ms", device_id=device_id, from_ts=BASE,
                         to_ts=BASE + 1000, bucket_ms=1000)[0]
    assert bucket["avg"] == pytest.approx(20.0)   # (10+20+30)/3，不含 800
    assert bucket["max"] == pytest.approx(30.0)   # 不含 800
    assert bucket["count"] == 3
    assert bucket["failed"] == 1


def test_series_bucket_with_only_failures_has_no_latency(repo: Repository) -> None:
    """整桶都是失败时 avg/max 必须是 null，而不是 0——
    ``0 ms`` 是一个看起来完全正常的响应时间，画在图上就是「这条链路很快」。"""
    device_id = repo.upsert_device("plc-01", "tcp", "x")
    repo.insert_samples([(device_id, BASE + 10, "response_ms", 800.0, 1)])

    bucket = repo.series("response_ms", device_id=device_id, from_ts=BASE,
                         to_ts=BASE + 1000, bucket_ms=1000)[0]
    assert bucket["avg"] is None
    assert bucket["max"] is None
    assert bucket["count"] == 0
    assert bucket["failed"] == 1


def test_series_rejects_bad_bucket(repo: Repository) -> None:
    with pytest.raises(ValueError):
        repo.series("response_ms", from_ts=BASE, to_ts=BASE + 1000, bucket_ms=0)


# ---------------------------------------------------------------------------
# alarms
# ---------------------------------------------------------------------------


def test_alarm_lifecycle(repo: Repository) -> None:
    device_id = repo.upsert_device("plc-01", "tcp", "x")
    alarm_id = repo.open_alarm(device_id, "timeout-streak", BASE, detail="连续 3 次超时")
    assert repo.find_active_alarm(device_id, "timeout-streak") is not None
    assert len(repo.active_alarms(device_id)) == 1

    repo.close_alarm(alarm_id, BASE + 60_000, detail="已恢复")
    assert repo.find_active_alarm(device_id, "timeout-streak") is None
    assert repo.active_alarms(device_id) == []

    row = repo.db.query_one("SELECT * FROM alarms WHERE id = ?", (alarm_id,))
    assert row["ended_at"] == BASE + 60_000
    assert row["detail"] == "已恢复"


def test_find_active_alarm_ignores_other_rules(repo: Repository) -> None:
    device_id = repo.upsert_device("plc-01", "tcp", "x")
    repo.open_alarm(device_id, "rule-a", BASE)
    assert repo.find_active_alarm(device_id, "rule-b") is None


def test_active_alarm_joins_device_name(repo: Repository) -> None:
    device_id = repo.upsert_device("plc-01", "tcp", "x")
    repo.open_alarm(device_id, "r", BASE)
    assert repo.active_alarms()[0]["device"] == "plc-01"


def test_list_and_count_alarms(repo: Repository) -> None:
    device_id = repo.upsert_device("plc-01", "tcp", "x")
    for i in range(5):
        alarm_id = repo.open_alarm(device_id, f"rule-{i}", BASE + i * 1000)
        repo.close_alarm(alarm_id, BASE + i * 1000 + 500)
    assert repo.count_alarms(device_id=device_id) == 5
    assert repo.count_alarms(device_id=device_id, from_ts=BASE + 2000, to_ts=BASE + 4000) == 2
    assert len(repo.list_alarms(device_id=device_id, limit=2)) == 2


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------


def test_run_lifecycle(repo: Repository) -> None:
    run_id = repo.start_run({"host": "ci", "python": "3.13"}, started_at=BASE)
    row = repo.get_run(run_id)
    assert row["started_at"] == BASE
    assert row["finished_at"] is None

    repo.finish_run(run_id, {"samples": 100}, finished_at=BASE + 60_000)
    row = repo.get_run(run_id)
    assert row["finished_at"] == BASE + 60_000
    assert "samples" in row["summary"]


# ---------------------------------------------------------------------------
# 索引确实被用上（不是「建了就算」）
# ---------------------------------------------------------------------------


def test_index_used_for_device_time_query(repo: Repository) -> None:
    plan = repo.query_plan(
        "SELECT * FROM samples WHERE device_id = ? AND ts >= ? AND ts < ?",
        (1, BASE, BASE + 1000),
    )
    joined = " | ".join(plan)
    assert "idx_samples_device_ts" in joined, joined


def test_index_used_for_metric_query(repo: Repository) -> None:
    plan = repo.query_plan(
        "SELECT * FROM samples WHERE device_id = ? AND metric = ? AND ts >= ? AND ts < ?",
        (1, "response_ms", BASE, BASE + 1000),
    )
    joined = " | ".join(plan)
    assert "idx_samples_device_metric_ts" in joined, joined


def test_index_used_for_active_alarm_lookup(repo: Repository) -> None:
    plan = repo.query_plan(
        "SELECT * FROM alarms WHERE device_id = ? AND rule = ? AND ended_at IS NULL", (1, "r")
    )
    joined = " | ".join(plan)
    assert "idx_alarms_device_rule" in joined, joined


# ---------------------------------------------------------------------------
# 批量写入的性能底线（宽松阈值，只为拦住「退化成逐条提交」这种回归）
# ---------------------------------------------------------------------------


def test_bulk_insert_is_not_pathologically_slow(repo: Repository) -> None:
    device_id = repo.upsert_device("plc-01", "tcp", "x")
    rows = _rows(device_id, 20_000)
    started = time.perf_counter()
    repo.insert_samples(rows)
    elapsed = time.perf_counter() - started
    assert repo.count_samples(device_id) == 20_000
    # 逐条提交的写法在这里通常要 5 秒以上；批量应在 1 秒内（宽松放到 5 秒）。
    assert elapsed < 5.0, f"2 万条写入耗时 {elapsed:.2f}s，疑似退化成逐条提交"


def test_cleanup_helper(repo: Repository) -> None:
    device_id = repo.upsert_device("plc-01", "tcp", "x")
    repo.insert_samples(_rows(device_id, 10))
    deleted = repo.delete_samples_before(BASE + 5000)
    assert deleted == 5
    assert repo.count_samples(device_id) == 5
