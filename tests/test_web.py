"""仪表盘页面与静态资源的用例。

页面本身没什么可断言的（逻辑都在浏览器里），但**「页面能不能打开」必须钉住**：
模板语法错误、静态目录没挂上、注入前端的配置 JSON 拼坏——这几种问题在
``pytest`` 里没有任何征兆，只有人打开浏览器才会发现，而那时通常已经交付了。

所以这里断言三件事：

* ``/`` 与 ``/dashboard`` 返回 200 且是 HTML；
* 注入的配置 JSON **能被解析**，且设备清单与配置一致（前端靠它填下拉框）；
* ``/static/app.js`` 真的取得到（挂载路径写错时页面会静默失效）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import parse_config
from app.main import create_app
from app.web import (
    DEFAULT_WINDOW,
    MIN_STALE_AFTER_MS,
    STALE_AFTER_POLLS,
    build_page_config,
)

CONFIG_PATTERN = re.compile(
    r'<script type="application/json" id="dashboard-config">(.*?)</script>', re.S
)


def _config_json(html: str) -> dict:
    match = CONFIG_PATTERN.search(html)
    assert match, "页面里应当有注入的配置 JSON 块"
    return json.loads(match.group(1))


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/dashboard"])
def test_dashboard_serves_html(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "设备数据采集平台" in response.text


def test_dashboard_injects_parseable_config(client: TestClient, app_config) -> None:
    html = client.get("/").text
    payload = _config_json(html)
    assert payload["pollIntervalMs"] > 0
    assert payload["defaultWindow"] == DEFAULT_WINDOW
    assert payload["defaultWindow"] in payload["windows"]
    assert payload["devices"], "配置里有设备时，注入的设备清单不能是空的"
    # 与配置逐一对齐：前端下拉框靠它填，漏一台设备就会少一条可看的曲线。
    assert [d["name"] for d in payload["devices"]] == [
        d.name for d in app_config.enabled_devices()
    ]
    assert payload["endpoints"]["summary"] == "/api/stats/summary"


def test_injected_windows_are_accepted_by_the_api(client: TestClient) -> None:
    """窗口下拉框里的每个取值都必须真被接口接受。

    写错一个字母（``10min`` 而不是 ``10m``）不会让页面报错，
    只会让每次请求都返回 400、页面永远停在「接口不可达」——
    这是最容易被漏到交付之后的一类问题。
    """
    payload = _config_json(client.get("/").text)
    assert payload["windows"], "至少要给一个可选窗口"
    for window in payload["windows"]:
        for endpoint in ("/api/stats/summary", "/api/stats/series", "/api/alarms"):
            response = client.get(f"{endpoint}?range={window}")
            assert response.status_code == 200, f"{endpoint}?range={window} -> {response.status_code}"


def test_default_window_is_not_the_api_default() -> None:
    """仪表盘默认窗口故意与接口默认（1h）不同，这里把理由钉住。

    接口默认 1 小时是为了「查询必须有时间范围」这条底线；
    仪表盘要的是**实时曲线**，1 小时切 60 桶 = 每桶 60 秒，
    几十秒的实时数据会整个落进一个桶，图上只剩一个点。
    """
    from app.api.time_range import DEFAULT_RANGE

    assert DEFAULT_WINDOW == "10m"
    assert DEFAULT_WINDOW != DEFAULT_RANGE


def test_dashboard_uses_shared_endpoints(client: TestClient) -> None:
    """前端调的接口必须真的存在——路径写错时页面会一直显示「接口不可达」。"""
    payload = _config_json(client.get("/").text)
    for url in payload["endpoints"].values():
        response = client.get(url)
        assert response.status_code == 200, f"{url} 应当可用，实际 {response.status_code}"


def test_static_assets_are_mounted(client: TestClient) -> None:
    response = client.get("/static/app.js")
    assert response.status_code == 200
    assert "dashboard-config" in response.text
    assert "setInterval" in response.text


def test_echarts_is_referenced_from_cdn(client: TestClient) -> None:
    """规格要求 ECharts 走 CDN；断网时前端会降级成只显示表格，不白屏。"""
    html = client.get("/").text
    assert "echarts" in html
    assert "cdn" in html


def test_dashboard_renders_without_config(tmp_path: Path) -> None:
    """配置坏了也要能打开页面——否则用户只看到一个 500，不知道该去改哪个文件。"""
    app = create_app(config_path=tmp_path / "missing.json")
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200
    payload = _config_json(response.text)
    assert payload["devices"] == []


def test_config_json_escapes_script_close() -> None:
    """设备名里出现 ``</script>`` 时不能把脚本块提前闭合。

    这不是假想：设备名来自配置文件，而配置文件是人手写的。
    """
    config = parse_config(
        {
            "database": "data/x.db",
            "poll": {"timeoutMs": 100},
            "devices": [
                {
                    "name": "</script><script>alert(1)</script>",
                    "transport": "csv",
                    "address": "data/replay/x.csv",
                    "unitId": 1,
                    "pollIntervalMs": 500,
                    "metrics": ["response_ms"],
                }
            ],
        }
    )
    payload = build_page_config(config, poll_interval_ms=5000, default_window="10m", buckets=60)
    rendered = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    assert "</script>" not in rendered
    assert json.loads(rendered)["devices"][0]["name"].startswith("</script>")


# ---------------------------------------------------------------------------
# 纯函数：无数据判定阈值
# ---------------------------------------------------------------------------


def test_stale_after_scales_with_poll_interval() -> None:
    payload = build_page_config(
        parse_config(
            {
                "database": "data/x.db",
                "poll": {"timeoutMs": 100},
                "devices": [
                    {
                        "name": "fast",
                        "transport": "csv",
                        "address": "data/replay/x.csv",
                        "unitId": 1,
                        "pollIntervalMs": 500,
                        "metrics": ["response_ms"],
                    },
                    {
                        "name": "slow",
                        "transport": "csv",
                        "address": "data/replay/y.csv",
                        "unitId": 2,
                        "pollIntervalMs": 10_000,
                        "metrics": ["response_ms"],
                    },
                ],
            }
        ),
        poll_interval_ms=5000,
        default_window="10m",
        buckets=60,
    )
    stale = {d["name"]: d["staleAfterMs"] for d in payload["devices"]}
    assert stale["fast"] == MIN_STALE_AFTER_MS  # 500 × 5 = 2500 < 下限 3000
    assert stale["slow"] == 10_000 * STALE_AFTER_POLLS


def test_disabled_devices_are_not_injected() -> None:
    """停用的设备不该出现在下拉框里，否则用户会盯着一条永远没数据的曲线。"""
    config = parse_config(
        {
            "database": "data/x.db",
            "poll": {"timeoutMs": 100},
            "devices": [
                {
                    "name": "on",
                    "transport": "csv",
                    "address": "data/replay/x.csv",
                    "unitId": 1,
                    "pollIntervalMs": 500,
                    "metrics": ["response_ms"],
                },
                {
                    "name": "off",
                    "transport": "csv",
                    "address": "data/replay/y.csv",
                    "unitId": 2,
                    "pollIntervalMs": 500,
                    "metrics": ["response_ms"],
                    "enabled": False,
                },
            ],
        }
    )
    payload = build_page_config(config, poll_interval_ms=5000, default_window="10m", buckets=60)
    assert [d["name"] for d in payload["devices"]] == ["on"]
