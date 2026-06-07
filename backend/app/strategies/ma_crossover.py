"""双均线金叉买入、死叉卖出。"""

from __future__ import annotations

import pandas as pd
import numpy as np
from pydantic import BaseModel, Field, model_validator

from app.backtest_engine import BacktestResult, run_from_signals
from app.strategies.base import BaseBacktestParams, StrategySpec


class MaCrossoverParams(BaseModel):
    fast_period: int = Field(10, ge=2, le=200)
    slow_period: int = Field(30, ge=3, le=400)

    @model_validator(mode="after")
    def slow_gt_fast(self) -> MaCrossoverParams:
        if self.slow_period <= self.fast_period:
            raise ValueError("slow_period 必须大于 fast_period")
        return self


def _run(
    df: pd.DataFrame,
    base: BaseBacktestParams,
    params: MaCrossoverParams,
) -> BacktestResult:
    close = df["close"].astype(float).values
    fast_ma = pd.Series(close).rolling(params.fast_period).mean().values
    slow_ma = pd.Series(close).rolling(params.slow_period).mean().values

    signal = np.zeros(len(close), dtype=np.int8)
    for i in range(1, len(close)):
        if np.isnan(fast_ma[i]) or np.isnan(slow_ma[i]):
            continue
        if fast_ma[i] > slow_ma[i] and fast_ma[i - 1] <= slow_ma[i - 1]:
            signal[i] = 1
        elif fast_ma[i] < slow_ma[i] and fast_ma[i - 1] >= slow_ma[i - 1]:
            signal[i] = -1

    return run_from_signals(
        df,
        signal,
        base.initial_cash,
        commission=base.commission,
        overlays={"fast_ma": fast_ma, "slow_ma": slow_ma},
    )


def _min_bars(params: MaCrossoverParams) -> int:
    return params.slow_period


STRATEGY = StrategySpec(
    id="ma_crossover",
    name="双均线交叉",
    description="快慢均线金叉全仓买入、死叉全仓卖出；手续费按比率扣除。",
    params_model=MaCrossoverParams,
    min_bars=_min_bars,
    run=_run,
)
