"""报告模块：把某一时刻的数据冻结成一份自包含的 HTML。

* :mod:`app.report.builder`——取数、算结论、渲染；
* :mod:`app.report.charts`——服务端渲染的内联 SVG 图表（零 JS 依赖）。

报告**不引用任何外部资源**，因此可以离线打开、当附件发送、打印成 PDF。
"""

from app.report.builder import (
    DEFAULT_TEMPLATE,
    ReportConfig,
    ReportContext,
    build_conclusion,
    build_context,
    default_output_path,
    generate_report,
    load_report_config,
    render_report,
)

__all__ = [
    "DEFAULT_TEMPLATE",
    "ReportConfig",
    "ReportContext",
    "build_conclusion",
    "build_context",
    "default_output_path",
    "generate_report",
    "load_report_config",
    "render_report",
]
