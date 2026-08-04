"""backtest_runner 与策略插件协作测试。"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest
from pydantic import BaseModel

from app.backtest_engine import BacktestResult
from app.backtest_runner import (
    params_for_strategy,
    run_backtest_request,
    run_backtest_universe_request,
)
from app.data_sources.a_stock_data import MarketKlineBar
from app.schemas import BacktestRequest
from app.strategies.registry import STRATEGIES


def test_all_registered_strategies_define_min_bars():
    expected = {
        "bollinger_reversion": 25,
        "donchian_breakout": 25,
        "ema_crossover": 30,
        "ma_crossover": 30,
        "macd": 40,
        "rsi_reversal": 19,
        "stochastic_cross": 25,
        "volume_ma_pulse": 26,
    }

    for strategy_id, spec in STRATEGIES.items():
        body = BacktestRequest(strategy_id=strategy_id)
        params = params_for_strategy(body, spec)
        min_bars = spec.min_bars(params)
        assert isinstance(min_bars, int)
        assert min_bars > 0
        if strategy_id in expected:
            assert min_bars == expected[strategy_id]


def test_run_backtest_uses_strategy_min_bars(monkeypatch):
    bars: list[MarketKlineBar] = [
        {
            "datetime": (date(2024, 1, 1) + timedelta(days=offset)).isoformat(),
            "open": 10.0,
            "high": 10.5,
            "low": 9.8,
            "close": 10.2,
            "volume": 100000,
            "amount": 1020000.0,
        }
        for offset in range(50)
    ]

    class _FakeAStockDataSDK:
        def get_klines(self, *_args, **_kwargs):  # noqa: ANN001
            return bars

    monkeypatch.setattr("app.data_sources.market_data.AStockDataSDK", _FakeAStockDataSDK)

    body = BacktestRequest(
        symbol="600000",
        strategy_id="macd",
        bars=50,
        strategy_params={"slow_period": 80, "signal_period": 20},
    )

    with pytest.raises(ValueError, match="至少需要 105 根"):
        run_backtest_request(body)


class _BatchParams(BaseModel):
    pass


class _BatchStrategy:
    id = "batch_test"
    params_model = _BatchParams

    @staticmethod
    def min_bars(_params):  # noqa: ANN001
        return 2

    @staticmethod
    def run(df, base, _params):  # noqa: ANN001
        final = base.initial_cash * float(df["factor"].iloc[-1])
        return BacktestResult(
            equity=[
                {"date": "2024-01-02", "equity": base.initial_cash},
                {"date": "2024-01-03", "equity": final},
            ],
            trades=[{"date": "2024-01-02", "side": "buy"}],
            metrics={
                "initial_cash": base.initial_cash,
                "final_equity": final,
                "total_return": final / base.initial_cash - 1.0,
                "max_drawdown": max(0.0, 1.0 - final / base.initial_cash),
                "sharpe": 0.0,
            },
            price=[{"date": "2024-01-02", "close": 10.0}],
        )


def test_universe_backtest_runs_independent_capital_and_keeps_order(monkeypatch):
    rows = [
        {"symbol": "600000", "name": "A"},
        {"symbol": "000001", "name": "B"},
        {"symbol": "300001", "name": "C"},
    ]
    monkeypatch.setattr("app.backtest_runner.get_strategy", lambda _sid: _BatchStrategy())
    monkeypatch.setattr(
        "app.backtest_runner.resolve_universe_rows",
        lambda **_kwargs: (rows, "测试股票池"),
    )

    def fake_load(symbol, _body):  # noqa: ANN001
        size = 1 if symbol == "300001" else 2
        factor = 1.1 if symbol == "600000" else 0.9
        return pd.DataFrame({"factor": [factor] * size})

    monkeypatch.setattr("app.backtest_runner.load_symbol_ohlcv", fake_load)
    body = BacktestRequest(
        mode="universe",
        strategy_id="batch_test",
        symbols=[row["symbol"] for row in rows],
        start_date="2024-01-01",
        end_date="2024-01-31",
        initial_cash=100_000,
        include_price=False,
        max_workers=2,
    )

    response = run_backtest_universe_request(body)

    assert [run.symbol for run in response.runs] == ["600000", "000001", "300001"]
    assert [run.status for run in response.runs] == ["ok", "ok", "skipped"]
    assert response.runs[0].metrics["initial_cash"] == 100_000
    assert response.runs[1].metrics["initial_cash"] == 100_000
    assert response.runs[0].rank == 1
    assert response.runs[1].rank == 2
    assert response.runs[0].price == []
    assert response.aggregate["metrics"]["total_return"] == pytest.approx(0.0)
    assert response.summary.succeeded == 2
    assert response.summary.skipped == 1


def test_universe_backtest_fails_when_no_symbol_can_run(monkeypatch):
    monkeypatch.setattr("app.backtest_runner.get_strategy", lambda _sid: _BatchStrategy())
    monkeypatch.setattr(
        "app.backtest_runner.resolve_universe_rows",
        lambda **_kwargs: ([{"symbol": "600000", "name": ""}], "测试股票池"),
    )
    monkeypatch.setattr(
        "app.backtest_runner.load_symbol_ohlcv",
        lambda _symbol, _body: pd.DataFrame({"factor": [1.0]}),
    )
    body = BacktestRequest(
        mode="universe",
        strategy_id="batch_test",
        symbols=["600000"],
        start_date="2024-01-01",
        end_date="2024-01-31",
    )

    with pytest.raises(ValueError, match="没有可用"):
        run_backtest_universe_request(body)
