"""连接管理的用例：内存库、事务回滚、多线程连接隔离、关闭语义。

这些分支平时不会走到，但一旦出错就是「数据写了一半」「进程退不出」这类事故，
所以显式钉住。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from app.storage import Database


def test_memory_database() -> None:
    db = Database(":memory:")
    db.init_schema()
    assert db.is_memory is True
    names = {row["name"] for row in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"devices", "samples", "alarms", "runs"} <= names
    db.execute("INSERT INTO devices(name, transport, address, unit_id, poll_interval_ms) "
               "VALUES ('a', 'tcp', 'x', 1, 1000)")
    assert db.scalar("SELECT COUNT(*) FROM devices") == 1
    db.close_all()


def test_memory_database_shares_connection() -> None:
    """内存库每个连接都是独立的空库，必须共用同一个连接，否则建的表看不见。"""
    db = Database(":memory:")
    db.init_schema()
    assert db.connect() is db.connect()
    db.close_all()


def test_transaction_commits(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    db.init_schema()
    with db.transaction() as conn:
        conn.execute("INSERT INTO devices(name, transport, address, unit_id, poll_interval_ms) "
                     "VALUES ('a', 'tcp', 'x', 1, 1000)")
    assert db.scalar("SELECT COUNT(*) FROM devices") == 1
    db.close_all()


def test_transaction_rolls_back_on_error(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    db.init_schema()
    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            conn.execute("INSERT INTO devices(name, transport, address, unit_id, poll_interval_ms) "
                         "VALUES ('a', 'tcp', 'x', 1, 1000)")
            raise RuntimeError("boom")
    assert db.scalar("SELECT COUNT(*) FROM devices") == 0
    db.close_all()


def test_rollback_does_not_break_next_transaction(tmp_path: Path) -> None:
    """回滚之后事务状态必须干净，否则下一次 BEGIN 会报「已在事务中」。"""
    db = Database(tmp_path / "t.db")
    db.init_schema()
    with pytest.raises(RuntimeError):
        with db.transaction():
            raise RuntimeError("boom")
    with db.transaction() as conn:
        conn.execute("INSERT INTO devices(name, transport, address, unit_id, poll_interval_ms) "
                     "VALUES ('b', 'tcp', 'y', 1, 1000)")
    assert db.scalar("SELECT COUNT(*) FROM devices") == 1
    db.close_all()


def test_each_thread_gets_own_connection(tmp_path: Path) -> None:
    """sqlite3 连接不能跨线程；每线程一个连接，采集线程与接口线程才能同时工作。"""
    db = Database(tmp_path / "t.db")
    db.init_schema()
    seen: dict[str, int] = {}

    def worker(tag: str) -> None:
        seen[tag] = id(db.connect())
        db.execute("INSERT INTO devices(name, transport, address, unit_id, poll_interval_ms) "
                   f"VALUES ('{tag}', 'tcp', 'x', 1, 1000)")

    threads = [threading.Thread(target=worker, args=(f"dev{i}",)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(set(seen.values())) == 3
    assert db.scalar("SELECT COUNT(*) FROM devices") == 3
    db.close_all()


def test_close_all_closes_every_thread_connection(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    db.init_schema()

    def worker() -> None:
        db.connect()

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    db.close_all()
    assert db._all == []


def test_close_is_safe_to_repeat(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    db.init_schema()
    db.connect()
    db.close()
    db.close()  # 第二次不能报错
    assert db.scalar("SELECT COUNT(*) FROM devices") == 0
    db.close_all()


def test_context_manager(tmp_path: Path) -> None:
    with Database(tmp_path / "t.db") as db:
        db.init_schema()
        assert db.scalar("SELECT COUNT(*) FROM devices") == 0
    assert db._all == []


def test_scalar_default_on_empty(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    db.init_schema()
    assert db.scalar("SELECT MAX(id) FROM devices", default=-1) == -1
    db.close_all()


def test_execute_and_executemany(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    db.init_schema()
    db.execute("INSERT INTO devices(name, transport, address, unit_id, poll_interval_ms) "
               "VALUES (?, 'tcp', 'x', 1, 1000)", ("a",))
    db.executemany(
        "INSERT INTO samples(device_id, ts, metric, value, quality) VALUES (?, ?, ?, ?, ?)",
        [(1, 1000, "response_ms", 1.0, 0), (1, 2000, "response_ms", 2.0, 0)],
    )
    assert db.scalar("SELECT COUNT(*) FROM samples") == 2
    db.close_all()


def test_query_one_returns_none(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    db.init_schema()
    assert db.query_one("SELECT * FROM devices WHERE name = ?", ("ghost",)) is None
    db.close_all()


def test_row_factory_returns_named_columns(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    db.init_schema()
    db.execute("INSERT INTO devices(name, transport, address, unit_id, poll_interval_ms) "
               "VALUES ('a', 'tcp', 'x', 1, 1000)")
    row = db.query_one("SELECT * FROM devices")
    assert row["name"] == "a"
    assert row["transport"] == "tcp"
    db.close_all()


def test_foreign_keys_pragma_on(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    db.init_schema()
    assert db.query_one("PRAGMA foreign_keys")[0] == 1
    db.close_all()


def test_parent_directory_created(tmp_path: Path) -> None:
    db = Database(tmp_path / "nested" / "deep" / "t.db")
    db.init_schema()
    assert (tmp_path / "nested" / "deep" / "t.db").exists()
    db.close_all()


def test_wal_can_be_disabled(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db", enable_wal=False)
    db.init_schema()
    assert str(db.query_one("PRAGMA journal_mode")[0]).lower() == "delete"
    db.close_all()


def test_init_schema_missing_file(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    with pytest.raises(FileNotFoundError):
        db.init_schema(tmp_path / "missing.sql")
    db.close_all()


def test_database_error_propagates(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    db.init_schema()
    with pytest.raises(sqlite3.OperationalError):
        db.query("SELECT * FROM not_a_table")
    db.close_all()
