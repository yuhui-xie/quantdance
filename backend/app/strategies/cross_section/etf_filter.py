"""ETF 动态池筛选：从历史时点存在的 ETF 池中用流动性筛出一批标的。

与 ``etf_rotation``（显式 ETF 池、纯动量选股）不同，本策略面向"从当时
存在的 ETF 池里动态筛一批"的场景：

- ``default_universe="etf"`` 且 ``requires_symbols=False``：走全市场 ETF 预设，
  而非强制显式 symbols。横截面引擎按决策日 asof 重构「当时已上市」的 ETF 池，
  避开幸存者偏差。
- **动态 as-of 池**：复用 ``app.strategies.cross_section.asof_pool``——
  ``panel_existing_at`` 按调仓日 asof 用 K 线覆盖过滤出当时已存在的标的，
  新上市 ETF 会在上市日之后自动加入候选。
- **存在性**：asof 之前无任何 K 线的标的自动剔除（尚未上市）。
- **流动性过滤**：按截至 asof 的近 ``liquidity_days`` 日均成交额/成交量
  过滤，剔除流动性过低的标的（可交易性筛选）。
- **行业均衡（可选）**：``per_industry_top_k>0`` 时按 ETF 名称关键词推断行业，
  每行业最多选 ``per_industry_top_k`` 只，再做全局动量排序取 top_n。

动量复用 ``app.factors.momentum.simple_momentum``；可交易性判断复用
``app.strategies.cross_section.common.asof_tradeable_row``。
"""

from __future__ import annotations

from typing import Any

import pandas as pd
from pydantic import Field

from app.factors.momentum import price_history, simple_momentum
from app.strategies.base import CrossSectionContext, CrossSectionStrategySpec
from app.strategies.cross_section.asof_pool import (
    infer_industry,
    liquidity_stats,
    panel_existing_at,
)
from app.strategies.cross_section.common import asof_tradeable_row
from app.strategies.cross_section.decision import DecisionFrequencyParams


class EtfFilterParams(DecisionFrequencyParams):
    top_n: int = Field(10, ge=1, le=50, description="最终保留的 ETF 数量上限")
    liquidity_days: int = Field(
        20, ge=1, le=250, description="流动性回看交易日数（取日均成交额/量）"
    )
    min_amount: float | None = Field(
        None, ge=0, description="日均成交额下限（元）；None 表示不启用成交额过滤"
    )
    min_volume: float | None = Field(
        None, ge=0, description="日均成交量下限；None 表示不启用成交量过滤"
    )
    momentum_days: int = Field(60, ge=2, le=504, description="动量回看交易日数")
    min_momentum: float = Field(
        0.0, ge=-1.0, le=10.0, description="最低区间收益率；0 表示只保留正动量 ETF"
    )
    per_industry_top_k: int = Field(
        0,
        ge=0,
        le=20,
        description="行业均衡：每行业最多选几只；0 表示关闭行业均衡（纯全局动量排序）",
    )
    exclude_limit: bool = True
    exclude_suspended: bool = True
    limit_pct_threshold: float = Field(9.5, ge=1.0, le=30.0)


def select_etf_filter(
    asof: str,
    ctx: CrossSectionContext,
    params: EtfFilterParams,
) -> tuple[list[str], list[dict[str, Any]]]:
    # 动态 as-of 池：按调仓日过滤出"当时已上市"的标的（新上市 ETF 自动纳入）
    existing = panel_existing_at(ctx.panel, asof)

    candidates: list[dict[str, Any]] = []
    required_bars = params.momentum_days + 1

    for symbol, payload in existing.items():
        value_df = payload.get("value")
        if value_df is None or value_df.empty:
            continue

        # 可交易性：asof 当日停牌或涨跌停则剔除（存在性已由 panel_existing_at 保证）
        row = asof_tradeable_row(
            value_df,
            asof,
            exclude_suspended=params.exclude_suspended,
            exclude_limit=params.exclude_limit,
            limit_pct_threshold=params.limit_pct_threshold,
        )
        if row is None:
            continue

        history = price_history(value_df, asof)
        if len(history) < required_bars:
            continue

        closes = history["close"].astype(float).values
        momentum = simple_momentum(closes, params.momentum_days)
        if momentum is None or momentum < params.min_momentum:
            continue

        # 流动性过滤：日均成交额/量低于阈值的剔除
        avg_amount, avg_volume = liquidity_stats(value_df, asof, params.liquidity_days)
        if params.min_amount is not None:
            if avg_amount is None or avg_amount < params.min_amount:
                continue
        if params.min_volume is not None:
            if avg_volume is None or avg_volume < params.min_volume:
                continue

        candidates.append(
            {
                "symbol": symbol,
                "name": ctx.names.get(symbol, ""),
                "industry": infer_industry(ctx.names.get(symbol, "")),
                "asof": str(asof)[:10],
                "close": float(history["close"].iloc[-1]),
                "momentum": momentum,
                "momentum_days": params.momentum_days,
                "avg_amount": avg_amount,
                "avg_volume": avg_volume,
                "liquidity_days": params.liquidity_days,
                "pct_change": row.get("pct_change"),
            }
        )

    candidates.sort(key=lambda item: (-item["momentum"], item["symbol"]))

    picked: list[dict[str, Any]] = []
    industry_count: dict[str, int] = {}
    for c in candidates:
        if len(picked) >= params.top_n:
            break
        ind = c["industry"]
        if params.per_industry_top_k > 0 and industry_count.get(ind, 0) >= params.per_industry_top_k:
            continue
        picked.append(c)
        industry_count[ind] = industry_count.get(ind, 0) + 1

    return [item["symbol"] for item in picked], picked


STRATEGY = CrossSectionStrategySpec(
    id="etf_filter",
    name="ETF 动态池筛选",
    description=(
        "从历史时点存在的 ETF 池（K 线覆盖判定，避开幸存者偏差）中，"
        "用近 N 日成交额/成交量过滤流动性，再做动量排序筛出一批标的；"
        "可选按行业均衡（每行业最多 K 只）"
    ),
    params_model=EtfFilterParams,
    select=select_etf_filter,
    default_universe="etf",
    requires_symbols=False,
    needs_fundamentals=False,
    default_top_n=10,
    warnings=(
        "「当时存在的池」由每只 ETF 的 K 线覆盖区间判定（首根 K 线 ≤ 决策日即为已上市），"
        "存在少量 ETF 清盘退市被低估的幸存者偏差，属可接受近似。",
        "流动性用截至决策日的近 liquidity_days 日均成交额（amount）/成交量（volume）；"
        "若 min_amount 已设置但标的 K 线无成交额列，则无法确认其流动性而被剔除。",
        "行业由 ETF 名称关键词推断（见 docs/etf-filter-strategy.md 映射表），非交易所官方板块；"
        "名称未命中或宽基类归入「其他/宽基」。",
        "动量排名仅使用调仓日及之前的收盘价，不使用未来数据。",
    ),
)
