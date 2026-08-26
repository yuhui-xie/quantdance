"""共享资金回测引擎测试。"""

from __future__ import annotations

import pandas as pd
import pytest

from app.backtest.risk_control import RiskControl
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


def test_tiered_stop_loss_scales_out_in_steps():
    # 建仓 1.00，逐级跌 10%：-10%/-20%/-30% 各卖 1/3，而非一次性清仓
    panel = _panel([10, 9.0, 8.0, 7.0])  # 相对 10 成本：-10%, -20%, -30%
    result = run_shared_backtest(
        panel,
        targets_by_date={panel.index[0]: ["000001"]},
        initial_cash=10_000,
        commission=0,
        min_commission=0,
        slippage=0,
        lot_size=1,
        stop_loss_tier_pct=0.10,
        stop_loss_tier_sell_fraction=1 / 3,
    )
    tiered = [t for t in result.trades if t["reason"] == "tiered_stop"]
    # 3 档各触发一次，每档按建仓基准(1000 股)的 1/3=333 股卖出，累计 999，仅余 1 股尘埃
    assert len(tiered) == 3
    assert all(t["side"] == "sell" for t in tiered)
    assert [int(t["shares"]) for t in tiered] == [333, 333, 333]
    assert sum(int(t["shares"]) for t in tiered) == 999
    # 剩余尘埃由下一次调仓清理，而非一次性全仓止损清掉
    assert len([t for t in result.trades if t["reason"] == "stop_loss"]) == 0


def test_tiered_stop_loss_peak_anchor_protects_rally_gains():
    # 建仓后先涨到 +50% 再回落：峰值锚定应在从最高点回落 10%/20% 时逐档减仓，
    # 而非等跌破成本（成本锚定此时永远不触发）
    panel = _panel([10, 12, 15, 13.5, 12.0])  # peak=15, 回落 -10%→13.5, -20%→12.0
    result = run_shared_backtest(
        panel,
        targets_by_date={panel.index[0]: ["000001"]},
        initial_cash=10_000,
        commission=0,
        min_commission=0,
        slippage=0,
        lot_size=1,
        stop_loss_tier_pct=0.10,
        stop_loss_tier_sell_fraction=0.5,
        stop_loss_tier_anchor="peak",
    )
    tiered = [t for t in result.trades if t["reason"] == "tiered_stop"]
    # peak=15：13.5 触发第一档、12.0 触发第二档（均远高于成本 10，成本锚定不会触发）
    assert len(tiered) == 2
    assert [t["price"] for t in tiered] == [13.5, 12.0]


def test_tiered_stop_loss_requires_both_fields():
    panel = _panel([10, 9])
    with pytest.raises(ValueError, match="stop_loss_tier"):
        run_shared_backtest(
            panel,
            targets_by_date={panel.index[0]: ["000001"]},
            initial_cash=10_000,
            stop_loss_tier_pct=0.1,
        )


def test_take_profit_requires_both_thresholds():
    panel = _panel([10, 11, 12])
    with pytest.raises(ValueError, match="take_profit"):
        run_shared_backtest(
            panel,
            targets_by_date={panel.index[0]: ["000001"]},
            initial_cash=10_000,
            take_profit_arm_pct=0.2,
        )


def test_win_rate_reflects_realized_pnl_not_account_cash():
    # A 盈利、B 亏损，两回合各持一手。真实胜率应为 0.5。
    # 旧实现用卖出后的账户级 cash_after 估算回合盈亏，B 的回合会被
    # A 的到账现金垫成盈利，导致胜率被系统性抬高。
    panel = pd.DataFrame(
        {"000001": [10.0, 11.0], "000002": [10.0, 9.0]},
        index=["2024-01-02", "2024-01-03"],
    )
    result = run_shared_backtest(
        panel,
        targets_by_date={"2024-01-02": ["000001", "000002"], "2024-01-03": []},
        initial_cash=10_000,
        commission=0,
        min_commission=0,
        slippage=0,
        lot_size=1,
    )
    m = result.metrics
    assert m["closed_trades"] == 2
    assert m["win_rate"] == 0.5
    assert m["avg_win"] > 0 and m["avg_loss"] < 0


def test_risk_control_reusable_component_drives_exits():
    # 独立复用：不经过引擎，直接逐日喂价，验证状态机与出场动作。
    risk = RiskControl(
        stop_loss_tier_pct=0.10,
        stop_loss_tier_sell_fraction=0.5,
        stop_loss_tier_anchor="peak",
    )
    risk.on_entry("510300", shares=1000, price=10.0)
    actions = []
    for price in [12, 15, 13.5, 12.0]:
        action = risk.evaluate("510300", price, cost=10.0)
        if action is not None:
            actions.append((price, action))
    # peak=15：回落 -10%→13.5 卖一档、-20%→12.0 再卖一档
    assert [(p, a.kind, a.reason, a.fraction) for p, a in actions] == [
        (13.5, "fraction", "tiered_stop", 0.5),
        (12.0, "fraction", "tiered_stop", 0.5),
    ]
    assert risk.entry_shares("510300") == 1000
    risk.on_close("510300")
    assert risk.has("510300") is False
    assert risk.entry_shares("510300") == 0


def test_risk_control_move_to_break_even_single_stop():
    risk = RiskControl(stop_loss_pct=0.1)
    risk.on_entry("000001", shares=100, price=10.0)
    # 高于成本不触发，跌破成本 10% 触发全仓止损
    assert risk.evaluate("000001", 9.5, 10.0) is None
    exit_ = risk.evaluate("000001", 8.9, 10.0)
    assert exit_ is not None and exit_.kind == "full" and exit_.reason == "stop_loss"


def test_risk_control_moving_take_profit_requires_exit_pullback():
    risk = RiskControl(take_profit_arm_pct=0.2, take_profit_exit_pct=0.1)
    risk.on_entry("000001", shares=100, price=10.0)
    # 未到 +20% 不触发
    assert risk.evaluate("000001", 11.0, 10.0) is None
    # 抵达 +20% 只挂标记，不立即卖
    assert risk.evaluate("000001", 12.0, 10.0) is None
    assert risk.has("000001")
    # 回落到 +10% 才兑现
    exit_ = risk.evaluate("000001", 11.0, 10.0)
    assert exit_ is not None and exit_.kind == "full" and exit_.reason == "take_profit"


def test_risk_control_validates_parameters():
    with pytest.raises(ValueError, match="take_profit"):
        RiskControl(take_profit_arm_pct=0.2)
    with pytest.raises(ValueError, match="stop_loss_tier"):
        RiskControl(stop_loss_tier_pct=0.1)
    with pytest.raises(ValueError, match="stop_loss_tier"):
        RiskControl(stop_loss_tier_pct=0.1, stop_loss_tier_sell_fraction=0)
    assert RiskControl().active() is False


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
