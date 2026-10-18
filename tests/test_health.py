"""/health 与启动期错误处理的用例。

这里守的是一条运维底线：**配置坏了，服务可以起不来，但必须说清为什么**。
所以断言的重点是「503 + 错误消息里含具体字段名」，而不是「抛异常」。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.config import AppConfig, ConfigError
from app.main import create_app


def test_health_ok(client) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["devices"] == 2
    assert body["enabledDevices"] == 2
    assert body["alarmRules"] == 2
    assert isinstance(body["uptimeSeconds"], (int, float))
    assert body["database"].replace("\\", "/").endswith("data/records.db")


def test_health_works_without_lifespan(client_factory, app_config: AppConfig) -> None:
    """不进入 with 块（等价于某些嵌入式 ASGI 用法）时，首次请求也要能兜底加载配置。"""
    client = client_factory(app_config)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_returns_503_when_config_invalid(tmp_path: Path, client_factory) -> None:
    bad = tmp_path / "app.json"
    bad.write_text('{"logLevel": "loud"}', encoding="utf-8")
    with client_factory(config_path=bad) as client:
        response = client.get("/health")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "error"
    assert "logLevel" in body["error"]


def test_health_returns_503_when_config_missing(tmp_path: Path, client_factory) -> None:
    with client_factory(config_path=tmp_path / "ghost.json") as client:
        response = client.get("/health")
    assert response.status_code == 503
    assert "文件不存在" in response.json()["error"]


def test_app_stays_alive_after_config_error(tmp_path: Path, client_factory) -> None:
    """配置错误不能让进程死掉：错误之后 /docs 之类的路由仍要可访问。"""
    bad = tmp_path / "app.json"
    bad.write_text("{ not json", encoding="utf-8")
    with client_factory(config_path=bad) as client:
        assert client.get("/health").status_code == 503
        assert client.get("/openapi.json").status_code == 200


def test_openapi_documents_health(client) -> None:
    schema = client.get("/openapi.json").json()
    assert "/health" in schema["paths"]


def test_docs_page_available(client) -> None:
    assert client.get("/docs").status_code == 200


def test_create_app_does_not_raise_on_bad_config(tmp_path: Path) -> None:
    """构造应用本身不能抛——否则 uvicorn 连错误页面都来不及返回。"""
    bad = tmp_path / "app.json"
    bad.write_text(json.dumps({"devices": [{"name": "a"}]}), encoding="utf-8")
    app = create_app(config_path=bad)
    assert app is not None
