"""多头排列策略：均线多头排列满仓买入、排列走坏空仓卖出。

利用 ``bullish_alignment`` 因子把“短 > 中 > 长”均线排列量化为 [0, 1] 强度，
用带滞回的上下阈值把强度切换为持仓状态，避免排列临界处来回打脸。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field, model_validator

from app.backtest_engine import BacktestResult, run_from_signals
from app.factors.bullish_alignment import bullish_alignment
from app.strategies.base import BaseBacktestParams, StrategySpec


class BullishAlignmentParams(BaseModel):
    periods: tuple[int, ...] = Field(
        (5, 10, 20),
        min_length=2,
        description="按周期升序排列的均线周期",
    )
    enter_threshold: float = Field(
        1.0,
        ge=0,
        le=1,
        description="由空仓转持仓的排列强度阈值",
    )
    exit_threshold: float = Field(
        0.5,
        ge=0,
        le=1,
        description="由持仓转空仓的排列强度阈值",
    )

    @model_validator(mode="after")
    def validate_params(self) -> BullishAlignmentParams:
        if any(
            isinstance(period, (bool, np.bool_))
            or not isinstance(period, (int, np.integer))
            for period in self.periods
        ):
            raise TypeError("periods 必须全部为整数")
        if any(period < 1 for period in self.periods):
            raise ValueError("periods 必须全部大于等于 1")
        if len(self.periods) != len(set(self.periods)):
            raise ValueError("periods 必须互不重复")
        if self.exit_threshold >= self.enter_threshold:
            raise ValueError("exit_threshold 必须小于 enter_threshold，以避免持仓状态反复切换")
        return self


def _positions_and_score(
    close: np.ndarray,
    params: BullishAlignmentParams,
) -> tuple[np.ndarray, np.ndarray]:
    score = bullish_alignment(close, periods=params.periods)
    positions = np.zeros(len(score), dtype=np.int8)
    active = 0
    for i, value in enumerate(score):
        if not np.isfinite(value):
            positions[i] = active
            continue
        if not active and value >= params.enter_threshold:
            active = 1
        elif active and value <= params.exit_threshold:
            active = 0
        positions[i] = active
    return positions, score


def _positions_to_events(positions: np.ndarray) -> np.ndarray:
    events = np.zeros(len(positions), dtype=np.int8)
    previous = 0
    for i, current in enumerate(positions):
        value = int(current)
        if value != previous:
            events[i] = 1 if value else -1
        previous = value
    return events


def _run(
    df: pd.DataFrame,
    base: BaseBacktestParams,
    params: BullishAlignmentParams,
) -> BacktestResult:
    close = df["close"].astype(float).to_numpy()
    positions, score = _positions_and_score(close, params)
    signal = _positions_to_events(positions)
    overlays: dict[str, np.ndarray] = {
        "bullish_alignment": score,
        "bullish_position": positions,
    }
    # 各周期均线曲线，供回测图叠加展示多头排列形态。
    close_series = pd.Series(close)
    for period in params.periods:
        overlays[f"ma_{int(period)}"] = (
            close_series.rolling(window=int(period), min_periods=int(period))
            .mean()
            .to_numpy()
        )
    return run_from_signals(
        df,
        signal,
        base.initial_cash,
        commission=base.commission,
        stop_loss_pct=base.stop_loss_pct,
        overlays=overlays,
    )


def _signals(
    df: pd.DataFrame,
    base: BaseBacktestParams,
    params: BullishAlignmentParams,
) -> np.ndarray:
    close = df["close"].astype(float).to_numpy()
    positions, _score = _positions_and_score(close, params)
    return _positions_to_events(positions)


def _min_bars(params: BullishAlignmentParams) -> int:
    return int(max(params.periods))


STRATEGY = StrategySpec(
    id="bullish_alignment",
    name="多头排列",
    description="短中长期均线呈多头排列（短 > 中 > 长）时满仓买入，排列走坏时空仓卖出；带滞回阈值防抖。",
    params_model=BullishAlignmentParams,
    min_bars=_min_bars,
    run=_run,
    signals=_signals,
)
