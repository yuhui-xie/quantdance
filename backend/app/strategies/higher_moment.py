"""高阶矩策略：滚动标准化中心矩经自适应 EMA 平滑后穿越零轴交易。"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field, model_validator

from app.backtest_engine import BacktestResult, run_from_signals
from app.indicators import ema_alpha, rolling_standardized_moment
from app.strategies.base import BaseBacktestParams, StrategySpec


class HigherMomentParams(BaseModel):
    order: int = Field(5, ge=3, le=10, description="标准化中心矩阶数")
    window: int = Field(20, ge=5, le=252, description="收益率滚动窗口")
    alpha: float = Field(0.20, gt=0.0, le=1.0, description="优化前的 EMA alpha")
    optimize_interval: int = Field(90, ge=1, le=1000, description="alpha 重选间隔")
    optimize_lookback: int = Field(252, ge=30, le=2000, description="优化回看期")
    alpha_min: float = Field(0.05, gt=0.0, le=1.0)
    alpha_max: float = Field(0.50, gt=0.0, le=1.0)
    alpha_step: float = Field(0.05, gt=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_optimization_params(self) -> HigherMomentParams:
        if self.alpha_max < self.alpha_min:
            raise ValueError("alpha_max 必须大于或等于 alpha_min")
        if not self.alpha_min <= self.alpha <= self.alpha_max:
            raise ValueError("alpha 必须位于 alpha_min 与 alpha_max 之间")
        if self.optimize_lookback <= self.window:
            raise ValueError("optimize_lookback 必须大于 window")
        return self


def _alpha_candidates(params: HigherMomentParams) -> list[float]:
    count = int(np.floor((params.alpha_max - params.alpha_min) / params.alpha_step + 1e-9))
    values = [params.alpha_min + i * params.alpha_step for i in range(count + 1)]
    if not values or values[-1] < params.alpha_max - 1e-9:
        values.append(params.alpha_max)
    return [float(min(value, params.alpha_max)) for value in values]


def _signals_from_smoothed(smoothed: np.ndarray) -> np.ndarray:
    """EMA 高阶矩上穿零轴买入、下穿零轴卖出。"""
    values = np.asarray(smoothed, dtype=float)
    signal = np.zeros(len(values), dtype=np.int8)
    previous = 0.0
    has_previous = False
    for i, current in enumerate(values):
        if not np.isfinite(current):
            continue
        baseline = previous if has_previous else 0.0
        if current > 0 and baseline <= 0:
            signal[i] = 1
        elif current < 0 and baseline >= 0:
            signal[i] = -1
        previous = float(current)
        has_previous = True
    return signal


def _select_alpha(
    df: pd.DataFrame,
    moment: np.ndarray,
    *,
    start: int,
    end: int,
    candidates: list[float],
    current_alpha: float,
    initial_cash: float,
    commission: float,
    stop_loss_pct: float | None = None,
) -> float:
    """仅使用 [start, end) 历史区间，选择样本内夏普率最高的 alpha。"""
    best_alpha = current_alpha
    best_score = float("-inf")
    for candidate in candidates:
        smoothed = ema_alpha(moment[:end], candidate)[start:end]
        result = run_from_signals(
            df.iloc[start:end],
            _signals_from_smoothed(smoothed),
            initial_cash,
            commission=commission,
            stop_loss_pct=stop_loss_pct,
        )
        score = float(result.metrics["sharpe"])
        if score > best_score + 1e-12:
            best_alpha = candidate
            best_score = score
        elif abs(score - best_score) <= 1e-12:
            if abs(candidate - current_alpha) < abs(best_alpha - current_alpha):
                best_alpha = candidate
    return float(best_alpha)


def _walk_forward_ema(
    df: pd.DataFrame,
    moment: np.ndarray,
    *,
    params: HigherMomentParams,
    initial_cash: float,
    commission: float,
    stop_loss_pct: float | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """按固定交易日间隔滚动优化 alpha，并连续更新 EMA 状态。"""
    smoothed = np.full(len(moment), np.nan, dtype=float)
    active_alpha = np.full(len(moment), params.alpha, dtype=float)
    candidates = _alpha_candidates(params)
    current_alpha = params.alpha
    state = np.nan

    for i, value in enumerate(moment):
        if (
            i >= params.optimize_lookback
            and (i - params.optimize_lookback) % params.optimize_interval == 0
        ):
            current_alpha = _select_alpha(
                df,
                moment,
                start=i - params.optimize_lookback,
                end=i,
                candidates=candidates,
                current_alpha=current_alpha,
                initial_cash=initial_cash,
                commission=commission,
                stop_loss_pct=stop_loss_pct,
            )
        active_alpha[i] = current_alpha
        if not np.isfinite(value):
            continue
        state = (
            value
            if not np.isfinite(state)
            else current_alpha * value + (1.0 - current_alpha) * state
        )
        smoothed[i] = state
    return smoothed, active_alpha


def _run(
    df: pd.DataFrame,
    base: BaseBacktestParams,
    params: HigherMomentParams,
) -> BacktestResult:
    close = df["close"].astype(float).to_numpy()
    moment = rolling_standardized_moment(
        close,
        order=params.order,
        window=params.window,
    )
    smoothed, active_alpha = _walk_forward_ema(
        df,
        moment,
        params=params,
        initial_cash=base.initial_cash,
        commission=base.commission,
        stop_loss_pct=base.stop_loss_pct,
    )
    return run_from_signals(
        df,
        _signals_from_smoothed(smoothed),
        base.initial_cash,
        commission=base.commission,
        stop_loss_pct=base.stop_loss_pct,
        overlays={
            "higher_moment": moment,
            "higher_moment_ema": smoothed,
            "ema_alpha": active_alpha,
        },
    )


def _min_bars(params: HigherMomentParams) -> int:
    return params.window + 1


STRATEGY = StrategySpec(
    id="higher_moment",
    name="高阶矩自适应 EMA",
    description="收益率标准化中心矩的 EMA 上穿零轴买入、下穿零轴卖出；alpha 每 90 日滚动优化。",
    params_model=HigherMomentParams,
    min_bars=_min_bars,
    run=_run,
)
