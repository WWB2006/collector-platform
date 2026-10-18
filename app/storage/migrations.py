"""建表与版本迁移。

现在的 schema 只有一版，但**从第一天就把版本号写进库**，
后面加字段时才有地方判断「这个库是旧的还是新的」，
而不是靠 ``try: ALTER TABLE ... except: pass`` 猜。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅用于类型标注，避免与 db.py 形成运行时循环导入
    from app.storage.db import Database

#: 当前 schema 版本。改 ``sql/schema.sql`` 时同步 +1，并在下面登记。
SCHEMA_VERSION = 1


def apply_schema(db: Database, schema_path: str | Path) -> None:
    """执行建表脚本并记录版本号（幂等，可重复调用）。"""
    path = Path(schema_path)
    if not path.exists():
        raise FileNotFoundError(f"建表脚本不存在：{path}")

    sql = path.read_text(encoding="utf-8")
    conn = db.connect()
    # executescript 会自行提交，因此不放进 transaction()。
    conn.executescript(sql)

    current = current_version(db)
    if current is None:
        conn.execute(
            "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, now_ms()),
        )
    elif current < SCHEMA_VERSION:
        conn.execute(
            "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, now_ms()),
        )


def current_version(db: Database) -> int | None:
    """已应用的最高版本号；库是空的（表还不存在）时返回 ``None``。"""
    try:
        row = db.query_one("SELECT MAX(version) AS v FROM schema_version")
    except sqlite3.OperationalError:
        return None
    return None if row is None or row["v"] is None else int(row["v"])


def now_ms() -> int:
    """当前 Unix 毫秒。全项目统一从这里取时间，避免各处写法不一致。"""
    return int(time.time() * 1000)
