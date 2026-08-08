"""独立资金批量回测交互 HTML 报告渲染。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.backtest.report_model import build_backtest_universe_report_model
from app.backtest.shared_report_html import render_backtest_shared_html

_TEMPLATE = Path(__file__).resolve().parent / "templates" / "backtest_universe.html"


def render_backtest_html(
    out: dict[str, Any],
    dest: Path,
    *,
    load_prices: bool = True,
) -> Path:
    """按结果结构渲染独立资金 universe 或共享资金横截面报告。"""
    if "aggregate" in out and "runs" in out:
        return render_backtest_universe_html(out, dest, load_prices=load_prices)
    if "rebalances" in out and "equity" in out:
        return render_backtest_shared_html(out, dest, load_prices=load_prices)
    raise ValueError("无法识别回测报告结构")


def render_backtest_html_from_json(
    json_path: Path,
    dest: Path | None = None,
    *,
    load_prices: bool = True,
) -> Path:
    source = json_path.expanduser().resolve()
    raw = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("回测 JSON 须为对象")
    return render_backtest_html(
        raw,
        dest or source.with_suffix(".html"),
        load_prices=load_prices,
    )


def render_backtest_universe_html(
    out: dict[str, Any],
    dest: Path,
    *,
    top_k: int | None = None,
    price_top_k: int = 20,
    load_prices: bool = True,
) -> Path:
    """生成自包含批量回测 HTML，返回实际写入路径。"""
    model = build_backtest_universe_report_model(
        out,
        top_k=top_k,
        price_top_k=price_top_k,
        load_prices=load_prices,
    )
    path = dest.expanduser().resolve()
    if path.suffix.lower() != ".html":
        path = path.with_suffix(".html")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(model, ensure_ascii=False).replace("<", "\\u003c")
    title = str(model.get("strategy_id") or "universe")
    html = (
        _TEMPLATE.read_text(encoding="utf-8")
        .replace("__TITLE__", title)
        .replace("__DATA__", payload)
    )
    path.write_text(html, encoding="utf-8")
    return path


def render_backtest_universe_html_from_json(
    json_path: Path,
    dest: Path | None = None,
    *,
    top_k: int | None = None,
    price_top_k: int = 20,
    load_prices: bool = True,
) -> Path:
    """从已有批量回测 JSON 生成 HTML。"""
    source = json_path.expanduser().resolve()
    raw = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("回测 JSON 须为对象")
    return render_backtest_universe_html(
        raw,
        dest or source.with_suffix(".html"),
        top_k=top_k,
        price_top_k=price_top_k,
        load_prices=load_prices,
    )
