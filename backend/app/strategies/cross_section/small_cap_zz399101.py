"""中小综指(399101)成分股中流通市值最小的 N 只，周期等权调仓。"""

from __future__ import annotations

from typing import Any, Literal, Sequence

from pydantic import BaseModel, Field

from app.strategies.base import (
    CrossSectionContext,
    CrossSectionStrategySpec,
    decision_dates_by_frequency,
)
from app.strategies.cross_section.common import asof_tradeable_row, is_st_stock


class SmallCapZZ399101Params(BaseModel):
    decision_frequency: Literal["daily", "weekly", "monthly"] = Field(
        "monthly", description="决策频率：daily=每日（冷启动期后）| weekly=每周末 | monthly=每自然月末"
    )
    decision_every_n: int = Field(1, ge=1, description="决策步长：monthly+3=季末、weekly+2=双周；daily 忽略")
    decision_warmup: int = Field(20, ge=0, le=250, description="冷启动期（交易日），仅 daily 生效")
    top_n: int = Field(5, ge=1, le=50, description="持仓只数，默认 5")
    exclude_st: bool = True
    exclude_limit: bool = True
    exclude_suspended: bool = True
    limit_pct_threshold: float = Field(9.5, ge=1.0, le=30.0)
    min_price: float = Field(0.0, ge=0, description="可选最低价过滤；0 表示不限制")
    max_price: float | None = Field(None, ge=0, description="可选最高价过滤")


def select_small_cap_zz399101(
    asof: str,
    ctx: CrossSectionContext,
    params: SmallCapZZ399101Params,
) -> tuple[list[str], list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    for symbol, payload in ctx.panel.items():
        value_df = payload.get("value")
        if value_df is None or value_df.empty:
            continue
        if params.exclude_st and is_st_stock(ctx.names.get(symbol)):
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

        close = float(row["close"])
        if close < params.min_price:
            continue
        if params.max_price is not None and close > params.max_price:
            continue

        float_mv = row.get("float_market_cap")
        market_cap = row.get("market_cap")
        rank_cap = float_mv if float_mv is not None else market_cap
        if rank_cap is None:
            continue

        candidates.append(
            {
                "symbol": symbol,
                "name": ctx.names.get(symbol, ""),
                "asof": asof,
                "fund_date": row.get("date"),
                "close": close,
                "float_market_cap": float(float_mv) if float_mv is not None else None,
                "market_cap": float(market_cap) if market_cap is not None else None,
                "rank_market_cap": float(rank_cap),
                "pct_change": row.get("pct_change"),
            }
        )

    candidates.sort(key=lambda x: x["rank_market_cap"])
    picked = candidates[: params.top_n]
    return [c["symbol"] for c in picked], picked


def decision_dates(
    calendar: Sequence[str],
    ctx: CrossSectionContext,
    params: SmallCapZZ399101Params,
) -> list[str]:
    del ctx
    return decision_dates_by_frequency(
        calendar,
        frequency=params.decision_frequency,
        every_n=params.decision_every_n,
        warmup=params.decision_warmup,
    )


STRATEGY = CrossSectionStrategySpec(
    id="small_cap_zz399101",
    name="中小综指微盘",
    description="中小综指(399101)成分股中流通市值最小的 N 只，周期等权调仓",
    params_model=SmallCapZZ399101Params,
    select=select_small_cap_zz399101,
    decision_dates=decision_dates,
    default_universe="zz399101",
    needs_dividend=False,
    default_top_n=5,
    warnings=(
        "股票池为中小综指当前成分股，历史回测存在幸存者偏差。",
        "按流通市值排序；若流通市值缺失则回退总市值。",
        "微盘股流动性差，默认建议保留较高滑点假设。",
    ),
)
