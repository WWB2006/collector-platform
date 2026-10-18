"""配置加载与校验用例。

重点不在「能读文件」，而在**读错时说的是人话**：
每条错误分支都断言消息里出现了具体文件名与字段名，
否则运维拿到「KeyError: 'devices'」等于没拿到信息。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import (
    DEFAULT_CONFIG_PATH,
    PROJECT_ROOT,
    AppConfig,
    ConfigError,
    load_config,
    parse_config,
)


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------


def test_real_config_loads(app_config: AppConfig) -> None:
    assert app_config.source_path == PROJECT_ROOT / "config" / "app.json"
    assert app_config.log_level == "info"
    assert len(app_config.devices) == 2
    assert len(app_config.alarms) == 2


def test_relative_paths_resolve_against_project_root(app_config: AppConfig) -> None:
    db = app_config.database_path
    assert db.is_absolute()
    assert db.as_posix().endswith("data/records.db")
    assert db.parent == PROJECT_ROOT / "data"


def test_device_fields(app_config: AppConfig) -> None:
    plc = app_config.device("plc-01")
    assert plc.transport == "tcp"
    assert plc.address == "127.0.0.1:5020"
    assert plc.unit_id == 1
    assert plc.poll_interval_ms == 500
    assert plc.metrics == ("response_ms", "register_0")
    assert plc.enabled is True
    assert plc.is_csv is False


def test_csv_device_detected(app_config: AppConfig) -> None:
    meter = app_config.device("meter-02")
    assert meter.is_csv is True
    assert app_config.resolve(meter.address).is_absolute()


def test_absolute_path_is_kept(tmp_path: Path) -> None:
    """绝对路径必须原样保留——配置层不能自作主张往前面拼项目根。"""
    absolute = (tmp_path / "x.db").resolve()
    cfg = parse_config({"database": str(absolute)})
    assert cfg.resolve(cfg.database) == absolute


def test_alarm_rules_grouped_by_device(app_config: AppConfig) -> None:
    assert len(app_config.alarms_for("plc-01")) == 2
    assert app_config.alarms_for("meter-02") == ()


def test_enabled_devices_filters(app_config: AppConfig) -> None:
    assert len(app_config.enabled_devices()) == 2
    cfg = parse_config(
        {
            "devices": [
                {"name": "a", "transport": "tcp", "address": "x", "enabled": True},
                {"name": "b", "transport": "tcp", "address": "y", "enabled": False},
            ]
        }
    )
    assert [d.name for d in cfg.enabled_devices()] == ["a"]


def test_unknown_device_raises(app_config: AppConfig) -> None:
    with pytest.raises(ConfigError, match="未找到名为"):
        app_config.device("ghost")


# ---------------------------------------------------------------------------
# 默认值：最小配置也要能跑起来
# ---------------------------------------------------------------------------


def test_minimal_config_uses_defaults() -> None:
    cfg = parse_config({"devices": []})
    assert cfg.database == "data/records.db"
    assert cfg.log_level == "info"
    assert cfg.log_dir == "logs"
    assert (cfg.poll.default_interval_ms, cfg.poll.timeout_ms, cfg.poll.retry) == (1000, 800, 2)
    assert cfg.devices == ()
    assert cfg.alarms == ()


def test_device_level_defaults() -> None:
    cfg = parse_config(
        {"devices": [{"name": "a", "transport": "tcp", "address": "127.0.0.1:1"}]}
    )
    dev = cfg.devices[0]
    assert dev.unit_id == 1
    assert dev.poll_interval_ms == 1000
    assert dev.metrics == ("response_ms",)
    assert dev.enabled is True


def test_default_config_path_constant() -> None:
    assert DEFAULT_CONFIG_PATH == PROJECT_ROOT / "config" / "app.json"


# ---------------------------------------------------------------------------
# 错误路径：消息必须带上「哪个文件、哪个字段」
# ---------------------------------------------------------------------------


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path / "nope.json")
    message = str(exc.value)
    assert "nope.json" in message
    assert "文件不存在" in message


def test_malformed_json_reports_line_and_column(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{\n  "devices": [}\n}', encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(bad)
    message = str(exc.value)
    assert "JSON 语法错误" in message
    assert "第 2 行" in message


@pytest.mark.parametrize(
    ("payload", "expect"),
    [
        ({"logLevel": "loud"}, "logLevel"),
        ({"logLevel": 3}, "logLevel"),
        (
            {"devices": [{"name": "a", "transport": "usb", "address": "x"}]},
            "transport",
        ),
        ({"poll": {"timeoutMs": "800"}}, "poll.timeoutMs"),
        ({"poll": {"timeoutMs": 0}}, "poll.timeoutMs"),
        ({"poll": {"retry": -1}}, "poll.retry"),
        ({"devices": [{"transport": "tcp", "address": "x"}]}, "devices[0].name"),
        (
            {"devices": [{"name": "a", "transport": "tcp", "address": "x", "metrics": []}]},
            "至少要有一个指标",
        ),
        (
            {"devices": [{"name": "a", "transport": "tcp", "address": "x", "metrics": "m"}]},
            "devices[0].metrics",
        ),
        ({"devices": "nope"}, "devices"),
        ({"alarms": "nope"}, "alarms"),
    ],
)
def test_invalid_field_message_names_the_field(
    tmp_path: Path, payload: dict, expect: str
) -> None:
    path = tmp_path / "app.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert expect in str(exc.value)


def test_duplicate_device_name_rejected() -> None:
    with pytest.raises(ConfigError, match="设备名重复"):
        parse_config(
            {
                "devices": [
                    {"name": "a", "transport": "tcp", "address": "x"},
                    {"name": "a", "transport": "tcp", "address": "y"},
                ]
            }
        )


def test_alarm_pointing_at_unknown_device_rejected() -> None:
    with pytest.raises(ConfigError, match="未定义"):
        parse_config(
            {
                "devices": [],
                "alarms": [{"name": "r", "device": "ghost", "rule": "timeout>=3"}],
            }
        )


def test_root_must_be_object(tmp_path: Path) -> None:
    path = tmp_path / "app.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ConfigError, match="应为对象"):
        load_config(path)


def test_error_carries_path_and_field_attributes(tmp_path: Path) -> None:
    path = tmp_path / "app.json"
    path.write_text('{"logLevel": "loud"}', encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert exc.value.path == path
    assert exc.value.field == "logLevel"


def test_boolean_is_not_accepted_as_int() -> None:
    """Python 里 True == 1，配置层必须显式挡掉，否则 pollIntervalMs: true 会被静默当成 1。"""
    with pytest.raises(ConfigError, match="应为整数"):
        parse_config(
            {"devices": [{"name": "a", "transport": "tcp", "address": "x",
                          "pollIntervalMs": True}]}
        )
