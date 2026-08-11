"""筹码单峰密集突破选股与组合回测测试（不访问外网）。

构造口径：用平滑上行（ramp）的价格序列作为"健康"筹码（获利盘适中、
站上成本、突破筹码峰、集中度高），用宽幅波动作为"分散"筹码，用大幅
急涨作为"超买"筹码，用冲高回落作为"未突破"筹码。
"""

from __future__ import annotations

import pandas as pd

from app.backtest_runner import run_backtest_request
from app.schemas import BacktestRequest
from app.strategies.base import CrossSectionContext
from app.strategies.cross_section.chip_accumulation import (
    ChipAccumulationParams,
    select_chip_accumulation,
)


def _dates(end: str, periods: int) -> list[str]:
    return [d.strftime("%Y-%m-%d") for d in pd.bdate_range(end=end, periods=periods)]


def _ramp_value(
    dates: list[str],
    *,
    start: float,
    end: float,
    final_close: float,
    turnover: float = 0.05,
) -> pd.DataFrame:
    """平滑上行序列作为健康筹码；date 为字符串列，升序。"""
    rows = []
    n = len(dates)
    for i, d in enumerate(dates):
        c = start + (i / (n - 1)) * (end - start)
        lo, hi = c - 0.1, c + 0.1
        close = final_close if i == n - 1 else (lo + hi) / 2.0
        rows.append(
            {
                "date": d,
                "open": (lo + hi) / 2.0,
                "high": max(hi, close),
                "low": min(lo, close),
                "close": close,
                "volume": 1_000_000.0,
                "amount": close * 1_000_000.0,
                "turnover_rate": turnover,
            }
        )
    df = pd.DataFrame(rows)
    df["pct_change"] = df["close"].pct_change() * 100.0
    return df


def _flat_value(
    dates: list[str],
    *,
    low: float,
    high: float,
    final_close: float,
    turnover: float = 0.05,
) -> pd.DataFrame:
    """区间宽幅/单区间平铺序列；final_close 只抬高末根收盘。"""
    rows = []
    n = len(dates)
    for i, d in enumerate(dates):
        lo, hi = low, high
        close = final_close if i == n - 1 else (lo + hi) / 2.0
        rows.append(
            {
                "date": d,
                "open": (lo + hi) / 2.0,
                "high": max(hi, close),
                "low": min(lo, close),
                "close": close,
                "volume": 1_000_000.0,
                "amount": close * 1_000_000.0,
                "turnover_rate": turnover,
            }
        )
    df = pd.DataFrame(rows)
    df["pct_change"] = df["close"].pct_change() * 100.0
    return df


def _turnover_df(dates: list[str], low: float, high: float, final_close: float) -> pd.DataFrame:
    """fetch_a_share_daily_turnover 返回（DatetimeIndex + turnover_rate）。"""
    value = _flat_value(dates, low=low, high=high, final_close=final_close)
    df = value.copy()
    df.index = pd.to_datetime(value["date"])
    return df.drop(columns=["date", "pct_change"])


def _good_value(dates: list[str]) -> pd.DataFrame:
    """健康筹码：区间 [10,11] 平铺、收盘 10.6（获利盘适中、站上成本、突破峰、集中）。"""
    return _flat_value(dates, low=10.0, high=11.0, final_close=10.6)


def test_select_prefers_concentrated_chips():
    asof = "2024-12-31"
    dates = _dates(asof, 120)
    # 筹码高度集中：区间 [10,11] 平铺、收盘 10.6（集中度高）——应入选
    tight = _good_value(dates)
    # 筹码分散：价格在 5-15 大幅波动（集中度低）——应剔除
    wide = _flat_value(dates, low=5.0, high=15.0, final_close=12.0)

    panel = {"AAA001": {"value": tight}, "BBB002": {"value": wide}}
    names = {k: "普通" for k in panel}

    symbols, details = select_chip_accumulation(
        asof,
        CrossSectionContext(panel=panel, names=names),
        ChipAccumulationParams(
            top_n=1,
            concentration_max=0.05,  # 仅高度集中的筹码通过
            profit_ratio_min=0.0,
            profit_ratio_max=0.9,
            require_price_above_cost=True,
            require_breakout_peak=True,
        ),
    )
    assert symbols == ["AAA001"]
    assert details[0]["concentration_90"] < 0.05


def test_select_filters_overbought_and_below_peak():
    asof = "2024-12-31"
    dates = _dates(asof, 120)
    # 超买：大量筹码成本远低于现价，获利盘接近 1 —— 应剔除
    overbought = _flat_value(dates, low=5.0, high=6.0, final_close=12.0)
    # 未突破筹码峰：价格近期上冲后回落，收盘低于峰值 —— 应剔除
    fell_back = _flat_value(dates, low=10.0, high=12.0, final_close=9.0)
    # 健康：区间 [10,11] 平铺、收盘 10.6 —— 应入选
    good = _good_value(dates)

    panel = {
        "AAA001": {"value": overbought},
        "BBB002": {"value": fell_back},
        "CCC003": {"value": good},
    }
    names = {k: "普通" for k in panel}

    symbols, details = select_chip_accumulation(
        asof,
        CrossSectionContext(panel=panel, names=names),
        ChipAccumulationParams(
            top_n=3,
            concentration_max=0.9,
            profit_ratio_min=0.0,
            profit_ratio_max=0.9,  # 过滤超买
            require_price_above_cost=False,  # 隔离突破条件
            require_breakout_peak=True,  # 过滤未突破
        ),
    )
    assert "AAA001" not in symbols  # 获利盘过高
    assert "BBB002" not in symbols  # 收盘低于筹码峰
    assert "CCC003" in symbols


def _run_mock(
    monkeypatch,
    request_kwargs: dict,
    panel: dict,
    strategy_params: dict,
) -> dict:
    import app.backtest.cross_section_runner as csr

    dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2024-01-02", periods=65)]

    def mock_resolve(*_a, **_k):
        return (
            [{"symbol": s, "name": "测试"} for s in panel],
            "mock universe",
        )

    # 每只股票都构造成健康筹码（区间平铺，可通过过滤）
    bands = {
        "000001": (9.0, 10.0, 9.6),
        "000002": (10.0, 11.0, 10.6),
        "000003": (11.0, 12.0, 11.6),
    }

    def mock_turnover(symbol, **_k):
        low, high, final = bands[symbol]
        return _turnover_df(dates, low, high, final)

    monkeypatch.setattr(csr, "_resolve_universe", mock_resolve)
    monkeypatch.setattr(csr, "fetch_a_share_daily_turnover", mock_turnover)

    return run_backtest_request(
        BacktestRequest(
            strategy_id="chip_accumulation",
            mode=request_kwargs.pop("mode", "universe"),
            symbols=list(panel),
            start_date="2024-01-02",
            end_date=dates[-1],
            initial_cash=50_000,
            commission=0.0003,
            min_commission=0.0,
            slippage=0.0,
            strategy_params=strategy_params,
            **request_kwargs,
        )
    )


def test_run_chip_portfolio_with_mocks(monkeypatch):
    dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2024-01-02", periods=65)]
    panel = {
        "000001": {"value": _flat_value(dates, low=9.0, high=10.0, final_close=9.6)},
        "000002": {"value": _flat_value(dates, low=10.0, high=11.0, final_close=10.6)},
        "000003": {"value": _flat_value(dates, low=11.0, high=12.0, final_close=11.6)},
    }

    resp = _run_mock(
        monkeypatch,
        {},
        panel,
        {"top_n": 2, "profit_ratio_min": 0.0, "profit_ratio_max": 0.9999},
    )
    assert resp["mode"] == "backtest"
    assert resp["metrics"]["final_equity"] > 0
    assert len(resp["equity"]) == len(dates)
    assert len(resp["rebalances"]) >= 1


def test_screen_mode_with_mocks(monkeypatch):
    dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2024-01-02", periods=65)]
    panel = {
        "000001": {"value": _flat_value(dates, low=9.0, high=10.0, final_close=9.6)},
    }

    resp = _run_mock(
        monkeypatch,
        {"mode": "screen"},
        panel,
        {"top_n": 1, "profit_ratio_min": 0.0, "profit_ratio_max": 0.9999},
    )
    assert resp["mode"] == "screen"
    assert resp["holdings"] and resp["holdings"][0]["symbol"] == "000001"


def test_load_panel_uses_turnover_source(monkeypatch):
    import app.backtest.cross_section_runner as csr

    dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2024-01-02", periods=65)]

    def mock_resolve(*_a, **_k):
        return ([{"symbol": "000001", "name": "测试"}], "mock")

    calls: list[str] = []

    def mock_turnover(symbol, **_k):
        calls.append(symbol)
        return _turnover_df(dates, 9.0, 10.0, 9.6)

    monkeypatch.setattr(csr, "_resolve_universe", mock_resolve)
    monkeypatch.setattr(csr, "fetch_a_share_daily_turnover", mock_turnover)

    resp = run_backtest_request(
        BacktestRequest(
            strategy_id="chip_accumulation",
            mode="screen",
            symbols=["000001"],
            end_date=dates[-1],
            strategy_params={
                "top_n": 1,
                "profit_ratio_min": 0.0,
                "profit_ratio_max": 0.9999,
            },
        )
    )
    assert resp["holdings"] and resp["holdings"][0]["symbol"] == "000001"
    # 数据接入走换手率源而非纯 OHLCV 源
    assert calls and calls == ["000001"]
