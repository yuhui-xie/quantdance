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


def test_take_profit_arms_then_exits_on_pullback():
    """浮盈先冲过 x=20%，再回落到 y=10% 时卖出。"""
    idx = pd.date_range("2024-01-02", periods=8, freq="B")
    # 成本≈10；第3日 12(+20%) 启动；第5日 10.5(+5%) 应卖出
    panel = pd.DataFrame(
        {"000001": [10.0, 11.0, 12.0, 12.5, 10.5, 10.2, 10.0, 9.8]},
        index=[d.strftime("%Y-%m-%d") for d in idx],
    )

    def select(asof: str, _ctx):
        return ["000001"]

    result = run_equal_weight_rebalance(
        panel,
        rebalance_dates=[panel.index[0]],
        select_holdings=select,
        initial_cash=10_000,
        commission=0.0,
        min_commission=0.0,
        slippage=0.0,
        lot_size=100,
        take_profit_arm_pct=0.20,
        take_profit_exit_pct=0.10,
    )
    sells = [t for t in result.trades if t["side"] == "sell"]
    assert len(sells) == 1
    assert sells[0]["reason"] == "take_profit"
    assert sells[0]["date"] == panel.index[4]
    assert sells[0]["price"] == 10.5
    # 卖出后现金回到约初始资金附近（无费用）
    assert result.equity[-1]["equity"] > 9_000


def test_stop_loss_exits_at_threshold():
    """浮亏达到 10% 时止损卖出。"""
    idx = pd.date_range("2024-01-02", periods=5, freq="B")
    panel = pd.DataFrame(
        {"000001": [10.0, 9.7, 9.0, 8.5, 8.0]},
        index=[d.strftime("%Y-%m-%d") for d in idx],
    )

    result = run_equal_weight_rebalance(
        panel,
        rebalance_dates=[panel.index[0]],
        select_holdings=lambda *_: ["000001"],
        initial_cash=10_000,
        commission=0.0,
        min_commission=0.0,
        slippage=0.0,
        lot_size=100,
        stop_loss_pct=0.10,
    )
    sells = [t for t in result.trades if t["side"] == "sell"]
    assert len(sells) == 1
    assert sells[0]["reason"] == "stop_loss"
    assert sells[0]["date"] == panel.index[2]
    assert sells[0]["price"] == 9.0


def test_take_profit_requires_both_params():
    idx = pd.date_range("2024-01-02", periods=3, freq="B")
    panel = pd.DataFrame(
        {"000001": [10.0, 11.0, 12.0]},
        index=[d.strftime("%Y-%m-%d") for d in idx],
    )
    try:
        run_equal_weight_rebalance(
            panel,
            rebalance_dates=[panel.index[0]],
            select_holdings=lambda *_: ["000001"],
            initial_cash=10_000,
            commission=0.0,
            min_commission=0.0,
            take_profit_arm_pct=0.2,
        )
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "take_profit" in str(exc)
