"""唐奇安通道突破：突破 N 日（不含当日）高买、跌破 N 日低卖。"""

from __future__ import annotations

import pandas as pd
import numpy as np
from pydantic import BaseModel, Field

from app.backtest_engine import BacktestResult, run_from_signals
from app.strategies.base import BaseBacktestParams, StrategySpec


class DonchianBreakoutParams(BaseModel):
    period: int = Field(20, ge=1, le=400)


def _run(
    df: pd.DataFrame,
    base: BaseBacktestParams,
    params: DonchianBreakoutParams,
) -> BacktestResult:
    close = df["close"].astype(float).values
    high = df["high"].astype(float).values
    low = df["low"].astype(float).values

    n = len(close)
    ch_high = np.full(n, np.nan)
    ch_low = np.full(n, np.nan)
    for i in range(params.period, n):
        ch_high[i] = float(np.max(high[i - params.period : i]))
        ch_low[i] = float(np.min(low[i - params.period : i]))

    signal = np.zeros(n, dtype=np.int8)
    for i in range(params.period + 1, n):
        if not np.isfinite(ch_high[i]) or not np.isfinite(ch_high[i - 1]):
            continue
        if not np.isfinite(ch_low[i]) or not np.isfinite(ch_low[i - 1]):
            continue
        if close[i] > ch_high[i] and close[i - 1] <= ch_high[i - 1]:
            signal[i] = 1
        elif close[i] < ch_low[i] and close[i - 1] >= ch_low[i - 1]:
            signal[i] = -1

    return run_from_signals(
        df,
        signal,
        base.initial_cash,
        commission=base.commission,
        overlays={"donchian_high": ch_high, "donchian_low": ch_low},
    )


def _min_bars(params: DonchianBreakoutParams) -> int:
    return params.period + 5


STRATEGY = StrategySpec(
    id="donchian_breakout",
    name="唐奇安突破",
    description="收盘价突破过去 N 日（不含当日）最高价买入，跌破过去 N 日最低价卖出。",
    params_model=DonchianBreakoutParams,
    min_bars=_min_bars,
    run=_run,
)
