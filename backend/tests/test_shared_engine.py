"""共享资金回测引擎测试。"""

from __future__ import annotations

import pandas as pd
import pytest

from app.backtest.shared_engine import build_close_panel, run_shared_backtest
from app.position_management import PositionManagementPolicy
from app.schemas import BacktestRequest


def _panel(values: list[float]) -> pd.DataFrame:
    days = pd.date_range("2024-01-02", periods=len(values), freq="B")
    return pd.DataFrame(
        {"000001": values},
        index=[day.strftime("%Y-%m-%d") for day in days],
    )


def test_backtest_request_uses_unified_cross_section_fields():
    request = BacktestRequest(
        strategy_id="market_auntie",
        mode="universe",
        start_date="2024-01-01",
        end_date="2024-12-31",
    )
    assert request.universe == "all_a"
    assert not hasattr(request, "rebalance_freq")


def test_build_close_panel_from_value_frames():
    first = pd.DataFrame(
        {"date": ["2024-01-02", "2024-01-03"], "close": [1.0, 1.1]}
    )
    second = pd.DataFrame(
        {"date": ["2024-01-02", "2024-01-03"], "close": [2.0, 2.2]}
    )
    panel = build_close_panel({"000001": first, "000002": second})
    assert list(panel.columns) == ["000001", "000002"]
    assert panel.loc["2024-01-03", "000001"] == 1.1


def test_shared_backtest_buys_and_marks_equity():
    panel = pd.concat([_panel([10, 10.5, 11]), _panel([5, 5.1, 5.2])], axis=1)
    panel.columns = ["000001", "000002"]
    result = run_shared_backtest(
        panel,
        targets_by_date={panel.index[0]: ["000001", "000002"]},
        initial_cash=20_000,
        commission=0,
        min_commission=0,
        slippage=0,
        lot_size=100,
    )
    assert result.metrics["num_trades"] >= 2
    assert result.rebalances[0]["targets"] == ["000001", "000002"]
    assert result.equity[-1]["equity"] > 0


def test_take_profit_and_stop_loss_paths():
    profit = _panel([10, 11, 12, 12.5, 10.5])
    result = run_shared_backtest(
        profit,
        targets_by_date={profit.index[0]: ["000001"]},
        initial_cash=10_000,
        commission=0,
        min_commission=0,
        lot_size=100,
        take_profit_arm_pct=0.2,
        take_profit_exit_pct=0.1,
    )
    assert [trade["reason"] for trade in result.trades if trade["side"] == "sell"] == [
        "take_profit"
    ]

    loss = _panel([10, 9.7, 9.0])
    stopped = run_shared_backtest(
        loss,
        targets_by_date={loss.index[0]: ["000001"]},
        initial_cash=10_000,
        commission=0,
        min_commission=0,
        lot_size=100,
        stop_loss_pct=0.1,
    )
    assert stopped.trades[-1]["reason"] == "stop_loss"


def test_take_profit_requires_both_thresholds():
    panel = _panel([10, 11, 12])
    with pytest.raises(ValueError, match="take_profit"):
        run_shared_backtest(
            panel,
            targets_by_date={panel.index[0]: ["000001"]},
            initial_cash=10_000,
            take_profit_arm_pct=0.2,
        )


def test_progressive_position_management_adds_and_exits():
    panel = _panel([10, 11, 12, 13, 10])
    policy = PositionManagementPolicy(
        max_positions=1,
        initial_allocation_pct=0.4,
        add_allocation_pct=0.2,
        add_trigger_pct=0.1,
        stop_loss_pct=None,
        take_profit_mode="trailing",
        take_profit_pct=0.1,
        trailing_drawdown_pct=0.1,
    )
    result = run_shared_backtest(
        panel,
        targets_by_date={panel.index[0]: ["000001"]},
        initial_cash=10_000,
        commission=0,
        min_commission=0,
        slippage=0,
        lot_size=1,
        position_policy=policy,
    )
    reasons = [trade["reason"] for trade in result.trades]
    assert "initial_entry" in reasons
    assert "pyramid_add" in reasons
    assert "take_profit" in reasons
