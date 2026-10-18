"""质量码：样本的「这次采集到底成没成」。

单独成一个模块（而不是塞进 storage 或 collector）的原因：
**采集层和存储层都要用它**，而依赖方向规定 collector 与 storage 互不依赖。
把它放在两边都能依赖的叶子上，避免为了一个常量引入反向依赖。

口径（README「数据字典」有同样一份）：

===== ========= ==================================================
码值  名称      含义
===== ========= ==================================================
0     ok        正常采到
1     timeout   超时（含连不上、无响应）
2     crc       帧校验失败（RTU 场景）
3     exception 从站返回异常码，或协议层错误
===== ========= ==================================================

**非 0 的样本必须入库**。如果采不到就不写记录，统计时分不清
「设备坏了」和「这段时间根本没采集」，成功率会虚高到 100%。
"""

from __future__ import annotations

QUALITY_OK = 0
QUALITY_TIMEOUT = 1
QUALITY_CRC = 2
QUALITY_EXCEPTION = 3

#: 码值 -> 英文名（接口返回、报告展示用）。
QUALITY_LABELS: dict[int, str] = {
    QUALITY_OK: "ok",
    QUALITY_TIMEOUT: "timeout",
    QUALITY_CRC: "crc",
    QUALITY_EXCEPTION: "exception",
}

#: 英文名 -> 码值（解析告警规则表达式 ``timeout>=3`` 用）。
QUALITY_NAMES: dict[str, int] = {label: code for code, label in QUALITY_LABELS.items()}

#: 全部合法码值。
QUALITY_CODES = tuple(sorted(QUALITY_LABELS))


def label(quality: int) -> str:
    """码值转名称；未知码值返回 ``unknown(N)`` 而不是抛异常（脏数据也要能显示）。"""
    return QUALITY_LABELS.get(int(quality), f"unknown({quality})")


def is_ok(quality: int) -> bool:
    return int(quality) == QUALITY_OK


def code_of(name: str) -> int | None:
    """名称转码值；不认识返回 ``None``（由调用方决定报错还是忽略）。"""
    return QUALITY_NAMES.get(name.strip().lower())
