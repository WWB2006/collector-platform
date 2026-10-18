"""告警引擎：把配置里的规则表达式变成「开一条告警 / 关一条告警」。

两个必须做对的地方（规格排错表里各对应一行）：

* **去重**——同一规则未恢复前只保留一条活动告警。
  少了这一步，每个采集周期都会插一条，一次故障刷出上百条，也就是「告警风暴」。
* **恢复**——条件不再成立时必须写 ``ended_at``。
  少了这一步，告警永远显示在活动列表里，没人知道它其实已经好了。

规则表达式刻意做成**纯字符串解析 + 纯函数判定**，不拼进 SQL：
规则能不能单测，决定了你敢不敢改它。SQL 字符串里的条件只能靠造数据来试。

支持两种规则::

    timeout>=3              连续 3 次超时（看窗口内**末尾**连续同码样本）
    p95_response_ms>500     窗口内 P95 响应时间超过 500 ms

左值可选：``ok`` / ``timeout`` / ``crc`` / ``exception``（连续次数），
或 ``p50_`` / ``p95_`` / ``p99_`` / ``mean_`` / ``max_`` + 指标名，
以及 ``success_rate``（窗口内成功率）。
比较符：``>=`` ``<=`` ``>`` ``<`` ``==`` ``!=``。
"""

from __future__ import annotations

import operator
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

from app.analytics.statistics import (
    latency_values,
    mean,
    percentile,
    success_rate,
)
from app.config import AlarmRuleConfig
from app.logging_setup import get_logger
from app.quality import QUALITY_NAMES, label as quality_label
from app.storage import Repository
from app.storage.migrations import now_ms

logger = get_logger("alarm")

#: 规则表达式：``<左值><比较符><数值>``。
_RULE_PATTERN = re.compile(
    r"^\s*(?P<subject>[A-Za-z_][A-Za-z0-9_]*)\s*"
    r"(?P<operator>>=|<=|==|!=|>|<)\s*"
    r"(?P<threshold>-?\d+(?:\.\d+)?)\s*$"
)

#: 比较符实现。
OPERATORS: dict[str, Callable[[float, float], bool]] = {
    ">=": operator.ge,
    ">": operator.gt,
    "<=": operator.le,
    "<": operator.lt,
    "==": operator.eq,
    "!=": operator.ne,
}

#: 统计量前缀 -> 取值函数（作用于已过滤的延迟值列表）。
_AGGREGATES: dict[str, Callable[[Sequence[float]], float]] = {
    "p50": lambda values: percentile(values, 50),
    "p95": lambda values: percentile(values, 95),
    "p99": lambda values: percentile(values, 99),
    "mean": mean,
    "max": lambda values: float(max(values)) if values else 0.0,
    "min": lambda values: float(min(values)) if values else 0.0,
}

#: 连续次数类规则可以用的左值。
_STREAK_SUBJECTS = tuple(QUALITY_NAMES)

#: 特殊左值。
_SPECIAL_SUBJECTS = ("success_rate", "total")


class RuleError(ValueError):
    """规则表达式无法解析或不合法。"""


@dataclass(frozen=True)
class Rule:
    """一条解析好的规则。"""

    raw: str
    subject: str
    operator: str
    threshold: float

    @property
    def kind(self) -> str:
        """``streak``：看连续同码次数；``threshold``：看窗口内统计量。"""
        return "streak" if self.subject in _STREAK_SUBJECTS else "threshold"

    @property
    def quality_code(self) -> int | None:
        return QUALITY_NAMES.get(self.subject)

    @property
    def metric(self) -> str | None:
        """阈值类规则作用的指标名，例如 ``p95_response_ms`` -> ``response_ms``。"""
        if "_" not in self.subject:
            return None
        prefix, _, metric = self.subject.partition("_")
        return metric if prefix in _AGGREGATES and metric else None

    @property
    def aggregate(self) -> str | None:
        if "_" not in self.subject:
            return None
        prefix = self.subject.split("_", 1)[0]
        return prefix if prefix in _AGGREGATES else None

    @classmethod
    def parse(cls, text: str) -> Rule:
        match = _RULE_PATTERN.match(text or "")
        if not match:
            raise RuleError(
                f"规则表达式无法解析：{text!r}；"
                "应形如 'timeout>=3' 或 'p95_response_ms>500'"
            )
        subject = match.group("subject")
        operator_text = match.group("operator")
        threshold = float(match.group("threshold"))

        if subject not in _STREAK_SUBJECTS and subject not in _SPECIAL_SUBJECTS:
            prefix, _, metric = subject.partition("_")
            if prefix not in _AGGREGATES or not metric:
                raise RuleError(
                    f"规则左值无法识别：{subject!r}；可用："
                    f"{', '.join(_STREAK_SUBJECTS)}、{', '.join(_SPECIAL_SUBJECTS)}、"
                    f"{'/'.join(_AGGREGATES)}_<指标名>"
                )
        return cls(raw=text, subject=subject, operator=operator_text, threshold=threshold)

    def compare(self, value: float) -> bool:
        return OPERATORS[self.operator](value, self.threshold)

    def describe(self) -> str:
        return f"{self.subject} {self.operator} {self.threshold:g}"


def trailing_streak(samples: Sequence[tuple[int, float]], code: int) -> int:
    """从末尾往前数，连续等于 ``code`` 的样本条数。

    「连续失败」必须看末尾，而不是「窗口内失败总数」——
    后者会在故障恢复后仍然大于阈值，告警永远结束不了。
    """
    count = 0
    for quality, _ in reversed(samples):
        if int(quality) == code:
            count += 1
        else:
            break
    return count


def compute_subject(subject: str, samples: Sequence[tuple[int, float]]) -> float:
    """算出规则左值在给定样本上的取值。"""
    if subject == "success_rate":
        return success_rate(samples)
    if subject == "total":
        return float(len(samples))
    if subject in QUALITY_NAMES:
        return float(trailing_streak(samples, QUALITY_NAMES[subject]))

    prefix, _, _metric = subject.partition("_")
    aggregate = _AGGREGATES.get(prefix)
    if aggregate is None:
        raise RuleError(f"无法识别的左值：{subject!r}")
    return aggregate(latency_values(samples))


def evaluate_rule(rule: Rule, samples: Sequence[tuple[int, float]]) -> tuple[bool, float, str]:
    """判定规则是否触发。

    :returns: ``(是否触发, 实际取值, 人话说明)``。
        返回实际取值是为了让告警详情能写清「连续 4 次超时（阈值 >=3）」，
        而不是只说一句「触发了」。
    """
    value = compute_subject(rule.subject, samples)
    triggered = rule.compare(value)

    if rule.kind == "streak":
        actual = f"连续 {int(value)} 次 {rule.subject}"
    elif rule.subject == "success_rate":
        actual = f"成功率 {value:.2%}"
    elif rule.subject == "total":
        actual = f"样本 {int(value)} 条"
    else:
        actual = f"{rule.subject} = {value:.3f}"

    detail = f"{actual}，规则 {rule.describe()}，窗口内 {len(samples)} 条样本"
    return triggered, value, detail


@dataclass(frozen=True)
class AlarmTransition:
    """一次状态变化，便于测试与日志（没有变化时不会产生对象）。"""

    device: str
    rule: str
    action: str  # opened / closed
    alarm_id: int | None
    value: float
    detail: str
    ts: int


class AlarmEngine:
    """按规则评估所有设备，维护 ``alarms`` 表里的活动告警。"""

    def __init__(
        self,
        repo: Repository,
        rules: Iterable[AlarmRuleConfig],
        *,
        clock: Callable[[], int] = now_ms,
    ) -> None:
        self.repo = repo
        self.clock = clock
        self._rules: list[tuple[AlarmRuleConfig, Rule]] = []
        for config in rules:
            try:
                parsed = Rule.parse(config.rule)
            except RuleError as exc:
                raise RuleError(f"告警规则 {config.name!r}（设备 {config.device}）：{exc}") from exc
            self._rules.append((config, parsed))

    @property
    def rules(self) -> list[tuple[AlarmRuleConfig, Rule]]:
        return list(self._rules)

    def evaluate_device(self, device_name: str, *, now: int | None = None) -> list[AlarmTransition]:
        """评估某台设备的所有规则，返回本次发生的状态变化。"""
        timestamp = int(now if now is not None else self.clock())
        device_id = self.repo.device_id(device_name)
        transitions: list[AlarmTransition] = []

        for config, rule in self._rules:
            if config.device != device_name:
                continue
            transitions.extend(self._evaluate_one(device_id, config, rule, timestamp))
        return transitions

    def evaluate_all(self, *, now: int | None = None) -> list[AlarmTransition]:
        """评估所有有规则的设备。"""
        timestamp = int(now if now is not None else self.clock())
        transitions: list[AlarmTransition] = []
        for device_name in sorted({config.device for config, _ in self._rules}):
            try:
                transitions.extend(self.evaluate_device(device_name, now=timestamp))
            except KeyError:
                logger.warning("告警规则指向的设备 %s 不在库里，已跳过", device_name)
        return transitions

    # ---- 单条规则 -------------------------------------------------------

    def _evaluate_one(
        self, device_id: int, config: AlarmRuleConfig, rule: Rule, timestamp: int
    ) -> list[AlarmTransition]:
        samples = self._window_samples(device_id, config, rule, timestamp)
        triggered, value, detail = evaluate_rule(rule, samples)

        active = self.repo.find_active_alarm(device_id, config.name)

        if triggered and active is None:
            alarm_id = self.repo.open_alarm(device_id, config.name, timestamp, detail)
            logger.warning("设备 %s 触发告警 %s：%s", config.device, config.name, detail)
            return [
                AlarmTransition(config.device, config.name, "opened", alarm_id, value, detail,
                                timestamp)
            ]

        if not triggered and active is not None:
            # 保留**触发时**的说明，把恢复信息追加在后面。
            # 直接覆盖的话，报告里就只剩「已恢复：连续 0 次 timeout」——
            # 读者看不到这条告警当初是为什么报的（「连续 3 次超时」），
            # 而恢复时刻其实已经由 ended_at 表达了。
            original = (active["detail"] or "").strip()
            note = f"已恢复：{detail}"
            self.repo.close_alarm(
                int(active["id"]), timestamp, f"{original}；{note}" if original else note
            )
            logger.info("设备 %s 告警 %s 已恢复", config.device, config.name)
            return [
                AlarmTransition(config.device, config.name, "closed", int(active["id"]), value,
                                detail, timestamp)
            ]

        # 已经活动且仍然触发 -> 什么都不做（这就是去重）；
        # 未触发且无活动告警 -> 什么都不做。
        return []

    def _window_samples(
        self, device_id: int, config: AlarmRuleConfig, rule: Rule, timestamp: int
    ) -> list[tuple[int, float]]:
        """取规则窗口内的样本（左闭右开，右端 +1 以包含当前时刻）。"""
        from_ts = timestamp - max(0, config.window_sec) * 1000
        metric = rule.metric
        if metric is None:
            # 连续次数类规则看的是采集结果，用延迟指标代表「一次采集」，
            # 否则 register_0 也带质量码，会把连续次数数成两倍。
            from app.storage.repository import LATENCY_METRIC

            metric = LATENCY_METRIC
        return self.repo.quality_values(
            device_id, from_ts=from_ts, to_ts=timestamp + 1, metric=metric
        )

    # ---- 汇总 -----------------------------------------------------------

    def active_alarms(self) -> list[dict[str, Any]]:
        """当前活动告警（接口与仪表盘用）。"""
        return [
            {
                "id": int(row["id"]),
                "device": row["device"],
                "rule": row["rule"],
                "startedAt": int(row["started_at"]),
                "detail": row["detail"],
            }
            for row in self.repo.active_alarms()
        ]

    def describe_rules(self) -> list[dict[str, Any]]:
        return [
            {
                "name": config.name,
                "device": config.device,
                "rule": config.rule,
                "windowSec": config.window_sec,
                "kind": rule.kind,
                "subject": rule.subject,
                "operator": rule.operator,
                "threshold": rule.threshold,
            }
            for config, rule in self._rules
        ]


def describe_quality(code: int) -> str:
    return quality_label(code)
