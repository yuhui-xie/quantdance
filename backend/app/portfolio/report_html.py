"""组合回测交互 HTML 报告渲染。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.portfolio.report_model import build_portfolio_report_model

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def _load_template(name: str) -> str:
    path = _TEMPLATES_DIR / name
    return path.read_text(encoding="utf-8")


def _write_embedded_html(
    template: str,
    model: dict[str, Any],
    dest: Path,
) -> Path:
    """将报告模型嵌入 HTML 模板并写入磁盘。"""
    dest = dest.expanduser().resolve()
    if dest.suffix.lower() != ".html":
        dest = dest.with_suffix(".html")
    dest.parent.mkdir(parents=True, exist_ok=True)
    title = str(model.get("strategy_id") or "portfolio")
    payload = json.dumps(model, ensure_ascii=False)
    # 避免 </script> 截断
    payload = payload.replace("<", "\\u003c")
    html = template.replace("__TITLE__", title).replace("__DATA__", payload)
    dest.write_text(html, encoding="utf-8")
    return dest


def render_portfolio_html(
    out: dict[str, Any],
    dest: Path,
    *,
    load_prices: bool = True,
) -> Path:
    """生成自包含交互 HTML 报告，返回写入路径。"""
    mode = str(out.get("mode") or "backtest")
    if mode == "backtest":
        model = build_portfolio_report_model(out, load_prices=load_prices)
        return _write_embedded_html(
            _load_template("portfolio_backtest.html"),
            model,
            dest,
        )
    raise ValueError(f"暂不支持 mode={mode!r} 的 HTML 报告")


def render_portfolio_html_from_json(json_path: Path, dest: Path | None = None) -> Path:
    """从已有回测 JSON 离线生成 HTML。"""
    raw = json.loads(json_path.expanduser().resolve().read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("回测 JSON 须为对象")
    out_path = dest
    if out_path is None:
        out_path = json_path.with_suffix(".html")
    return render_portfolio_html(raw, out_path)
