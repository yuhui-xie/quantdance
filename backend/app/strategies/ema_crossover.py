"""双 EMA 金叉买入、死叉卖出。"""

from __future__ import annotations

import pandas as pd
import numpy as np
from pydantic import BaseModel, Field, model_validator

from app.backtest_engine import BacktestResult, run_from_signals
from app.strategies.base import BaseBacktestParams, StrategySpec


class EmaCrossoverParams(BaseModel):
    fast_period: int = Field(10, ge=2, le=200)
    slow_period: int = Field(30, ge=3, le=400)

    @model_validator(mode="after")
    def slow_gt_fast(self) -> EmaCrossoverParams:
        if self.slow_period <= self.fast_period:
            raise ValueError("slow_period 必须大于 fast_period")
        return self


def _run(
    df: pd.DataFrame,
    base: BaseBacktestParams,
    params: EmaCrossoverParams,
) -> BacktestResult:
    close = df["close"].astype(float).values
    close_s = pd.Series(close)
    fast_ema = close_s.ewm(span=params.fast_period, adjust=False).mean().values
    slow_ema = close_s.ewm(span=params.slow_period, adjust=False).mean().values

    signal = np.zeros(len(close), dtype=np.int8)
    for i in range(1, len(close)):
        if np.isnan(fast_ema[i]) or np.isnan(slow_ema[i]):
            continue
        if fast_ema[i] > slow_ema[i] and fast_ema[i - 1] <= slow_ema[i - 1]:
            signal[i] = 1
        elif fast_ema[i] < slow_ema[i] and fast_ema[i - 1] >= slow_ema[i - 1]:
            signal[i] = -1

    return run_from_signals(
        df,
        signal,
        base.initial_cash,
        commission=base.commission,
        overlays={"fast_ma": fast_ema, "slow_ma": slow_ema},
    )


def _min_bars(params: EmaCrossoverParams) -> int:
    return params.slow_period


STRATEGY = StrategySpec(
    id="ema_crossover",
    name="双 EMA 交叉",
    description="快慢指数均线金叉全仓买入、死叉全仓卖出；手续费按比率扣除。",
    params_model=EmaCrossoverParams,
    min_bars=_min_bars,
    run=_run,
)
