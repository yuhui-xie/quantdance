"""因子 IC 分析自包含交互 HTML 报告渲染。

输入为 ``compute_factor_ic`` 的返回 dict（见 ``app/ic_analysis.py``），不依赖网络，
只把结果渲染为自包含 HTML：顶部统计卡、逐期 RankIC 折线图（因子/持有期切换）、
可排序的「因子 | 来源 | 持有期 | 均值 RankIC | RankICIR | t 值 | IC>0 占比 | 期数」汇总表。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_TEMPLATE = Path(__file__).resolve().parent / "backtest" / "templates" / "factor_ic.html"


def render_factor_ic_html(out: dict[str, Any], dest: Path) -> Path:
    """把 IC 分析结果 dict 渲染为自包含 HTML，返回实际写入路径。

    ``dest`` 无后缀时自动补 ``.html``，父目录自动创建。
    """
    path = dest.expanduser().resolve()
    if path.suffix.lower() != ".html":
        path = path.with_suffix(".html")
    path.parent.mkdir(parents=True, exist_ok=True)
    # 紧凑分隔符削减体积，转义 `<` 防内嵌数据破坏 HTML
    payload = json.dumps(out, ensure_ascii=False, separators=(",", ":")).replace(
        "<", "\\u003c"
    )
    title = str(out.get("universe_note") or out.get("universe") or "factor-ic")
    html = (
        _TEMPLATE.read_text(encoding="utf-8")
        .replace("__TITLE__", title)
        .replace("__DATA__", payload)
    )
    path.write_text(html, encoding="utf-8")
    return path


def render_factor_ic_html_from_json(json_path: Path, dest: Path | None = None) -> Path:
    """从已有 IC 分析 JSON 生成 HTML。"""
    source = json_path.expanduser().resolve()
    raw = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("IC 分析 JSON 须为对象")
    return render_factor_ic_html(raw, dest or source.with_suffix(".html"))
