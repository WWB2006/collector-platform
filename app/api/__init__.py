"""接口层：REST 路由与统一错误处理。

分工：

* :mod:`app.api.time_range`——时间范围与分页参数的解析和上限校验；
* :mod:`app.api.errors`——统一错误结构（所有失败响应同一个形状）；
* :mod:`app.api.models`——Pydantic 响应模型（``/docs`` 里能看到字段含义）；
* ``routes_*``——四个资源：设备、样本、统计、告警。

路由里**不写统计口径**，一律调用 :mod:`app.analytics.statistics`；
也不写 SQL，一律调用 :class:`~app.storage.repository.Repository`。
"""

from fastapi import FastAPI

from app.api.errors import (
    ApiError,
    InvalidParameterError,
    NotFoundError,
    error_payload,
    register_error_handlers,
)
from app.api.routes_alarms import router as alarms_router
from app.api.routes_devices import router as devices_router
from app.api.routes_samples import router as samples_router
from app.api.routes_stats import router as stats_router

__all__ = [
    "ApiError",
    "InvalidParameterError",
    "NotFoundError",
    "error_payload",
    "register_api",
]


def register_api(app: FastAPI) -> None:
    """把错误处理器与全部路由挂到应用上。"""
    register_error_handlers(app)
    for router in (devices_router, samples_router, stats_router, alarms_router):
        app.include_router(router)
