"""中小综指微盘策略测试（不访问外网）。"""

from __future__ import annotations

import pandas as pd

from app.portfolio.base import PortfolioSelectContext
from app.portfolio.runner import run_portfolio_request
from app.portfolio.small_cap_zz399101 import SmallCapZZ399101Params, select_small_cap_zz399101
from app.schemas import PortfolioBacktestRequest


def _value_df(rows: list[tuple[str, float, float, float]]) -> pd.DataFrame:
    # date, close, float_mv, pct
    return pd.DataFrame(
        [
            {
                "date": d,
                "close": c,
                "pct_change": pct,
                "market_cap": fmv * 1.1,
                "float_market_cap": fmv,
                "pe_ttm": 10.0,
                "pb": 1.0,
                "peg": 0.5,
                "ps_ttm": 1.0,
            }
            for d, c, fmv, pct in rows
        ]
    )


def test_select_picks_smallest_float_cap():
    panel = {
        "A": {"value": _value_df([("2024-01-02", 5.0, 1e9, 0.1)])},
        "B": {"value": _value_df([("2024-01-02", 5.0, 3e9, 0.1)])},
        "C": {"value": _value_df([("2024-01-02", 5.0, 2e9, 0.1)])},
        "D": {"value": _value_df([("2024-01-02", 5.0, 4e9, 9.8)])},  # 涨停
    }
    names = {k: "普通" for k in panel}
    syms, details = select_small_cap_zz399101(
        "2024-01-02",
        PortfolioSelectContext(panel=panel, names=names),
        SmallCapZZ399101Params(top_n=2),
    )
    assert syms == ["A", "C"]
    assert details[0]["float_market_cap"] < details[1]["float_market_cap"]


def test_run_small_cap_backtest_mocked(monkeypatch):
    dates = pd.bdate_range("2024-01-02", periods=40)
    date_strs = [d.strftime("%Y-%m-%d") for d in dates]

    def make(fmv0: float) -> dict:
        rows = [(d, 4.0 + i * 0.01, fmv0 + i, 0.2) for i, d in enumerate(date_strs)]
        return {"value": _value_df(rows), "dividend": pd.DataFrame()}

    panel = {f"00000{i}": make(1e9 * i) for i in range(1, 6)}
    monkeypatch.setattr(
        "app.portfolio.runner.resolve_universe",
        lambda _req, default_universe=None: (
            [{"symbol": s, "name": "测试"} for s in panel],
            "mock",
        ),
    )
    monkeypatch.setattr(
        "app.portfolio.runner.load_fundamentals_panel",
        lambda symbols, **_kw: {s: panel[s] for s in symbols if s in panel},
    )

    resp = run_portfolio_request(
        PortfolioBacktestRequest(
            strategy_id="small_cap_zz399101",
            mode="backtest",
            symbols=list(panel),
            start_date="2024-01-02",
            end_date=date_strs[-1],
            rebalance_freq=20,
            slippage=0.0,
            min_commission=0.0,
            strategy_params={"top_n": 3},
        )
    )
    assert resp.strategy_id == "small_cap_zz399101"
    assert len(resp.rebalances) >= 1
    assert resp.metrics["final_equity"] > 0
