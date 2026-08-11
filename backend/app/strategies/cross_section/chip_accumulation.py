"""筹码单峰密集突破：筹码集中 + 获利盘适中 + 价格突破筹码峰的选股策略。

在决策日对股票池中每股用近期带换手率的日线计算筹码成本分布
（复用 ``chip_cost_distribution``），从中筛选"主力吸筹完成、上方套牢盘
消化、价格站上成本区并突破筹码峰"的个股，按筹码集中度优先选取 top-N，
共享账户等权持有。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from pydantic import Field, model_validator

from app.factors.chip_distribution import chip_cost_distribution
from app.strategies.base import (
    CrossSectionContext,
    CrossSectionStrategySpec,
)
from app.strategies.cross_section.common import asof_tradeable_row, is_st_stock
from app.strategies.cross_section.decision import DecisionFrequencyParams


class ChipAccumulationParams(DecisionFrequencyParams):
    decision_frequency: str = Field(
        "monthly",
        description="决策频率：daily=每日（冷启动期后）| weekly=每周末 | monthly=每自然月末",
    )
    top_n: int = Field(10, ge=1, le=50, description="最多持有的股票数量")
    lookback_bars: int = Field(
        120, ge=30, le=1000, description="筹码分布回溯的交易日 K 线数量"
    )
    bins: int = Field(
        200, ge=50, le=1000, description="筹码分布价格网格数（越小计算越快，越大分布越精细）"
    )
    profit_ratio_min: float = Field(
        0.35, ge=0.0, lt=1.0, description="获利盘比例下限（过滤仍处深套的个股）"
    )
    profit_ratio_max: float = Field(
        0.90, gt=0.0, lt=1.0, description="获利盘比例上限（过滤严重超买、追高风险的个股）"
    )
    concentration_max: float = Field(
        0.5, gt=0.0, lt=1.0, description="90% 成本集中度上限，越小表示筹码越集中（单峰越密集）"
    )
    require_price_above_cost: bool = Field(
        True, description="是否要求收盘价站上平均成本（套牢盘基本消化）"
    )
    require_breakout_peak: bool = Field(
        True, description="是否要求收盘价不低于筹码峰值（突破最密集成本位）"
    )
    exclude_st: bool = Field(True, description="是否剔除 ST / 退市风险股票")

    @model_validator(mode="after")
    def _ranges_ordered(self) -> "ChipAccumulationParams":
        if self.profit_ratio_max <= self.profit_ratio_min:
            raise ValueError("需要 profit_ratio_max > profit_ratio_min")
        return self


def _chip_slice(
    value_df: pd.DataFrame,
    asof: str,
    lookback_bars: int,
) -> pd.DataFrame | None:
    """取截止 asof 的最近 lookback_bars 根、换手率有效的日线子集（升序）。

    返回列 open/high/low/close/turnover_rate 的 DataFrame（date 列为字符串）。
    行数不足或换手率全无效时返回 None。
    """
    if value_df is None or value_df.empty:
        return None
    needed = ("open", "high", "low", "close", "turnover_rate")
    if not all(col in value_df.columns for col in needed):
        return None
    if "date" not in value_df.columns:
        return None

    value_df = value_df[value_df["date"].astype(str) <= str(asof)[:10]]
    value_df = value_df[
        value_df["turnover_rate"].notna()
        & value_df["turnover_rate"].astype(float).gt(0)
    ]
    if len(value_df) < 2:
        return None
    value_df = value_df.tail(lookback_bars)
    for col in needed:
        value_df[col] = pd.to_numeric(value_df[col], errors="coerce")
    value_df = value_df.dropna(subset=list(needed))
    if len(value_df) < 2:
        return None
    return value_df[list(needed)].copy()


def select_chip_accumulation(
    asof: str,
    ctx: CrossSectionContext,
    params: ChipAccumulationParams,
) -> tuple[list[str], list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    for symbol, payload in ctx.panel.items():
        value_df = payload.get("value")
        if value_df is None or value_df.empty:
            continue
        if params.exclude_st and is_st_stock(ctx.names.get(symbol)):
            continue

        # 用估值面板行判断决策日是否可交易（可结合停牌/涨跌停过滤）。
        row = asof_tradeable_row(
            value_df,
            asof,
            exclude_suspended=True,
            exclude_limit=True,
        )
        if row is None:
            continue
        close = float(row["close"])
        if close <= 0:
            continue

        slice_df = _chip_slice(value_df, asof, params.lookback_bars)
        if slice_df is None:
            continue

        try:
            chip = chip_cost_distribution(slice_df, bins=params.bins)
        except ValueError:
            continue
        if "warning" in chip:
            continue

        profit_ratio = float(chip["profit_ratio"])
        average_cost = float(chip["average_cost"])
        peak_price = float(chip["peak_price"])
        concentration_90 = float(chip["concentration_90"])

        # ── 过滤：筹码单峰密集 + 套牢盘消化 + 价格站上成本区/突破筹码峰 ──
        if not (params.profit_ratio_min <= profit_ratio <= params.profit_ratio_max):
            continue
        if params.require_price_above_cost and not (close > average_cost):
            continue
        if concentration_90 > params.concentration_max:
            continue
        if params.require_breakout_peak and not (close >= peak_price):
            continue

        breakout_pct = (
            (close - peak_price) / peak_price if peak_price > 0 else 0.0
        )
        candidates.append(
            {
                "symbol": symbol,
                "name": ctx.names.get(symbol, ""),
                "asof": asof,
                "fund_date": row.get("date"),
                "close": close,
                "profit_ratio": profit_ratio,
                "average_cost": average_cost,
                "peak_price": peak_price,
                "concentration_90": concentration_90,
                "interval_90_low": float(chip["interval_90_low"]),
                "interval_90_high": float(chip["interval_90_high"]),
                "breakout_pct": breakout_pct,
            }
        )

    # ── 打分排序：筹码越集中优先，其次突破筹码峰幅度越大越优先 ──
    candidates.sort(
        key=lambda c: (c["concentration_90"], -c["breakout_pct"])
    )
    picked = candidates[: params.top_n]
    return [c["symbol"] for c in picked], picked


STRATEGY = CrossSectionStrategySpec(
    id="chip_accumulation",
    name="筹码单峰密集突破",
    description="筹码高度集中 + 获利盘适中 + 价格站上成本区并突破筹码峰的选股，按集中度优先等权持有。",
    params_model=ChipAccumulationParams,
    select=select_chip_accumulation,
    default_universe="zz500",
    needs_fundamentals=False,
    needs_turnover=True,
    default_top_n=10,
    warnings=(
        "筹码分布为基于历史换手率衰减的统计估算，并非真实持仓数据。",
        "换手率口径可能因股本变动而失真，数据以腾讯财经接口为准。",
    ),
)
