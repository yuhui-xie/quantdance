"""my_strategy：多头排列 + LLT 斜率组合策略（初版）。

组合语义（双确认 + 滞回）：
- 进场：多头排列强度达标（score >= enter_threshold）**且** LLT 斜率向上，
  两个条件同时满足才由空仓转持仓，避免单独任一指标的假信号。
- 离场：多头排列走坏（score <= exit_threshold）**或** LLT 斜率转下，
  任一条件触发即由持仓转空仓，及时离场。

「结构」由多头因子刻画（短>中>长均线排列），「方向/时机」由 LLT 斜率刻画。

大环境过滤（可选）：用 Choppiness Index 判断行情是否处于震荡市。
当 CHOP > chop_threshold（默认 62）判定为震荡市，此时不开趋势单、
强制空仓，避免趋势策略在无趋势行情中反复进出。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from app.backtest_engine import BacktestResult, run_from_signals
from app.factors import choppiness_index, llt
from app.factors.bullish_alignment import bullish_alignment
from app.factors.llt import llt_slope
from app.strategies.base import BaseBacktestParams, StrategySpec


class MyStrategyParams(BaseModel):
    # --- 多头排列（结构确认）---
    periods: tuple[int, ...] = Field(
        (5, 10, 20),
        min_length=2,
        description="按周期升序排列的均线周期",
    )
    enter_threshold: float = Field(
        1.0,
        ge=0,
        le=1,
        description="由空仓转持仓所需的多头排列强度阈值",
    )
    exit_threshold: float = Field(
        0.5,
        ge=0,
        le=1,
        description="由持仓转空仓的多头排列强度阈值",
    )

    # --- LLT 斜率（动量方向）---
    llt_period: int = Field(20, ge=2, le=400, description="LLT 平滑周期")
    slope_lookback: int = Field(
        1,
        ge=1,
        le=60,
        description="判断 LLT 斜率时回看的交易日数",
    )
    slope_threshold: float = Field(
        0.0,
        ge=0.0,
        description=(
            "斜率阈值（价格单位）：斜率上穿 +threshold 才买入，下穿 "
            "-threshold 才卖出；介于正负阈值之间为观望死区，用于过滤拐点附近"
            "的噪音震荡。设 0 即退化为原始过零逻辑。"
        ),
    )

    # --- 大环境过滤（震荡市不开趋势单）---
    use_chop_filter: bool = Field(
        True,
        description="是否启用 Choppiness 震荡市过滤；为 False 时按原逻辑交易",
    )
    chop_period: int = Field(14, ge=2, le=400, description="Choppiness Index 周期")
    chop_threshold: float = Field(
        62.0,
        ge=0,
        le=100,
        description="CHOP 高于此值判定为震荡市，不开趋势单（空仓）",
    )

    def _validate_periods(self) -> None:
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


def _positions(
    score: np.ndarray,
    slope: np.ndarray,
    ranging: np.ndarray,
    params: MyStrategyParams,
) -> np.ndarray:
    """按「多头排列达标 且 斜率向上」进场、「任一走坏」离场，生成持仓状态。

    - 进场：``score >= enter_threshold`` **且** ``slope > +slope_threshold``
      同时满足才由空仓转持仓（双确认；斜率落在死区时不进场）。
    - 离场：``score <= exit_threshold`` **或** ``slope < -slope_threshold``
      任一触发即由持仓转空仓。

    ``ranging`` 为布尔掩码，True 表示处于震荡市（CHOP > threshold），
    该区间强制空仓、不开趋势单。
    """
    n = len(slope)
    positions = np.zeros(n, dtype=np.int8)
    active = 0
    for i in range(n):
        # 震荡市：不开趋势单，强制空仓
        if ranging[i]:
            active = 0
            positions[i] = active
            continue
        sc = score[i]
        sl = slope[i]
        if not np.isfinite(sc) or not np.isfinite(sl):
            positions[i] = active
            continue
        if not active:
            if sc >= params.enter_threshold and sl > params.slope_threshold:
                active = 1
        else:
            if sc <= params.exit_threshold or sl < -params.slope_threshold:
                active = 0
        positions[i] = active
    return positions


def _positions_to_events(positions: np.ndarray) -> np.ndarray:
    events = np.zeros(len(positions), dtype=np.int8)
    previous = 0
    for i, current in enumerate(positions):
        value = int(current)
        if value != previous:
            events[i] = 1 if value else -1
        previous = value
    return events


def _compute(
    df: pd.DataFrame,
    params: MyStrategyParams,
) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """计算持仓状态、事件信号与叠加曲线。"""
    close = df["close"].astype(float).to_numpy()
    score = bullish_alignment(close, periods=params.periods)
    trend = llt(close, period=params.llt_period)
    slope = llt_slope(trend, params.slope_lookback)

    if params.use_chop_filter:
        chop = choppiness_index(
            df["high"].astype(float).to_numpy(),
            df["low"].astype(float).to_numpy(),
            close,
            period=params.chop_period,
        )
    else:
        chop = np.full(len(close), np.nan, dtype=float)
    ranging = np.where(np.isfinite(chop), chop > params.chop_threshold, False)

    positions = _positions(score, slope, ranging, params)
    signal = _positions_to_events(positions)
    overlays: dict[str, np.ndarray] = {
        "llt": trend,
        "llt_dt": slope,
        "bullish_alignment": score,
        "chop": chop,
        "chop_ranging": ranging.astype(np.int8),
        "position": positions,
    }
    return signal, overlays


def _run(
    df: pd.DataFrame,
    base: BaseBacktestParams,
    params: MyStrategyParams,
) -> BacktestResult:
    signal, overlays = _compute(df, params)
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
    params: MyStrategyParams,
) -> np.ndarray:
    signal, _overlays = _compute(df, params)
    return signal


def _min_bars(params: MyStrategyParams) -> int:
    need = max(params.periods)
    if params.use_chop_filter:
        need = max(need, params.chop_period)
    return int(need) + params.slope_lookback + 1


STRATEGY = StrategySpec(
    id="my_strategy",
    name="我的策略",
    description="多头排列（结构确认）与 LLT 斜率（动量方向）双确认组合策略；进场需两者同时满足，离场由任一走坏触发；可选启用 Choppiness（CHOP>62）震荡市过滤，震荡市不开趋势单、强制空仓。",
    params_model=MyStrategyParams,
    min_bars=_min_bars,
    run=_run,
    signals=_signals,
)

