"""BBI 趋势策略：收盘价上穿 BBI 买入，下穿 BBI 卖出。"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field, model_validator

from app.backtest_engine import BacktestResult, run_from_signals
from app.indicators import bbi
from app.strategies.base import BaseBacktestParams, StrategySpec


class BbiTrendParams(BaseModel):
    short_period: int = Field(3, ge=1, le=200, description="第一条简单均线周期")
    medium_period: int = Field(6, ge=1, le=250, description="第二条简单均线周期")
    long_period: int = Field(12, ge=2, le=300, description="第三条简单均线周期")
    longest_period: int = Field(24, ge=3, le=400, description="第四条简单均线周期")

    @model_validator(mode="after")
    def periods_are_strictly_increasing(self) -> BbiTrendParams:
        periods = self.periods
        if any(left >= right for left, right in zip(periods, periods[1:])):
            raise ValueError("BBI 均线周期必须严格递增")
        return self

    @property
    def periods(self) -> tuple[int, int, int, int]:
        return (
            self.short_period,
            self.medium_period,
            self.long_period,
            self.longest_period,
        )


def _signals_from_bbi(close: np.ndarray, bbi_values: np.ndarray) -> np.ndarray:
    """按收盘价与 BBI 的交叉生成事件型买卖信号。"""
    if close.ndim != 1 or bbi_values.ndim != 1 or len(close) != len(bbi_values):
        raise ValueError("close 与 bbi_values 必须是等长一维数组")

    signal = np.zeros(len(close), dtype=np.int8)
    for i in range(1, len(close)):
        if not (
            np.isfinite(close[i])
            and np.isfinite(close[i - 1])
            and np.isfinite(bbi_values[i])
            and np.isfinite(bbi_values[i - 1])
        ):
            continue
        if close[i] > bbi_values[i] and close[i - 1] <= bbi_values[i - 1]:
            signal[i] = 1
        elif close[i] < bbi_values[i] and close[i - 1] >= bbi_values[i - 1]:
            signal[i] = -1
    return signal


def _run(
    df: pd.DataFrame,
    base: BaseBacktestParams,
    params: BbiTrendParams,
) -> BacktestResult:
    close = df["close"].astype(float).to_numpy()
    bbi_values = bbi(close, periods=params.periods)
    signal = _signals_from_bbi(close, bbi_values)
    return run_from_signals(
        df,
        signal,
        base.initial_cash,
        commission=base.commission,
        stop_loss_pct=base.stop_loss_pct,
        overlays={"bbi": bbi_values},
    )


def _min_bars(params: BbiTrendParams) -> int:
    return params.longest_period + 1


STRATEGY = StrategySpec(
    id="bbi_trend",
    name="BBI 多空趋势",
    description="收盘价上穿 BBI 时全仓买入，下穿 BBI 时全仓卖出。",
    params_model=BbiTrendParams,
    min_bars=_min_bars,
    run=_run,
)
