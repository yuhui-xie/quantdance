"""批量回测报告支持。"""

from app.backtest.report_html import (
    render_backtest_universe_html,
    render_backtest_universe_html_from_json,
)
from app.backtest.report_model import build_backtest_universe_report_model

__all__ = [
    "build_backtest_universe_report_model",
    "render_backtest_universe_html",
    "render_backtest_universe_html_from_json",
]
