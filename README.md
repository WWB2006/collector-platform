# collector-platform · 设备数据采集与测试报告平台

把现场的 PLC / 仪表数据采回来，落进本地库，算出**可解释**的指标，
出问题时能报出来，最后产出一份**可以当附件发出去**的 HTML 报告。

```
Modbus TCP / 串口 / CSV 回放  ->  SQLite  ->  统计 · 告警  ->  接口 · 仪表盘 · HTML 报告
```

这个项目的重点不在功能多，而在**口径清楚、异常可查、报告能交付**：
每一个统计数字都能回答「你这是什么口径」，每一次采集失败都能在库里找到痕迹。

---

## 快速开始（复制粘贴就能跑）

不需要 PLC、不需要串口、不需要网络——回放采集器会把一段录好的现场数据重演一遍。

```bash
git clone https://github.com/WWB2006/collector-platform.git
cd collector-platform

python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # Windows
# source .venv/bin/activate && pip install -r requirements.txt   # Linux / macOS

# 1) 造一份回放数据（10 分钟，含周期性的连续超时）
python scripts/seed_demo_data.py --minutes 10

# 2) 采集 60 秒（约等于回放数据里的 10 分钟）
python scripts/run_collector.py --config config/app.test.json --seconds 60

# 3) 出一份报告
python scripts/generate_report.py --config config/app.test.json --range 1h

# 4) 打开仪表盘
uvicorn app.main:app --port 8000     # 然后浏览器打开 http://127.0.0.1:8000/
```

第 2 步的输出会是这样：

```
运行时长      : 60.0 s
采集轮次      : 177
样本条数      : 296（失败轮次 9）
已落库        : 296 条
告警          : 触发 3 次、恢复 3 次
  - meter-02     轮次 60     样本 60     成功率 98.3%
  - plc-01       轮次 117    样本 234    成功率 92.7%
```

> 想连真实的 Modbus 从站，用 `config/app.json`（里面 `plc-01` 指向 `127.0.0.1:5020`）。
> 仓库配套的从站模拟器在 [WWB2006-Modbus-Gateway](https://github.com/WWB2006/Modbus-Gateway)，
> 直接跑它的 `slave_simulator` 即可。

---

## 它解决什么问题

现场数据的难点从来不是「读一个寄存器」，而是下面这几件事：

| 问题 | 这个项目怎么处理 |
| --- | --- |
| 设备偶尔超时，成功率算出来是 100% | **失败样本也入库**（质量码 1/2/3），成功率分母包含超时 |
| 超时值 800 ms 混进响应时间，P95 被拉爆 | 延迟分位**只统计成功样本**；失败样本单独计数 |
| 每采一轮就插一条告警，一次故障刷出上百条 | 告警**去重**：同一规则未恢复前只保留一条 |
| 故障恢复了告警还挂在列表里 | 告警**自动恢复**，写 `ended_at` |
| 报告发出去，别人打开发现数字变了 | 报告是**生成时刻的快照**，渲染阶段不碰数据库 |
| 报告断网打不开 | 图表是服务端渲染的**内联 SVG**，零外部资源 |
| 短促故障被批次边界吃掉，告警不触发 | 在**质量码变化处**评估告警，而不是只看批次末尾 |

---

## 架构

```
                    ┌─────────────────────────────────────────────┐
   现场设备          │  采集层  collector/                          │
  ┌──────────┐      │   ├─ ModbusCollector    (TCP / 一次事务读块)  │
  │ PLC      │◄─────┼───┤                                          │
  │ 仪表     │      │   └─ CsvReplayCollector (离线 / 端到端 / CI) │
  └──────────┘      └───────────────┬─────────────────────────────┘
                                    │  CollectResult(设备,指标,值,时间戳,质量码)
                                    ▼
                     ┌──────────────────────────────┐
                     │  调度器 scheduler.py          │
                     │  N 个采集线程 ─队列─► 1 个写线程 │   ← 攒批 + 定时双触发
                     └──────────────┬───────────────┘
                                    ▼
                     ┌──────────────────────────────┐
                     │  存储层 storage/  (SQLite/WAL) │
                     └──────────────┬───────────────┘
                                    ▼
                     ┌──────────────────────────────┐
                     │  分析层 analytics/            │
                     │   statistics.py  alarm_engine │
                     └───────┬──────────────┬───────┘
                             ▼              ▼
                    ┌────────────┐  ┌──────────────┐
                    │ 接口 api/  │  │ 报告 report/ │
                    │  + 仪表盘  │  │  + 内联 SVG  │
                    └────────────┘  └──────────────┘
```

**依赖方向是单向的**：`config` ← `collector` / `storage` ← `analytics` ← `api` / `report` ← `web`。
`collector` 与 `storage` 互不依赖，共用的常量抽到叶子模块 `app/quality.py`。

关键抽象是 `CollectResult`——`(device, metric, value, ts_ms, quality)` 五元组。
统计、告警、报告都不关心数据从哪来，所以同一份代码既跑真实 PLC 也跑回放，
测试里还能塞一个假采集器。

---

## 目录结构

```
collector-platform/
├── app/
│   ├── main.py              FastAPI 装配（配置/日志/库/告警/路由）
│   ├── config.py            配置加载与校验（报错必须是人话）
│   ├── logging_setup.py     日志格式、级别、滚动
│   ├── quality.py           质量码定义（叶子模块，谁都能用）
│   ├── scheduler.py         生产者—消费者调度、优雅退出、告警触发
│   ├── collector/           base.py · modbus_collector.py · csv_collector.py
│   ├── storage/             db.py · repository.py · migrations.py
│   ├── analytics/           statistics.py · alarm_engine.py
│   ├── api/                 errors · time_range · deps · models · routes_*
│   ├── report/              builder.py · charts.py · templates/report.html.j2
│   └── web/                 templates/dashboard.html.j2 · static/app.js
├── config/                  app.json · app.test.json · report.json
├── sql/schema.sql           建表语句
├── scripts/                 init_db · run_collector · generate_report · seed_demo_data · benchmark
├── deploy/                  systemd 单元 ×2 · Dockerfile
├── tests/                   14 个测试文件
└── .github/workflows/ci.yml
```

---

## 统计口径（每个数字的定义）

这一节是项目里最重要的部分。**同一个词在不同地方口径不同，是数据类项目最常见的事故来源**，
所以这里把每个数字的定义写死，代码里也只有一个实现。

| 指标 | 口径 | 为什么 |
| --- | --- | --- |
| **成功率** | `质量码 == 0` 的条数 ÷ **全部**条数 | 超时必须留在分母里，否则设备越坏成功率越好看 |
| **样本条数** | 一次采集算 **1 条**（以 `response_ms` 为准） | 一次采集会同时写 `response_ms` 和 `register_N`，不区分就会翻倍 |
| **响应时间 P50/P95/P99** | **只统计成功样本**，**最近秩法**（非插值） | 超时的 800 ms 不是测出来的响应时间；插值出的响应时间在系统里不存在 |
| **平均响应时间** | 只统计成功样本 | 同上 |
| **质量码分布** | 按 `response_ms` 计数，不重复计数 | 同上 |
| **时间区间** | **左闭右开** `[from, to)` | 相邻两段拼接时不会重复或漏掉边界样本 |
| **告警「连续 N 次」** | 窗口内**末尾**连续同码样本数 | 用「窗口内失败总数」的话，故障恢复后告警永远结束不了 |

### 质量码

| 码 | 含义 | 什么时候产生 |
| --- | --- | --- |
| 0 | `ok` | 采集成功 |
| 1 | `timeout` | 连接超时 / 连接被拒 / 重试耗尽 |
| 2 | `crc` | 报文校验失败（预留给串口链路） |
| 3 | `exception` | 设备返回 Modbus 异常码 |

**非 0 的样本也必须入库**，并且**失败时产出与成功时同样条数的记录**（值置 0）。
少写一条，就无法区分「设备坏了」和「这个周期根本没采」——前者要告警，后者是配置问题。

---

## 数据字典

### `devices` 设备

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `id` | INTEGER | 主键 |
| `name` | TEXT | 设备名，唯一，配置与接口都用它引用设备 |
| `transport` | TEXT | `tcp` / `rtu` / `csv` |
| `address` | TEXT | `host:port`、`COM11` 或回放文件路径 |
| `unit_id` | INTEGER | Modbus 从站号 |
| `poll_interval_ms` | INTEGER | 轮询周期 |
| `enabled` | INTEGER | 1 启用 / 0 停用；停用的设备不采集 |

### `samples` 样本（表最大，按 `(device_id, ts)` 建索引）

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `id` | INTEGER | 主键 |
| `device_id` | INTEGER | 外键 -> `devices.id` |
| `ts` | INTEGER | **Unix 毫秒**，采集时刻 |
| `metric` | TEXT | `response_ms` / `register_0` / `register_N` |
| `value` | REAL | 数值。**失败样本的 `value` 是配置的超时上限，不是测得的响应时间** |
| `quality` | INTEGER | 质量码，见上表 |

### `alarms` 告警

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `id` | INTEGER | 主键 |
| `device_id` | INTEGER | 外键 -> `devices.id` |
| `rule` | TEXT | 规则名，与 `config/app.json` 里的 `alarms[].name` 对应 |
| `started_at` | INTEGER | 触发时刻（Unix 毫秒），落在**故障成立的那一刻**，不是检测到的那一刻 |
| `ended_at` | INTEGER | 恢复时刻；`NULL` 表示**仍在告警中** |
| `detail` | TEXT | 触发原因 + 恢复说明（恢复时**追加**，不覆盖触发原因） |

### `runs` 采集运行记录

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `id` | INTEGER | 主键 |
| `started_at` / `finished_at` | INTEGER | 起止时刻（Unix 毫秒） |
| `env` | TEXT | JSON：采集器版本、Python 版本、平台、配置路径、设备清单 |
| `summary` | TEXT | JSON：时长、轮次、样本数、失败轮次、落库条数、告警次数、分设备成功率 |

---

## 配置

`config/app.json`（连真实设备）与 `config/app.test.json`（纯回放，用于演示与 CI）。

```jsonc
{
  "database": "data/records.db",
  "logLevel": "info",
  "logDir": "logs",
  "poll": { "defaultIntervalMs": 1000, "timeoutMs": 800, "retry": 2 },
  "devices": [
    {
      "name": "plc-01",
      "transport": "tcp",              // tcp / rtu / csv
      "address": "127.0.0.1:5020",
      "unitId": 1,
      "pollIntervalMs": 500,
      "metrics": ["response_ms", "register_0"]
    }
  ],
  "alarms": [
    { "name": "timeout-streak", "device": "plc-01", "rule": "timeout>=3",     "windowSec": 60  },
    { "name": "p95-latency",    "device": "plc-01", "rule": "p95_response_ms>500", "windowSec": 300 }
  ]
}
```

**告警规则语法**：`<左值><比较符><数值>`，比较符支持 `>= <= > < == !=`。

| 左值 | 含义 |
| --- | --- |
| `ok` / `timeout` / `crc` / `exception` | 窗口内**末尾连续**同码样本数 |
| `p50_<指标>` / `p95_` / `p99_` / `mean_` / `max_` / `min_` | 窗口内该统计量（**只算成功样本**） |
| `success_rate` | 窗口内成功率 |

配置写错时的报错形如：
`配置文件 config/app.json：字段 'devices[0].pollIntervalMs'：应为正整数，实际是 "500ms"`

---

## 接口

`uvicorn app.main:app` 之后打开 `/docs` 可以看到交互式文档。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查；配置或库有问题时返回 503 + 人话原因 |
| GET | `/api/devices` | 设备清单 + 样本总数 + 最近数据时刻 + 近一小时成功率 |
| GET | `/api/samples` | 样本分页查询（支持 `device` / `metric` / `range` / `from` / `to` / `limit` / `offset`） |
| GET | `/api/stats/summary` | 区间总览：总数、成功/失败、成功率、延迟分位、质量码分布 |
| GET | `/api/stats/series` | 响应时间曲线（分桶，`buckets` 封顶 500） |
| GET | `/api/alarms` | 活动告警 + 历史告警 |
| GET | `/api/alarm-rules` | 配置里的规则及其解析结果 |
| GET | `/` · `/dashboard` | 实时仪表盘页面 |

所有查询接口都必须带时间范围（默认 `1h`），上限 7 天；未知设备返回 **404** 而不是空列表。
错误统一成：

```json
{ "error": { "code": "range_too_large", "message": "时间范围过大…", "field": "range" } }
```

---

## 仪表盘

浏览器打开 `http://127.0.0.1:8000/`。前端**只做展示与轮询，不做任何统计计算**——
所有数字都来自 `/api/stats/*`，与报告、CLI 共用同一份 Python 实现。

- 默认窗口 **10 分钟**（不是接口默认的 1 小时）：曲线把窗口切成 60 个桶，
  1 小时切 60 桶是每桶 60 秒，几十秒的实时数据会整个落进一个桶，图上只剩一个点。
- 轮询间隔 **5 秒**，且**页面切到后台时停轮询**。
- 曲线只画成功样本，**有失败的时段涂成红色区块**——否则一条漂亮的平滑曲线
  会让人误以为这段时间一切正常。
- 设备超过「轮询周期 × 5」没有数据就标成 **无数据**，采集恢复后自动回到 **正常**。
- ECharts 从 CDN 引入；断网时图表区降级成一句提示，表格与告警列表照常可用。

---

## 测试

```bash
pytest -q                                   # 全部用例
pytest --cov=app --cov-report=term-missing   # 带覆盖率
ruff check .                                 # 代码风格
```

覆盖五类（规格「五类测试」）：单元、存储、接口、端到端、性能。

| 类型 | 说明 |
| --- | --- |
| 单元 | 统计计算、告警规则、时间窗口边界、配置校验、日志 |
| 存储 | 建表、批量写入、分页、索引生效、事务回滚 |
| 接口 | 状态码、参数校验、分页上限、错误结构、边界拒绝 |
| 端到端 | CSV 回放驱动采集→落库→统计→告警→报告，**不依赖模拟器与网络** |
| 性能 | `scripts/benchmark.py` 实测写入吞吐与查询延迟 |

测试的两条铁律：**不依赖真实设备与网络**（采集器用假实现替换）、
**不共用同一个数据库**（每个用例一个临时库，避免互相干扰）。

`tests/test_collector_integration.py` 会连真实的 Modbus 从站模拟器，
找不到模拟器时**自动跳过**，所以本地和 CI 都能全绿。

---

## 性能

`scripts/benchmark.py` 在本机（Windows 11 / Python 3.13 / SQLite WAL）实测：

| 项目 | 结果 |
| --- | --- |
| 批量写入 | 100,000 条 / 0.29 s ≈ **34.9 万条/秒** |
| 分页查询 P95 | < 5 ms |
| 索引 | `EXPLAIN QUERY PLAN` 确认走 `idx_samples_device_ts` |

批量写入用 `executemany` + 显式事务（每批 500 条），比逐条提交快一个数量级。

---

## 部署

### systemd（推荐）

```bash
sudo useradd --system --create-home collector
sudo mkdir -p /opt/collector-platform && sudo chown collector:collector /opt/collector-platform
git clone https://github.com/WWB2006/collector-platform.git /opt/collector-platform
cd /opt/collector-platform
sudo -u collector python3 -m venv .venv
sudo -u collector .venv/bin/pip install -r requirements.txt
sudo -u collector .venv/bin/python scripts/init_db.py

sudo cp deploy/collector-platform.service     /etc/systemd/system/
sudo cp deploy/collector-platform-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now collector-platform collector-platform-api

systemctl status collector-platform
journalctl -u collector-platform -f
```

采集与接口是**两个进程**：采集要长时间稳定运行，接口会被重启发版，
绑在一起会让「改个接口」变成「中断采集」。

### Docker

```bash
docker build -t collector-platform -f deploy/Dockerfile .
docker run --rm -v "$PWD/data:/app/data" collector-platform
```

---

## 已知限制

- **SQLite 单写者**：写入集中在调度器的一个线程里（这正是它的设计），
  单机几十台设备没问题；要上多机，存储层需要换成 PostgreSQL——`repository.py`
  把所有 SQL 收在一处，就是为这次替换留的口子。
- **串口（RTU）未实现**：`transport: "rtu"` 会给出可操作的错误提示，
  建议先用 CSV 回放把链路跑通。真实串口需要处理 `pyserial` 的超时语义与
  报文分帧，是独立的一块工作。
- **接口没有鉴权**：systemd 单元默认只监听 `127.0.0.1`，
  要对内网开放请在前面挂一层带鉴权的反向代理。
- **仪表盘没有历史回溯**：它是实时视图；要看长周期趋势请生成报告。

---

## 相关项目

- [WWB2006-Modbus-Gateway](https://github.com/WWB2006/Modbus-Gateway) —
  C++17/Qt 的 Modbus 网关，本项目可直接连它的从站模拟器做端到端验证。

## 许可

MIT
