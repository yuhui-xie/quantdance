"""因子 IC 分析测试：纯内存构造，不访问网络。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.ic_analysis import (
    FACTOR_REGISTRY,
    _rankdata,
    compute_factor_ic,
    spearman_rank,
)
from app.stock_screening import (
    compute_liquidity_breakdown,
    compute_ma_alignment_breakdown,
    compute_momentum_breakdown,
    compute_short_reversal_breakdown,
    compute_volume_pulse_breakdown,
)
from app.factors.momentum import bias_momentum, simple_momentum, slope_momentum


def _make_df(n: int = 120, seed: int = 0, start="2023-01-01") -> pd.DataFrame:
    """构造带 open/high/low/close/volume/amount 的日线 DataFrame（DatetimeIndex）。"""
    rng = np.random.default_rng(seed)
    close = 10.0 + np.cumsum(rng.normal(0, 0.3, n))
    close = np.maximum(close, 0.1)
    high = close * (1 + rng.uniform(0, 0.02, n))
    low = close * (1 - rng.uniform(0, 0.02, n))
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    idx = pd.date_range(start, periods=n, freq="B")
    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": rng.integers(1_000_000, 5_000_000, n).astype(float),
            "amount": rng.integers(100_000_000, 500_000_000, n).astype(float),
        },
        index=idx,
    )


def _monotonic_df(n: int = 40) -> pd.DataFrame:
    """单调递增收盘价，便于构造 IC=1 的合成场景。"""
    close = np.arange(1.0, n + 1.0)
    idx = pd.date_range("2023-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close,
         "volume": np.ones(n) * 1e6, "amount": np.ones(n) * 1e8},
        index=idx,
    )


# ---------------------------------------------------------------------------
# 统计工具
# ---------------------------------------------------------------------------

def test_rankdata_average_ties():
    np.testing.assert_allclose(_rankdata(np.array([3.0, 1.0, 2.0, 1.0])), [4.0, 1.5, 3.0, 1.5])


def test_spearman_monotonic_and_inverse():
    x = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    assert spearman_rank(x, x) == pytest.approx(1.0)
    assert spearman_rank(x, -x) == pytest.approx(-1.0)


def test_spearman_hand_computed():
    # 秩相关：x=[1,2,3], y=[3,1,2] → 秩 x=[1,2,3], 秩 y=[2,1,3]
    x = np.array([1.0, 2.0, 3.0])
    y = np.array([3.0, 1.0, 2.0])
    rx = np.array([1.0, 2.0, 3.0])
    ry = np.array([3.0, 1.0, 2.0])
    expected = float(np.corrcoef(rx, ry)[0, 1])
    assert spearman_rank(x, y) == pytest.approx(expected)


def test_spearman_zero_variance_returns_none():
    assert spearman_rank(np.array([1.0, 1.0, 1.0]), np.array([1.0, 2.0, 3.0])) is None


# ---------------------------------------------------------------------------
# 因子对齐：IC 因子末值 == 选股标量
# ---------------------------------------------------------------------------

def test_screen_factors_match_breakdown_last_value():
    df = _make_df(n=90, seed=7)
    bd_m = compute_momentum_breakdown(df)
    assert FACTOR_REGISTRY["ret_20"](df).iloc[-1] == pytest.approx(bd_m["ret_20"])
    assert FACTOR_REGISTRY["ret_60"](df).iloc[-1] == pytest.approx(bd_m["ret_60"])
    assert FACTOR_REGISTRY["vol_20"](df).iloc[-1] == pytest.approx(bd_m["vol_20"])

    bd_v = compute_volume_pulse_breakdown(df)
    assert FACTOR_REGISTRY["vol_ratio"](df).iloc[-1] == pytest.approx(bd_v["vol_ratio"])
    assert FACTOR_REGISTRY["price_vs_ma5"](df).iloc[-1] == pytest.approx(bd_v["price_vs_ma5"])

    bd_a = compute_ma_alignment_breakdown(df)
    assert FACTOR_REGISTRY["ma_spread"](df).iloc[-1] == pytest.approx(bd_a["ma_spread"])
    assert FACTOR_REGISTRY["sma5_slope"](df).iloc[-1] == pytest.approx(bd_a["sma5_slope"])

    bd_r = compute_short_reversal_breakdown(df)
    assert FACTOR_REGISTRY["ret_5"](df).iloc[-1] == pytest.approx(bd_r["ret_5"])
    assert FACTOR_REGISTRY["ret_10"](df).iloc[-1] == pytest.approx(bd_r["ret_10"])

    bd_l = compute_liquidity_breakdown(df)
    assert FACTOR_REGISTRY["rel_vol"](df).iloc[-1] == pytest.approx(bd_l["rel_vol"])
    assert FACTOR_REGISTRY["amihud_illiq_20"](df).iloc[-1] == pytest.approx(bd_l["amihud_illiq_20"])


def test_momentum_factors_match_scalar_last_value():
    df = _make_df(n=120, seed=11)
    close = df["close"].astype(float).to_numpy()
    assert FACTOR_REGISTRY["simple_momentum"](df).iloc[-1] == pytest.approx(
        simple_momentum(close, 20)
    )
    assert FACTOR_REGISTRY["bias_momentum"](df).iloc[-1] == pytest.approx(
        bias_momentum(close, 20, 5)
    )
    assert FACTOR_REGISTRY["slope_momentum"](df).iloc[-1] == pytest.approx(
        slope_momentum(close, 5)
    )


# ---------------------------------------------------------------------------
# 未来收益与 IC 计算
# ---------------------------------------------------------------------------

def test_forward_return_horizon_alignment():
    df = _make_df(n=30, seed=3)
    close = df["close"].astype(float)
    fwd5 = (close.shift(-5) / close - 1.0)
    assert fwd5.iloc[0] == pytest.approx(close.iloc[5] / close.iloc[0] - 1.0)
    # 尾部 5 根为 NaN
    assert np.isnan(fwd5.iloc[-5:]).all()


def test_synthetic_monotonic_ic_is_one():
    # 5 只股票，同一交易日历；因子值 = 涨跌幅（单调），未来收益与之完全同序 → RankIC≈1
    n, k = 60, 5
    idx = pd.date_range("2023-01-01", periods=n, freq="B")
    # 每只股票收盘价为行索引等差偏移，越靠后动量越高
    dfs = {}
    for j in range(k):
        dfs[f"s{j}"] = _monotonic_df(n).add(j * 10, axis=0)
    # 改用 compute_factor_ic 之前，先直接构造面板
    panel = {}
    for sym, df in dfs.items():
        panel[sym] = df
    # 简化：单用 ret_5，h=5，应在每一期给出 +1（完全同序）
    close_panel = pd.DataFrame({sym: df["close"].astype(float) for sym, df in dfs.items()})
    fwd5 = close_panel / close_panel.shift(5) - 1.0
    ret5 = close_panel / close_panel.shift(5) - 1.0
    ics = []
    for date in idx[10:20]:
        vf = ret5.loc[date].to_numpy()
        vr = fwd5.loc[date].to_numpy()
        mask = np.isfinite(vf) & np.isfinite(vr)
        ic = spearman_rank(vf[mask], vr[mask])
        if ic is not None:
            ics.append(ic)
    assert ics and all(i == pytest.approx(1.0) for i in ics)


def test_aggregate_stats_on_known_series():
    from app.ic_analysis import _aggregate_horizon
    ics = [{"date": "2023-01-01", "ic": 0.2}, {"date": "2023-01-02", "ic": 0.4}]
    out = _aggregate_horizon(5, ics)
    assert out["n_dates"] == 2
    assert out["mean_ic"] == pytest.approx(0.3)
    assert out["std_ic"] == pytest.approx(np.std([0.2, 0.4], ddof=1))
    assert out["icir"] == pytest.approx(0.3 / np.std([0.2, 0.4], ddof=1))
    assert out["ic_positive_ratio"] == 1.0
    assert out["t_stat"] == pytest.approx(0.3 / (np.std([0.2, 0.4], ddof=1) / np.sqrt(2)))


def test_aggregate_empty_returns_nones():
    from app.ic_analysis import _aggregate_horizon
    out = _aggregate_horizon(5, [])
    assert out["n_dates"] == 0 and out["mean_ic"] is None and out["icir"] is None


def test_validate_unknown_factor(monkeypatch):
    from app.ic_analysis import _validate_factors
    with pytest.raises(ValueError, match="未知因子"):
        _validate_factors(["ret_20", "not_a_factor"])


def test_compute_factor_ic_end_to_end(monkeypatch):
    from app import ic_analysis as mod

    dfs = {f"s{j}": _make_df(n=80, seed=j) for j in range(5)}
    monkeypatch.setattr(mod, "_load_panel", lambda *a, **kw: dfs)

    out = compute_factor_ic(
        ["s0", "s1", "s2", "s3", "s4"],
        start="2023-01-01",
        end="2024-12-31",
        horizons=(5, 10),
        min_cs=3,
        factors=["ret_20", "vol_20"],
    )
    assert out["count"] == 5
    assert out["horizons"] == [5, 10]
    assert len(out["results"]) == 2
    for factor in out["results"]:
        assert factor["factor"] in {"ret_20", "vol_20"}
        assert factor["source"] == "screening"
        assert len(factor["horizons"]) == 2
        for h in factor["horizons"]:
            assert h["horizon"] in {5, 10}
            assert h["n_dates"] > 0
            assert h["mean_ic"] is not None
            assert h["ic_series"]


def test_strategy_factors_registered_and_valid():
    df = _make_df(n=120, seed=5)
    for name in ("llt", "vpt", "llt_slope", "vpt_slope", "adx", "chop"):
        assert name in FACTOR_REGISTRY
        series = FACTOR_REGISTRY[name](df)
        assert len(series) == len(df)
        # 至少有一半为有限值（暖机后应稳定）
        assert series.notna().mean() > 0.5
    # 来源标签
    from app.ic_analysis import factor_source
    assert factor_source("llt") == "strategy"
    assert factor_source("llt_slope") == "strategy"
    assert factor_source("ret_20") == "screening"
    assert factor_source("simple_momentum") == "momentum"


def test_validate_factors_strips_whitespace():
    from app.ic_analysis import _validate_factors
    names = _validate_factors(["ret_20 ", "  vol_20", "  "])
    assert names == ["ret_20", "vol_20"]


def test_compute_factor_ic_min_cs_gate(monkeypatch):
    from app import ic_analysis as mod

    dfs = {f"s{j}": _make_df(n=80, seed=j) for j in range(3)}
    monkeypatch.setattr(mod, "_load_panel", lambda *a, **kw: dfs)

    # min_cs=100 > 横截面股票数 → 全部日期被跳过 → n_dates=0
    out = compute_factor_ic(
        ["s0", "s1", "s2"],
        start="2023-01-01",
        end="2024-12-31",
        horizons=(5,),
        min_cs=100,
        factors=["ret_20"],
    )
    assert out["results"][0]["horizons"][0]["n_dates"] == 0
