"""backtest_engine 通用执行层测试。"""

from __future__ import annotations

import pandas as pd

from app.backtest_engine import run_from_signals


def _ohlcv(close: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": close,
            "high": [price + 0.5 for price in close],
            "low": [price - 0.5 for price in close],
            "close": close,
            "volume": [1000.0] * len(close),
        },
        index=pd.date_range("2024-01-01", periods=len(close), freq="D"),
    )


def test_run_from_signals_executes_full_position_round_trip():
    result = run_from_signals(
        _ohlcv([10.0, 12.0, 11.0]),
        signal=[0, 1, -1],
        initial_cash=100.0,
        commission=0.1,
        overlays={"custom_indicator": [None, 1.5, 2.5]},
    )

    assert [trade["side"] for trade in result.trades] == ["buy", "sell"]
    assert result.trades[0]["shares"] == 7.5
    assert result.trades[1]["cash_after"] == 74.25
    assert result.metrics["final_equity"] == 74.25
    assert result.equity[-1]["equity"] == 74.25
    assert result.price[0]["custom_indicator"] is None
    assert result.price[1]["custom_indicator"] == 1.5
