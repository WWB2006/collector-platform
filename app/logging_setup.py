"""日志配置：统一格式、级别与按大小轮转的文件输出。

约定（规格「编码规范」）：

* 控制台只留摘要（``warning`` 以上），细节进文件，避免刷屏；
* 文件按大小轮转（默认 5 MB × 5 份），长时间运行不会把磁盘写满；
* 同一条日志里带上设备名等上下文，排查时省一半时间。
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

#: 文件日志格式：时间 / 级别 / 模块 / 消息。
FILE_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
#: 控制台格式更短，只用于看摘要。
CONSOLE_FORMAT = "%(levelname)-7s %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5

#: 本包自己的 logger 前缀，重复调用 setup_logging 时只清理这一片。
_ROOT_LOGGER_NAME = "collector"


def setup_logging(
    level: str = "info",
    log_dir: str | Path | None = None,
    *,
    filename: str = "collector.log",
    console_level: str = "warning",
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
) -> logging.Logger:
    """配置并返回应用 logger。

    :param level: 文件日志级别（``debug``/``info``/``warning``/``error``/``critical``）。
    :param log_dir: 日志目录；为 ``None`` 时只输出到控制台。
    :param console_level: 控制台级别，默认 ``warning``（只留摘要）。
    :returns: 名为 ``collector`` 的 logger，调用方用它写业务日志。
    """
    numeric_level = _to_level(level)
    numeric_console = _to_level(console_level)

    logger = logging.getLogger(_ROOT_LOGGER_NAME)
    logger.setLevel(numeric_level)
    logger.propagate = False  # 不让 root logger 再打印一遍

    # 幂等：重复调用时先摘掉自己上次挂的 handler，避免日志重复输出。
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(FILE_FORMAT, datefmt=DATE_FORMAT)

    console = logging.StreamHandler(stream=sys.stderr)
    console.setLevel(numeric_console)
    console.setFormatter(logging.Formatter(CONSOLE_FORMAT))
    logger.addHandler(console)

    if log_dir is not None:
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            directory / filename,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """取一个挂在 ``collector`` 下的子 logger，例如 ``get_logger("collector.modbus")``。"""
    if not name:
        return logging.getLogger(_ROOT_LOGGER_NAME)
    if name == _ROOT_LOGGER_NAME or name.startswith(_ROOT_LOGGER_NAME + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{_ROOT_LOGGER_NAME}.{name}")


def _to_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    resolved = logging.getLevelName(str(level).upper())
    return resolved if isinstance(resolved, int) else logging.INFO
