"""接口层的依赖注入。

路由不自己去 ``app.state`` 上摸对象，一律通过这里的依赖拿，
好处是「配置没加载好」这种状态只需要在一处判断并转成 503。

``app.state`` 上的运行时对象（``config`` / ``db`` / ``repo`` / ``alarm_engine``）
由 :func:`app.main.ensure_runtime` 惰性建好，测试可以直接注入自己的仓储层。
"""

from __future__ import annotations

from typing import Any

from fastapi import Request

from app.analytics.alarm_engine import AlarmEngine
from app.api.errors import ApiError, NotFoundError
from app.config import AppConfig
from app.storage import Repository


def _state(request: Request) -> Any:
    return request.app.state


def get_config(request: Request) -> AppConfig:
    config = getattr(_state(request), "config", None)
    if config is None:
        raise ApiError(
            getattr(_state(request), "config_error", None) or "配置未加载",
            code="config_unavailable",
            status_code=503,
        )
    return config


def get_repo(request: Request) -> Repository:
    repo = getattr(_state(request), "repo", None)
    if repo is None:
        raise ApiError("数据库尚未就绪", code="storage_unavailable", status_code=503)
    return repo


def get_alarm_engine(request: Request) -> AlarmEngine:
    engine = getattr(_state(request), "alarm_engine", None)
    if engine is None:
        raise ApiError("告警引擎尚未就绪", code="alarm_unavailable", status_code=503)
    return engine


def resolve_device_id(repo: Repository, device: str | None) -> int | None:
    """设备名 -> id；未知设备返回 404 而不是让后续查询静默返回空列表。

    「设备名打错了」和「这台设备这段时间没有数据」是两件不同的事，
    前端需要区分，所以这里必须报错。
    """
    if device is None or device == "":
        return None
    try:
        return repo.device_id(device)
    except KeyError as exc:
        raise NotFoundError(f"设备不存在：{device}", field="device") from exc
