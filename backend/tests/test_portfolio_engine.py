"""组合再平衡引擎单测（不访问外网）。"""

from __future__ import annotations

import pandas as pd

from app.portfolio_engine import (
    build_close_panel,
    every_n_trading_days,
    run_equal_weight_rebalance,
)


def test_every_n_trading_days():
    days = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"]
    assert every_n_trading_days(days, 2) == ["2024-01-02", "2024-01-04", "2024-01-08"]
    assert every_n_trading_days(days, 1) == days
    assert every_n_trading_days(days, 20) == ["2024-01-02"]


def test_equal_weight_rebalance_buys_and_marks():
    idx = pd.date_range("2024-01-02", periods=5, freq="B")
    panel = pd.DataFrame(
        {
            "000001": [10.0, 10.5, 11.0, 10.8, 11.2],
            "000002": [5.0, 5.1, 5.2, 5.0, 4.8],
        },
        index=[d.strftime("%Y-%m-%d") for d in idx],
    )

    def select(asof: str, _ctx):
        return ["000001", "000002"]

    result = run_equal_weight_rebalance(
        panel,
        rebalance_dates=[panel.index[0]],
        select_holdings=select,
        initial_cash=20_000,
        commission=0.0,
        min_commission=0.0,
        slippage=0.0,
        lot_size=100,
    )
    assert result.metrics["num_trades"] >= 2
    assert result.equity[0]["equity"] > 0
    assert result.equity[-1]["equity"] > 0
    assert result.rebalances[0]["targets"] == ["000001", "000002"]


def test_build_close_panel_from_value_frames():
    a = pd.DataFrame({"date": ["2024-01-02", "2024-01-03"], "close": [1.0, 1.1]})
    b = pd.DataFrame({"date": ["2024-01-02", "2024-01-03"], "close": [2.0, 2.2]})
    panel = build_close_panel({"000001": a, "000002": b})
    assert list(panel.columns) == ["000001", "000002"]
    assert panel.loc["2024-01-03", "000001"] == 1.1
