"""布林带均值回归：触及下轨反弹买入、触及上轨回落卖出。"""

from __future__ import annotations

import pandas as pd
import numpy as np
from pydantic import BaseModel, Field

from app.backtest_engine import BacktestResult, run_from_signals
from app.strategies.base import BaseBacktestParams, StrategySpec


class BollingerReversionParams(BaseModel):
    period: int = Field(20, ge=2, le=400)
    num_std: float = Field(2.0, ge=0.1, le=10.0)


def _run(
    df: pd.DataFrame,
    base: BaseBacktestParams,
    params: BollingerReversionParams,
) -> BacktestResult:
    close = df["close"].astype(float).values
    close_s = pd.Series(close)
    middle = close_s.rolling(params.period).mean().values
    std = close_s.rolling(params.period).std(ddof=0).values
    upper = middle + params.num_std * std
    lower = middle - params.num_std * std

    signal = np.zeros(len(close), dtype=np.int8)
    for i in range(1, len(close)):
        if (
            np.isnan(lower[i])
            or np.isnan(lower[i - 1])
            or np.isnan(upper[i])
            or np.isnan(upper[i - 1])
        ):
            continue
        if close[i - 1] < lower[i - 1] and close[i] >= lower[i]:
            signal[i] = 1
        elif close[i - 1] > upper[i - 1] and close[i] <= upper[i]:
            signal[i] = -1

    return run_from_signals(
        df,
        signal,
        base.initial_cash,
        commission=base.commission,
        overlays={
            "bb_upper": upper,
            "bb_middle": middle,
            "bb_lower": lower,
        },
    )


def _min_bars(params: BollingerReversionParams) -> int:
    return params.period + 5


STRATEGY = StrategySpec(
    id="bollinger_reversion",
    name="布林带均值回归",
    description="收盘价自下而上上穿下轨买入，自上而下下穿上轨卖出。",
    params_model=BollingerReversionParams,
    min_bars=_min_bars,
    run=_run,
)
