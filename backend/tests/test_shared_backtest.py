"""共享账户引擎与横截面编排测试。"""

from __future__ import annotations

import pandas as pd
from pydantic import BaseModel

from app.backtest.cross_section_runner import run_cross_section_backtest
from app.backtest.shared_engine import run_shared_backtest
from app.backtest_runner import run_backtest_request
from app.schemas import BacktestRequest, BacktestSharedResponse
from app.strategies.base import CrossSectionStrategySpec


def _prices() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "A": [10.0, 11.0, 12.0, 13.0],
            "B": [20.0, 19.0, 18.0, 17.0],
        },
        index=["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"],
    )


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
