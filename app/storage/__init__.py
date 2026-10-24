"""存储层：建表、写入、查询、分页。

对外只暴露 :class:`~app.storage.db.Database`、
:class:`~app.storage.repository.Repository` 与质量码常量，
上层不应直接拼 SQL。
"""

from app.storage.db import Database
from app.storage.repository import (
    QUALITY_CRC,
    QUALITY_EXCEPTION,
    QUALITY_LABELS,
    QUALITY_NAMES,
    QUALITY_OK,
    QUALITY_TIMEOUT,
    Repository,
    SampleRecord,
)

__all__ = [
    "Database",
    "Repository",
    "SampleRecord",
    "QUALITY_OK",
    "QUALITY_TIMEOUT",
    "QUALITY_CRC",
    "QUALITY_EXCEPTION",
    "QUALITY_LABELS",
    "QUALITY_NAMES",
]
