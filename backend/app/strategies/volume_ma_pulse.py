"""量比上穿放量阈值买入、下穿缩量阈值卖出。"""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field, model_validator

from app.backtest_engine import BacktestResult, run_from_signals
from app.strategies.base import BaseBacktestParams, StrategySpec


class VolumeMaPulseParams(BaseModel):
    volume_ma_period: int = Field(20, ge=2, le=500, description="活动量均线周期 N（SMA，不含当日）")
    volume_metric: Literal["volume", "amount"] = Field(
        "amount",
        description="量比分子/分母所用字段：成交量或成交额（无 amount 时回退 volume）",
    )
    threshold_mode: Literal["fixed", "percentile"] = Field(
        "fixed",
        description="阈值模式：fixed 用固定倍数；percentile 用滚动分位",
    )
    high_ratio: float = Field(1.5, gt=1, description="固定模式放量阈值：量比上穿该值触发买入")
    low_ratio: float = Field(0.8, lt=1, description="固定模式缩量阈值：量比下穿该值触发卖出")
    high_percentile: float = Field(
        0.8, gt=0, lt=1, description="分位模式放量阈值：过去窗口量比的高分位"
    )
    low_percentile: float = Field(
        0.2, gt=0, lt=1, description="分位模式缩量阈值：过去窗口量比的低分位"
    )
    percentile_lookback: int = Field(
        120, ge=20, le=1000, description="分位模式滚动窗口长度（不含当日）"
    )
    require_bull_bar: bool = Field(True, description="买入时是否要求阳线 close > open")
    require_bear_bar: bool = Field(True, description="卖出时是否要求阴线 close < open")
    price_ma_period: int = Field(20, ge=2, le=500, description="价格均线过滤周期")
    trend_ma_period: int = Field(60, ge=2, le=500, description="趋势均线过滤周期")
    breakout_period: int = Field(20, ge=2, le=500, description="价格突破过滤周期")
    require_price_above_ma: bool = Field(False, description="买入时是否要求 close > 价格均线")
    require_trend_up: bool = Field(False, description="买入时是否要求 close > 趋势均线且趋势均线向上")
    require_breakout: bool = Field(False, description="买入时是否要求 close 突破前 N 日收盘高点")
    entry_confirm_bars: int = Field(
        0,
        ge=0,
        le=5,
        description="放量日后再观察 N 日确认买入：0=当日买；>0 时站稳放量日收盘才买，跌破放量日低点作废",
    )
    stop_loss_pct: float | None = Field(None, gt=0, le=0.8, description="收盘价相对入场价跌幅止损")
    take_profit_pct: float | None = Field(None, gt=0, le=5, description="收盘价相对入场价涨幅止盈")

    @model_validator(mode="after")
    def _thresholds_ordered(self) -> "VolumeMaPulseParams":
        if self.threshold_mode == "fixed":
            if self.high_ratio <= self.low_ratio:
                raise ValueError("需要 high_ratio > low_ratio")
        elif self.high_percentile <= self.low_percentile:
            raise ValueError("需要 high_percentile > low_percentile")
        return self


def _activity_series(df: pd.DataFrame, metric: Literal["volume", "amount"]) -> pd.Series:
    if metric == "amount" and "amount" in df.columns:
        amount = pd.to_numeric(df["amount"], errors="coerce")
        if amount.notna().any() and float(amount.fillna(0).abs().sum()) > 0:
            return amount.astype(float)
    return df["volume"].astype(float)


def _bar_low(df: pd.DataFrame, close: np.ndarray, open_a: np.ndarray) -> np.ndarray:
    if "low" in df.columns:
        low = pd.to_numeric(df["low"], errors="coerce").astype(float).values
        fallback = np.minimum(close, open_a)
        return np.where(np.isfinite(low), low, fallback)
    return np.minimum(close, open_a)


def _filters_ok(
    i: int,
    *,
    close: np.ndarray,
    open_a: np.ndarray,
    price_ma: np.ndarray,
    trend_ma: np.ndarray,
    breakout_high: np.ndarray,
    params: VolumeMaPulseParams,
    require_bull: bool,
) -> bool:
    bull_ok = (not require_bull) or close[i] > open_a[i]
    price_ma_ok = (
        not params.require_price_above_ma
        or (np.isfinite(price_ma[i]) and close[i] > price_ma[i])
    )
    trend_up_ok = (
        not params.require_trend_up
        or (
            np.isfinite(trend_ma[i])
            and np.isfinite(trend_ma[i - 1])
            and close[i] > trend_ma[i]
            and trend_ma[i] > trend_ma[i - 1]
        )
    )
    breakout_ok = (
        not params.require_breakout
        or (np.isfinite(breakout_high[i]) and close[i] > breakout_high[i])
    )
    return bool(bull_ok and price_ma_ok and trend_up_ok and breakout_ok)


def _run(
    df: pd.DataFrame,
    base: BaseBacktestParams,
    params: VolumeMaPulseParams,
) -> BacktestResult:
    close = df["close"].astype(float).values
    open_a = df["open"].astype(float).values
    low = _bar_low(df, close, open_a)
    activity = _activity_series(df, params.volume_metric)
    activity_v = activity.values

    # 均量取过去 N 日（不含当日），避免放量日抬高分母、压低量比。
    vol_ma = activity.shift(1).rolling(params.volume_ma_period).mean().values
    vol_ratio = np.full(len(close), np.nan)
    mask = (vol_ma > 0) & np.isfinite(vol_ma) & np.isfinite(activity_v)
    vol_ratio[mask] = activity_v[mask] / vol_ma[mask]

    if params.threshold_mode == "percentile":
        vr_s = pd.Series(vol_ratio)
        high_th = vr_s.shift(1).rolling(params.percentile_lookback).quantile(params.high_percentile).values
        low_th = vr_s.shift(1).rolling(params.percentile_lookback).quantile(params.low_percentile).values
    else:
        high_th = np.full(len(close), params.high_ratio, dtype=float)
        low_th = np.full(len(close), params.low_ratio, dtype=float)

    close_s = pd.Series(close)
    price_ma = close_s.rolling(params.price_ma_period).mean().values
    trend_ma = close_s.rolling(params.trend_ma_period).mean().values
    breakout_high = close_s.shift(1).rolling(params.breakout_period).max().values

    signal = np.zeros(len(close), dtype=np.int8)
    in_position = False
    entry_price = np.nan
    # 放量候选：(到期下标, 放量日收盘, 放量日最低)
    pending: tuple[int, float, float] | None = None
    for i in range(1, len(close)):
        if (
            not np.isfinite(vol_ratio[i])
            or not np.isfinite(vol_ratio[i - 1])
            or not np.isfinite(high_th[i])
            or not np.isfinite(low_th[i])
        ):
            continue

        volume_exit = vol_ratio[i - 1] >= low_th[i] and vol_ratio[i] < low_th[i]
        bear_ok = not params.require_bear_bar or close[i] < open_a[i]
        stop_loss = (
            params.stop_loss_pct is not None
            and np.isfinite(entry_price)
            and close[i] <= entry_price * (1 - params.stop_loss_pct)
        )
        take_profit = (
            params.take_profit_pct is not None
            and np.isfinite(entry_price)
            and close[i] >= entry_price * (1 + params.take_profit_pct)
        )
        if in_position and ((volume_exit and bear_ok) or stop_loss or take_profit):
            signal[i] = -1
            in_position = False
            entry_price = np.nan
            pending = None
            continue

        if in_position:
            continue

        # 确认窗口内：跌破放量日低点作废；站稳放量日收盘则买入。
        if pending is not None:
            expire_i, pulse_close, pulse_low = pending
            if i > expire_i or close[i] < pulse_low:
                pending = None
            elif close[i] >= pulse_close and _filters_ok(
                i,
                close=close,
                open_a=open_a,
                price_ma=price_ma,
                trend_ma=trend_ma,
                breakout_high=breakout_high,
                params=params,
                require_bull=params.require_bull_bar,
            ):
                signal[i] = 1
                in_position = True
                entry_price = close[i]
                pending = None
                continue

        volume_entry = vol_ratio[i - 1] <= high_th[i] and vol_ratio[i] > high_th[i]
        filters_ok = _filters_ok(
            i,
            close=close,
            open_a=open_a,
            price_ma=price_ma,
            trend_ma=trend_ma,
            breakout_high=breakout_high,
            params=params,
            require_bull=params.require_bull_bar,
        )
        if not volume_entry or not filters_ok:
            continue

        if params.entry_confirm_bars <= 0:
            signal[i] = 1
            in_position = True
            entry_price = close[i]
            pending = None
        else:
            # 新放量脉冲刷新候选；放量当日不买，延后观察。
            pending = (i + params.entry_confirm_bars, float(close[i]), float(low[i]))

    return run_from_signals(
        df,
        signal,
        base.initial_cash,
        commission=base.commission,
        overlays={
            "volume_ma": vol_ma,
            "vol_ratio": vol_ratio,
            "high_th": high_th,
            "low_th": low_th,
            "price_ma": price_ma,
            "trend_ma": trend_ma,
            "breakout_high": breakout_high,
        },
    )


def _min_bars(params: VolumeMaPulseParams) -> int:
    periods = [params.volume_ma_period + 1]
    if params.threshold_mode == "percentile":
        periods.append(params.volume_ma_period + params.percentile_lookback + 1)
    if params.require_price_above_ma:
        periods.append(params.price_ma_period)
    if params.require_trend_up:
        periods.append(params.trend_ma_period + 1)
    if params.require_breakout:
        periods.append(params.breakout_period + 1)
    periods.append(params.entry_confirm_bars + 1)
    return max(periods) + 5


STRATEGY = StrategySpec(
    id="volume_ma_pulse",
    name="量比放量/缩量脉冲",
    description="量比（相对过去N日均量，不含当日）上穿放量阈值买入、下穿缩量阈值或触发风控卖出；支持成交额、分位阈值与放量后确认延迟。",
    params_model=VolumeMaPulseParams,
    min_bars=_min_bars,
    run=_run,
)
