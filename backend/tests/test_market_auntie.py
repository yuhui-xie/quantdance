"""菜场大妈选股与组合回测测试（不访问外网）。"""

from __future__ import annotations

import pandas as pd

from app.portfolio.base import PortfolioSelectContext
from app.portfolio.market_auntie import MarketAuntieParams, select_market_auntie
from app.portfolio.runner import run_portfolio_request
from app.schemas import PortfolioBacktestRequest


def _value_df(rows: list[tuple[str, float, float, float, float]]) -> pd.DataFrame:
    # date, close, market_cap, peg, pct_change
    return pd.DataFrame(
        [
            {
                "date": d,
                "close": c,
                "pct_change": pct,
                "market_cap": mv,
                "float_market_cap": mv,
                "pe_ttm": 10.0,
                "pb": 1.0,
                "peg": peg,
                "ps_ttm": 1.0,
            }
            for d, c, mv, peg, pct in rows
        ]
    )


def _div_df(ex_date: str, cash_per_share: float) -> pd.DataFrame:
    return pd.DataFrame([{"ex_date": ex_date, "cash_per_share": cash_per_share}])


def test_select_market_auntie_prefers_small_cap_quality():
    panel = {
        "AAA001": {
            "value": _value_df([("2024-01-02", 5.0, 1e9, 0.5, 1.0)]),
            "dividend": _div_df("2023-06-01", 0.2),
        },
        "BBB002": {
            "value": _value_df([("2024-01-02", 5.0, 5e9, 0.5, 1.0)]),
            "dividend": _div_df("2023-06-01", 0.2),
        },
        "CCC003": {
            "value": _value_df([("2024-01-02", 15.0, 1e8, 0.5, 1.0)]),  # 价高
            "dividend": _div_df("2023-06-01", 0.2),
        },
        "DDD004": {
            "value": _value_df([("2024-01-02", 5.0, 2e8, 2.5, 1.0)]),  # PEG 过高
            "dividend": _div_df("2023-06-01", 0.2),
        },
        "EEE005": {
            "value": _value_df([("2024-01-02", 5.0, 3e8, 0.5, 1.0)]),
            "dividend": pd.DataFrame(columns=["ex_date", "cash_per_share"]),  # 无股息
        },
    }
    names = {k: "普通" for k in panel}
    panel["STX006"] = {
        "value": _value_df([("2024-01-02", 5.0, 1e8, 0.5, 1.0)]),
        "dividend": _div_df("2023-06-01", 0.2),
    }
    names["STX006"] = "*ST风险"

    symbols, details = select_market_auntie(
        "2024-01-02",
        PortfolioSelectContext(panel=panel, names=names),
        MarketAuntieParams(top_n=2, max_price=9, max_peg=1.0),
    )
    assert symbols == ["AAA001", "BBB002"] or symbols[0] == "AAA001"
    assert "CCC003" not in symbols
    assert "DDD004" not in symbols
    assert "EEE005" not in symbols
    assert "STX006" not in symbols
    assert details[0]["market_cap"] <= details[1]["market_cap"]


def test_run_market_auntie_portfolio_with_mocks(monkeypatch):
    dates = pd.bdate_range("2024-01-02", periods=40)
    date_strs = [d.strftime("%Y-%m-%d") for d in dates]

    def make_stock(close0: float, mv0: float, peg: float) -> dict:
        rows = []
        for i, d in enumerate(date_strs):
            rows.append((d, close0 + i * 0.01, mv0 + i, peg, 0.5))
        return {
            "value": _value_df(rows),
            "dividend": _div_df("2023-06-15", 0.15),
        }

    panel = {
        "000001": make_stock(4.0, 1.0e9, 0.4),
        "000002": make_stock(5.0, 2.0e9, 0.5),
        "000003": make_stock(6.0, 3.0e9, 0.6),
        "000004": make_stock(7.0, 4.0e9, 0.7),
    }

    monkeypatch.setattr(
        "app.portfolio.runner.resolve_universe",
        lambda _req, default_universe=None: (
            [{"symbol": s, "name": "测试"} for s in panel],
            "mock universe",
        ),
    )
    monkeypatch.setattr(
        "app.portfolio.runner.load_fundamentals_panel",
        lambda symbols, **_kw: {s: panel[s] for s in symbols if s in panel},
    )

    resp = run_portfolio_request(
        PortfolioBacktestRequest(
            strategy_id="market_auntie",
            mode="backtest",
            symbols=list(panel),
            start_date="2024-01-02",
            end_date=date_strs[-1],
            initial_cash=50_000,
            commission=0.0003,
            min_commission=0.0,
            slippage=0.0,
            strategy_params={"top_n": 2, "max_price": 9.0, "max_peg": 1.0},
        )
    )
    assert resp.mode == "backtest"
    assert resp.metrics["final_equity"] > 0
    assert len(resp.rebalances) >= 1
    assert len(resp.equity) == len(date_strs)


def test_screen_mode(monkeypatch):
    panel = {
        "000001": {
            "value": _value_df([("2024-06-03", 3.5, 8e8, 0.3, 0.2)]),
            "dividend": _div_df("2024-01-10", 0.1),
        }
    }
    monkeypatch.setattr(
        "app.portfolio.runner.resolve_universe",
        lambda _req, default_universe=None: ([{"symbol": "000001", "name": "测试"}], "mock"),
    )
    monkeypatch.setattr(
        "app.portfolio.runner.load_fundamentals_panel",
        lambda symbols, **_kw: panel,
    )
    resp = run_portfolio_request(
        PortfolioBacktestRequest(
            strategy_id="market_auntie",
            mode="screen",
            symbols=["000001"],
            end_date="2024-06-03",
            strategy_params={"top_n": 1},
        )
    )
    assert resp.mode == "screen"
    assert resp.holdings[0]["symbol"] == "000001"
