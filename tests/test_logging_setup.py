"""日志配置用例。

守两件事：轮转参数确实生效（长时间运行不会写满磁盘），
以及重复初始化不会叠加 handler（否则日志行数会指数增长）。
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from app.logging_setup import get_logger, setup_logging


def _file_handler(logger: logging.Logger) -> logging.handlers.RotatingFileHandler | None:
    for handler in logger.handlers:
        if isinstance(handler, logging.handlers.RotatingFileHandler):
            return handler
    return None


def test_file_and_console_handlers(tmp_path: Path) -> None:
    logger = setup_logging("debug", tmp_path / "logs")
    assert len(logger.handlers) == 2
    assert _file_handler(logger) is not None


def test_rotation_settings(tmp_path: Path) -> None:
    logger = setup_logging("info", tmp_path / "logs", max_bytes=1024, backup_count=3)
    handler = _file_handler(logger)
    assert handler is not None
    assert handler.maxBytes == 1024
    assert handler.backupCount == 3


def test_console_only_shows_warnings(tmp_path: Path) -> None:
    logger = setup_logging("debug", tmp_path / "logs")
    console = [
        h
        for h in logger.handlers
        if isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert len(console) == 1
    assert console[0].level == logging.WARNING


def test_repeated_setup_does_not_stack_handlers(tmp_path: Path) -> None:
    logger = setup_logging("info", tmp_path / "logs")
    assert len(logger.handlers) == 2
    setup_logging("info", tmp_path / "logs")
    assert len(logger.handlers) == 2
    setup_logging("debug", tmp_path / "logs")
    assert len(logger.handlers) == 2


def test_log_dir_is_created(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "nested"
    setup_logging("info", target)
    assert target.is_dir()


def test_console_only_when_no_log_dir() -> None:
    logger = setup_logging("info", None)
    assert _file_handler(logger) is None
    assert len(logger.handlers) == 1


def test_child_logger_naming() -> None:
    assert get_logger("modbus").name == "collector.modbus"
    assert get_logger("collector.storage").name == "collector.storage"
    assert get_logger().name == "collector"


def test_file_receives_debug_lines(tmp_path: Path) -> None:
    logger = setup_logging("debug", tmp_path / "logs", filename="t.log")
    get_logger("probe").debug("hello-file")
    for handler in logger.handlers:
        handler.flush()
    content = (tmp_path / "logs" / "t.log").read_text(encoding="utf-8")
    assert "hello-file" in content


def test_level_name_is_case_insensitive(tmp_path: Path) -> None:
    logger = setup_logging("DEBUG", tmp_path / "logs")
    assert logger.level == logging.DEBUG
