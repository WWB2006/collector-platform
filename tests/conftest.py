"""共享测试夹具。

两条铁律（规格「测试夹具与用例示例」）：

* **不依赖真实设备与网络**——采集器用假实现替换；
* **不共用同一个数据库**——每个用例一个临时目录建库，互不干扰。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig, PROJECT_ROOT, load_config, parse_config
from app.main import create_app
from app.storage import Database, Repository

#: 仓库根目录，供需要读真实配置/模板的用例使用。
REPO_ROOT = PROJECT_ROOT


@pytest.fixture
def db(tmp_path: Path):
    """临时文件数据库（不是 :memory:，这样 WAL/索引行为与生产一致）。"""
    database = Database(tmp_path / "test.db")
    database.init_schema()
    yield database
    database.close_all()


@pytest.fixture
def repo(db: Database) -> Repository:
    """规格示例中的 repo 夹具：临时库 + 仓储层。"""
    return Repository(db)


@pytest.fixture
def api(tmp_path: Path, app_config: AppConfig):
    """带真实临时库的测试客户端。

    返回一个对象而不是元组，用例里 ``api.client`` / ``api.repo`` 读起来更清楚。
    数据库是**文件**而不是 ``:memory:``，这样索引、WAL、并发行为与生产一致。
    """
    from types import SimpleNamespace

    database = Database(tmp_path / "api.db")
    database.init_schema()
    repository = Repository(database)
    repository.sync_devices(app_config.devices)

    app = create_app(app_config, repo=repository, db=database)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, repo=repository, db=database, config=app_config)
    database.close_all()


@pytest.fixture
def project_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def app_config() -> AppConfig:
    """仓库里真实的 config/app.json 解析结果。"""
    return load_config()


@pytest.fixture
def config_factory(tmp_path: Path):
    """在临时目录里造一份配置，避免用例之间互相看到对方的文件。"""

    def build(payload: dict, *, name: str = "app.json") -> AppConfig:
        path = tmp_path / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return load_config(path)

    return build


@pytest.fixture
def client(app_config: AppConfig):
    """带 lifespan 的测试客户端（启动即加载配置与日志）。"""
    with TestClient(create_app(app_config)) as c:
        yield c


@pytest.fixture
def client_factory():
    """按需构造客户端的工厂，用于测试配置错误等启动期场景。"""

    def build(config: AppConfig | None = None, *, config_path: Path | None = None):
        return TestClient(create_app(config, config_path=config_path))

    return build


@pytest.fixture
def sample_payload() -> dict:
    """一份最小可用的配置载荷，用例按需覆盖其中字段。"""
    return {
        "database": "data/test.db",
        "logLevel": "info",
        "poll": {"defaultIntervalMs": 1000, "timeoutMs": 800, "retry": 2},
        "devices": [
            {
                "name": "plc-01",
                "transport": "tcp",
                "address": "127.0.0.1:5020",
                "unitId": 1,
                "pollIntervalMs": 500,
                "metrics": ["response_ms", "register_0"],
            }
        ],
        "alarms": [
            {"name": "timeout-streak", "device": "plc-01", "rule": "timeout>=3", "windowSec": 60}
        ],
    }


@pytest.fixture
def parse_only():
    """直接解析字典（不落盘），用于纯配置逻辑用例。"""
    return parse_config
