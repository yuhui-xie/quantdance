"""煤炭 ETF 周线布林下轨反弹 + CHOP(28) 策略。

每周最后一个交易日做一次判断，只在该日收盘后产生信号：

- **买入**：周收盘价 **低于布林下轨**（中轨 − ``bb_num_std`` × 标准差，默认 1.5σ），
  且周线 CHOP(``chop_period``=28) **≥ 50**（震荡/超跌环境）→ 全仓买入；
- **卖出**：周收盘价 **回到布林中轨及以上** → 全仓卖出。

默认在判断日之后的**下一个交易日**收盘成交（``execute_next_day=True``），对应"每周
最后一日判断、下次买入"的口径；设为 ``False`` 则在判断日当日收盘成交。信号只使用
已经走完的周线，不存在未来函数。

示例：

```bash
python -m app.script backtest --strategy coal_etf_bb_chop_bounce --symbol 515220 \\
    --start-date 2021-01-01 --end-date 2026-09-18
```
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from app.backtest_engine import BacktestResult, run_from_signals
from app.factors.range_motion import choppiness_index
from app.strategies.base import BaseBacktestParams, StrategySpec


class CoalEtfBbChopBounceParams(BaseModel):
    bb_period: int = Field(20, ge=2, le=200, description="周线布林带中轨窗口（周数）")
    bb_num_std: float = Field(
        1.5, ge=0.1, le=10.0, description="布林下轨的标准差倍数（默认 1.5σ）"
    )
    chop_period: int = Field(
        28, ge=2, le=200, description="周线 CHOP（碎形指数）窗口（周数）"
    )
    chop_threshold: float = Field(
        50.0,
        ge=0.0,
        le=100.0,
        description="买入要求的 CHOP 下限（≥该值才买，默认 50）",
    )
    execute_next_day: bool = Field(
        True,
        description=(
            "True=周判断日之后的下一交易日收盘成交（默认）；"
            "False=判断日当日收盘成交。"
        ),
    )


def _weekly_frame(
    df: pd.DataFrame,
    params: CoalEtfBbChopBounceParams,
) -> pd.DataFrame:
    """日线聚合为周线并计算布林与 CHOP；索引改为该周最后一个真实交易日。"""
    if not isinstance(df.index, pd.DatetimeIndex):
        df = df.copy()
        df.index = pd.to_datetime(df.index)

    weekly = df.resample("W-FRI").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    ).dropna(subset=["close"])
    if weekly.empty:
        return weekly

    per_week = pd.Series(df.index, index=df.index).groupby(
        df.index.to_period("W-FRI")
    ).max()
    weekly = weekly.assign(
        completed=pd.to_datetime(
            per_week.reindex(weekly.index.to_period("W-FRI")).to_numpy()
        )
    ).sort_values("completed")

    closes = weekly["close"].astype(float)
    middle = closes.rolling(params.bb_period, min_periods=params.bb_period).mean()
    std = closes.rolling(params.bb_period, min_periods=params.bb_period).std(ddof=0)
    weekly["bb_middle"] = middle
    weekly["bb_upper"] = middle + params.bb_num_std * std
    weekly["bb_lower"] = middle - params.bb_num_std * std
    weekly["chop"] = choppiness_index(
        weekly["high"].astype(float).to_numpy(),
        weekly["low"].astype(float).to_numpy(),
        closes.to_numpy(),
        period=params.chop_period,
    )
    return weekly


def _compute_signals(
    df: pd.DataFrame,
    params: CoalEtfBbChopBounceParams,
) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """返回 (信号数组, 指标 overlay)；信号 1=买入、-1=卖出、0=观望。"""
    n = len(df)
    signal = np.zeros(n, dtype=np.int8)
    keys = ("bb_upper", "bb_middle", "bb_lower", "chop")
    weekly = _weekly_frame(df, params)
    if weekly.empty:
        return signal, {key: np.full(n, np.nan) for key in keys}

    close = weekly["close"].to_numpy(dtype=float)
    middle = weekly["bb_middle"].to_numpy(dtype=float)
    lower = weekly["bb_lower"].to_numpy(dtype=float)
    chop = weekly["chop"].to_numpy(dtype=float)

    # 周级状态机：只在一个周判断日上发出一次买/卖，避免重复信号。
    weekly_signal = np.zeros(len(weekly), dtype=np.int8)
    holding = False
    for i in range(len(weekly)):
        if not (np.isfinite(close[i]) and np.isfinite(middle[i]) and np.isfinite(lower[i])):
            continue
        if holding:
            if close[i] >= middle[i]:
                weekly_signal[i] = -1
                holding = False
        else:
            if close[i] < lower[i] and np.isfinite(chop[i]) and chop[i] >= params.chop_threshold:
                weekly_signal[i] = 1
                holding = True

    daily_dates = df.index.to_numpy().astype("datetime64[ns]")
    completed = pd.to_datetime(weekly["completed"]).to_numpy().astype("datetime64[ns]")
    # 判断日 = 该周最后一个真实交易日；默认顺延到下一个交易日执行。
    completion_pos = np.searchsorted(daily_dates, completed, side="right") - 1
    shift = 1 if params.execute_next_day else 0
    for k in np.flatnonzero(weekly_signal):
        pos = int(completion_pos[k]) + shift
        if 0 <= pos < n:
            signal[pos] = weekly_signal[k]

    # overlay：按「完成日 ≤ 当日」映射，便于图表查看；不影响信号。
    mapped_pos = np.searchsorted(completed, daily_dates, side="right") - 1
    valid = mapped_pos >= 0

    def _map(values: np.ndarray) -> np.ndarray:
        out = np.full(n, np.nan, dtype=float)
        out[valid] = np.asarray(values, dtype=float)[mapped_pos[valid]]
        return out

    overlays = {
        "bb_upper": _map(weekly["bb_upper"].to_numpy()),
        "bb_middle": _map(weekly["bb_middle"].to_numpy()),
        "bb_lower": _map(weekly["bb_lower"].to_numpy()),
        "chop": _map(weekly["chop"].to_numpy()),
    }
    return signal, overlays


def _run(
    df: pd.DataFrame,
    base: BaseBacktestParams,
    params: CoalEtfBbChopBounceParams,
) -> BacktestResult:
    signal, overlays = _compute_signals(df, params)
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
    params: CoalEtfBbChopBounceParams,
) -> np.ndarray:
    signal, _ = _compute_signals(df, params)
    return signal


def _min_bars(params: CoalEtfBbChopBounceParams) -> int:
    return (max(params.bb_period, params.chop_period) + 2) * 5


STRATEGY = StrategySpec(
    id="coal_etf_bb_chop_bounce",
    name="煤炭ETF 周线布林下轨反弹+CHOP",
    description=(
        "每周最后一个交易日判断：周收盘跌破布林下轨（默认 1.5σ）且 CHOP(28)≥50 时"
        "全仓买入，周收盘回到中轨及以上卖出；默认下一交易日收盘成交。"
    ),
    params_model=CoalEtfBbChopBounceParams,
    min_bars=_min_bars,
    run=_run,
    signals=_signals,
)
