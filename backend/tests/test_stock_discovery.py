"""策略回测驱动的股票发现测试（不访问外网）。"""

from __future__ import annotations

from typing import Any

import pandas as pd
from pydantic import BaseModel

from app.backtest_engine import BacktestResult, run_from_signals
from app.schemas import DiscoveryRequest
from app.stock_discovery import run_discovery
from app.strategies.base import BaseBacktestParams, StrategySpec


class _Params(BaseModel):
    lookback: int = 10


def _ohlcv(close: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": close,
            "high": [x + 0.2 for x in close],
            "low": [x - 0.2 for x in close],
            "close": close,
            "volume": [1_000_000.0] * len(close),
        },
        index=pd.date_range("2024-01-01", periods=len(close), freq="B"),
    )


def _fake_run(df: pd.DataFrame, base: BaseBacktestParams, _params: BaseModel) -> BacktestResult:
    total_return = float(df["close"].iloc[-1] / df["close"].iloc[0] - 1.0)
    final_equity = base.initial_cash * (1.0 + total_return)
    trades: list[dict[str, Any]] = []
    if total_return > 0:
        trades = [
            {
                "date": df.index[0].date().isoformat(),
                "side": "buy",
                "price": float(df["close"].iloc[0]),
                "shares": 100.0,
                "cash_after": 0.0,
            },
            {
                "date": df.index[-1].date().isoformat(),
                "side": "sell",
                "price": float(df["close"].iloc[-1]),
                "shares": 100.0,
                "cash_after": float(df["close"].iloc[-1] * 100.0),
            },
        ]
    return BacktestResult(
        equity=[
            {"date": df.index[0].date().isoformat(), "equity": base.initial_cash},
            {"date": df.index[-1].date().isoformat(), "equity": final_equity},
        ],
        trades=trades,
        metrics={
            "initial_cash": base.initial_cash,
            "final_equity": final_equity,
            "total_return": total_return,
            "annualized_return": total_return,
            "max_drawdown": 0.05 if total_return > 0 else 0.2,
            "sharpe": 1.5 if total_return > 0 else -0.5,
            "num_trades": float(len(trades)),
            "closed_trades": 1.0 if trades else 0.0,
            "win_rate": 1.0 if total_return > 0 else 0.0,
            "profit_factor": 2.0 if total_return > 0 else 0.0,
            "return_drawdown_ratio": total_return / 0.05 if total_return > 0 else 0.0,
        },
        price=[],
    )


def _fake_strategy() -> StrategySpec:
    return StrategySpec(
        id="fake_strategy",
        name="Fake",
        description="test strategy",
        params_model=_Params,
        min_bars=lambda _params: 50,
        run=_fake_run,
    )


def test_run_discovery_ranks_profitable_candidates(monkeypatch):
    prices = {
        "000001": _ohlcv([10.0 + i * 0.1 for i in range(260)]),
        "000002": _ohlcv([20.0 - i * 0.02 for i in range(260)]),
    }

    monkeypatch.setattr("app.stock_discovery.get_strategy", lambda _sid: _fake_strategy())
    monkeypatch.setattr("app.stock_discovery.fetch_a_share_daily", lambda symbol, **_kw: prices[symbol])

    resp = run_discovery(
        DiscoveryRequest(
            symbols=["000001", "000002"],
            strategies=["fake_strategy"],
            top_k=5,
            min_bars=120,
            min_trades=1,
            min_total_return=0.0,
            robustness_windows=[126, 252],
        )
    )

    assert resp.universe_note == "使用请求传入股票池，共 2 只。"
    assert [item.symbol for item in resp.candidates] == ["000001"]
    assert resp.candidates[0].robustness["tested_windows"] == 2.0
    assert resp.candidates[0].robustness["tested_param_sets"] == 2.0
    assert resp.candidates[0].robustness["pass_rate"] == 1.0
    assert resp.candidates[0].latest_signal == "hold_cash"
    assert resp.summary["run_count"] == 1.0
    assert resp.summary["average_total_return"] == resp.candidates[0].metrics["total_return"]


def test_run_discovery_applies_prefilter_and_universe(monkeypatch):
    monkeypatch.setattr(
        "app.stock_discovery.fetch_a_share_universe",
        lambda max_universe, seed=None: (
            [{"symbol": "000001", "name": "A"}, {"symbol": "000002", "name": "B"}],
            f"mock universe {max_universe} {seed}",
        ),
    )
    monkeypatch.setattr("app.stock_discovery.get_strategy", lambda _sid: _fake_strategy())
    monkeypatch.setattr(
        "app.stock_discovery.fetch_a_share_daily",
        lambda symbol, **_kw: _ohlcv([10.0] * (130 if symbol == "000001" else 40)),
    )

    resp = run_discovery(
        DiscoveryRequest(
            strategies=["fake_strategy"],
            max_universe=2,
            min_bars=120,
            min_trades=0,
            robustness_windows=[],
        )
    )

    assert resp.universe_note == "mock universe 2 None"
    assert any("K 线不足" in warning for warning in resp.warnings)


def test_run_discovery_hs300_adds_equal_weight_benchmark(monkeypatch):
    prices = {
        "000001": _ohlcv([10.0 + i * 0.1 for i in range(260)]),
        "000002": _ohlcv([20.0 for _ in range(260)]),
    }

    monkeypatch.setattr("app.stock_discovery.get_strategy", lambda _sid: _fake_strategy())
    monkeypatch.setattr("app.stock_discovery.fetch_a_share_daily", lambda symbol, **_kw: prices[symbol])
    monkeypatch.setattr(
        "app.stock_discovery.fetch_hs300_universe",
        lambda max_universe, seed=None: (
            [{"symbol": "000001", "name": "A"}, {"symbol": "000002", "name": "B"}],
            "mock hs300",
        ),
    )

    resp = run_discovery(
        DiscoveryRequest(
            universe="hs300",
            strategies=["fake_strategy"],
            top_k=2,
            min_bars=120,
            min_trades=0,
            robustness_windows=[],
            param_perturbation_pct=0.0,
        )
    )

    benchmark = resp.benchmarks["hs300_equal_weight"]
    strategy_curve = resp.benchmarks["strategy_equal_weight"]
    assert resp.universe_note == "mock hs300"
    assert strategy_curve["metrics"]["member_count"] == 2.0
    assert strategy_curve["metrics"]["total_return"] > benchmark["metrics"]["total_return"]
    assert benchmark["metrics"]["member_count"] == 2.0
    assert benchmark["metrics"]["total_return"] > 0.0
    assert "benchmark_total_return" in resp.candidates[0].metrics
    assert "excess_total_return" in resp.candidates[0].metrics
    assert resp.candidates[0].metrics["excess_total_return"] > 0.0
    assert resp.summary["run_count"] == 2.0
    assert resp.summary["average_total_return"] > 0.0
    assert "average_excess_total_return" in resp.summary


def test_run_discovery_applies_fundamental_filters(monkeypatch):
    prices = {
        "000001": _ohlcv([10.0 + i * 0.1 for i in range(260)]),
        "000002": _ohlcv([12.0 + i * 0.1 for i in range(260)]),
    }
    valuations = {
        "000001": {"pe_ttm": 12.0, "pb": 1.2, "market_cap": 100_000_000_000.0},
        "000002": {"pe_ttm": 80.0, "pb": 8.0, "market_cap": 200_000_000_000.0},
    }

    monkeypatch.setattr("app.stock_discovery.get_strategy", lambda _sid: _fake_strategy())
    monkeypatch.setattr("app.stock_discovery.fetch_a_share_daily", lambda symbol, **_kw: prices[symbol])
    monkeypatch.setattr(
        "app.stock_discovery.fetch_a_share_valuation_snapshot",
        lambda symbol: valuations[symbol],
    )

    resp = run_discovery(
        DiscoveryRequest(
            symbols=["000001", "000002"],
            strategies=["fake_strategy"],
            top_k=5,
            min_trades=1,
            max_pe_ttm=30.0,
            max_pb=3.0,
            robustness_windows=[],
            param_perturbation_pct=0.0,
        )
    )

    assert [item.symbol for item in resp.candidates] == ["000001"]
    assert resp.candidates[0].filters["pe_ttm"] == 12.0
    assert resp.candidates[0].filters["pb"] == 1.2
    assert any("pe_ttm 高于阈值" in warning for warning in resp.warnings)


def test_backtest_metrics_include_trade_quality_fields():
    result = run_from_signals(
        _ohlcv([10.0, 12.0, 11.0, 13.0]),
        signal=[1, -1, 1, -1],
        initial_cash=1000.0,
        commission=0.0,
    )

    assert result.metrics["annualized_return"] != 0.0
    assert result.metrics["closed_trades"] == 2.0
    assert result.metrics["win_rate"] == 1.0
    assert result.metrics["profit_factor"] > 0.0
    assert "return_drawdown_ratio" in result.metrics
