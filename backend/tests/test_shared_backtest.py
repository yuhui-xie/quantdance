"""共享账户引擎与横截面编排测试。"""

from __future__ import annotations

import pandas as pd
from pydantic import BaseModel

from app.backtest.cross_section_runner import (
    deprecated_decision_interval_warning,
    run_cross_section_backtest,
)
from app.backtest.shared_engine import run_shared_backtest
from app.backtest_runner import run_backtest_request
from app.schemas import BacktestRequest, BacktestSharedResponse
from app.strategies.base import CrossSectionStrategySpec
from app.strategies.cross_section.common import apply_hysteresis


def _prices() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "A": [10.0, 11.0, 12.0, 13.0],
            "B": [20.0, 19.0, 18.0, 17.0],
        },
        index=["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"],
    )


def _prices_abc() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "A": [10.0, 11.0, 12.0, 13.0],
            "B": [20.0, 19.0, 18.0, 17.0],
            "C": [5.0, 5.5, 6.0, 6.5],
        },
        index=["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"],
    )


def test_incremental_rebalance_keeps_shared_positions():
    result = run_shared_backtest(
        _prices_abc(),
        targets_by_date={
            "2024-01-02": ["A", "B"],
            "2024-01-04": ["A", "C"],
        },
        initial_cash=10_000,
        commission=0,
        min_commission=0,
        slippage=0,
        lot_size=1,
        rebalance_mode="incremental",
    )

    sells = [t for t in result.trades if t["side"] == "sell"]
    buys = [t for t in result.trades if t["side"] == "buy"]
    # 共同持仓 A 不卖出；只卖掉落出名单的 B、买进新进的 C
    assert [t["symbol"] for t in sells] == ["B"]
    assert [t["symbol"] for t in buys] == ["A", "B", "C"]
    assert sum(t["symbol"] == "A" for t in buys) == 1  # A 只在首日建仓，未重复买入


def test_full_rebalance_liquidates_all_on_change():
    result = run_shared_backtest(
        _prices_abc(),
        targets_by_date={
            "2024-01-02": ["A", "B"],
            "2024-01-04": ["A", "C"],
        },
        initial_cash=10_000,
        commission=0,
        min_commission=0,
        slippage=0,
        lot_size=1,
        rebalance_mode="full",
    )

    sells = [t for t in result.trades if t["side"] == "sell"]
    assert [t["symbol"] for t in sells] == ["A", "B"]  # 全仓清空重建


def test_incremental_rebalance_unchanged_target_no_trades():
    result = run_shared_backtest(
        _prices_abc(),
        targets_by_date={
            "2024-01-02": ["A", "B"],
            "2024-01-04": ["A", "B"],
        },
        initial_cash=10_000,
        commission=0,
        min_commission=0,
        slippage=0,
        lot_size=1,
        rebalance_mode="incremental",
    )
    assert not [t for t in result.trades if t["date"] == "2024-01-04"]


def test_cross_section_runner_hysteresis_stabilizes_targets(monkeypatch):
    """验证 runner→select(hysteresis via ctx.cache)→增量引擎 的完整链路。"""

    def select(asof, ctx, params):  # noqa: ANN001
        order = ["A", "B", "C"] if asof == "2024-01-02" else ["B", "C", "A"]
        cands = [{"symbol": s, "score": float(10 - i)} for i, s in enumerate(order)]
        symbols, details = apply_hysteresis(ctx, cands, top_n=2, threshold_rank=1)
        return symbols, details

    def decision_dates(calendar, _ctx, _params):  # noqa: ANN001
        return list(calendar)

    spec = CrossSectionStrategySpec(
        id="hysteresis_runner",
        name="test",
        description="test",
        params_model=_Params,
        select=select,
        decision_dates=decision_dates,
        needs_fundamentals=False,
        requires_symbols=True,
    )
    value_a = pd.DataFrame({
        "date": ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"],
        "close": [10, 11, 12, 13, 14],
    })
    value_b = pd.DataFrame({
        "date": ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"],
        "close": [20, 19, 18, 17, 16],
    })
    value_c = pd.DataFrame({
        "date": ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"],
        "close": [5, 5.5, 6, 6.5, 7],
    })
    monkeypatch.setattr(
        "app.backtest.cross_section_runner._resolve_universe",
        lambda *_args: (
            [
                {"symbol": "A", "name": "A"},
                {"symbol": "B", "name": "B"},
                {"symbol": "C", "name": "C"},
            ],
            "test",
        ),
    )
    monkeypatch.setattr(
        "app.backtest.cross_section_runner._load_panel",
        lambda *_args, **_kwargs: {
            "A": {"value": value_a},
            "B": {"value": value_b},
            "C": {"value": value_c},
        },
    )
    request = BacktestRequest(
        strategy_id="hysteresis_runner",
        symbols=["A", "B", "C"],
        start_date="2024-01-02",
        end_date="2024-01-08",
        initial_cash=10_000,
        commission=0,
        min_commission=0,
        slippage=0,
        lot_size=1,
        rebalance_mode="incremental",
        strategy_params={"top_n": 2},
    )

    result = run_cross_section_backtest(request, spec)

    # 第 1 天选 [A,B]；第 2 天起 A 掉出 top-2、候选 C 领先 1 位达到阈值 → 换入 C
    assert [item["targets"] for item in result.rebalances] == [
        ["A", "B"],
        ["B", "C"],
        ["B", "C"],
        ["B", "C"],
        ["B", "C"],
    ]
    # 增量换仓：只卖 A、买 C，共同持仓 B 不重复交易
    sells = [t for t in result.trades if t["side"] == "sell"]
    assert [t["symbol"] for t in sells] == ["A"]
    assert sum(t["symbol"] == "B" for t in result.trades if t["side"] == "buy") == 1


def test_target_mapping_only_rebalances_on_decision_dates():
    result = run_shared_backtest(
        _prices(),
        targets_by_date={
            "2024-01-02": ["A"],
            "2024-01-04": ["B"],
        },
        initial_cash=10_000,
        commission=0,
        min_commission=0,
        slippage=0,
        lot_size=1,
    )

    assert [item["date"] for item in result.rebalances] == [
        "2024-01-02",
        "2024-01-04",
    ]
    assert not [trade for trade in result.trades if trade["date"] == "2024-01-03"]
    assert result.rebalances[-1]["targets"] == ["B"]


def test_unchanged_target_records_decision_without_retrading():
    result = run_shared_backtest(
        _prices(),
        targets_by_date={
            "2024-01-02": ["A"],
            "2024-01-04": ["A"],
        },
        initial_cash=10_000,
        commission=0,
        min_commission=0,
        slippage=0,
        lot_size=1,
    )

    assert [item["date"] for item in result.rebalances] == [
        "2024-01-02",
        "2024-01-04",
    ]
    assert not [trade for trade in result.trades if trade["date"] == "2024-01-04"]


def test_shared_cash_never_overspends_with_minimum_commission():
    result = run_shared_backtest(
        _prices(),
        targets_by_date={"2024-01-02": ["A", "B"]},
        initial_cash=1_000,
        commission=0.001,
        min_commission=5,
        slippage=0.01,
        lot_size=10,
    )

    assert result.trades
    assert all(trade["cash_after"] >= -1e-9 for trade in result.trades)
    assert result.rebalances[0]["cash"] >= -1e-9


def test_deprecated_decision_interval_surfaces_warning():
    old = BacktestRequest(
        strategy_id="market_auntie",
        start_date="2024-01-01",
        end_date="2024-12-31",
        strategy_params={"decision_interval": 20, "top_n": 2},
    )
    warning = deprecated_decision_interval_warning(old)
    assert warning is not None
    assert "decision_frequency" in warning

    # 新写法（decision_frequency）不再触发弃用警告
    new = BacktestRequest(
        strategy_id="market_auntie",
        start_date="2024-01-01",
        end_date="2024-12-31",
        strategy_params={"decision_frequency": "monthly", "top_n": 2},
    )
    assert deprecated_decision_interval_warning(new) is None


class _Params(BaseModel):
    top_n: int = 1


def test_cross_section_runner_calls_strategy_decision_dates(monkeypatch):
    calls: list[list[str]] = []
    selections: list[str] = []

    def decision_dates(calendar, _context, _params):  # noqa: ANN001
        calls.append(list(calendar))
        return [calendar[0], calendar[2]]

    def select(asof, _context, _params):  # noqa: ANN001
        selections.append(asof)
        symbol = "A" if len(selections) == 1 else "B"
        return [symbol], [{"symbol": symbol, "score": 1.0}]

    spec = CrossSectionStrategySpec(
        id="cross_test",
        name="test",
        description="test",
        params_model=_Params,
        select=select,
        decision_dates=decision_dates,
        needs_fundamentals=False,
        requires_symbols=True,
    )
    value_a = pd.DataFrame({
        "date": ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"],
        "close": [10, 11, 12, 13, 14],
    })
    value_b = pd.DataFrame({
        "date": ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"],
        "close": [20, 19, 18, 17, 16],
    })
    monkeypatch.setattr(
        "app.backtest.cross_section_runner._resolve_universe",
        lambda *_args: ([{"symbol": "A", "name": "A"}, {"symbol": "B", "name": "B"}], "test"),
    )
    monkeypatch.setattr(
        "app.backtest.cross_section_runner._load_panel",
        lambda *_args, **_kwargs: {"A": {"value": value_a}, "B": {"value": value_b}},
    )
    request = BacktestRequest(
        strategy_id="cross_test",
        symbols=["A", "B"],
        start_date="2024-01-02",
        end_date="2024-01-08",
        initial_cash=10_000,
        commission=0,
        min_commission=0,
        slippage=0,
        lot_size=1,
        strategy_params={"top_n": 1},
    )

    result = run_cross_section_backtest(request, spec)

    assert len(calls) == 1
    assert selections == ["2024-01-02", "2024-01-04"]
    assert [item["targets"] for item in result.rebalances] == [["A"], ["B"]]


def test_unified_backtest_runner_routes_cross_section_to_shared_runner(monkeypatch):
    spec = CrossSectionStrategySpec(
        id="cross_route",
        name="test",
        description="test",
        params_model=_Params,
        select=lambda *_args: ([], []),
        decision_dates=lambda *_args: [],
    )
    sentinel = BacktestSharedResponse(strategy_id="cross_route", equity=[])
    monkeypatch.setattr(
        "app.backtest_runner.get_registered_strategy",
        lambda _strategy_id: spec,
    )
    monkeypatch.setattr(
        "app.backtest_runner.run_cross_section_backtest",
        lambda request, received_spec: (
            sentinel
            if request.strategy_id == "cross_route" and received_spec is spec
            else {}
        ),
    )

    response = run_backtest_request(
        BacktestRequest(
            strategy_id="cross_route",
            mode="universe",
            start_date="2024-01-01",
            end_date="2024-01-31",
        )
    )

    assert response == sentinel.model_dump(mode="json")
