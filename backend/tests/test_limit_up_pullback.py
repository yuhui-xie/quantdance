"""涨停回落埋伏策略测试（不访问外网）。"""

from __future__ import annotations

import pandas as pd

from app.strategies.registry import get_cross_section_strategy
from app.backtest_runner import run_backtest_request
from app.schemas import BacktestRequest
from app.strategies.base import CrossSectionContext
from app.strategies.cross_section.limit_up_pullback import (
    LimitUpPullbackParams,
    select_limit_up_pullback,
)


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
    spec = get_cross_section_strategy("limit_up_pullback")
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
        "000001": {"value": good},
        "000002": {"value": big},
        "000004": {"value": loss},
        "000005": {"value": no_limit},
        "000006": {"value": consecutive},
    }
    names = {k: "普通" for k in panel}
    # 调仓日不要涨停：最后一根已是小涨
    syms, details = select_limit_up_pullback(
        asof,
        CrossSectionContext(panel=panel, names=names),
        LimitUpPullbackParams(top_n=5, max_market_cap=1e10),
    )
    assert syms == ["000001"]
    assert details[0]["limit_up_count"] >= 1
    assert details[0]["platform"] or details[0]["mild_ma_up"]


def test_main_board_only_excludes_chinext_star_bse():
    main = _make_qualifying_history(market_cap=2e9)
    cyb = _make_qualifying_history(market_cap=1.8e9)
    kcb = _make_qualifying_history(market_cap=1.9e9)
    bse = _make_qualifying_history(market_cap=1.7e9)
    asof = str(main["date"].iloc[-1])
    panel = {
        "600000": {"value": main},
        "300001": {"value": cyb},
        "688001": {"value": kcb},
        "830001": {"value": bse},
    }
    names = {k: "普通" for k in panel}
    syms, _ = select_limit_up_pullback(
        asof,
        CrossSectionContext(panel=panel, names=names),
        LimitUpPullbackParams(top_n=5, main_board_only=True),
    )
    assert syms == ["600000"]
    syms_all, _ = select_limit_up_pullback(
        asof,
        CrossSectionContext(panel=panel, names=names),
        LimitUpPullbackParams(top_n=5, main_board_only=False),
    )
    assert set(syms_all) == {"600000", "300001", "688001", "830001"}


def test_min_period_return_filters_window_loss():
    """窗口累计涨幅为负时，默认 min_period_return=0 应剔除。"""
    good = _make_qualifying_history(market_cap=2e9)
    lost = _make_qualifying_history(market_cap=1.8e9)
    # 抬高窗口起点收盘价，使 lookback 累计收益为负
    asof_idx = lost.index[-1]
    look_start = asof_idx - 39
    lost.loc[look_start, "close"] = float(lost.loc[asof_idx, "close"]) * 1.5

    asof = str(good["date"].iloc[-1])
    panel = {"000001": {"value": good}, "000002": {"value": lost}}
    names = {k: "普通" for k in panel}
    syms, _ = select_limit_up_pullback(
        asof,
        CrossSectionContext(panel=panel, names=names),
        LimitUpPullbackParams(top_n=5, min_period_return=0.0),
    )
    assert syms == ["000001"]


def test_concept_symbols_whitelist():
    a = _make_qualifying_history(market_cap=2e9)
    b = _make_qualifying_history(market_cap=2.5e9)
    asof = str(a["date"].iloc[-1])
    panel = {"000001": {"value": a}, "000002": {"value": b}}
    names = {"000001": "甲", "000002": "乙"}
    syms, _ = select_limit_up_pullback(
        asof,
        CrossSectionContext(panel=panel, names=names),
        LimitUpPullbackParams(top_n=5, concept_symbols=["000002"]),
    )
    assert syms == ["000002"]


def test_run_limit_up_pullback_backtest_mocked(monkeypatch):
    panel = {f"00000{i}": {"value": _make_qualifying_history(market_cap=1e9 * i)} for i in range(1, 6)}
    asof_end = str(next(iter(panel.values()))["value"]["date"].iloc[-1])
    asof_start = str(next(iter(panel.values()))["value"]["date"].iloc[80])

    monkeypatch.setattr(
        "app.backtest.cross_section_runner._resolve_universe",
        lambda _req, default_universe=None: (
            [{"symbol": s, "name": "测试"} for s in panel],
            "mock",
        ),
    )
    monkeypatch.setattr(
        "app.backtest.cross_section_runner.load_fundamentals_panel",
        lambda symbols, **_kw: {s: panel[s] for s in symbols if s in panel},
    )

    resp = run_backtest_request(
        BacktestRequest(
            strategy_id="limit_up_pullback",
            mode="universe",
            symbols=list(panel),
            start_date=asof_start,
            end_date=asof_end,
            slippage=0.0,
            min_commission=0.0,
            strategy_params={"top_n": 3, "max_market_cap": 1e11},
        )
    )
    assert resp["strategy_id"] == "limit_up_pullback"
    assert len(resp["rebalances"]) >= 1
    assert resp["metrics"]["final_equity"] > 0


def test_screen_future_date_falls_back_to_latest_data(monkeypatch):
    value = _make_qualifying_history(market_cap=2e9)
    latest = str(value["date"].iloc[-1])
    panel = {"000001": {"value": value}}

    monkeypatch.setattr(
        "app.backtest.cross_section_runner._resolve_universe",
        lambda _req, default_universe=None: (
            [{"symbol": "000001", "name": "测试"}],
            "mock",
        ),
    )
    monkeypatch.setattr(
        "app.backtest.cross_section_runner.load_fundamentals_panel",
        lambda symbols, **_kw: panel,
    )

    resp = run_backtest_request(
        BacktestRequest(
            strategy_id="limit_up_pullback",
            mode="screen",
            symbols=["000001"],
            end_date="2099-01-01",
            strategy_params={"top_n": 1},
        )
    )

    assert resp["asof"] == latest
    assert [holding["symbol"] for holding in resp["holdings"]] == ["000001"]
    assert any("已回退至" in warning for warning in resp["warnings"])
