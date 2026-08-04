"""将多个单票策略的持仓状态组合为一条买卖信号。"""

from __future__ import annotations

from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field, model_validator

from app.backtest_engine import BacktestResult, run_from_signals
from app.strategies.base import BaseBacktestParams, StrategySpec


_BASE_PRICE_FIELDS = {"date", "datetime", "open", "high", "low", "close", "volume"}


class CompositeLeg(BaseModel):
    strategy_id: str = Field(..., min_length=1, description="子策略 id")
    weight: float = Field(1.0, gt=0, description="weighted 模式下的投票权重")
    params: dict[str, Any] = Field(default_factory=dict, description="子策略专属参数")


def _default_legs() -> list[CompositeLeg]:
    return [
        CompositeLeg(strategy_id="ema_crossover"),
        CompositeLeg(strategy_id="llt_trend"),
    ]


class SignalCompositeParams(BaseModel):
    mode: Literal["all", "any", "majority", "weighted"] = Field(
        "weighted",
        description="持仓状态组合方式",
    )
    legs: list[CompositeLeg] = Field(
        default_factory=_default_legs,
        min_length=2,
        max_length=20,
    )
    entry_threshold: float = Field(
        0.6,
        ge=0,
        le=1,
        description="weighted 模式由空仓转持仓的得分阈值",
    )
    exit_threshold: float = Field(
        0.4,
        ge=0,
        le=1,
        description="weighted 模式由持仓转空仓的得分阈值",
    )

    @model_validator(mode="after")
    def validate_composite(self) -> SignalCompositeParams:
        if self.exit_threshold >= self.entry_threshold:
            raise ValueError("exit_threshold 必须小于 entry_threshold，以避免持仓状态反复切换")
        if any(leg.strategy_id == "signal_composite" for leg in self.legs):
            raise ValueError("signal_composite 不能递归引用自身")
        return self


def _resolve_leg(leg: CompositeLeg) -> tuple[StrategySpec, BaseModel]:
    # 延迟导入，避免 registry 扫描本模块时产生循环导入。
    from app.strategies.registry import get_strategy

    spec = get_strategy(leg.strategy_id)
    if spec is None:
        raise ValueError(f"未知子策略: {leg.strategy_id}")
    try:
        params = spec.params_model.model_validate(leg.params)
    except Exception as exc:
        raise ValueError(f"子策略 {leg.strategy_id} 参数无效: {exc}") from exc
    return spec, params


def _events_to_positions(signal: np.ndarray) -> np.ndarray:
    """把稀疏买卖事件转换为持续的 long-only 持仓状态。"""
    positions = np.zeros(len(signal), dtype=np.int8)
    active = 0
    for i, event in enumerate(signal):
        if event == 1:
            active = 1
        elif event == -1:
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


def _target_positions(score: np.ndarray, states: np.ndarray, params: SignalCompositeParams) -> np.ndarray:
    if params.mode == "all":
        return np.all(states == 1, axis=0).astype(np.int8)
    if params.mode == "any":
        return np.any(states == 1, axis=0).astype(np.int8)
    if params.mode == "majority":
        return (np.mean(states, axis=0) > 0.5).astype(np.int8)

    positions = np.zeros(len(score), dtype=np.int8)
    active = 0
    for i, value in enumerate(score):
        if not active and value >= params.entry_threshold:
            active = 1
        elif active and value <= params.exit_threshold:
            active = 0
        positions[i] = active
    return positions


def _compute(
    df: pd.DataFrame,
    base: BaseBacktestParams,
    params: SignalCompositeParams,
) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    states: list[np.ndarray] = []
    weights: list[float] = []
    overlays: dict[str, np.ndarray] = {}
    child_base = BaseBacktestParams(
        initial_cash=base.initial_cash,
        commission=base.commission,
        stop_loss_pct=None,
    )

    for index, leg in enumerate(params.legs, start=1):
        spec, leg_params = _resolve_leg(leg)
        child_result = spec.run(df, child_base, leg_params)
        signal = np.asarray(child_result.signal, dtype=np.int8)
        if len(signal) != len(df):
            raise ValueError(f"策略 {spec.id} 的 signal 长度必须与行情数据一致")
        if not np.isin(signal, (-1, 0, 1)).all():
            raise ValueError(f"策略 {spec.id} 的 signal 只能包含 -1、0、1")
        state = _events_to_positions(signal)
        states.append(state)
        weights.append(leg.weight)
        leg_key = f"leg_{index}_{spec.id}"
        overlays[leg_key] = state

        indicator_keys = {
            key
            for row in child_result.price
            for key in row
            if key not in _BASE_PRICE_FIELDS
        }
        for key in sorted(indicator_keys):
            overlays[f"{leg_key}__{key}"] = np.asarray(
                [row.get(key, np.nan) for row in child_result.price],
                dtype=float,
            )

    state_matrix = np.vstack(states)
    normalized_weights = np.asarray(weights, dtype=float)
    normalized_weights /= float(normalized_weights.sum())
    score = np.average(state_matrix, axis=0, weights=normalized_weights)
    positions = _target_positions(score, state_matrix, params)
    events = _positions_to_events(positions)
    overlays["composite_score"] = score
    overlays["composite_position"] = positions
    return events, overlays


def _signals(
    df: pd.DataFrame,
    base: BaseBacktestParams,
    params: SignalCompositeParams,
) -> np.ndarray:
    return _compute(df, base, params)[0]


def _run(
    df: pd.DataFrame,
    base: BaseBacktestParams,
    params: SignalCompositeParams,
) -> BacktestResult:
    signal, overlays = _compute(df, base, params)
    return run_from_signals(
        df,
        signal,
        base.initial_cash,
        commission=base.commission,
        stop_loss_pct=base.stop_loss_pct,
        overlays=overlays,
    )


def _min_bars(params: SignalCompositeParams) -> int:
    minimums: list[int] = []
    for leg in params.legs:
        spec, leg_params = _resolve_leg(leg)
        minimums.append(spec.min_bars(leg_params))
    return max(minimums)


STRATEGY = StrategySpec(
    id="signal_composite",
    name="多策略信号组合",
    description="先将子策略买卖事件转换为持仓状态，再通过交集、并集、多数或加权投票生成组合信号。",
    params_model=SignalCompositeParams,
    min_bars=_min_bars,
    run=_run,
    signals=_signals,
)
