-- ===========================================================================
-- 设备数据采集与测试报告平台 —— 表结构
--
-- 约定（README「数据字典」有逐字段说明）：
--   * 时间统一用 Unix 毫秒整数（INTEGER），不用字符串，避免时区与排序问题；
--   * 查询区间一律「左闭右开」[from_ts, to_ts)，边界只在一个地方定义；
--   * quality 是质量码，0 表示正常；非 0 的样本**必须入库**，
--     否则统计时分不清「设备坏了」和「根本没采」。
-- ===========================================================================

CREATE TABLE IF NOT EXISTS devices (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT    NOT NULL UNIQUE,
    transport        TEXT    NOT NULL,               -- tcp / rtu / csv
    address          TEXT    NOT NULL,               -- host:port 或 COM11
    unit_id          INTEGER NOT NULL,
    poll_interval_ms INTEGER NOT NULL,
    enabled          INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS samples (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL,
    ts        INTEGER NOT NULL,                      -- unix milliseconds
    metric    TEXT    NOT NULL,                      -- response_ms / register_0 ...
    value     REAL    NOT NULL,
    quality   INTEGER NOT NULL DEFAULT 0             -- 0 ok, 1 timeout, 2 crc, 3 exception
);

-- 主查询路径：按设备 + 时间范围（分页、统计、图表都走它）。
CREATE INDEX IF NOT EXISTS idx_samples_device_ts ON samples(device_id, ts);

-- 次查询路径：按设备 + 指标 + 时间（画单指标曲线时避免回表过滤）。
CREATE INDEX IF NOT EXISTS idx_samples_device_metric_ts ON samples(device_id, metric, ts);

CREATE TABLE IF NOT EXISTS alarms (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id  INTEGER NOT NULL,
    rule       TEXT    NOT NULL,
    started_at INTEGER NOT NULL,
    ended_at   INTEGER,                              -- NULL = 仍在告警中
    detail     TEXT
);

-- 告警引擎每轮都要查「这条规则还有没有活动告警」，必须走索引。
CREATE INDEX IF NOT EXISTS idx_alarms_device_rule ON alarms(device_id, rule);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  INTEGER NOT NULL,
    finished_at INTEGER,
    env         TEXT,
    summary     TEXT
);

-- 迁移版本记录，让 schema 演进有据可查（见 storage/migrations.py）。
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at INTEGER NOT NULL
);
