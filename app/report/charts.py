"""服务端渲染的内联 SVG 图表。

为什么报告不用 ECharts（虽然仪表盘用了）：

规格要求「报告可离线打开且数据一致」。ECharts 从 CDN 加载，
同事把报告存到本地、断网打开，看到的就是一片空白——
而报告恰恰是要**当作附件发出去**的东西，它必须自包含。

所以这里在服务端把数据渲染成 SVG 字符串直接写进 HTML：
零 JS 依赖、零网络请求、可打印、能被文本方式 diff。
仪表盘那边用 ECharts 是因为它要实时轮询，那是另一种场景。

配色刻意用浅底深字，适合打印与投屏。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

# 报告是独立文档（不跟随 IDE 主题），固定用浅色底 + 深色字，保证打印可读。
BACKGROUND = "#ffffff"
PANEL = "#f7f8fa"
AXIS = "#c9ced6"
GRID = "#e8ebef"
TEXT = "#1f2329"
MUTED = "#6b7280"
PRIMARY = "#2563eb"       # 平均值曲线
SECONDARY = "#f59e0b"     # 最大值曲线
DANGER = "#dc2626"        # 超时/失败
OK_COLOR = "#16a34a"

#: 质量码 -> 展示色。
QUALITY_COLORS: dict[int, str] = {
    0: OK_COLOR,
    1: DANGER,
    2: "#f59e0b",
    3: "#7c3aed",
}


def _fmt_time(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000).strftime("%m-%d %H:%M")


def _fmt_number(value: float, digits: int = 1) -> str:
    if value >= 1000:
        return f"{value:,.0f}"
    return f"{value:.{digits}f}"


def _nice_ceiling(value: float) -> float:
    """把 Y 轴上限取整到一个「好看」的数，避免轴标签是 47.382 这种。"""
    if value <= 0:
        return 1.0
    magnitude = 10 ** (len(str(int(value))) - 1)
    for factor in (1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        candidate = factor * magnitude
        if candidate >= value:
            return float(candidate)
    return float(10 * magnitude)


@dataclass(frozen=True)
class ChartSize:
    width: int = 760
    height: int = 240
    padding_left: int = 56
    padding_right: int = 16
    padding_top: int = 18
    padding_bottom: int = 34


def empty_chart(message: str, size: ChartSize | None = None) -> str:
    """没有数据时也要画个框并说明原因，而不是留一块空白。"""
    dims = size or ChartSize()
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {dims.width} {dims.height}" '
        f'width="100%" height="{dims.height}" role="img" aria-label="{message}">'
        f'<rect x="0" y="0" width="{dims.width}" height="{dims.height}" '
        f'fill="{PANEL}" rx="6"/>'
        f'<text x="{dims.width / 2}" y="{dims.height / 2}" text-anchor="middle" '
        f'fill="{MUTED}" font-size="13" font-family="system-ui, sans-serif">{message}</text>'
        "</svg>"
    )


def line_chart(
    points: Sequence[dict[str, Any]],
    *,
    label: str = "响应时间 (ms)",
    size: ChartSize | None = None,
    show_max: bool = True,
) -> str:
    """把分桶后的时间序列画成折线图。

    ``points`` 元素形如 ``{"ts": 毫秒, "avg": 12.3, "max": 20.1, "count": 5}``。
    """
    dims = size or ChartSize()
    usable = [p for p in points if p.get("avg") is not None]
    if not usable:
        return empty_chart("该时间范围内没有可绘制的数据", dims)

    plot_w = dims.width - dims.padding_left - dims.padding_right
    plot_h = dims.height - dims.padding_top - dims.padding_bottom

    values = [float(p["avg"]) for p in usable]
    if show_max:
        values += [float(p["max"]) for p in usable if p.get("max") is not None]
    top = _nice_ceiling(max(values))
    bottom = 0.0

    def x_of(index: int) -> float:
        if len(usable) == 1:
            return dims.padding_left + plot_w / 2
        return dims.padding_left + plot_w * index / (len(usable) - 1)

    def y_of(value: float) -> float:
        ratio = (value - bottom) / (top - bottom) if top > bottom else 0.0
        return dims.padding_top + plot_h * (1 - ratio)

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {dims.width} {dims.height}" '
        f'width="100%" height="{dims.height}" role="img" aria-label="{label} 折线图">',
        f'<rect x="0" y="0" width="{dims.width}" height="{dims.height}" '
        f'fill="{BACKGROUND}" rx="6"/>',
    ]

    # Y 轴网格与刻度
    for step in range(5):
        value = bottom + (top - bottom) * step / 4
        y = y_of(value)
        parts.append(
            f'<line x1="{dims.padding_left}" y1="{y:.1f}" x2="{dims.width - dims.padding_right}" '
            f'y2="{y:.1f}" stroke="{GRID}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{dims.padding_left - 8}" y="{y + 4:.1f}" text-anchor="end" '
            f'fill="{MUTED}" font-size="11" font-family="system-ui, sans-serif">'
            f"{_fmt_number(value)}</text>"
        )

    # X 轴刻度（首、中、尾）
    tick_indices = sorted({0, len(usable) // 2, len(usable) - 1})
    for index in tick_indices:
        x = x_of(index)
        parts.append(
            f'<text x="{x:.1f}" y="{dims.height - 12}" text-anchor="middle" fill="{MUTED}" '
            f'font-size="11" font-family="system-ui, sans-serif">'
            f"{_fmt_time(int(usable[index]['ts']))}</text>"
        )

    parts.append(
        f'<line x1="{dims.padding_left}" y1="{dims.height - dims.padding_bottom}" '
        f'x2="{dims.width - dims.padding_right}" y2="{dims.height - dims.padding_bottom}" '
        f'stroke="{AXIS}" stroke-width="1"/>'
    )

    # 最大值曲线（淡）在平均值之前画，避免压住主线
    if show_max and any(p.get("max") is not None for p in usable):
        max_points = " ".join(
            f"{x_of(i):.1f},{y_of(float(p['max'])):.1f}"
            for i, p in enumerate(usable)
            if p.get("max") is not None
        )
        parts.append(
            f'<polyline points="{max_points}" fill="none" stroke="{SECONDARY}" '
            f'stroke-width="1.2" stroke-dasharray="4 3" opacity="0.85"/>'
        )

    avg_points = " ".join(f"{x_of(i):.1f},{y_of(v):.1f}" for i, v in enumerate(values))
    parts.append(
        f'<polyline points="{avg_points}" fill="none" stroke="{PRIMARY}" '
        f'stroke-width="2" stroke-linejoin="round"/>'
    )
    for i, value in enumerate(values):
        parts.append(
            f'<circle cx="{x_of(i):.1f}" cy="{y_of(value):.1f}" r="2.4" fill="{PRIMARY}"/>'
        )

    # 图例
    legend_y = 12
    parts.append(f'<circle cx="{dims.padding_left}" cy="{legend_y}" r="4" fill="{PRIMARY}"/>')
    parts.append(
        f'<text x="{dims.padding_left + 10}" y="{legend_y + 4}" fill="{TEXT}" font-size="11" '
        f'font-family="system-ui, sans-serif">平均</text>'
    )
    if show_max:
        parts.append(
            f'<circle cx="{dims.padding_left + 56}" cy="{legend_y}" r="4" fill="{SECONDARY}"/>'
        )
        parts.append(
            f'<text x="{dims.padding_left + 66}" y="{legend_y + 4}" fill="{TEXT}" font-size="11" '
            f'font-family="system-ui, sans-serif">最大</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def bar_chart(
    items: Sequence[tuple[str, float, str]],
    *,
    label: str = "分布",
    size: ChartSize | None = None,
) -> str:
    """横向条形图，用于质量码分布。``items`` 为 ``(名称, 数值, 颜色)``。"""
    dims = size or ChartSize(height=180)
    if not items:
        return empty_chart("没有可统计的样本", dims)

    label_w = 96
    plot_w = dims.width - label_w - 76
    bar_h = 22
    gap = 10

    total = sum(value for _, value, _ in items) or 1
    maximum = max(value for _, value, _ in items) or 1

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {dims.width} {dims.height}" '
        f'width="100%" height="{dims.height}" role="img" aria-label="{label} 条形图">',
        f'<rect x="0" y="0" width="{dims.width}" height="{dims.height}" '
        f'fill="{BACKGROUND}" rx="6"/>',
    ]

    for index, (name, value, color) in enumerate(items):
        y = 14 + index * (bar_h + gap)
        width = max(2.0, plot_w * value / maximum)
        share = value / total * 100
        parts.append(
            f'<text x="{label_w - 8}" y="{y + bar_h - 6}" text-anchor="end" fill="{TEXT}" '
            f'font-size="12" font-family="system-ui, sans-serif">{name}</text>'
        )
        parts.append(
            f'<rect x="{label_w}" y="{y}" width="{plot_w}" height="{bar_h}" fill="{PANEL}" rx="4"/>'
        )
        parts.append(
            f'<rect x="{label_w}" y="{y}" width="{width:.1f}" height="{bar_h}" '
            f'fill="{color}" rx="4"/>'
        )
        parts.append(
            f'<text x="{label_w + plot_w + 8}" y="{y + bar_h - 6}" fill="{MUTED}" font-size="11" '
            f'font-family="system-ui, sans-serif">{value:g}（{share:.1f}%）</text>'
        )

    parts.append("</svg>")
    return "".join(parts)


def donut_chart(
    items: Sequence[tuple[str, float, str]],
    *,
    size: int = 150,
    center_label: str = "",
) -> str:
    """环形图，用于成功率。用 ``stroke-dasharray`` 拼弧，不需要 JS。"""
    total = sum(value for _, value, _ in items)
    radius = size / 2 - 14
    circumference = 2 * 3.141592653589793 * radius

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
        f'width="{size}" height="{size}" role="img" aria-label="成功率环形图">',
        f'<circle cx="{size / 2}" cy="{size / 2}" r="{radius}" fill="none" '
        f'stroke="{PANEL}" stroke-width="16"/>',
    ]

    if total <= 0:
        parts.append(
            f'<text x="{size / 2}" y="{size / 2 + 4}" text-anchor="middle" fill="{MUTED}" '
            f'font-size="12" font-family="system-ui, sans-serif">无数据</text>'
        )
        parts.append("</svg>")
        return "".join(parts)

    offset = 0.0
    for _name, value, color in items:
        share = value / total
        length = circumference * share
        parts.append(
            f'<circle cx="{size / 2}" cy="{size / 2}" r="{radius}" fill="none" '
            f'stroke="{color}" stroke-width="16" '
            f'stroke-dasharray="{length:.2f} {circumference - length:.2f}" '
            f'stroke-dashoffset="{-offset:.2f}" '
            f'transform="rotate(-90 {size / 2} {size / 2})"/>'
        )
        offset += length

    if center_label:
        parts.append(
            f'<text x="{size / 2}" y="{size / 2 + 5}" text-anchor="middle" fill="{TEXT}" '
            f'font-size="18" font-weight="600" font-family="system-ui, sans-serif">'
            f"{center_label}</text>"
        )
    parts.append("</svg>")
    return "".join(parts)
