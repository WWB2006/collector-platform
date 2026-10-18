"""SQLite 连接管理与建表。

为什么要自己管连接，而不用 ORM：

* 本项目所有 SQL 都是手写的聚合查询，ORM 只会增加一层要维护的映射；
* 写入性能的关键是「批量 executemany + 单次提交」，这需要能显式控制事务边界。

线程模型：**一个线程一个连接**。sqlite3 的连接不能跨线程使用，
而采集线程与 API 请求线程会同时访问同一个库文件。
用 ``threading.local`` 缓存每线程连接，既避免加锁，也避免每次请求重新建连。
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from app.config import PROJECT_ROOT

#: 默认建表脚本位置。
DEFAULT_SCHEMA_PATH = PROJECT_ROOT / "sql" / "schema.sql"

#: 内存库标识；用它时不做 WAL（内存库不支持）。
MEMORY = ":memory:"


class Database:
    """一个 SQLite 库文件的连接工厂 + 建表入口。

    典型用法::

        db = Database("data/records.db")
        db.init_schema()
        repo = Repository(db)
        ...
        db.close_all()
    """

    def __init__(
        self,
        path: str | Path,
        *,
        timeout: float = 10.0,
        enable_wal: bool = True,
        schema_path: str | Path | None = None,
    ) -> None:
        self.path = str(path)
        self._is_memory = self.path == MEMORY
        self._timeout = timeout
        self._enable_wal = enable_wal and not self._is_memory
        self._schema_path = Path(schema_path) if schema_path else DEFAULT_SCHEMA_PATH

        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._lock = threading.Lock()

    # ---- 连接 -----------------------------------------------------------

    @property
    def is_memory(self) -> bool:
        return self._is_memory

    def connect(self) -> sqlite3.Connection:
        """返回**当前线程**的连接，首次调用时创建。

        内存库例外：``:memory:`` 每个连接都是独立的空库，
        所以内存模式下所有线程共用同一个连接（测试里是单线程，安全）。
        """
        existing = getattr(self._local, "conn", None)
        if existing is not None:
            return existing

        if self._is_memory:
            with self._lock:
                if self._all:
                    self._local.conn = self._all[0]
                    return self._all[0]

        conn = self._open()
        with self._lock:
            if self._is_memory and self._all:
                conn.close()
                conn = self._all[0]
            else:
                self._all.append(conn)
        self._local.conn = conn
        return conn

    def _open(self) -> sqlite3.Connection:
        if not self._is_memory:
            directory = Path(self.path).parent
            directory.mkdir(parents=True, exist_ok=True)

        # isolation_level=None -> 完全由我们显式控制 BEGIN/COMMIT，
        # 避免「读操作意外开启事务」这类隐蔽行为。
        conn = sqlite3.connect(
            self.path,
            timeout=self._timeout,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if self._enable_wal:
            # WAL：读写不互相阻塞，采集写库时接口仍能查询。
            conn.execute("PRAGMA journal_mode = WAL")
            # NORMAL 在 WAL 下已足够安全（崩溃不丢已提交事务），且写入快得多。
            conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    # ---- 建表 -----------------------------------------------------------

    def init_schema(self, schema_path: str | Path | None = None) -> None:
        """执行建表脚本（脚本本身是幂等的 ``CREATE ... IF NOT EXISTS``）。"""
        from app.storage.migrations import apply_schema

        apply_schema(self, schema_path or self._schema_path)

    # ---- 便捷执行 -------------------------------------------------------

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self.connect().execute(sql, params)

    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> sqlite3.Cursor:
        return self.connect().executemany(sql, seq)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return list(self.connect().execute(sql, params).fetchall())

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self.connect().execute(sql, params).fetchone()

    def scalar(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = self.query_one(sql, params)
        if row is None:
            return default
        value = row[0]
        return default if value is None else value

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """显式事务：正常结束提交，出异常回滚。

        批量写入必须包在这里面——否则每行一次隐式提交，
        10 万条的耗时会差出一个数量级。
        """
        conn = self.connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    # ---- 关闭 -----------------------------------------------------------

    def close(self) -> None:
        """关闭当前线程的连接。"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            return
        with self._lock:
            if conn in self._all:
                self._all.remove(conn)
        try:
            conn.close()
        finally:
            self._local.conn = None

    def close_all(self) -> None:
        """关闭所有线程的连接（进程退出前调用）。"""
        with self._lock:
            connections, self._all = self._all, []
        for conn in connections:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        self._local.conn = None

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close_all()
