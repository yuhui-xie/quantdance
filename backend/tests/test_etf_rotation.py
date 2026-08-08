"""ETF 动量轮动策略测试（不访问外网）。"""

from __future__ import annotations

import pandas as pd

from app.strategies.registry import get_cross_section_strategy
from app.backtest_runner import run_backtest_request
from app.schemas import BacktestRequest
from app.strategies.base import CrossSectionContext
from app.strategies.cross_section.etf_rotation import EtfRotationParams, select_etf_rotation


def _value_df(dates: list[str], closes: list[float]) -> pd.DataFrame:
    close = pd.Series(closes, dtype=float)
    return pd.DataFrame(
        {
            "date": dates,
            "close": close,
            "pct_change": close.pct_change().fillna(0.0) * 100.0,
        }
    )


def test_select_picks_strongest_positive_momentum():
    dates = pd.bdate_range("2024-01-02", periods=10).strftime("%Y-%m-%d").tolist()
    panel = {
        "510300": {"value": _value_df(dates, [10 + i * 0.1 for i in range(10)])},
        "510500": {"value": _value_df(dates, [10 + i * 0.2 for i in range(10)])},
        "518880": {"value": _value_df(dates, [10 - i * 0.1 for i in range(10)])},
    }

    symbols, details = select_etf_rotation(
        dates[-1],
        CrossSectionContext(panel=panel, names={}),
        EtfRotationParams(top_n=1, lookback_days=5, trend_days=3),
    )

    assert symbols == ["510500"]
    assert details[0]["momentum"] > 0


def test_select_holds_cash_when_no_etf_passes_filter():
    dates = pd.bdate_range("2024-01-02", periods=10).strftime("%Y-%m-%d").tolist()
    panel = {
        "510300": {"value": _value_df(dates, [10 - i * 0.1 for i in range(10)])},
        "510500": {"value": _value_df(dates, [9 - i * 0.1 for i in range(10)])},
    }

    symbols, details = select_etf_rotation(
        dates[-1],
        CrossSectionContext(panel=panel, names={}),
        EtfRotationParams(lookback_days=5, trend_days=3),
    )

    assert symbols == []
    assert details == []


def test_run_etf_rotation_backtest_with_market_data(monkeypatch):
    dates = pd.bdate_range("2024-01-02", periods=80)
    symbols = ["510300", "510500", "518880"]
    closes = {
        "510300": [10 + i * 0.02 for i in range(80)],
        "510500": [10 + i * 0.04 for i in range(80)],
        "518880": [10 + i * 0.01 for i in range(80)],
    }

    def fake_daily(symbol: str, **_kwargs) -> pd.DataFrame:
        return pd.DataFrame(
            {"close": closes[symbol]},
            index=dates,
        )

    monkeypatch.setattr("app.backtest.cross_section_runner.fetch_a_share_daily", fake_daily)
    monkeypatch.setattr(
        "app.backtest.cross_section_runner._resolve_universe",
        lambda _req, default_universe=None: (
            [{"symbol": symbol, "name": symbol} for symbol in symbols],
            "mock ETF pool",
        ),
    )
    monkeypatch.setattr(
        "app.backtest.cross_section_runner.attach_hs300_benchmark",
        lambda raw, **_kwargs: raw,
    )

    response = run_backtest_request(
        BacktestRequest(
            strategy_id="etf_rotation",
            mode="universe",
            symbols=symbols,
            start_date=dates[20].strftime("%Y-%m-%d"),
            end_date=dates[-1].strftime("%Y-%m-%d"),
            slippage=0.0,
            min_commission=0.0,
            strategy_params={
                "top_n": 1,
                "lookback_days": 10,
                "trend_days": 20,
            },
        )
    )

    assert get_cross_section_strategy("etf_rotation") is not None
    assert response["strategy_id"] == "etf_rotation"
    assert response["rebalances"]
    assert response["metrics"]["final_equity"] > 0
