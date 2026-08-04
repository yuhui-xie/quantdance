"""LLT 斜率转向策略：趋势转正买入，趋势转负卖出。"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from app.backtest_engine import BacktestResult, run_from_signals
from app.indicators import llt
from app.strategies.base import BaseBacktestParams, StrategySpec


class LltTrendParams(BaseModel):
    period: int = Field(20, ge=2, le=400, description="LLT 平滑周期")
    slope_lookback: int = Field(
        1,
        ge=1,
        le=60,
        description="判断 LLT 斜率时回看的交易日数",
    )


def _signals_from_llt(
    trend: np.ndarray,
    *,
    period: int,
    slope_lookback: int,
) -> np.ndarray:
    """仅按 LLT 斜率过零生成买卖信号，并跳过初始化阶段。"""
    signal = np.zeros(len(trend), dtype=np.int8)
    first = max(period, slope_lookback + 1)
    for i in range(first, len(trend)):
        current = trend[i] - trend[i - slope_lookback]
        previous = trend[i - 1] - trend[i - 1 - slope_lookback]
        if not np.isfinite(current) or not np.isfinite(previous):
            continue
        if current > 0 and previous <= 0:
            signal[i] = 1
        elif current < 0 and previous >= 0:
            signal[i] = -1
    return signal


def _run(
    df: pd.DataFrame,
    base: BaseBacktestParams,
    params: LltTrendParams,
) -> BacktestResult:
    close = df["close"].astype(float).to_numpy()
    trend = llt(close, period=params.period)
    signal = _signals_from_llt(
        trend,
        period=params.period,
        slope_lookback=params.slope_lookback,
    )
    return run_from_signals(
        df,
        signal,
        base.initial_cash,
        commission=base.commission,
        stop_loss_pct=base.stop_loss_pct,
        overlays={"llt": trend},
    )


def _min_bars(params: LltTrendParams) -> int:
    return params.period + params.slope_lookback + 1


STRATEGY = StrategySpec(
    id="llt_trend",
    name="LLT 趋势拐点",
    description="仅使用 LLT：斜率由非正转正时全仓买入，由非负转负时全仓卖出。",
    params_model=LltTrendParams,
    min_bars=_min_bars,
    run=_run,
)
