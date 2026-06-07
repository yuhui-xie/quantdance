"""backtest_runner 与策略插件协作测试。"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.backtest_runner import params_for_strategy, run_backtest_request
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
