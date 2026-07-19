"""组合回测交互 HTML 报告测试（不访问外网）。"""

from __future__ import annotations

from pathlib import Path

from app.cli_portfolio_report import build_portfolio_report_model, render_portfolio_html


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
            "sharpe": 1.2,
            "num_trades": 4.0,
            "win_rate": 1.0,
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
    model = build_portfolio_report_model(_mini_out())
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


def test_render_portfolio_html(tmp_path: Path):
    dest = tmp_path / "report.html"
    path = render_portfolio_html(_mini_out(), dest)
    assert path == dest.resolve()
    text = path.read_text(encoding="utf-8")
    assert "组合回测报告" in text
    assert "small_cap_zz399101" in text
    assert "000001" in text
    assert "平安银行" in text
    assert "const DATA =" in text
    assert "period_return" in text
