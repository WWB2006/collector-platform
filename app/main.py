"""FastAPI 应用装配。

这个文件是「服务长什么样」的唯一入口：配置、日志、数据库、告警引擎、路由
都在这儿串起来，看一个文件就知道全貌。

设计要点：**配置或数据库出问题都不抛栈**。加载结果缓存进 ``app.state``，
``/health`` 返回 503 并把原因说清楚（形如「配置文件 X：字段 'logLevel'：取值非法」），
服务进程本身仍然活着——运维看到的是一句人话，而不是一屏 traceback。

加载时机有两条路径，结果等价：

1. lifespan 启动时（uvicorn 正常启动走这条，问题在启动日志里就能看到）；
2. 第一次用到运行时对象的请求（惰性兜底，保证不跑 lifespan 的测试客户端
   与嵌入式用法行为一致）。
"""

from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import __version__
from app.analytics.alarm_engine import AlarmEngine
from app.api import register_api
from app.config import AppConfig, ConfigError, load_config
from app.logging_setup import get_logger, setup_logging
from app.storage import Database, Repository
from app.web import register_web

logger = get_logger("main")

#: 环境变量：指定配置文件，便于 ``COLLECTOR_CONFIG=config/app.test.json uvicorn app.main:app``。
CONFIG_ENV_VAR = "COLLECTOR_CONFIG"


def env_config_path() -> Path | None:
    raw = os.environ.get(CONFIG_ENV_VAR)
    return Path(raw) if raw else None


# ---------------------------------------------------------------------------
# 运行时装配（幂等）
# ---------------------------------------------------------------------------


def _load_config_into_state(app: FastAPI) -> None:
    state = app.state
    if getattr(state, "config", None) is not None:
        return
    if getattr(state, "config_error", None) is not None:
        return

    state.config = None
    state.config_error = None
    state.warnings = []

    provided: AppConfig | None = getattr(state, "_provided_config", None)
    config_path: Path | None = getattr(state, "_provided_config_path", None)

    try:
        config = provided if provided is not None else load_config(config_path)
    except ConfigError as exc:
        state.config_error = str(exc)
        logger.error("配置加载失败：%s", exc)
        return

    state.config = config
    try:
        setup_logging(config.log_level, config.log_dir_path)
    except OSError as exc:
        # 日志写不了不影响服务本身，降级成 warning 上报，不阻断。
        state.warnings.append(f"日志初始化失败：{exc}")
        logger.warning("日志初始化失败：%s", exc)
    else:
        logger.info(
            "配置加载完成：%d 台设备（%d 台启用）、%d 条告警规则，数据库 %s",
            len(config.devices),
            len(config.enabled_devices()),
            len(config.alarms),
            config.database_path,
        )


def _open_storage(app: FastAPI) -> None:
    state = app.state
    if getattr(state, "repo", None) is not None:
        return
    if getattr(state, "storage_error", None) is not None:
        return
    if getattr(state, "config", None) is None:
        return

    state.storage_error = None
    provided_repo: Repository | None = getattr(state, "_provided_repo", None)
    if provided_repo is not None:
        state.repo = provided_repo
        state.db = getattr(state, "_provided_db", None)
    else:
        try:
            database = Database(state.config.database_path)
            database.init_schema()
        except Exception as exc:  # noqa: BLE001 - 建库失败也要给人话
            state.storage_error = f"数据库初始化失败：{exc}"
            logger.error("数据库初始化失败：%s", exc)
            return
        state.db = database
        state.repo = Repository(database)

    try:
        state.alarm_engine = AlarmEngine(state.repo, state.config.alarms)
    except Exception as exc:  # noqa: BLE001 - 规则写错时给出可读提示
        state.storage_error = f"告警规则无法解析：{exc}"
        logger.error("告警规则无法解析：%s", exc)
        return

    logger.info("存储就绪：%s", state.config.database_path)


def ensure_runtime(app: FastAPI) -> None:
    """确保 ``app.state`` 上的 config / repo / alarm_engine 都已就绪（幂等）。"""
    _load_config_into_state(app)
    _open_storage(app)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动时装配运行时；退出时关闭数据库。"""
    app.state.started_at = time.monotonic()
    ensure_runtime(app)
    try:
        yield
    finally:
        database = getattr(app.state, "db", None)
        if database is not None and getattr(app.state, "_provided_db", None) is None:
            try:
                database.close_all()
            except Exception as exc:  # noqa: BLE001 - 关闭失败不该影响退出
                logger.warning("关闭数据库失败：%s", exc)
        logger.info("服务退出")


# ---------------------------------------------------------------------------
# 应用工厂
# ---------------------------------------------------------------------------


def create_app(
    config: AppConfig | None = None,
    *,
    config_path: str | Path | None = None,
    repo: Repository | None = None,
    db: Database | None = None,
) -> FastAPI:
    """构造 FastAPI 应用。

    :param config: 直接传入已解析的配置（测试常用）；为 ``None`` 时读文件。
    :param config_path: 配置文件路径，缺省为 ``config/app.json``。
    :param repo: 注入自定义仓储层（测试用临时库）；为 ``None`` 时按配置建库。
    :param db: 与 ``repo`` 配套的 Database；传了 ``repo`` 时由调用方负责关闭。
    """
    app = FastAPI(
        title="设备数据采集与测试报告平台",
        version=__version__,
        description=(
            "Modbus / CSV 回放采集 -> SQLite -> 统计、告警、接口与 HTML 报告。\n\n"
            "所有查询接口默认只返回最近一小时，时间范围上限 7 天。"
        ),
        lifespan=lifespan,
    )
    # 存进 state 而不是闭包捕获，保证 uvicorn --reload 与多次 create_app 互不串味。
    app.state.started_at = time.monotonic()
    app.state.config = None
    app.state.config_error = None
    app.state.storage_error = None
    app.state.warnings = []
    app.state.db = None
    app.state.repo = None
    app.state.alarm_engine = None
    app.state._provided_config = config
    app.state._provided_config_path = (
        Path(config_path) if config_path is not None else env_config_path()
    )
    app.state._provided_repo = repo
    app.state._provided_db = db

    _register_health(app)
    register_api(app)
    register_web(app)
    return app


def _register_health(app: FastAPI) -> None:
    @app.get("/health", tags=["system"], summary="健康检查")
    def health(request: Request) -> Any:
        """返回服务状态；配置或数据库有问题时以 503 + 明确原因回应。"""
        ensure_runtime(request.app)
        state = request.app.state
        cfg = getattr(state, "config", None)
        error = getattr(state, "config_error", None) or getattr(state, "storage_error", None)

        if cfg is None or getattr(state, "repo", None) is None:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "error",
                    "version": __version__,
                    "error": error or "运行时未就绪",
                },
            )

        started = getattr(state, "started_at", None)
        payload: dict[str, Any] = {
            "status": "ok",
            "version": __version__,
            "uptimeSeconds": round(time.monotonic() - started, 3) if started else 0.0,
            "devices": len(cfg.devices),
            "enabledDevices": len(cfg.enabled_devices()),
            "alarmRules": len(cfg.alarms),
            "database": str(cfg.database_path),
        }
        warnings = list(getattr(state, "warnings", []) or [])
        if warnings:
            payload["warnings"] = warnings
        return payload


#: uvicorn 入口：``uvicorn app.main:app``。
app = create_app()
