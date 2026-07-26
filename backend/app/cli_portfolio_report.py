"""组合回测交互 HTML 报告：调仓点、买卖明细、区间收益、单票买卖点钻取。"""

from __future__ import annotations

from pathlib import Path

from app.portfolio.report_html import (
    render_portfolio_html,
    render_portfolio_html_from_json,
)
from app.portfolio.report_model import (
    _bars_cover_trade_dates,
    _load_symbol_bars,
    build_portfolio_report_model,
)

__all__ = [
    "_bars_cover_trade_dates",
    "_load_symbol_bars",
    "build_portfolio_report_model",
    "render_portfolio_html",
    "render_portfolio_html_from_json",
    "main",
]


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="从组合回测 JSON 生成交互 HTML 报告")
    parser.add_argument("json_path", type=Path, help="portfolio 回测结果 JSON")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="HTML 输出路径（默认与 JSON 同名 .html）",
    )
    args = parser.parse_args(argv)
    path = render_portfolio_html_from_json(args.json_path, args.output)
    print(f"报告已保存: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
