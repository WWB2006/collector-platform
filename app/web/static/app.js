/*
 * 仪表盘前端。
 *
 * 三条纪律（对应规格里列的三个常见错误）：
 *
 * 1. **不做统计计算**。成功率、分位数、分桶全部来自 /api/stats/*，
 *    与报告、CLI 共用同一份 Python 实现。前端自己再算一遍，
 *    只会得到「同一个数字在两处对不上、而且没人知道哪个对」。
 * 2. **不每秒请求**。默认 5 秒一轮，且**页面切到后台时停轮询**——
 *    没人看的页面继续打接口，是后端压力最没意义的那一部分。
 * 3. **图表数据不累积**。每次刷新整体替换数据，且点数由服务端 buckets
 *    参数封顶（默认 60），跑一整天也不会越跑越慢。
 *
 * 另外：ECharts 从 CDN 引入，断网时会加载失败。这时不白屏——
 * 卡片、表格、告警列表仍然可用，只是图表区显示一句提示。
 */
(function () {
  "use strict";

  var CFG = JSON.parse(document.getElementById("dashboard-config").textContent || "{}");
  var EP = CFG.endpoints || {};
  var DEVICES = CFG.devices || [];
  var POLL_MS = CFG.pollIntervalMs || 5000;
  var BUCKETS = CFG.buckets || 60;
  var WINDOWS = CFG.windows || ["10m", "1h", "6h", "24h"];
  var window_ = CFG.defaultWindow || "10m";

  var timer = null;
  var paused = false;
  var selectedDevice = "";
  var latencyChart = null;
  var qualityChart = null;
  var chartsReady = false;

  var el = function (id) { return document.getElementById(id); };

  // ---------------------------------------------------------------- 工具

  function pad(n) { return n < 10 ? "0" + n : String(n); }

  function fmtTime(ms) {
    if (!ms) { return "—"; }
    var d = new Date(ms);
    return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }

  function fmtDateTime(ms) {
    if (!ms) { return "—"; }
    var d = new Date(ms);
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) +
      " " + fmtTime(ms);
  }

  function fmtPct(x) {
    return (x === null || x === undefined) ? "—" : (x * 100).toFixed(1) + "%";
  }

  function fmtMs(x) {
    return (x === null || x === undefined) ? "—" : x.toFixed(1) + " ms";
  }

  function fmtCount(x) {
    return (x === null || x === undefined) ? "—" : String(x);
  }

  function text(node, value) {
    if (node) { node.textContent = value; }
  }

  function getJSON(url) {
    return fetch(url, { headers: { "Accept": "application/json" } })
      .then(function (res) {
        if (!res.ok) { throw new Error(url + " -> HTTP " + res.status); }
        return res.json();
      });
  }

  function qs(params) {
    var parts = [];
    Object.keys(params).forEach(function (k) {
      var v = params[k];
      if (v === undefined || v === null || v === "") { return; }
      parts.push(encodeURIComponent(k) + "=" + encodeURIComponent(v));
    });
    return parts.length ? "?" + parts.join("&") : "";
  }

  // ---------------------------------------------------------------- 图表

  function initCharts() {
    if (chartsReady) { return true; }
    if (typeof window.echarts === "undefined") { return false; }
    latencyChart = window.echarts.init(el("chart-latency"));
    qualityChart = window.echarts.init(el("chart-quality"));
    // 轮询刷新时关掉动画：否则每次刷新都重放一遍过渡，看着像在闪。
    latencyChart.setOption({ animation: false });
    qualityChart.setOption({ animation: false });
    window.addEventListener("resize", function () {
      if (latencyChart) { latencyChart.resize(); }
      if (qualityChart) { qualityChart.resize(); }
    });
    chartsReady = true;
    return true;
  }

  function showChartFallback() {
    ["chart-latency", "chart-quality"].forEach(function (id) {
      var node = el(id);
      if (node && !node.dataset.fallback) {
        node.dataset.fallback = "1";
        node.innerHTML = '<div class="empty">图表库（ECharts CDN）未能加载，' +
          '表格数据不受影响。</div>';
      }
    });
  }

  function renderLatencyChart(series) {
    if (!initCharts()) { showChartFallback(); return; }
    var points = (series && series.points) || [];
    var labels = points.map(function (p) { return fmtTime(p.ts); });
    var sparse = points.length <= 24;

    // 失败样本不进 avg/max（超时值不是测得的响应时间），但必须让人**看得见**：
    // 把有失败的桶在 x 轴上连成区间涂成浅红。否则一条漂亮的平滑曲线会让人
    // 误以为这段时间一切正常。
    var failedIndexes = [];
    points.forEach(function (p, i) { if (p.failed > 0) { failedIndexes.push(i); } });
    var ranges = [];
    failedIndexes.forEach(function (i) {
      var last = ranges[ranges.length - 1];
      if (last && last[1] === i - 1) { last[1] = i; } else { ranges.push([i, i]); }
    });
    var markArea = ranges.map(function (r) {
      return [{ xAxis: labels[r[0]] }, { xAxis: labels[r[1]] }];
    });

    latencyChart.setOption({
      tooltip: {
        trigger: "axis",
        formatter: function (items) {
          if (!items || !items.length) { return ""; }
          var idx = items[0].dataIndex;
          var p = points[idx] || {};
          var lines = ["<strong>" + labels[idx] + "</strong>"];
          items.forEach(function (it) {
            if (it.value !== null && it.value !== undefined) {
              lines.push(it.marker + it.seriesName + "：" + Number(it.value).toFixed(2) + " ms");
            }
          });
          lines.push("成功 " + (p.count || 0) + " 条 / 失败 " + (p.failed || 0) + " 条");
          return lines.join("<br>");
        }
      },
      legend: { data: ["平均", "最大"], right: 0, top: 0, itemWidth: 12, itemHeight: 8 },
      grid: { left: 46, right: 16, top: 32, bottom: 28 },
      xAxis: {
        type: "category",
        boundaryGap: false,
        data: labels,
        axisLabel: { color: "#6b7280", fontSize: 11 }
      },
      yAxis: {
        type: "value",
        name: "ms",
        nameTextStyle: { color: "#6b7280", fontSize: 11 },
        axisLabel: { color: "#6b7280", fontSize: 11 },
        splitLine: { lineStyle: { color: "#eef0f3" } }
      },
      series: [
        {
          // 点少的时候必须把标记画出来：只有一个桶时，一条没有标记的折线
          // 在图上什么都看不到，页面看起来像「图表坏了」。
          name: "平均", type: "line", smooth: true,
          showSymbol: sparse,
          symbolSize: 5,
          connectNulls: true,
          lineStyle: { width: 2, color: "#2563eb" },
          itemStyle: { color: "#2563eb" },
          areaStyle: { color: "rgba(37,99,235,0.08)" },
          markArea: {
            silent: true,
            itemStyle: { color: "rgba(220,38,38,0.10)" },
            label: {
              show: true, position: "insideTop", color: "#dc2626", fontSize: 10,
              formatter: "有失败样本"
            },
            data: markArea
          },
          data: points.map(function (p) { return p.avg; })
        },
        {
          name: "最大", type: "line", smooth: true,
          showSymbol: sparse,
          symbolSize: 5,
          connectNulls: true,
          lineStyle: { width: 1.5, color: "#f59e0b", type: "dashed" },
          itemStyle: { color: "#f59e0b" },
          data: points.map(function (p) { return p.max; })
        }
      ]
    });
  }

  function renderQualityChart(summary) {
    if (!initCharts()) { showChartFallback(); return; }
    var quality = (summary && summary.quality) || {};
    var data = Object.keys(quality).map(function (code) {
      return { name: quality[code].label, value: quality[code].count };
    });
    if (!data.length) {
      qualityChart.clear();
      qualityChart.setOption({
        title: {
          text: "区间内没有样本", left: "center", top: "middle",
          textStyle: { color: "#6b7280", fontSize: 13, fontWeight: "normal" }
        }
      });
      return;
    }
    qualityChart.setOption({
      tooltip: { trigger: "item", formatter: "{b}: {c} ({d}%)" },
      legend: { bottom: 0, itemWidth: 10, itemHeight: 8, textStyle: { fontSize: 12 } },
      color: ["#16a34a", "#dc2626", "#f59e0b", "#7c3aed"],
      series: [{
        type: "pie",
        radius: ["46%", "70%"],
        center: ["50%", "44%"],
        avoidLabelOverlap: true,
        label: { formatter: "{d}%", fontSize: 11, color: "#1f2329" },
        data: data
      }]
    });
  }

  // ---------------------------------------------------------------- 渲染

  function renderCards(summary) {
    text(el("kpi-total"), fmtCount(summary ? summary.total : null));
    text(el("kpi-rate"), fmtPct(summary ? summary.successRate : null));
    text(el("kpi-p95"), summary && summary.latency ? fmtMs(summary.latency.p95) : "—");
  }

  function renderDevices(payload) {
    var body = el("device-rows");
    var devices = (payload && payload.devices) || [];
    var now = Date.now();
    var staleCount = 0;

    if (!devices.length) {
      body.innerHTML = '<tr><td colspan="7" class="empty">还没有配置设备</td></tr>';
      text(el("device-hint"), "");
      return { total: 0, stale: 0 };
    }

    body.innerHTML = devices.map(function (d) {
      var stale = !d.lastSampleAt || (now - d.lastSampleAt) > (d.staleAfterMs || 15000);
      if (stale) { staleCount += 1; }
      var badge = stale ? '<span class="badge bad">无数据</span>'
        : (d.successRate !== null && d.successRate < 0.9
          ? '<span class="badge warn">成功率偏低</span>'
          : '<span class="badge ok">正常</span>');
      var rate = (d.successRate === null || d.successRate === undefined)
        ? "—"
        : '<span style="color:' + (d.successRate < 0.9 ? "#dc2626" : "#16a34a") + '">' +
          fmtPct(d.successRate) + "</span>";
      return "<tr>" +
        "<td><code>" + d.name + "</code></td>" +
        "<td>" + d.transport + "</td>" +
        '<td class="num">' + d.pollIntervalMs + " ms</td>" +
        '<td class="num">' + d.totalSamples + "</td>" +
        '<td class="num">' + rate + "</td>" +
        '<td class="num">' + (d.lastSampleAt ? fmtDateTime(d.lastSampleAt) : "—") +
          (d.lastSampleAt ? ' <span style="color:#6b7280">(' +
            Math.round((now - d.lastSampleAt) / 1000) + " 秒前)</span>" : "") + "</td>" +
        "<td>" + badge + "</td>" +
        "</tr>";
    }).join("");

    text(el("device-hint"), staleCount
      ? staleCount + " / " + devices.length + " 台设备当前没有数据"
      : devices.length + " 台设备全部正常");
    return { total: devices.length, stale: staleCount };
  }

  function renderAlarms(payload) {
    var body = el("alarm-rows");
    var items = (payload && payload.items) || [];
    var activeCount = (payload && payload.active) || 0;

    text(el("kpi-alarms"), String(activeCount));
    text(el("alarm-hint"), activeCount
      ? activeCount + " 条活动告警"
      : (items.length ? "区间内共 " + items.length + " 条记录，当前无活动告警" : "暂无告警记录"));

    if (!items.length) {
      body.innerHTML = '<tr><td colspan="6" class="empty">暂无告警记录</td></tr>';
      return;
    }
    body.innerHTML = items.map(function (a) {
      var badge = a.active
        ? '<span class="badge bad">告警中</span>'
        : '<span class="badge ok">已恢复</span>';
      var duration = a.durationMs
        ? '<span style="color:#6b7280">（持续 ' + Math.round(a.durationMs / 1000) + " 秒）</span>"
        : "";
      return "<tr>" +
        "<td><code>" + a.device + "</code></td>" +
        "<td>" + a.rule + "</td>" +
        '<td class="num">' + fmtDateTime(a.startedAt) + "</td>" +
        '<td class="num">' + (a.endedAt ? fmtDateTime(a.endedAt) : "—") + "</td>" +
        "<td>" + badge + "</td>" +
        '<td style="color:#6b7280">' + (a.detail || "") + duration + "</td>" +
        "</tr>";
    }).join("");
  }

  function renderHealth(health) {
    var badge = el("health-badge");
    badge.className = "badge ok";
    text(badge, "服务正常");
    if (health && health.warnings && health.warnings.length) {
      badge.className = "badge warn";
      text(badge, "服务正常（有告警）");
    }
  }

  function markOffline() {
    var badge = el("health-badge");
    badge.className = "badge bad";
    text(badge, "接口不可达");
    el("conn-banner").classList.add("show");
  }

  function clearOffline() {
    el("conn-banner").classList.remove("show");
  }

  // ---------------------------------------------------------------- 轮询

  function refresh() {
    var seriesUrl = EP.series + qs({ device: selectedDevice, range: window_, buckets: BUCKETS });
    return Promise.all([
      getJSON(EP.health),
      getJSON(EP.devices),
      getJSON(EP.summary + qs({ range: window_ })),
      getJSON(seriesUrl),
      getJSON(EP.alarms + qs({ range: window_, limit: 20 }))
    ]).then(function (results) {
      clearOffline();
      renderHealth(results[0]);
      renderDevices(results[1]);
      renderCards(results[2]);
      renderLatencyChart(results[3]);
      renderQualityChart(results[2]);
      renderAlarms(results[4]);
      text(el("last-refresh"), "最后刷新 " + fmtTime(Date.now()));
    }).catch(function (err) {
      markOffline();
      if (window.console) { console.error("[dashboard]", err); }
    });
  }

  function start() {
    stop();
    timer = window.setInterval(function () {
      // 页面在后台时不轮询：没人看的页面继续打接口是纯粹的压力。
      if (document.hidden) { return; }
      refresh();
    }, POLL_MS);
  }

  function stop() {
    if (timer !== null) { window.clearInterval(timer); timer = null; }
  }

  function setPaused(next) {
    paused = next;
    text(el("btn-pause"), paused ? "继续" : "暂停");
    if (paused) { stop(); } else { start(); refresh(); }
  }

  // ---------------------------------------------------------------- 启动

  function boot() {
    var select = el("device-select");
    DEVICES.forEach(function (d) {
      var option = document.createElement("option");
      option.value = d.name;
      option.textContent = d.name;
      select.appendChild(option);
    });
    select.addEventListener("change", function () {
      selectedDevice = select.value;
      refresh();
    });

    var windowSelect = el("window-select");
    windowSelect.value = window_;
    windowSelect.addEventListener("change", function () {
      window_ = windowSelect.value;
      refresh();
    });

    el("btn-refresh").addEventListener("click", function () { refresh(); });
    el("btn-pause").addEventListener("click", function () { setPaused(!paused); });

    // 从后台切回来立刻补一次，不用干等一个轮询周期。
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden && !paused) { refresh(); }
    });

    refresh();
    start();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
