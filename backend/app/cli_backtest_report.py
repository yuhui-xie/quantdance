"""从独立资金或共享资金回测 JSON 生成交互 HTML 报告。"""

from __future__ import annotations

from pathlib import Path

from app.backtest.report_html import (
    render_backtest_html,
    render_backtest_html_from_json,
    render_backtest_universe_html,
    render_backtest_universe_html_from_json,
)
from app.backtest.report_model import build_backtest_universe_report_model
from app.backtest.shared_report_model import (
    _bars_cover_trade_dates,
    _load_symbol_bars,
    build_backtest_shared_report_model,
)
from app.backtest.shared_report_html import (
    render_backtest_shared_html,
    render_backtest_shared_html_from_json,
)

__all__ = [
    "build_backtest_universe_report_model",
    "build_backtest_shared_report_model",
    "render_backtest_html",
    "render_backtest_html_from_json",
    "render_backtest_shared_html",
    "render_backtest_shared_html_from_json",
    "render_backtest_universe_html",
    "render_backtest_universe_html_from_json",
    "main",
]


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="从回测 JSON 生成交互 HTML 报告")
    parser.add_argument("json_path", type=Path, help="独立或共享资金回测结果 JSON")
    parser.add_argument("-o", "--output", type=Path, default=None, help="HTML 输出路径")
    parser.add_argument(
        "--top-k",
        type=int,
        default=None,
        help="仅嵌入前 N 名明细（默认嵌入全部成功结果）",
    )
    parser.add_argument(
        "--price-top-k",
        type=int,
        default=None,
        help="从行情缓存补拉 K 线的股票数；0 或省略=全部，正数=仅前 N 名",
    )
    parser.add_argument(
        "--no-load-prices",
        action="store_true",
        help="不从行情缓存补充逐票价格",
    )
    args = parser.parse_args(argv)
    raw = __import__("json").loads(args.json_path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "aggregate" in raw and "runs" in raw:
        path = render_backtest_universe_html_from_json(
            args.json_path,
            args.output,
            top_k=None if args.top_k is None else max(0, args.top_k),
            price_top_k=(
                None if args.price_top_k is None or args.price_top_k <= 0 else args.price_top_k
            ),
            load_prices=not args.no_load_prices,
        )
    else:
        path = render_backtest_html_from_json(
            args.json_path,
            args.output,
            load_prices=not args.no_load_prices,
        )
    print(f"报告已保存: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
