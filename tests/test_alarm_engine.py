"""告警引擎用例。

两条最值钱的回归：

* :func:`test_no_alarm_storm`——同一故障持续 20 轮，告警表里只能有 1 条。
  没有去重的话，一次断线能刷出上百条告警，值班的人直接就不看了。
* :func:`test_alarm_closes_when_recovered`——故障消失后必须写 ``ended_at``。
  没有恢复判定的话，活动告警列表永远挂着一条已经好了的告警。

另外规则解析本身是纯函数，所有表达式都单独测过——
规则能不能单测，决定了你敢不敢改它。
"""

from __future__ import annotations

import pytest

from app.analytics.alarm_engine import (
    AlarmEngine,
    Rule,
    RuleError,
    compute_subject,
    evaluate_rule,
    trailing_streak,
)
from app.config import AlarmRuleConfig
from app.quality import QUALITY_OK, QUALITY_TIMEOUT
from app.storage import Repository
from app.storage.repository import LATENCY_METRIC

BASE = 1_700_000_000_000
WINDOW = 60
STEP = 500


# ---------------------------------------------------------------------------
# 规则解析
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kind", "subject", "op", "threshold"),
    [
        ("timeout>=3", "streak", "timeout", ">=", 3.0),
        ("timeout>2", "streak", "timeout", ">", 2.0),
        ("crc==1", "streak", "crc", "==", 1.0),
        ("exception>=5", "streak", "exception", ">=", 5.0),
        ("ok<=0", "streak", "ok", "<=", 0.0),
        ("p95_response_ms>500", "threshold", "p95_response_ms", ">", 500.0),
        ("p50_response_ms>=10.5", "threshold", "p50_response_ms", ">=", 10.5),
        ("p99_response_ms>1000", "threshold", "p99_response_ms", ">", 1000.0),
        ("mean_response_ms>20", "threshold", "mean_response_ms", ">", 20.0),
        ("max_response_ms>800", "threshold", "max_response_ms", ">", 800.0),
        ("success_rate<0.9", "threshold", "success_rate", "<", 0.9),
        ("total<5", "threshold", "total", "<", 5.0),
    ],
)
def test_rule_parsing(text: str, kind: str, subject: str, op: str, threshold: float) -> None:
    rule = Rule.parse(text)
    assert rule.kind == kind
    assert rule.subject == subject
    assert rule.operator == op
    assert rule.threshold == threshold


def test_rule_parsing_tolerates_whitespace() -> None:
    rule = Rule.parse("  timeout  >=  3  ")
    assert rule.subject == "timeout"
    assert rule.threshold == 3.0


@pytest.mark.parametrize(
    "bad",
    ["", "timeout", "timeout>=", ">=3", "unknown>3", "p95_>3", "timeout>abc", "timeout~3"],
)
def test_bad_rules_rejected(bad: str) -> None:
    with pytest.raises(RuleError):
        Rule.parse(bad)


def test_rule_metric_and_aggregate() -> None:
    rule = Rule.parse("p95_response_ms>500")
    assert rule.metric == "response_ms"
    assert rule.aggregate == "p95"
    assert Rule.parse("timeout>=3").metric is None


def test_rule_compare() -> None:
    assert Rule.parse("timeout>=3").compare(3) is True
    assert Rule.parse("timeout>=3").compare(2) is False
    assert Rule.parse("p95_response_ms>500").compare(501) is True


def test_rule_describe() -> None:
    assert Rule.parse("timeout>=3").describe() == "timeout >= 3"


# ---------------------------------------------------------------------------
# 纯函数判定
# ---------------------------------------------------------------------------


def test_trailing_streak() -> None:
    samples = [(0, 1.0), (1, 0.0), (1, 0.0), (1, 0.0)]
    assert trailing_streak(samples, QUALITY_TIMEOUT) == 3
    assert trailing_streak(samples, QUALITY_OK) == 0


def test_trailing_streak_resets_on_success() -> None:
    """中间成功过一次，连续计数必须归零——否则告警永远结束不了。"""
    samples = [(1, 0.0), (1, 0.0), (0, 10.0), (1, 0.0)]
    assert trailing_streak(samples, QUALITY_TIMEOUT) == 1


def test_trailing_streak_empty() -> None:
    assert trailing_streak([], QUALITY_TIMEOUT) == 0


def test_compute_subject_streak() -> None:
    samples = [(1, 0.0)] * 4 + [(0, 10.0)]
    assert compute_subject("timeout", samples) == 0.0  # 末尾是成功
    assert compute_subject("ok", samples) == 1.0


def test_compute_subject_success_rate() -> None:
    samples = [(0, 10.0), (0, 12.0), (1, 0.0), (0, 11.0)]
    assert compute_subject("success_rate", samples) == pytest.approx(0.75)


def test_compute_subject_percentiles() -> None:
    samples = [(0, float(v)) for v in (10, 20, 30, 40, 50)]
    assert compute_subject("p50_response_ms", samples) == 30.0
    assert compute_subject("p95_response_ms", samples) == 50.0
    assert compute_subject("mean_response_ms", samples) == 30.0
    assert compute_subject("max_response_ms", samples) == 50.0


def test_compute_subject_ignores_failed_samples() -> None:
    """超时的 800ms 不能参与分位——否则 p95 会被超时次数主导。"""
    samples = [(0, float(v)) for v in (10, 20, 30, 40, 50)] + [(1, 800.0)] * 10
    assert compute_subject("p95_response_ms", samples) == 50.0
    assert compute_subject("max_response_ms", samples) == 50.0


def test_compute_subject_total() -> None:
    assert compute_subject("total", [(0, 1.0)] * 7) == 7.0


def test_compute_subject_unknown_raises() -> None:
    with pytest.raises(RuleError):
        compute_subject("nonsense", [])


def test_evaluate_rule_reports_actual_value() -> None:
    rule = Rule.parse("timeout>=3")
    triggered, value, detail = evaluate_rule(rule, [(1, 0.0)] * 4)
    assert triggered is True
    assert value == 4.0
    assert "连续 4 次 timeout" in detail
    assert "timeout >= 3" in detail


def test_evaluate_rule_threshold_detail() -> None:
    rule = Rule.parse("p95_response_ms>500")
    triggered, value, detail = evaluate_rule(rule, [(0, 600.0)] * 3)
    assert triggered is True
    assert value == 600.0
    assert "p95_response_ms" in detail


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------


@pytest.fixture
def plc(repo: Repository) -> tuple[Repository, int]:
    return repo, repo.upsert_device("plc-01", "tcp", "x", 1, 500)


def insert_samples(repo: Repository, device_id: int, samples, *, start: int = BASE,
                   step: int = STEP, metric: str = LATENCY_METRIC) -> int:
    rows = [
        (device_id, start + i * step, metric, float(value), int(quality))
        for i, (quality, value) in enumerate(samples)
    ]
    return repo.insert_samples(rows)


def streak_engine(repo: Repository, *, rule: str = "timeout>=3",
                  window: int = WINDOW) -> AlarmEngine:
    return AlarmEngine(repo, [AlarmRuleConfig("timeout-streak", "plc-01", rule, window)])


def test_opens_alarm_when_streak_reached(plc) -> None:
    repo, device_id = plc
    insert_samples(repo, device_id, [(1, 800.0)] * 3)
    engine = streak_engine(repo)

    transitions = engine.evaluate_device("plc-01", now=BASE + 5000)
    assert len(transitions) == 1
    assert transitions[0].action == "opened"
    assert transitions[0].value == 3.0
    assert len(repo.active_alarms()) == 1


def test_does_not_open_below_threshold(plc) -> None:
    repo, device_id = plc
    insert_samples(repo, device_id, [(1, 800.0)] * 2)
    engine = streak_engine(repo)
    assert engine.evaluate_device("plc-01", now=BASE + 5000) == []
    assert repo.active_alarms() == []


def test_no_alarm_storm(plc) -> None:
    """同一故障持续 20 轮评估，告警表里只能有一条活动告警。"""
    repo, device_id = plc
    insert_samples(repo, device_id, [(1, 800.0)] * 20)
    engine = streak_engine(repo)

    opened = 0
    for round_no in range(20):
        transitions = engine.evaluate_device("plc-01", now=BASE + 5000 + round_no * 100)
        opened += sum(1 for t in transitions if t.action == "opened")

    assert opened == 1, "重复触发产生了多条告警（告警风暴）"
    assert len(repo.active_alarms()) == 1
    assert repo.count_alarms() == 1


def test_alarm_closes_when_recovered(plc) -> None:
    repo, device_id = plc
    insert_samples(repo, device_id, [(1, 800.0)] * 3)
    engine = streak_engine(repo)
    engine.evaluate_device("plc-01", now=BASE + 5000)
    assert len(repo.active_alarms()) == 1

    # 故障恢复：末尾出现成功样本
    insert_samples(repo, device_id, [(0, 12.0)], start=BASE + 10_000)
    transitions = engine.evaluate_device("plc-01", now=BASE + 15_000)

    assert len(transitions) == 1
    assert transitions[0].action == "closed"
    assert repo.active_alarms() == []

    row = repo.db.query_one("SELECT * FROM alarms ORDER BY id DESC LIMIT 1")
    assert row["ended_at"] == BASE + 15_000
    assert "已恢复" in row["detail"]


def test_recovery_keeps_trigger_detail(plc) -> None:
    """恢复之后，报告里仍要看得出这条告警当初是**为什么**报的。

    如果 ``close_alarm`` 直接用恢复说明覆盖 ``detail``，报告上就只剩
    「已恢复：连续 0 次 timeout」——触发原因（连续 3 次超时）丢失。
    而恢复时刻本来就已经由 ``ended_at`` 表达了，不需要占用 detail。
    """
    repo, device_id = plc
    insert_samples(repo, device_id, [(1, 800.0)] * 3)
    engine = streak_engine(repo)
    engine.evaluate_device("plc-01", now=BASE + 5_000)

    insert_samples(repo, device_id, [(0, 12.0)], start=BASE + 10_000)
    engine.evaluate_device("plc-01", now=BASE + 15_000)

    row = repo.db.query_one("SELECT * FROM alarms ORDER BY id DESC LIMIT 1")
    assert row["ended_at"] == BASE + 15_000
    assert "连续 3 次 timeout" in row["detail"], "触发原因不能被恢复说明覆盖"
    assert "已恢复" in row["detail"]


def test_alarm_reopens_after_new_failure(plc) -> None:
    repo, device_id = plc
    insert_samples(repo, device_id, [(1, 800.0)] * 3)
    engine = streak_engine(repo)
    engine.evaluate_device("plc-01", now=BASE + 5000)

    insert_samples(repo, device_id, [(0, 12.0)], start=BASE + 10_000)
    engine.evaluate_device("plc-01", now=BASE + 15_000)

    insert_samples(repo, device_id, [(1, 800.0)] * 3, start=BASE + 20_000)
    transitions = engine.evaluate_device("plc-01", now=BASE + 25_000)

    assert [t.action for t in transitions] == ["opened"]
    assert len(repo.active_alarms()) == 1
    assert repo.count_alarms() == 2


def test_recovered_alarm_does_not_close_twice(plc) -> None:
    repo, device_id = plc
    insert_samples(repo, device_id, [(1, 800.0)] * 3)
    engine = streak_engine(repo)
    engine.evaluate_device("plc-01", now=BASE + 5000)
    insert_samples(repo, device_id, [(0, 12.0)], start=BASE + 10_000)
    engine.evaluate_device("plc-01", now=BASE + 15_000)

    assert engine.evaluate_device("plc-01", now=BASE + 16_000) == []


def test_samples_outside_window_ignored(plc) -> None:
    repo, device_id = plc
    # 三分钟前的连续超时，窗口只有 60 秒，不该触发
    insert_samples(repo, device_id, [(1, 800.0)] * 5, start=BASE - 300_000)
    engine = streak_engine(repo)
    assert engine.evaluate_device("plc-01", now=BASE) == []


def test_threshold_rule_on_p95(plc) -> None:
    repo, device_id = plc
    insert_samples(repo, device_id, [(0, 600.0)] * 10)
    engine = AlarmEngine(
        repo, [AlarmRuleConfig("p95-latency", "plc-01", "p95_response_ms>500", 300)]
    )
    transitions = engine.evaluate_device("plc-01", now=BASE + 10_000)
    assert [t.action for t in transitions] == ["opened"]
    assert transitions[0].value == 600.0


def test_threshold_rule_not_triggered_when_fast(plc) -> None:
    repo, device_id = plc
    insert_samples(repo, device_id, [(0, 12.0)] * 10)
    engine = AlarmEngine(
        repo, [AlarmRuleConfig("p95-latency", "plc-01", "p95_response_ms>500", 300)]
    )
    assert engine.evaluate_device("plc-01", now=BASE + 10_000) == []


def test_threshold_rule_closes_when_latency_drops(plc) -> None:
    repo, device_id = plc
    insert_samples(repo, device_id, [(0, 600.0)] * 10)
    engine = AlarmEngine(
        repo, [AlarmRuleConfig("p95-latency", "plc-01", "p95_response_ms>500", 300)]
    )
    engine.evaluate_device("plc-01", now=BASE + 10_000)

    # 窗口滑过去，只剩快速样本
    insert_samples(repo, device_id, [(0, 10.0)] * 10, start=BASE + 400_000)
    transitions = engine.evaluate_device("plc-01", now=BASE + 410_000)
    assert [t.action for t in transitions] == ["closed"]


def test_success_rate_rule(plc) -> None:
    repo, device_id = plc
    insert_samples(repo, device_id, [(1, 0.0)] * 9 + [(0, 10.0)])
    engine = AlarmEngine(
        repo, [AlarmRuleConfig("low-success", "plc-01", "success_rate<0.5", 60)]
    )
    transitions = engine.evaluate_device("plc-01", now=BASE + 10_000)
    assert [t.action for t in transitions] == ["opened"]


def test_streak_uses_latency_metric_only(plc) -> None:
    """一次采集会写 response_ms + register_0 两条；连续次数不能数成两倍。"""
    repo, device_id = plc
    insert_samples(repo, device_id, [(1, 800.0)] * 3)
    insert_samples(repo, device_id, [(1, 0.0)] * 3, metric="register_0")
    engine = streak_engine(repo)
    transitions = engine.evaluate_device("plc-01", now=BASE + 5000)
    assert transitions[0].value == 3.0, "连续次数被非延迟指标放大了"


def test_evaluate_all_covers_every_device(repo: Repository) -> None:
    repo.upsert_device("plc-01", "tcp", "x")
    other = repo.upsert_device("meter-02", "csv", "y")
    insert_samples(repo, repo.device_id("plc-01"), [(1, 800.0)] * 3)
    insert_samples(repo, other, [(1, 800.0)] * 3)

    engine = AlarmEngine(
        repo,
        [
            AlarmRuleConfig("a", "plc-01", "timeout>=3", 60),
            AlarmRuleConfig("b", "meter-02", "timeout>=3", 60),
        ],
    )
    transitions = engine.evaluate_all(now=BASE + 5000)
    assert sorted(t.device for t in transitions) == ["meter-02", "plc-01"]
    assert len(repo.active_alarms()) == 2


def test_evaluate_all_skips_missing_device(repo: Repository) -> None:
    repo.upsert_device("plc-01", "tcp", "x")
    engine = AlarmEngine(repo, [AlarmRuleConfig("a", "ghost", "timeout>=3", 60)])
    assert engine.evaluate_all(now=BASE) == []


def test_rules_for_other_devices_are_ignored(plc) -> None:
    repo, device_id = plc
    insert_samples(repo, device_id, [(1, 800.0)] * 3)
    engine = AlarmEngine(repo, [AlarmRuleConfig("other", "meter-02", "timeout>=3", 60)])
    assert engine.evaluate_device("plc-01", now=BASE + 5000) == []


def test_bad_rule_in_config_raises_at_construction(repo: Repository) -> None:
    """规则写错要在构造引擎时就报出来，而不是等到评估时静默不触发。"""
    with pytest.raises(RuleError, match="timeout-streak"):
        AlarmEngine(repo, [AlarmRuleConfig("timeout-streak", "plc-01", "timeout>>>3", 60)])


def test_active_alarms_payload(plc) -> None:
    repo, device_id = plc
    insert_samples(repo, device_id, [(1, 800.0)] * 3)
    engine = streak_engine(repo)
    engine.evaluate_device("plc-01", now=BASE + 5000)

    payload = engine.active_alarms()
    assert len(payload) == 1
    assert payload[0]["device"] == "plc-01"
    assert payload[0]["rule"] == "timeout-streak"
    assert payload[0]["startedAt"] == BASE + 5000
    assert "连续 3 次 timeout" in payload[0]["detail"]


def test_describe_rules(plc) -> None:
    repo, _ = plc
    engine = AlarmEngine(
        repo,
        [
            AlarmRuleConfig("timeout-streak", "plc-01", "timeout>=3", 60),
            AlarmRuleConfig("p95-latency", "plc-01", "p95_response_ms>500", 300),
        ],
    )
    described = engine.describe_rules()
    assert len(described) == 2
    assert described[0]["kind"] == "streak"
    assert described[1]["kind"] == "threshold"
    assert described[1]["threshold"] == 500.0


def test_engine_uses_real_config_rules(repo: Repository, app_config) -> None:
    """配置里的两条规则要能直接构造引擎（防止配置与解析器脱节）。"""
    repo.upsert_device("plc-01", "tcp", "x")
    engine = AlarmEngine(repo, app_config.alarms)
    assert {r["name"] for r in engine.describe_rules()} == {"timeout-streak", "p95-latency"}
