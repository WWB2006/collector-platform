"""接口层的统一错误结构。

规格要求「错误统一返回结构化 JSON，便于自动化测试断言」，
所以**所有**失败响应都是同一个形状::

    {"error": {"code": "invalid_range", "message": "...", "field": "range"}}

包括 FastAPI 自己抛的参数校验错误（默认是 ``{"detail": [...]}``）——
不统一的话，前端与测试要写两套解析逻辑。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.logging_setup import get_logger

logger = get_logger("api")


class ApiError(Exception):
    """业务层可以直接抛这个，交给统一处理器转成 JSON。"""

    status_code = 400
    code = "bad_request"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        field: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.field = field
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code


class NotFoundError(ApiError):
    status_code = 404
    code = "not_found"


class InvalidParameterError(ApiError):
    status_code = 400
    code = "invalid_parameter"


def error_payload(
    code: str, message: str, *, field: str | None = None, details: Any = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {"code": code, "message": message}
    if field:
        payload["field"] = field
    if details is not None:
        payload["details"] = details
    return {"error": payload}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _handle_api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(exc.code, exc.message, field=exc.field),
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """把 FastAPI 的 ``{"detail": [...]}`` 也转成统一结构（状态码仍是 422）。"""
        problems = [
            {
                "field": ".".join(str(p) for p in error.get("loc", ()) if p != "query"),
                "message": error.get("msg", ""),
            }
            for error in exc.errors()
        ]
        first = problems[0] if problems else {"field": None, "message": "参数校验失败"}
        return JSONResponse(
            status_code=422,
            content=error_payload(
                "validation_error",
                first["message"] or "参数校验失败",
                field=first.get("field"),
                details=problems,
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_error(
        _request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(f"http_{exc.status_code}", str(exc.detail)),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(_request: Request, exc: Exception) -> JSONResponse:
        """未预料到的错误也要返回结构化 JSON——否则前端拿到的是 HTML 错误页。"""
        logger.exception("接口发生未处理异常：%s", exc)
        return JSONResponse(
            status_code=500,
            content=error_payload("internal_error", "服务内部错误，详情见服务端日志"),
        )
