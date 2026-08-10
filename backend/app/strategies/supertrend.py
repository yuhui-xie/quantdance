"""超级趋势（SuperTrend）：ATR 通道突破配合 ADX 趋势强度过滤与长期 EMA 确认。

SuperTrend 由 Olivier Seban 提出，是实战中广泛使用的趋势跟踪指标。
本实现额外叠加了 ADX 滤波器（仅在趋势强度足够时才入场）和长期 EMA 过滤器
（仅在价格处于长期均线上方时做多），显著降低震荡市的假突破损耗。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from app.backtest_engine import BacktestResult, run_from_signals
from app.strategies.base import BaseBacktestParams, StrategySpec


class SuperTrendParams(BaseModel):
    atr_period: int = Field(10, ge=2, le=200, description="ATR 计算周期，控制通道对波动的敏感度")
    multiplier: float = Field(3.0, ge=1.0, le=10.0, description="ATR 乘数，越大带宽越宽、信号越少")
    adx_period: int = Field(14, ge=2, le=100, description="ADX 趋势强度指标的计算周期")
    min_adx: float = Field(20.0, ge=0.0, le=100.0, description="入场最低 ADX 阈值；低于此值视为震荡市，过滤假突破")
    ema_period: int = Field(60, ge=5, le=400, description="长期趋势 EMA 周期；仅当收盘价在均线上方时才允许买入")


# ---------------------------------------------------------------------------
# ADX (Average Directional Index) 计算
# ---------------------------------------------------------------------------

def _compute_adx(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    period: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """返回 (adx, plus_di, minus_di) 三个等长数组，前 period 根 bar 为 NaN。"""
    n = len(close)
    tr = np.full(n, np.nan)
    plus_dm = np.full(n, np.nan)
    minus_dm = np.full(n, np.nan)

    for i in range(1, n):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )
        up_move = high[i] - high[i - 1]
        down_move = low[i - 1] - low[i]
        if up_move > down_move and up_move > 0:
            plus_dm[i] = up_move
        else:
            plus_dm[i] = 0.0
        if down_move > up_move and down_move > 0:
            minus_dm[i] = down_move
        else:
            minus_dm[i] = 0.0

    # Wilder 平滑（指数平滑，alpha = 1/period）
    atr_smooth = np.full(n, np.nan)
    pdm_smooth = np.full(n, np.nan)
    mdm_smooth = np.full(n, np.nan)
    plus_di = np.full(n, np.nan)
    minus_di = np.full(n, np.nan)
    adx = np.full(n, np.nan)

    start = period
    if n <= start:
        return adx, plus_di, minus_di

    atr_smooth[start] = np.nanmean(tr[1 : start + 1])
    pdm_smooth[start] = np.nansum(plus_dm[1 : start + 1])
    mdm_smooth[start] = np.nansum(minus_dm[1 : start + 1])

    if atr_smooth[start] > 0:
        plus_di[start] = 100.0 * pdm_smooth[start] / atr_smooth[start]
        minus_di[start] = 100.0 * mdm_smooth[start] / atr_smooth[start]
    else:
        plus_di[start] = 0.0
        minus_di[start] = 0.0

    denom = plus_di[start] + minus_di[start]
    dx_start = 100.0 * abs(plus_di[start] - minus_di[start]) / denom if denom > 0 else 0.0
    adx[start] = dx_start

    alpha = 1.0 / period
    for i in range(start + 1, n):
        atr_smooth[i] = atr_smooth[i - 1] + alpha * (tr[i] - atr_smooth[i - 1])
        pdm_smooth[i] = pdm_smooth[i - 1] + alpha * (plus_dm[i] - pdm_smooth[i - 1])
        mdm_smooth[i] = mdm_smooth[i - 1] + alpha * (minus_dm[i] - mdm_smooth[i - 1])

        if atr_smooth[i] > 0:
            plus_di[i] = 100.0 * pdm_smooth[i] / atr_smooth[i]
            minus_di[i] = 100.0 * mdm_smooth[i] / atr_smooth[i]
        else:
            plus_di[i] = 0.0
            minus_di[i] = 0.0

        di_sum = plus_di[i] + minus_di[i]
        dx = 100.0 * abs(plus_di[i] - minus_di[i]) / di_sum if di_sum > 0 else 0.0
        adx[i] = adx[i - 1] + alpha * (dx - adx[i - 1])

    return adx, plus_di, minus_di


# ---------------------------------------------------------------------------
# SuperTrend 计算
# ---------------------------------------------------------------------------

def _compute_supertrend(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    period: int,
    multiplier: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """返回 (supertrend, direction, atr, final_upper, final_lower)。

    direction: 1=上升趋势, -1=下降趋势
    supertrend: 上升趋势时为 final_lower（跟踪止损），下降趋势时为 final_upper（跟踪阻力）
    """
    n = len(close)

    # True Range
    tr = np.full(n, np.nan)
    for i in range(1, n):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )

    # Wilder ATR
    atr = np.full(n, np.nan)
    if n > period:
        atr[period] = np.nanmean(tr[1 : period + 1])
    alpha = 1.0 / period
    for i in range(period + 1, n):
        atr[i] = atr[i - 1] + alpha * (tr[i] - atr[i - 1])

    # 基础上下轨
    hl2 = (high + low) / 2.0
    basic_upper = hl2 + multiplier * atr
    basic_lower = hl2 - multiplier * atr

    # 带跟踪的最终上下轨
    final_upper = np.full(n, np.nan)
    final_lower = np.full(n, np.nan)
    direction = np.zeros(n, dtype=np.int8)
    st = np.full(n, np.nan)

    # 初始化：在有效 ATR 开始处取基础轨道值
    init = period + 1
    if init >= n:
        return st, direction, atr, final_upper, final_lower

    final_upper[init] = basic_upper[init]
    final_lower[init] = basic_lower[init]
    # 初始方向按收盘价靠哪边更近来判断
    if close[init] > basic_upper[init]:
        direction[init] = 1
    elif close[init] < basic_lower[init]:
        direction[init] = -1
    else:
        direction[init] = 1  # 默认看多
    st[init] = final_lower[init] if direction[init] == 1 else final_upper[init]

    for i in range(init + 1, n):
        # 最终上轨：当基础值更低或上根收盘价已突破时下移
        fu_prev = final_upper[i - 1]
        fl_prev = final_lower[i - 1]
        if np.isfinite(basic_upper[i]) and (basic_upper[i] < fu_prev or close[i - 1] > fu_prev):
            final_upper[i] = basic_upper[i]
        else:
            final_upper[i] = fu_prev

        # 最终下轨：当基础值更高或上根收盘价已跌破时上移
        if np.isfinite(basic_lower[i]) and (basic_lower[i] > fl_prev or close[i - 1] < fl_prev):
            final_lower[i] = basic_lower[i]
        else:
            final_lower[i] = fl_prev

        # 方向翻转判定
        prev_dir = direction[i - 1]
        if prev_dir <= 0 and close[i] > final_upper[i - 1]:
            direction[i] = 1
        elif prev_dir >= 0 and close[i] < final_lower[i - 1]:
            direction[i] = -1
        else:
            direction[i] = prev_dir

        st[i] = final_lower[i] if direction[i] == 1 else final_upper[i]

    return st, direction, atr, final_upper, final_lower


# ---------------------------------------------------------------------------
# 信号生成
# ---------------------------------------------------------------------------

def _generate_signals(
    close: np.ndarray,
    st_dir: np.ndarray,
    adx: np.ndarray,
    ema_long: np.ndarray,
    params: SuperTrendParams,
) -> np.ndarray:
    """按 SuperTrend 方向翻转 + ADX 滤波 + EMA 确认生成事件信号。"""
    n = len(close)
    signal = np.zeros(n, dtype=np.int8)
    warmup = max(params.atr_period, params.adx_period, params.ema_period) + 3

    for i in range(warmup, n):
        prev_dir = st_dir[i - 1]
        curr_dir = st_dir[i]
        if prev_dir == 0 or curr_dir == 0:
            continue

        # 买入：SuperTrend 由空翻多 + ADX 确认趋势 + 价格在长期均线上方
        if prev_dir == -1 and curr_dir == 1:
            adx_ok = np.isfinite(adx[i]) and adx[i] >= params.min_adx
            ema_ok = np.isfinite(ema_long[i]) and close[i] > ema_long[i]
            if adx_ok and ema_ok:
                signal[i] = 1

        # 卖出：SuperTrend 由多翻空（无条件出场，保护本金）
        elif prev_dir == 1 and curr_dir == -1:
            signal[i] = -1

    return signal


# ---------------------------------------------------------------------------
# 策略入口
# ---------------------------------------------------------------------------

def _run(
    df: pd.DataFrame,
    base: BaseBacktestParams,
    params: SuperTrendParams,
) -> BacktestResult:
    high = df["high"].astype(float).values
    low = df["low"].astype(float).values
    close = df["close"].astype(float).values

    # SuperTrend
    st, st_dir, atr, final_upper, final_lower = _compute_supertrend(
        high, low, close, params.atr_period, params.multiplier,
    )

    # ADX
    adx, plus_di, minus_di = _compute_adx(high, low, close, params.adx_period)

    # 长期 EMA
    close_s = pd.Series(close)
    ema_long = close_s.ewm(span=params.ema_period, adjust=False).mean().values

    # 信号
    signal = _generate_signals(close, st_dir, adx, ema_long, params)

    return run_from_signals(
        df,
        signal,
        base.initial_cash,
        commission=base.commission,
        stop_loss_pct=base.stop_loss_pct,
        overlays={
            "supertrend": st,
            "ema_long": ema_long,
            "adx": adx,
            "plus_di": plus_di,
            "minus_di": minus_di,
        },
    )


def _min_bars(params: SuperTrendParams) -> int:
    return max(params.atr_period, params.adx_period, params.ema_period) + 10


STRATEGY = StrategySpec(
    id="supertrend",
    name="超级趋势",
    description=(
        "SuperTrend（ATR 通道突破）叠加 ADX 趋势强度过滤与长期 EMA 确认："
        "SuperTrend 由空翻多 + ADX ≥ 阈值 + 价格在 EMA 上方时买入，多翻空时无条件卖出。"
    ),
    params_model=SuperTrendParams,
    min_bars=_min_bars,
    run=_run,
)
