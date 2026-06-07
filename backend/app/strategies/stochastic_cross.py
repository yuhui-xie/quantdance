"""随机指标：超卖区 K 上穿 D 买入，超买区 K 下穿 D 卖出。"""

from __future__ import annotations

import pandas as pd
import numpy as np
from pydantic import BaseModel, Field, model_validator

from app.backtest_engine import BacktestResult, run_from_signals
from app.strategies.base import BaseBacktestParams, StrategySpec


class StochasticCrossParams(BaseModel):
    k_period: int = Field(14, ge=2, le=200)
    d_period: int = Field(3, ge=1, le=100)
    smooth: int = Field(3, ge=1, le=50)
    oversold: float = Field(20.0, ge=1, le=50)
    overbought: float = Field(80.0, ge=50, le=99)

    @model_validator(mode="after")
    def oversold_below_overbought(self) -> StochasticCrossParams:
        if self.oversold >= self.overbought:
            raise ValueError("oversold 必须小于 overbought")
        return self


def _run(
    df: pd.DataFrame,
    base: BaseBacktestParams,
    params: StochasticCrossParams,
) -> BacktestResult:
    close = df["close"].astype(float).values
    high = df["high"].astype(float).values
    low = df["low"].astype(float).values

    lowest = pd.Series(low).rolling(params.k_period).min()
    highest = pd.Series(high).rolling(params.k_period).max()
    span = (highest - lowest).replace(0, np.nan)
    raw_k = 100.0 * (pd.Series(close) - lowest) / span
    k_line = raw_k.rolling(params.smooth).mean().values
    d_line = pd.Series(k_line).rolling(params.d_period).mean().values

    signal = np.zeros(len(close), dtype=np.int8)
    for i in range(1, len(close)):
        if (
            np.isnan(k_line[i])
            or np.isnan(k_line[i - 1])
            or np.isnan(d_line[i])
            or np.isnan(d_line[i - 1])
        ):
            continue
        if (
            k_line[i] > d_line[i]
            and k_line[i - 1] <= d_line[i - 1]
            and k_line[i - 1] < params.oversold
        ):
            signal[i] = 1
        elif (
            k_line[i] < d_line[i]
            and k_line[i - 1] >= d_line[i - 1]
            and k_line[i - 1] > params.overbought
        ):
            signal[i] = -1

    return run_from_signals(
        df,
        signal,
        base.initial_cash,
        commission=base.commission,
        overlays={"stoch_k": k_line, "stoch_d": d_line},
    )


def _min_bars(params: StochasticCrossParams) -> int:
    return params.k_period + params.smooth + params.d_period + 5


STRATEGY = StrategySpec(
    id="stochastic_cross",
    name="随机指标交叉",
    description="%K 在超卖区上穿 %D 买入，在超买区下穿 %D 卖出。",
    params_model=StochasticCrossParams,
    min_bars=_min_bars,
    run=_run,
)
