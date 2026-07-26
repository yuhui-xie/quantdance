"""组合回测交互 HTML 报告测试（不访问外网）。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from app.cli_portfolio_report import (
    _bars_cover_trade_dates,
    _load_symbol_bars,
    build_portfolio_report_model,
    render_portfolio_html,
)


def _mini_out() -> dict:
    return {
        "strategy_id": "small_cap_zz399101",
        "mode": "backtest",
        "asof": "2020-03-01",
        "universe_note": "测试股票池",
        "warnings": ["demo"],
        "disclaimer": "测试",
        "metrics": {
            "initial_cash": 100000.0,
            "final_equity": 110000.0,
            "total_return": 0.1,
            "annualized_return": 0.2,
            "max_drawdown": 0.05,
            "return_drawdown_ratio": 2.0,
            "sharpe": 1.2,
            "num_trades": 4.0,
            "win_rate": 1.0,
            "benchmark_total_return": 0.04,
            "excess_total_return": 0.06,
        },
        "benchmarks": {
            "hs300": {
                "name": "沪深300",
                "metrics": {
                    "initial_cash": 100000.0,
                    "total_return": 0.04,
                    "max_drawdown": 0.02,
                },
                "equity": [
                    {"date": "2020-01-02", "equity": 100000.0, "nav": 1.0},
                    {"date": "2020-01-31", "equity": 102000.0, "nav": 1.02},
                    {"date": "2020-02-28", "equity": 104000.0, "nav": 1.04},
                ],
            }
        },
        "equity": [
            {"date": "2020-01-02", "equity": 100000.0},
            {"date": "2020-01-31", "equity": 105000.0},
            {"date": "2020-02-28", "equity": 110000.0},
        ],
        "trades": [
            {
                "date": "2020-01-02",
                "symbol": "000001",
                "side": "buy",
                "price": 10.0,
                "shares": 1000.0,
                "cash_after": 90000.0,
                "cost": 5.0,
            },
            {
                "date": "2020-01-02",
                "symbol": "000002",
                "side": "buy",
                "price": 20.0,
                "shares": 500.0,
                "cash_after": 80000.0,
                "cost": 5.0,
            },
            {
                "date": "2020-01-31",
                "symbol": "000001",
                "side": "sell",
                "price": 11.0,
                "shares": 1000.0,
                "cash_after": 91000.0,
                "cost": 5.0,
            },
            {
                "date": "2020-01-31",
                "symbol": "000003",
                "side": "buy",
                "price": 15.0,
                "shares": 600.0,
                "cash_after": 82000.0,
                "cost": 5.0,
            },
        ],
        "rebalances": [
            {
                "date": "2020-01-02",
                "targets": ["000001", "000002"],
                "weights": {"000001": 0.5, "000002": 0.5},
                "cash": 80000.0,
                "selection": [
                    {"symbol": "000001", "name": "平安银行", "close": 10.0, "float_market_cap": 1e9},
                    {"symbol": "000002", "name": "万科A", "close": 20.0, "float_market_cap": 2e9},
                ],
            },
            {
                "date": "2020-01-31",
                "targets": ["000002", "000003"],
                "weights": {"000002": 0.5, "000003": 0.5},
                "cash": 82000.0,
                "selection": [
                    {"symbol": "000002", "name": "万科A", "close": 21.0, "float_market_cap": 2.1e9},
                    {"symbol": "000003", "name": "国农科技", "close": 15.0, "float_market_cap": 3e8},
                ],
            },
        ],
    }


def test_build_portfolio_report_model_periods_and_pnl():
    model = build_portfolio_report_model(_mini_out(), load_prices=False)
    assert model["strategy_id"] == "small_cap_zz399101"
    assert len(model["periods"]) == 2
    assert model["rebalance_dates"] == ["2020-01-02", "2020-01-31"]

    p0 = model["periods"][0]
    assert p0["date"] == "2020-01-02"
    assert p0["next_date"] == "2020-01-31"
    assert p0["buy_count"] == 2
    assert p0["sell_count"] == 0
    assert p0["equity_start"] == 100000.0
    assert p0["equity_end"] == 105000.0
    assert abs(p0["period_return"] - 0.05) < 1e-9
    assert {b["symbol"] for b in p0["buys"]} == {"000001", "000002"}
    assert p0["buys"][0]["name"] in {"平安银行", "万科A"}

    p1 = model["periods"][1]
    assert p1["sell_count"] == 1
    assert p1["buy_count"] == 1
    assert p1["sells"][0]["symbol"] == "000001"
    assert p1["sells"][0]["name"] == "平安银行"
    assert p1["next_date"] == "2020-02-28"
    assert abs(p1["period_return"] - (110000.0 - 105000.0) / 105000.0) < 1e-9

    assert len(model["trades"]) == 4
    assert len(model["round_trips"]) == 1
    trip = model["round_trips"][0]
    assert trip["symbol"] == "000001"
    assert trip["buy_date"] == "2020-01-02"
    assert trip["sell_date"] == "2020-01-31"
    assert abs(trip["pnl"] - 1000.0) < 1e-6
    assert abs(trip["pnl_pct"] - 0.1) < 1e-9

    assert model["equity"][0]["nav"] == 1.0
    assert abs(model["equity"][-1]["nav"] - 1.1) < 1e-9
    assert model["buy_dates"] == ["2020-01-02", "2020-01-31"]
    assert model["sell_dates"] == ["2020-01-31"]
    assert len(model["benchmark"]) == 3
    assert abs(model["benchmark"][-1]["nav"] - 1.04) < 1e-9
    assert abs(model["metrics"]["return_drawdown_ratio"] - 2.0) < 1e-9

    assert "000001" in model["by_symbol"]
    assert len(model["by_symbol"]["000001"]["trades"]) == 2
    assert {s["symbol"] for s in model["symbol_index"]} == {"000001", "000002", "000003"}


def test_render_portfolio_html(tmp_path: Path):
    dest = tmp_path / "report.html"
    path = render_portfolio_html(_mini_out(), dest, load_prices=False)
    assert path == dest.resolve()
    text = path.read_text(encoding="utf-8")
    assert "组合回测报告" in text
    assert "small_cap_zz399101" in text
    assert "000001" in text
    assert "平安银行" in text
    assert "const DATA =" in text
    assert "period_return" in text
    assert "收益/回撤" in text
    assert "沪深300" in text
    assert "buy_dates" in text
    assert "triUp" in text
    assert "openSymbol" in text
    assert "by_symbol" in text
    assert "个股列表" in text
    assert "zoomAroundTrades" in text
    assert "has_ohlc" in text
    assert "早于价格序列" in text


def test_bars_cover_trade_dates_rejects_late_cache():
    late_bars = [
        ["2023-04-06", 10.0, 11.0, 9.0, 10.5],
        ["2023-04-07", 10.5, 11.0, 10.0, 10.8],
    ]
    assert not _bars_cover_trade_dates(late_bars, ["2023-01-03", "2023-03-07"])
    early_bars = [
        ["2023-01-03", 10.0, 11.0, 9.0, 10.5],
        ["2023-03-07", 10.5, 11.0, 10.0, 10.8],
        ["2023-04-06", 10.8, 11.0, 10.0, 10.9],
    ]
    assert _bars_cover_trade_dates(early_bars, ["2023-01-03", "2023-03-07"])


def test_load_symbol_bars_backfills_when_cache_starts_late():
    """日 K 缓存晚于成交日时，应用估值收盘价回填，避免买卖点落到第 0 根。"""
    late_cache = [
        {
            "datetime": "2023-04-06 15:00",
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": 10.5,
        }
    ]
    closes = [
        ["2023-01-03", 12.0],
        ["2023-03-07", 13.0],
        ["2023-04-06", 10.5],
    ]
    with (
        patch("app.portfolio.report_model._read_stale_day_klines", return_value=late_cache),
        patch("app.portfolio.report_model._fetch_day_klines", return_value=[]),
        patch("app.portfolio.report_model._cache_only_closes", return_value=closes),
    ):
        bars, has_ohlc = _load_symbol_bars(
            "300606",
            "2023-01-01",
            "2023-04-30",
            fetch_missing=True,
            cover_dates=["2023-01-03", "2023-03-07"],
        )
    dates = [row[0] for row in bars]
    assert "2023-01-03" in dates
    assert "2023-03-07" in dates
    assert has_ohlc is True
