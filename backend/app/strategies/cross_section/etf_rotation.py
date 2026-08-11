"""ETF 动量轮动：选择趋势向上且中期动量最强的 ETF。

动量与趋势均线复用 ``app.factors`` 因子库的 ``simple_momentum`` /
``price_history`` / ``trend_ma``。
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from app.factors.momentum import price_history, simple_momentum
from app.factors.trend import trend_ma
from app.strategies.base import (
    CrossSectionContext,
    CrossSectionStrategySpec,
)
from app.strategies.cross_section.common import asof_tradeable_row
from app.strategies.cross_section.decision import DecisionFrequencyParams


class EtfRotationParams(DecisionFrequencyParams):
    top_n: int = Field(1, ge=1, le=10, description="持有动量最强的 ETF 数量")
    lookback_days: int = Field(60, ge=2, le=504, description="动量回看交易日数")
    trend_days: int = Field(
        120,
        ge=0,
        le=504,
        description="趋势均线交易日数；0 表示关闭趋势过滤",
    )
    min_momentum: float = Field(
        0.0,
        ge=-1.0,
        le=10.0,
        description="最低区间收益率；0 表示只持有正动量 ETF",
    )
    exclude_limit: bool = True
    exclude_suspended: bool = True
    limit_pct_threshold: float = Field(9.5, ge=1.0, le=30.0)


def select_etf_rotation(
    asof: str,
    ctx: CrossSectionContext,
    params: EtfRotationParams,
) -> tuple[list[str], list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    required_bars = max(params.lookback_days + 1, params.trend_days)

    for symbol, payload in ctx.panel.items():
        value_df = payload.get("value")
        if value_df is None or value_df.empty:
            continue

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

        close = float(history["close"].iloc[-1])
        momentum = simple_momentum(
            history["close"].astype(float).values, params.lookback_days
        )
        if momentum is None or momentum < params.min_momentum:
            continue

        trend_value: float | None = None
        if params.trend_days > 0:
            trend_value = trend_ma(
                history["close"].astype(float).values, params.trend_days
            )
            if trend_value is None or close < trend_value:
                continue

        candidates.append(
            {
                "symbol": symbol,
                "name": ctx.names.get(symbol, ""),
                "asof": str(asof)[:10],
                "close": close,
                "momentum": momentum,
                "lookback_days": params.lookback_days,
                "trend_ma": trend_value,
                "trend_days": params.trend_days,
                "pct_change": row.get("pct_change"),
            }
        )

    candidates.sort(key=lambda item: (-item["momentum"], item["symbol"]))
    picked = candidates[: params.top_n]
    return [item["symbol"] for item in picked], picked


STRATEGY = CrossSectionStrategySpec(
    id="etf_rotation",
    name="ETF 动量轮动",
    description="在显式 ETF 池中选择趋势向上且中期动量最强的标的，周期等权调仓",
    params_model=EtfRotationParams,
    select=select_etf_rotation,
    default_universe="all_a",
    requires_symbols=True,
    needs_fundamentals=False,
    default_top_n=1,
    warnings=(
        "请通过 symbols 显式提供 ETF 池；策略不会自动识别或维护历史 ETF 池。",
        "当所有 ETF 均未达到最低动量或趋势条件时，组合持有现金。",
        "动量排名仅使用调仓日及之前的收盘价，不使用未来数据。",
    ),
)
