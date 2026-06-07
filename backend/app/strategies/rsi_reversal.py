"""Wilder RSI 超卖上穿买入、超买下穿卖出。"""

from __future__ import annotations

import pandas as pd
import numpy as np
from pydantic import BaseModel, Field, model_validator

from app.backtest_engine import BacktestResult, run_from_signals
from app.strategies.base import BaseBacktestParams, StrategySpec


class RsiReversalParams(BaseModel):
    period: int = Field(14, ge=2, le=200)
    oversold: float = Field(30.0, ge=1, le=50)
    overbought: float = Field(70.0, ge=50, le=99)

    @model_validator(mode="after")
    def oversold_below_overbought(self) -> RsiReversalParams:
        if self.oversold >= self.overbought:
            raise ValueError("oversold 必须小于 overbought")
        return self


def _wilder_rsi(close: np.ndarray, period: int) -> np.ndarray:
    """Wilder RSI，与常见交易软件一致。"""
    n = len(close)
    rsi = np.full(n, np.nan)
    if period < 2 or n <= period:
        return rsi
    delta = np.diff(close, prepend=close[0])
    gains = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)
    avg_gain = float(np.mean(gains[1 : period + 1]))
    avg_loss = float(np.mean(losses[1 : period + 1]))
    if avg_loss == 0:
        rsi[period] = 100.0 if avg_gain > 0 else 50.0
    else:
        rs = avg_gain / avg_loss
        rsi[period] = 100.0 - (100.0 / (1.0 + rs))
    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rsi[i] = 100.0 if avg_gain > 0 else 50.0
        else:
            rs = avg_gain / avg_loss
            rsi[i] = 100.0 - (100.0 / (1.0 + rs))
    return rsi


def _run(
    df: pd.DataFrame,
    base: BaseBacktestParams,
    params: RsiReversalParams,
) -> BacktestResult:
    close = df["close"].astype(float).values
    rsi = _wilder_rsi(close, params.period)

    signal = np.zeros(len(close), dtype=np.int8)
    for i in range(1, len(close)):
        if not np.isfinite(rsi[i]) or not np.isfinite(rsi[i - 1]):
            continue
        if rsi[i] > params.oversold and rsi[i - 1] <= params.oversold:
            signal[i] = 1
        elif rsi[i] < params.overbought and rsi[i - 1] >= params.overbought:
            signal[i] = -1

    return run_from_signals(
        df,
        signal,
        base.initial_cash,
        commission=base.commission,
        overlays={"rsi": rsi},
    )


def _min_bars(params: RsiReversalParams) -> int:
    return params.period + 5


STRATEGY = StrategySpec(
    id="rsi_reversal",
    name="RSI 反转",
    description="RSI 自下而上穿越超卖线买入，自上而下穿越超买线卖出；Wilder 平滑。",
    params_model=RsiReversalParams,
    min_bars=_min_bars,
    run=_run,
)
