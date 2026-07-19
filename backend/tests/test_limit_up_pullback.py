"""涨停回落埋伏策略测试（不访问外网）。"""

from __future__ import annotations

import pandas as pd

from app.portfolio.base import PortfolioSelectContext
from app.portfolio.limit_up_pullback import (
    LimitUpPullbackParams,
    select_limit_up_pullback,
)
from app.portfolio.registry import get_portfolio_strategy
from app.portfolio.runner import run_portfolio_request
from app.schemas import PortfolioBacktestRequest


def _series_value(
    dates: list[str],
    closes: list[float],
    pcts: list[float],
    *,
    market_cap: float = 3e9,
    pe_ttm: float = 12.0,
) -> pd.DataFrame:
    assert len(dates) == len(closes) == len(pcts)
    return pd.DataFrame(
        [
            {
                "date": d,
                "close": c,
                "pct_change": p,
                "market_cap": market_cap,
                "float_market_cap": market_cap * 0.8,
                "pe_ttm": pe_ttm,
                "pb": 1.2,
                "peg": 0.8,
                "ps_ttm": 1.5,
            }
            for d, c, p in zip(dates, closes, pcts)
        ]
    )


def _make_qualifying_history(
    *,
    market_cap: float = 3e9,
    pe_ttm: float = 12.0,
    limit_day: int = 105,
    after_limit_pct: float = -2.0,
) -> pd.DataFrame:
    """构造约 130 根：前高后低，近 40 日内一次涨停后次日回落，仍处相对低位。"""
    n = 130
    dates = pd.bdate_range("2023-07-03", periods=n)
    date_strs = [d.strftime("%Y-%m-%d") for d in dates]
    closes: list[float] = []
    pcts: list[float] = []
    # 前半段较高，随后落到低位平台，保证 price_position 偏低
    for i in range(n):
        if i < 40:
            c = 14.0 - i * 0.05 + (i % 5) * 0.01
        elif i < limit_day:
            c = 8.2 + (i % 6) * 0.02
        else:
            c = 8.2
        closes.append(c)
        pcts.append(0.1 if i == 0 else (closes[i] / closes[i - 1] - 1.0) * 100.0)

    closes[limit_day] = closes[limit_day - 1] * 1.10
    pcts[limit_day] = 10.0
    closes[limit_day + 1] = closes[limit_day] * (1 + after_limit_pct / 100.0)
    pcts[limit_day + 1] = after_limit_pct
    base = closes[limit_day + 1]
    for i in range(limit_day + 2, n):
        closes[i] = base * (1.0 + 0.002 * ((i % 5) - 2))
        pcts[i] = (closes[i] / closes[i - 1] - 1.0) * 100.0
    return _series_value(date_strs, closes, pcts, market_cap=market_cap, pe_ttm=pe_ttm)


def test_strategy_registered():
    spec = get_portfolio_strategy("limit_up_pullback")
    assert spec is not None
    assert spec.name == "涨停回落埋伏"


def test_select_picks_limit_up_pullback_and_filters():
    good = _make_qualifying_history(market_cap=2e9)
    big = _make_qualifying_history(market_cap=5e10)  # 过大市值
    loss = _make_qualifying_history(market_cap=2.5e9, pe_ttm=-5.0)
    no_limit = _make_qualifying_history(market_cap=2.2e9)
    # 去掉观察窗内涨停
    no_limit.loc[no_limit.index[105], "pct_change"] = 1.0
    no_limit.loc[no_limit.index[105], "close"] = float(
        no_limit.loc[no_limit.index[104], "close"]
    )

    consecutive = _make_qualifying_history(market_cap=2.1e9)
    # 连板两日
    consecutive.loc[consecutive.index[105], "pct_change"] = 10.0
    consecutive.loc[consecutive.index[106], "pct_change"] = 10.0
    consecutive.loc[consecutive.index[106], "close"] = float(
        consecutive.loc[consecutive.index[105], "close"]
    ) * 1.10

    asof = str(good["date"].iloc[-1])
    panel = {
        "GOOD": {"value": good},
        "BIG": {"value": big},
        "LOSS": {"value": loss},
        "NOLU": {"value": no_limit},
        "LIAN": {"value": consecutive},
    }
    names = {k: "普通" for k in panel}
    # 调仓日不要涨停：最后一根已是小涨
    syms, details = select_limit_up_pullback(
        asof,
        PortfolioSelectContext(panel=panel, names=names),
        LimitUpPullbackParams(top_n=5, max_market_cap=1e10),
    )
    assert syms == ["GOOD"]
    assert details[0]["limit_up_count"] >= 1
    assert details[0]["platform"] or details[0]["mild_ma_up"]


def test_concept_symbols_whitelist():
    a = _make_qualifying_history(market_cap=2e9)
    b = _make_qualifying_history(market_cap=2.5e9)
    asof = str(a["date"].iloc[-1])
    panel = {"AAA": {"value": a}, "BBB": {"value": b}}
    names = {"AAA": "甲", "BBB": "乙"}
    syms, _ = select_limit_up_pullback(
        asof,
        PortfolioSelectContext(panel=panel, names=names),
        LimitUpPullbackParams(top_n=5, concept_symbols=["BBB"]),
    )
    assert syms == ["BBB"]


def test_run_limit_up_pullback_backtest_mocked(monkeypatch):
    panel = {f"00000{i}": {"value": _make_qualifying_history(market_cap=1e9 * i)} for i in range(1, 6)}
    asof_end = str(next(iter(panel.values()))["value"]["date"].iloc[-1])
    asof_start = str(next(iter(panel.values()))["value"]["date"].iloc[80])

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
            strategy_id="limit_up_pullback",
            mode="backtest",
            symbols=list(panel),
            start_date=asof_start,
            end_date=asof_end,
            rebalance_freq=20,
            slippage=0.0,
            min_commission=0.0,
            strategy_params={"top_n": 3, "max_market_cap": 1e11},
        )
    )
    assert resp.strategy_id == "limit_up_pullback"
    assert len(resp.rebalances) >= 1
    assert resp.metrics["final_equity"] > 0
