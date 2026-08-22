"""my_strategy 策略测试（重点覆盖 Choppiness 震荡市过滤）。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.strategies.my_strategy import (
    MyStrategyParams,
    STRATEGY,
    _compute,
    _positions,
)


def _params(**overrides) -> MyStrategyParams:
    defaults = dict(
        llt_period=20,
        slope_lookback=1,
    )
    defaults.update(overrides)
    return MyStrategyParams(**defaults)


def test_ranging_blocks_new_trend_entry():
    # 条件全部满足（斜率向上），但处于震荡市区间 → 不开单、强制空仓
    slope = np.ones(9)
    ranging = np.array([0, 0, 0, 1, 1, 1, 0, 0, 0], dtype=bool)
    vpt_ok = np.ones(9, dtype=bool)
    params = _params()

    positions = _positions(slope, ranging, vpt_ok, params)

    assert positions.tolist() == [1, 1, 1, 0, 0, 0, 1, 1, 1]


def test_ranging_forces_exit_of_existing_position():
    # 已持仓后进入震荡市 → 立即平仓转空
    slope = np.ones(4)
    ranging = np.array([0, 0, 1, 1], dtype=bool)
    vpt_ok = np.ones(4, dtype=bool)
    params = _params()

    positions = _positions(slope, ranging, vpt_ok, params)

    assert positions.tolist() == [1, 1, 0, 0]


def test_no_ranging_opens_trend_position():
    slope = np.ones(5)
    ranging = np.zeros(5, dtype=bool)
    vpt_ok = np.ones(5, dtype=bool)
    params = _params()

    positions = _positions(slope, ranging, vpt_ok, params)

    assert positions.tolist() == [1, 1, 1, 1, 1]


def test_chop_filter_disabled_ignores_ranging():
    # use_chop_filter=False 时，即使 CHOP 高也不阻塞，且不输出 chop 叠加曲线
    params = _params(use_chop_filter=False, use_adx_filter=False)
    df = _choppy_df()

    _signal, overlays = _compute(df, params)

    assert "chop_ranging" not in overlays
    assert "chop" not in overlays
    # 无任何过滤时 ranging 恒为 False
    assert not overlays["ranging"].astype(bool).any()


def test_compute_chop_overlay_matches_threshold_mask():
    df = _choppy_df()
    params = _params()

    _signal, overlays = _compute(df, params)
    chop = overlays["chop"]
    chop_ranging = overlays["chop_ranging"].astype(bool)

    finite = np.isfinite(chop)
    np.testing.assert_array_equal(chop_ranging[finite], chop[finite] > params.chop_threshold)
    # 非有限（暖机期）不应误判为震荡
    assert not chop_ranging[~finite].any()


def test_positions_flat_wherever_ranging():
    df = _choppy_df()
    params = _params()

    _signal, overlays = _compute(df, params)
    ranging = overlays["chop_ranging"].astype(bool)
    positions = overlays["position"]

    assert (positions[ranging] == 0).all()


def test_min_bars_includes_chop_period_when_filter_enabled():
    params = _params(use_chop_filter=True, chop_period=30)
    assert STRATEGY.min_bars(params) == 32  # max(20, 30) + 1 + slope(1)


def test_min_bars_ignores_chop_period_when_filter_disabled():
    params = _params(use_chop_filter=False, chop_period=30)
    assert STRATEGY.min_bars(params) == 22  # 20 + 1 + slope(1)


# --- VPT 量价过滤 ---


def test_vpt_filter_negative_slope_forces_exit():
    # 斜率全部满足动量向上，但 VPT 转负（量价背离）→ 触发离场
    slope = np.ones(5)
    ranging = np.zeros(5, dtype=bool)
    # vpt_ok：前 3 天 True（量价共振），后 2 天 False（背离）
    vpt_ok = np.array([True, True, True, False, False])
    params = _params(use_vpt_filter=True)

    positions = _positions(slope, ranging, vpt_ok, params)

    assert positions.tolist() == [1, 1, 1, 0, 0]


def test_vpt_filter_negative_slope_blocks_entry():
    # 动量向上但 VPT 转负 → 即使斜率满足也不进场
    slope = np.ones(5)
    ranging = np.zeros(5, dtype=bool)
    vpt_ok = np.array([False, False, True, True, True])
    params = _params(use_vpt_filter=True)

    positions = _positions(slope, ranging, vpt_ok, params)

    assert positions.tolist() == [0, 0, 1, 1, 1]


def test_vpt_filter_disabled_ignores_vpt_ok():
    # use_vpt_filter=False 时，vpt_ok 不影响持仓（与原始逻辑一致）
    slope = np.ones(5)
    ranging = np.zeros(5, dtype=bool)
    vpt_ok = np.array([False, False, False, False, False])
    params = _params(use_vpt_filter=False)

    positions = _positions(slope, ranging, vpt_ok, params)

    assert positions.tolist() == [1, 1, 1, 1, 1]


def test_compute_outputs_vpt_overlays():
    df = _choppy_df()
    params = _params(use_vpt_filter=True)

    _signal, overlays = _compute(df, params)

    for key in ("vpt", "vpt_slope", "vpt_ok"):
        assert key in overlays
        assert overlays[key].shape == (len(df),)
    # vpt_ok 恒为非 0/1 的 int8 布尔掩码
    assert overlays["vpt_ok"].dtype == np.int8
    assert set(np.unique(overlays["vpt_ok"])).issubset({0, 1})


def test_compute_vpt_filter_disabled_emits_no_vpt_overlay():
    # 默认 use_vpt_filter=False：不应输出 vpt 叠加曲线（避免报告多画一张图）
    df = _choppy_df()
    params = _params()

    _signal, overlays = _compute(df, params)

    for key in ("vpt", "vpt_slope", "vpt_ok"):
        assert key not in overlays


def test_min_bars_includes_vpt_lookback_when_filter_enabled():
    # vpt_slope_lookback 大于 llt_period 时纳入上限
    params = _params(use_vpt_filter=True, vpt_slope_lookback=30)
    assert STRATEGY.min_bars(params) == 32  # max(20, 14, 30) + 1 + slope(1)


# --- ADX 趋势强度过滤 ---


def test_compute_outputs_adx_overlays():
    df = _choppy_df()
    params = _params()

    _signal, overlays = _compute(df, params)

    for key in ("adx", "adx_weak", "ranging"):
        assert key in overlays
        assert overlays[key].shape == (len(df),)
    assert overlays["adx_weak"].dtype == np.int8
    assert set(np.unique(overlays["adx_weak"])).issubset({0, 1})


def test_adx_weak_matches_threshold_mask():
    df = _choppy_df()
    params = _params()

    _signal, overlays = _compute(df, params)
    adx_arr = overlays["adx"]
    adx_weak = overlays["adx_weak"].astype(bool)

    finite = np.isfinite(adx_arr)
    np.testing.assert_array_equal(adx_weak[finite], adx_arr[finite] < params.adx_threshold)
    assert not adx_weak[~finite].any()


def test_ranging_is_union_of_chop_and_adx():
    df = _choppy_df()
    params = _params()

    _signal, overlays = _compute(df, params)
    ranging = overlays["ranging"].astype(bool)
    chop_ranging = overlays["chop_ranging"].astype(bool)
    adx_weak = overlays["adx_weak"].astype(bool)

    np.testing.assert_array_equal(ranging, chop_ranging | adx_weak)


def test_adx_filter_disabled_has_no_adx_weak():
    df = _choppy_df()
    params = _params(use_adx_filter=False)

    _signal, overlays = _compute(df, params)

    # ADX 关闭：不输出 adx/adx_weak 叠加曲线，ranging 即 chop_ranging
    assert "adx_weak" not in overlays
    assert "adx" not in overlays
    np.testing.assert_array_equal(
        overlays["ranging"].astype(bool),
        overlays["chop_ranging"].astype(bool),
    )


def test_min_bars_includes_adx_period_when_filter_enabled():
    params = _params(use_adx_filter=True, adx_period=30)
    assert STRATEGY.min_bars(params) == 32  # max(20, 14, 30) + 1 + slope(1)


def test_min_bars_ignores_adx_period_when_filter_disabled():
    params = _params(use_adx_filter=False, adx_period=30)
    assert STRATEGY.min_bars(params) == 22  # 20 + 1 + slope(1)


# --- LLT 斜率拟合窗口（slope_fit_window）---


def test_compute_uses_reg_slope_when_fit_window_set():
    # slope_fit_window>=2 时，llt_dt 用滚动回归斜率而非单点差分，前 window-1 项为 NaN
    df = _choppy_df()
    params = _params(slope_fit_window=5)
    use_chop = _params()  # slope_fit_window=1，走差分

    _signal, fit_overlays = _compute(df, params)
    _signal, diff_overlays = _compute(df, use_chop)

    fit_slope = fit_overlays["llt_dt"]
    diff_slope = diff_overlays["llt_dt"]
    # 回归斜率暖机期更长（前 4 项 NaN）
    assert np.isnan(fit_slope[:4]).all()
    assert not np.isnan(diff_slope[1:]).any()
    # 从有效段起，两者都非全 NaN
    valid = np.isfinite(fit_slope)
    assert valid.any()
    # 回归斜率整体变化应更平缓（标准差更小）
    assert np.nanstd(fit_slope) <= np.nanstd(diff_slope) + 1e-9


def test_min_bars_includes_fit_window_warmup():
    # slope_fit_window=10：max(slope_lookback=1, 10-1=9) + 1
    params = _params(slope_fit_window=10)
    assert STRATEGY.min_bars(params) == 30  # max(20,14) + 9 + 1


def test_min_bars_fit_window_1_uses_diff_warmup():
    params = _params(slope_fit_window=1)
    assert STRATEGY.min_bars(params) == 22  # 20 + 1 + slope(1)


# --- 回归拟合质量过滤（R²）---


def test_low_r2_blocks_entry():
    # slope_ok 前 3 天 False（拟合差不可信）→ 即使斜率向上也不进场
    slope = np.ones(5)
    ranging = np.zeros(5, dtype=bool)
    vpt_ok = np.ones(5, dtype=bool)
    slope_ok = np.array([False, False, False, True, True])
    params = _params()

    positions = _positions(slope, ranging, vpt_ok, params, slope_ok)

    assert positions.tolist() == [0, 0, 0, 1, 1]


def test_r2_ok_allows_entry_when_slope_up():
    slope = np.ones(4)
    ranging = np.zeros(4, dtype=bool)
    vpt_ok = np.ones(4, dtype=bool)
    slope_ok = np.ones(4, dtype=bool)
    params = _params()

    positions = _positions(slope, ranging, vpt_ok, params, slope_ok)

    assert positions.tolist() == [1, 1, 1, 1]


def test_compute_outputs_r2_overlays_when_fit_window_set():
    df = _choppy_df()
    params = _params(slope_fit_window=5)

    _signal, overlays = _compute(df, params)

    assert "llt_r2" in overlays and "llt_fit_ok" in overlays
    r2 = overlays["llt_r2"]
    fit_ok = overlays["llt_fit_ok"].astype(bool)
    # R² 前 window-1 项为 NaN，之后应在 [0,1]
    assert np.isnan(r2[:4]).all()
    finite = np.isfinite(r2)
    assert ((r2[finite] >= 0) & (r2[finite] <= 1)).all()
    # llt_fit_ok = r2 >= min_fit_r2（默认 0，故有限处恒 True）
    assert (fit_ok == np.where(np.isfinite(r2), r2 >= 0, True)).all()


def test_min_fit_r2_blocks_entry_via_compute():
    df = _choppy_df()
    # 提高 R² 阈值，看是否可能屏蔽进场（掩码应反映 r2 < threshold 处为 False）
    params = _params(slope_fit_window=5, min_fit_r2=0.9)

    _signal, overlays = _compute(df, params)
    fit_ok = overlays["llt_fit_ok"].astype(bool)
    r2 = overlays["llt_r2"]
    finite = np.isfinite(r2)

    if finite.any():
        np.testing.assert_array_equal(fit_ok[finite], r2[finite] >= 0.9)


def _choppy_df() -> pd.DataFrame:
    """构造一段高频强震荡行情（CHOP 稳定高于 62）。"""
    n = 120
    close = 10.0 + np.concatenate(
        [np.repeat(np.arange(k, k + 2), 4) for k in range(30)]
    )[:n].astype(float)
    high = close + 0.8
    low = close - 0.8
    return pd.DataFrame(
        {"high": high, "low": low, "close": close, "volume": np.ones(n)}
    )
