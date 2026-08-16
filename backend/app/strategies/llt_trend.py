"""LLT 斜率转向策略：趋势转正买入，趋势转负卖出。"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from app.backtest_engine import BacktestResult, run_from_signals
from app.factors import llt
from app.factors.llt import llt_slope
from app.strategies.base import BaseBacktestParams, StrategySpec


class LltTrendParams(BaseModel):
    period: int = Field(20, ge=2, le=400, description="LLT 平滑周期")
    slope_lookback: int = Field(
        1,
        ge=1,
        le=60,
        description="判断 LLT 斜率时回看的交易日数",
    )
    slope_threshold: float = Field(
        0.0,
        ge=0.0,
        description=(
            "斜率阈值（价格单位）：斜率上穿 +threshold 才买入，下穿 "
            "-threshold 才卖出；介于正负阈值之间为观望死区，用于过滤拐点附近"
            "的噪音震荡。设 0 即退化为原始过零逻辑。"
        ),
    )


def _signals_from_llt(
    trend: np.ndarray,
    *,
    period: int,
    slope_lookback: int,
    threshold: float = 0.0,
) -> np.ndarray:
    """按 LLT 斜率与阈值生成买卖信号，并跳过初始化阶段。

    只有当斜率由 ≤+threshold 上穿至 >+threshold 时买入，由 ≥-threshold
    下穿至 <-threshold 时卖出；斜率落在 (-threshold, +threshold) 区间内不
    产生任何信号（死区过滤噪音）。threshold 为 0 时等价于原始的过零逻辑。
    """
    signal = np.zeros(len(trend), dtype=np.int8)
    first = max(period, slope_lookback + 1)
    for i in range(first, len(trend)):
        current = trend[i] - trend[i - slope_lookback]
        previous = trend[i - 1] - trend[i - 1 - slope_lookback]
        if not np.isfinite(current) or not np.isfinite(previous):
            continue
        if current > threshold and previous <= threshold:
            signal[i] = 1
        elif current < -threshold and previous >= -threshold:
            signal[i] = -1
    return signal


def _run(
    df: pd.DataFrame,
    base: BaseBacktestParams,
    params: LltTrendParams,
) -> BacktestResult:
    close = df["close"].astype(float).to_numpy()
    trend = llt(close, period=params.period)
    slope = llt_slope(trend, params.slope_lookback)
    signal = _signals_from_llt(
        trend,
        period=params.period,
        slope_lookback=params.slope_lookback,
        threshold=params.slope_threshold,
    )
    return run_from_signals(
        df,
        signal,
        base.initial_cash,
        commission=base.commission,
        stop_loss_pct=base.stop_loss_pct,
        overlays={"llt": trend, "llt_dt": slope},
    )


def _min_bars(params: LltTrendParams) -> int:
    return params.period + params.slope_lookback + 1


STRATEGY = StrategySpec(
    id="llt_trend",
    name="LLT 趋势拐点",
    description="仅使用 LLT：斜率上穿 +slope_threshold 全仓买入，下穿 -slope_threshold 全仓卖出；正负阈值之间为观望死区（过滤噪音）。slope_threshold=0 时即过零拐点。",
    params_model=LltTrendParams,
    min_bars=_min_bars,
    run=_run,
)
