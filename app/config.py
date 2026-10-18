"""配置加载与校验。

设计约定（见 README「关键设计决定」）：

* 所有相对路径一律相对**项目根**解析，配置里不出现绝对路径，换机器即可运行；
* 配置出错抛 :class:`ConfigError`，消息里带文件名与出错字段，
  不把 ``JSONDecodeError`` / ``KeyError`` 的裸栈抛给使用者；
* 每个字段都有默认值，字段缺失用默认值，但**类型错误必须报出来**——
  静默把 ``"500"`` 当成 500 用，会在采集阶段才炸，那时已经很难定位。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: 项目根目录（``<root>/app/config.py`` 的上两级）。配置里的相对路径都以此为基准。
PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 默认配置文件位置。
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "app.json"

VALID_TRANSPORTS = ("tcp", "rtu", "csv")
VALID_LOG_LEVELS = ("debug", "info", "warning", "error", "critical")


class ConfigError(Exception):
    """配置文件缺失、格式错误或字段非法。

    属性 :attr:`path` 与 :attr:`field` 让调用方能给出「哪个文件、哪个字段」的提示。
    """

    def __init__(self, message: str, *, path: Path | str | None = None, field: str | None = None):
        self.path = path
        self.field = field
        parts: list[str] = []
        if path is not None:
            parts.append(f"配置文件 {path}")
        if field is not None:
            parts.append(f"字段 {field!r}")
        prefix = "：".join(parts)
        super().__init__(f"{prefix}：{message}" if prefix else message)


@dataclass(frozen=True)
class PollConfig:
    """采集的默认节拍与超时。"""

    default_interval_ms: int = 1000
    timeout_ms: int = 800
    retry: int = 2


@dataclass(frozen=True)
class DeviceConfig:
    """一台采集设备。"""

    name: str
    transport: str
    address: str
    unit_id: int
    poll_interval_ms: int
    metrics: tuple[str, ...]
    enabled: bool = True

    @property
    def is_csv(self) -> bool:
        return self.transport == "csv"


@dataclass(frozen=True)
class AlarmRuleConfig:
    """一条告警规则（表达式字符串，由 alarm_engine 解析）。"""

    name: str
    device: str
    rule: str
    window_sec: int


@dataclass(frozen=True)
class AppConfig:
    """整份应用配置。"""

    database: str
    log_level: str
    log_dir: str
    poll: PollConfig
    devices: tuple[DeviceConfig, ...]
    alarms: tuple[AlarmRuleConfig, ...]
    source_path: Path
    base_dir: Path = field(default=PROJECT_ROOT)

    # ---- 路径解析 -------------------------------------------------------

    def resolve(self, relative: str | os.PathLike[str]) -> Path:
        """把配置里的相对路径解析成绝对路径；已是绝对路径则原样返回。"""
        p = Path(relative)
        return p if p.is_absolute() else (self.base_dir / p)

    @property
    def database_path(self) -> Path:
        return self.resolve(self.database)

    @property
    def log_dir_path(self) -> Path:
        return self.resolve(self.log_dir)

    # ---- 查询辅助 -------------------------------------------------------

    def device(self, name: str) -> DeviceConfig:
        for d in self.devices:
            if d.name == name:
                return d
        raise ConfigError(f"未找到名为 {name!r} 的设备", path=self.source_path, field="devices")

    def enabled_devices(self) -> tuple[DeviceConfig, ...]:
        return tuple(d for d in self.devices if d.enabled)

    def alarms_for(self, device_name: str) -> tuple[AlarmRuleConfig, ...]:
        return tuple(a for a in self.alarms if a.device == device_name)


# ---------------------------------------------------------------------------
# 解析辅助：每个 helper 都负责把「类型不对」变成一句人话
# ---------------------------------------------------------------------------


def _require_mapping(value: Any, path: Path, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(
            f"应为对象，实际是 {type(value).__name__}", path=path, field=field_name
        )
    return value


def _get_str(raw: dict[str, Any], key: str, default: str | None, path: Path, where: str) -> str:
    value = raw.get(key, default)
    if value is None:
        raise ConfigError(f"缺少必填字段 {where}.{key}", path=path, field=f"{where}.{key}")
    if not isinstance(value, str):
        raise ConfigError(
            f"应为字符串，实际是 {type(value).__name__}", path=path, field=f"{where}.{key}"
        )
    return value


def _get_int(raw: dict[str, Any], key: str, default: int, path: Path, where: str) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(
            f"应为整数，实际是 {type(value).__name__}", path=path, field=f"{where}.{key}"
        )
    return value


def _get_bool(raw: dict[str, Any], key: str, default: bool, path: Path, where: str) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(
            f"应为布尔值，实际是 {type(value).__name__}", path=path, field=f"{where}.{key}"
        )
    return value


def _parse_device(raw: Any, index: int, path: Path) -> DeviceConfig:
    where = f"devices[{index}]"
    item = _require_mapping(raw, path, where)

    name = _get_str(item, "name", None, path, where)
    transport = _get_str(item, "transport", None, path, where)
    address = _get_str(item, "address", None, path, where)

    if transport not in VALID_TRANSPORTS:
        raise ConfigError(
            f"取值非法，应为 {'/'.join(VALID_TRANSPORTS)} 之一，实际是 {transport!r}",
            path=path,
            field=f"{where}.transport",
        )

    metrics_raw = item.get("metrics", ["response_ms"])
    if not isinstance(metrics_raw, list) or not all(isinstance(m, str) for m in metrics_raw):
        raise ConfigError(
            "应为字符串数组", path=path, field=f"{where}.metrics"
        )
    if not metrics_raw:
        raise ConfigError("至少要有一个指标", path=path, field=f"{where}.metrics")

    return DeviceConfig(
        name=name,
        transport=transport,
        address=address,
        unit_id=_get_int(item, "unitId", 1, path, where),
        poll_interval_ms=_get_int(item, "pollIntervalMs", 1000, path, where),
        metrics=tuple(metrics_raw),
        enabled=_get_bool(item, "enabled", True, path, where),
    )


def _parse_alarm(raw: Any, index: int, path: Path) -> AlarmRuleConfig:
    where = f"alarms[{index}]"
    item = _require_mapping(raw, path, where)
    return AlarmRuleConfig(
        name=_get_str(item, "name", None, path, where),
        device=_get_str(item, "device", None, path, where),
        rule=_get_str(item, "rule", None, path, where),
        window_sec=_get_int(item, "windowSec", 60, path, where),
    )


def parse_config(data: Any, *, path: Path = DEFAULT_CONFIG_PATH,
                 base_dir: Path = PROJECT_ROOT) -> AppConfig:
    """把已解析的 JSON 对象转成 :class:`AppConfig`（纯函数，便于单测）。"""
    root = _require_mapping(data, path, "<root>")

    log_level = _get_str(root, "logLevel", "info", path, "<root>").lower()
    if log_level not in VALID_LOG_LEVELS:
        raise ConfigError(
            f"取值非法，应为 {'/'.join(VALID_LOG_LEVELS)} 之一，实际是 {log_level!r}",
            path=path,
            field="logLevel",
        )

    poll_raw = _require_mapping(root.get("poll", {}), path, "poll")
    poll = PollConfig(
        default_interval_ms=_get_int(poll_raw, "defaultIntervalMs", 1000, path, "poll"),
        timeout_ms=_get_int(poll_raw, "timeoutMs", 800, path, "poll"),
        retry=_get_int(poll_raw, "retry", 2, path, "poll"),
    )
    if poll.timeout_ms <= 0:
        raise ConfigError("必须为正数", path=path, field="poll.timeoutMs")
    if poll.retry < 0:
        raise ConfigError("不能为负数", path=path, field="poll.retry")

    devices_raw = root.get("devices", [])
    if not isinstance(devices_raw, list):
        raise ConfigError("应为数组", path=path, field="devices")
    devices = tuple(_parse_device(d, i, path) for i, d in enumerate(devices_raw))

    seen: set[str] = set()
    for d in devices:
        if d.name in seen:
            raise ConfigError(
                f"设备名重复：{d.name!r}", path=path, field="devices[].name"
            )
        seen.add(d.name)

    alarms_raw = root.get("alarms", [])
    if not isinstance(alarms_raw, list):
        raise ConfigError("应为数组", path=path, field="alarms")
    alarms = tuple(_parse_alarm(a, i, path) for i, a in enumerate(alarms_raw))

    for a in alarms:
        if a.device not in seen:
            raise ConfigError(
                f"规则 {a.name!r} 指向未定义的设备 {a.device!r}",
                path=path,
                field="alarms[].device",
            )

    return AppConfig(
        database=_get_str(root, "database", "data/records.db", path, "<root>"),
        log_level=log_level,
        log_dir=_get_str(root, "logDir", "logs", path, "<root>"),
        poll=poll,
        devices=devices,
        alarms=alarms,
        source_path=Path(path),
        base_dir=Path(base_dir),
    )


def load_config(path: str | os.PathLike[str] | None = None,
                *, base_dir: Path | None = None) -> AppConfig:
    """读取并校验配置文件。

    出错时抛 :class:`ConfigError`，消息形如
    ``配置文件 C:/.../app.json：字段 'poll.timeoutMs'：必须为正数``。
    """
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    root = Path(base_dir) if base_dir is not None else PROJECT_ROOT

    if not config_path.is_absolute():
        config_path = root / config_path

    if not config_path.exists():
        raise ConfigError("文件不存在", path=config_path)

    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"读取失败（{exc.strerror or exc}）", path=config_path) from exc

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"JSON 语法错误（第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}）", path=config_path
        ) from exc

    return parse_config(data, path=config_path, base_dir=root)
