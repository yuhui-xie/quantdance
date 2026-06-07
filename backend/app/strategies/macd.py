"""MACD 线与信号线金叉买入、死叉卖出。"""

from __future__ import annotations

import pandas as pd
import numpy as np
from pydantic import BaseModel, Field, model_validator

from app.backtest_engine import BacktestResult, run_from_signals
from app.strategies.base import BaseBacktestParams, StrategySpec


class MacdParams(BaseModel):
    fast_period: int = Field(12, ge=2, le=200)
    slow_period: int = Field(26, ge=3, le=400)
    signal_period: int = Field(9, ge=2, le=200)

    @model_validator(mode="after")
    def slow_gt_fast(self) -> MacdParams:
        if self.slow_period <= self.fast_period:
            raise ValueError("slow_period 必须大于 fast_period")
        return self


def _run(
    df: pd.DataFrame,
    base: BaseBacktestParams,
    params: MacdParams,
) -> BacktestResult:
    close = df["close"].astype(float).values
    close_s = pd.Series(close)
    ema_fast = close_s.ewm(span=params.fast_period, adjust=False).mean().values
    ema_slow = close_s.ewm(span=params.slow_period, adjust=False).mean().values
    macd_line = ema_fast - ema_slow
    signal_line = pd.Series(macd_line).ewm(span=params.signal_period, adjust=False).mean().values
    hist = macd_line - signal_line

    signal = np.zeros(len(close), dtype=np.int8)
    for i in range(1, len(close)):
        if np.isnan(macd_line[i]) or np.isnan(signal_line[i]):
            continue
        if macd_line[i] > signal_line[i] and macd_line[i - 1] <= signal_line[i - 1]:
            signal[i] = 1
        elif macd_line[i] < signal_line[i] and macd_line[i - 1] >= signal_line[i - 1]:
            signal[i] = -1

    return run_from_signals(
        df,
        signal,
        base.initial_cash,
        commission=base.commission,
        overlays={
            "macd": macd_line,
            "macd_signal": signal_line,
            "macd_hist": hist,
        },
    )


def _min_bars(params: MacdParams) -> int:
    return params.slow_period + params.signal_period + 5


STRATEGY = StrategySpec(
    id="macd",
    name="MACD 交叉",
    description="MACD 上穿信号线全仓买入、下穿全仓卖出；手续费按比率扣除。",
    params_model=MacdParams,
    min_bars=_min_bars,
    run=_run,
)
