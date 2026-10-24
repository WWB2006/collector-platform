"""仪表盘页面与静态资源的装配。

这一层刻意做得很薄：模板只负责排版，前端只负责轮询与展示，
所有数字都来自 :mod:`app.api` 的接口。理由见 ``templates/dashboard.html.j2``
顶部的说明——**统计口径只能有一份实现**。

一个细节：页面把配置（设备清单、轮询间隔、接口地址）以 JSON 形式注入模板，
而不是在 JS 里写死。这样换一台设备、改一次轮询周期，前端不用动。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from app import __version__
from app.logging_setup import get_logger

logger = get_logger("web")

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

#: 前端轮询间隔。**故意不是 1 秒**：每秒打一轮接口，后端压力陡增而人眼收益为零。
DEFAULT_POLL_INTERVAL_MS = 5000

#: 判定「设备当前没有数据」的宽限倍数：超过 ``轮询周期 × 该倍数`` 即视为无数据。
#: 取 5 倍是为了容忍调度抖动与偶发慢查询，又不至于把停机误判成正常。
STALE_AFTER_POLLS = 5

#: 宽限下限，避免轮询周期极短（如 50 ms）时判据过于灵敏。
MIN_STALE_AFTER_MS = 3000

#: ECharts CDN。规格要求用 CDN 引入；加载失败时页面会降级成「只有表格」而不是白屏。
ECHARTS_URL = "https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js"

#: 可选的时间窗口。
#:
#: 默认取 **10 分钟**而不是接口默认的 1 小时，原因是分桶口径：
#: 曲线把窗口切成固定数量的桶（``buckets``），1 小时切 60 桶就是每桶 60 秒——
#: 一段只有几十秒的实时数据会整个落进同一个桶，图上只剩一个点，等于没有曲线。
#: 10 分钟切 60 桶是每桶 10 秒，实时视图才画得出形状。
#: 要看长周期的整体趋势，那是**报告**的职责（快照、可离线、可当附件）。
WINDOWS: tuple[str, ...] = ("10m", "1h", "6h", "24h")
DEFAULT_WINDOW = "10m"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _stale_after_ms(poll_interval_ms: int) -> int:
    return max(MIN_STALE_AFTER_MS, int(poll_interval_ms) * STALE_AFTER_POLLS)


def build_page_config(config: Any, *, poll_interval_ms: int, default_window: str,
                      buckets: int) -> dict[str, Any]:
    """给前端的配置。设备清单取自**当前配置**，不是写死的常量。"""
    devices = []
    for device in config.enabled_devices():
        devices.append(
            {
                "name": device.name,
                "transport": device.transport,
                "pollIntervalMs": device.poll_interval_ms,
                "staleAfterMs": _stale_after_ms(device.poll_interval_ms),
            }
        )
    return {
        "pollIntervalMs": poll_interval_ms,
        "defaultWindow": default_window,
        "windows": list(WINDOWS),
        "buckets": buckets,
        "devices": devices,
        "endpoints": {
            "health": "/health",
            "devices": "/api/devices",
            "summary": "/api/stats/summary",
            "series": "/api/stats/series",
            "alarms": "/api/alarms",
        },
    }


def _page_context(request: Request) -> dict[str, Any]:
    """构造模板上下文。配置没就绪时也要能渲染出一个能看懂的页面。"""
    from app.main import ensure_runtime  # 延迟导入：避免 main <-> web 循环

    ensure_runtime(request.app)
    state = request.app.state
    config = getattr(state, "config", None)
    error = getattr(state, "config_error", None) or getattr(state, "storage_error", None)

    page_config = build_page_config(
        config,
        poll_interval_ms=DEFAULT_POLL_INTERVAL_MS,
        default_window=DEFAULT_WINDOW,
        buckets=60,
    ) if config is not None else {
        "pollIntervalMs": DEFAULT_POLL_INTERVAL_MS,
        "defaultWindow": DEFAULT_WINDOW,
        "windows": list(WINDOWS),
        "buckets": 60,
        "devices": [],
        "endpoints": {},
    }

    return {
        "title": "设备数据采集平台 · 实时仪表盘",
        "version": __version__,
        "poll_interval_ms": DEFAULT_POLL_INTERVAL_MS,
        "default_window": DEFAULT_WINDOW,
        "stale_after_polls": STALE_AFTER_POLLS,
        "static_url": "/static/",
        "echarts_url": ECHARTS_URL,
        # 注入到 <script type="application/json"> 里。转义 '<' 是为了防止
        # 设备名里出现 "</script>" 把脚本块提前闭合。
        "config_json": json.dumps(page_config, ensure_ascii=False).replace("<", "\\u003c"),
        "runtime_error": error,
    }


def register_web(
    app: FastAPI,
    *,
    prefix: str = "",
) -> None:
    """挂载仪表盘页面与 ``/static`` 静态资源。"""
    if STATIC_DIR.exists():
        app.mount(f"{prefix}/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    else:  # pragma: no cover - 打包漏了静态目录时的兜底
        logger.warning("静态资源目录不存在：%s", STATIC_DIR)

    @app.get(f"{prefix}/", response_class=HTMLResponse, include_in_schema=False)
    @app.get(f"{prefix}/dashboard", response_class=HTMLResponse, include_in_schema=False)
    def dashboard(request: Request) -> HTMLResponse:
        context = _page_context(request)
        return templates.TemplateResponse(request, "dashboard.html.j2", context)


__all__ = [
    "DEFAULT_POLL_INTERVAL_MS",
    "DEFAULT_WINDOW",
    "ECHARTS_URL",
    "MIN_STALE_AFTER_MS",
    "STALE_AFTER_POLLS",
    "STATIC_DIR",
    "TEMPLATES_DIR",
    "WINDOWS",
    "build_page_config",
    "register_web",
]
