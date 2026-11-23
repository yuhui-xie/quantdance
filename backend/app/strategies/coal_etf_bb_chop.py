"""煤炭 ETF 周线布林 + CHOP(28) 策略：按周线行情性质切换「趋势跟随 / 震荡超跌」。

策略只在**周线**上判定，所有指标都来自已经走完的周线，日线仅用于承载信号与成交：

- ``CHOP < chop_trend_threshold``（默认 40.7，趋势市）：周收盘站上布林中轨做多，
  周收盘跌破中轨退出；
- ``CHOP > chop_range_threshold``（默认 59.3，震荡市）：周内最低价触及布林下轨
  （超跌）做多，周内最高价触及上轨（超买）退出；
- 两个阈值之间为中性区：不开新仓；已有仓位既不因中性区卖出，也继续持有到下一个
  明确的退出条件。

信号是**状态型**的：只要最新已完成周线处于「允许做多」状态，每个交易日都输出买入
信号（空仓时买入一次，持仓时引擎忽略）；处于「周线退出」状态时每天输出卖出信号。
同日买卖同时成立（例如一根周线既触及下轨又触及上轨）时卖出优先。

周线完成日取该 ISO 周内最后一个真实交易日，交易日 ``d`` 只使用「完成日 ≤ d」的
最后一根周线，因此周五收盘即可用当周周线，不存在未来函数。信号在当日收盘价成交。

示例：

```bash
python -m app.script backtest --strategy coal_etf_bb_chop --symbol 515220 \\
    --start-date 2021-01-01 --end-date 2026-09-18
```
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from app.backtest_engine import BacktestResult, run_from_signals
from app.factors.range_motion import choppiness_index
from app.strategies.base import BaseBacktestParams, StrategySpec


class CoalEtfBbChopParams(BaseModel):
    bb_period: int = Field(20, ge=2, le=200, description="周线布林带中轨窗口（周数）")
    bb_num_std: float = Field(
        2.0, ge=0.1, le=10.0, description="周线布林带上下轨的标准差倍数"
    )
    chop_period: int = Field(
        28, ge=2, le=200, description="周线 CHOP（碎形指数）窗口（周数）"
    )
    # CHOP 以 log10(n) 归一化，随机游走锚定在 50 附近；周期越长分布越窄。经典
    # 38.2/61.8 是 n=14 的口径，换到 n=28 时按 log 周期等比缩放：
    # 50 − (50−38.2)×log10(14)/log10(28) ≈ 40.7，镜像得 59.3。直接沿用 38.2/61.8
    # 会让 515220 的周线 CHOP(28) 几乎永远落在中性区，策略完全空仓。
    chop_trend_threshold: float = Field(
        40.7,
        ge=0.0,
        le=100.0,
        description="CHOP 低于该值视为趋势市：周收盘站上布林中轨做多、跌破则退出",
    )
    chop_range_threshold: float = Field(
        59.3,
        ge=0.0,
        le=100.0,
        description="CHOP 高于该值视为震荡市：周内触及布林下轨做多、触及上轨则退出",
    )
    neutral_action: Literal["flat", "hold"] = Field(
        "flat",
        description=(
            "CHOP 落在两个阈值之间（中性区）时的处理：flat=不持有（退出已有仓位，"
            "默认，只在明确的多头状态里持仓）| hold=保持现有仓位，仅按趋势/震荡市"
            "的明确退出条件卖出。"
        ),
    )


def _weekly_layer(
    df: pd.DataFrame,
    params: CoalEtfBbChopParams,
) -> dict[str, np.ndarray]:
    """把日线重采样为周线并计算布林/CHOP，再映射回每个交易日。

    返回与 ``df`` 等长的数组字典：``bb_upper`` / ``bb_middle`` / ``bb_lower`` /
    ``chop`` / ``weekly_high`` / ``weekly_low`` / ``weekly_close``。

    映射口径：交易日 ``d`` 取「完成日 ≤ d」的最后一根周线；周线完成日取其在该周内
    最后一个真实交易日（周五休市时不会把未走完的周当作已完成）。
    """
    if not isinstance(df.index, pd.DatetimeIndex):
        df = df.copy()
        df.index = pd.to_datetime(df.index)

    keys = (
        "bb_upper",
        "bb_middle",
        "bb_lower",
        "chop",
        "weekly_high",
        "weekly_low",
        "weekly_close",
    )
    n_daily = len(df)
    weekly = df.resample("W-FRI").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    )
    weekly = weekly.dropna(subset=["close"])
    if weekly.empty:
        return {key: np.full(n_daily, np.nan) for key in keys}

    # 每根周线实际走完的日期 = 该 ISO 周内最后一个交易日（可能不是周五）。
    per_week = pd.Series(df.index, index=df.index).groupby(
        df.index.to_period("W-FRI")
    ).max()
    completion = pd.to_datetime(
        per_week.reindex(weekly.index.to_period("W-FRI")).to_numpy()
    )
    weekly = weekly.assign(completed=completion).sort_values("completed")

    closes = weekly["close"].astype(float)
    middle = closes.rolling(params.bb_period, min_periods=params.bb_period).mean()
    std = closes.rolling(params.bb_period, min_periods=params.bb_period).std(ddof=0)
    chop = choppiness_index(
        weekly["high"].astype(float).to_numpy(),
        weekly["low"].astype(float).to_numpy(),
        closes.to_numpy(),
        period=params.chop_period,
    )

    completed = pd.to_datetime(weekly["completed"]).to_numpy().astype("datetime64[ns]")
    daily = df.index.to_numpy().astype("datetime64[ns]")
    pos = np.searchsorted(completed, daily, side="right") - 1
    valid = pos >= 0

    def _map(values: np.ndarray) -> np.ndarray:
        values = np.asarray(values, dtype=float)
        out = np.full(n_daily, np.nan, dtype=float)
        out[valid] = values[pos[valid]]
        return out

    return {
        "bb_upper": _map((middle + params.bb_num_std * std).to_numpy()),
        "bb_middle": _map(middle.to_numpy()),
        "bb_lower": _map((middle - params.bb_num_std * std).to_numpy()),
        "chop": _map(chop),
        "weekly_high": _map(weekly["high"].to_numpy()),
        "weekly_low": _map(weekly["low"].to_numpy()),
        "weekly_close": _map(closes.to_numpy()),
    }


def _compute_signals(
    df: pd.DataFrame,
    params: CoalEtfBbChopParams,
) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """返回 (信号数组, 指标 overlay)；信号 1=买入、-1=卖出、0=观望。"""
    weekly = _weekly_layer(df, params)

    chop = weekly["chop"]
    w_close = weekly["weekly_close"]
    w_high = weekly["weekly_high"]
    w_low = weekly["weekly_low"]
    middle = weekly["bb_middle"]
    upper = weekly["bb_upper"]
    lower = weekly["bb_lower"]

    trend = chop < params.chop_trend_threshold
    ranging = chop > params.chop_range_threshold

    long_ok = (trend & (w_close > middle)) | (ranging & (w_low <= lower))
    weekly_exit = (trend & (w_close < middle)) | (ranging & (w_high >= upper))
    signal = np.zeros(len(df), dtype=np.int8)
    signal[long_ok] = 1
    # 卖出优先：同一天既允许做多又触发退出时，不做多。
    signal[weekly_exit] = -1
    if params.neutral_action == "flat":
        # 不持仓模式：只要不是明确的多头状态就退出（空仓时该 -1 会被引擎忽略）。
        # 这也意味着震荡市「触及下轨」买入后，若下一周不再满足超跌条件即离场。
        signal[~long_ok] = -1

    overlays = {
        "bb_upper": upper,
        "bb_middle": middle,
        "bb_lower": lower,
        "chop": chop,
    }
    return signal, overlays


def _run(
    df: pd.DataFrame,
    base: BaseBacktestParams,
    params: CoalEtfBbChopParams,
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
    params: CoalEtfBbChopParams,
) -> np.ndarray:
    signal, _ = _compute_signals(df, params)
    return signal


def _min_bars(params: CoalEtfBbChopParams) -> int:
    # 周线至少需要 max(布林窗口, CHOP 窗口) 周，再留 2 周缓冲，按每周 5 个交易日折算。
    return (max(params.bb_period, params.chop_period) + 2) * 5


STRATEGY = StrategySpec(
    id="coal_etf_bb_chop",
    name="煤炭ETF 周线布林+CHOP",
    description=(
        "周线布林+CHOP(28) 定方向：趋势市周收盘站上中轨做多、跌破退出；震荡市周内"
        "触及下轨做多、触及上轨退出；默认标的 515220 煤炭ETF。"
    ),
    params_model=CoalEtfBbChopParams,
    min_bars=_min_bars,
    run=_run,
    signals=_signals,
)
