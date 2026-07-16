"""菜场大妈：质好（股息率+PEG）+ 价低 + 小市值，周期等权调仓。"""

from __future__ import annotations

from typing import Any

import pandas as pd
from pydantic import BaseModel, Field, model_validator

from app.data_sources.em_fundamentals import trailing_dividend_yield
from app.portfolio.base import PortfolioSelectContext, PortfolioStrategySpec
from app.portfolio.common import asof_tradeable_row, is_st_stock


class MarketAuntieParams(BaseModel):
    top_n: int = Field(10, ge=1, le=50)
    min_price: float = Field(2.0, ge=0)
    max_price: float = Field(9.0, gt=0)
    min_dividend_yield: float = Field(0.0, ge=0)
    min_peg: float = Field(0.0)
    max_peg: float = Field(1.0, gt=0)
    require_dividend: bool = True
    require_peg: bool = True
    exclude_st: bool = True
    exclude_limit: bool = True
    exclude_suspended: bool = True
    limit_pct_threshold: float = Field(9.5, ge=1.0, le=30.0)

    @model_validator(mode="after")
    def check_ranges(self) -> MarketAuntieParams:
        if self.max_price < self.min_price:
            raise ValueError("max_price 不能小于 min_price")
        if self.max_peg <= self.min_peg:
            raise ValueError("max_peg 必须大于 min_peg")
        return self


def select_market_auntie(
    asof: str,
    ctx: PortfolioSelectContext,
    params: MarketAuntieParams,
) -> tuple[list[str], list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    for symbol, payload in ctx.panel.items():
        value_df = payload.get("value")
        div_df = payload.get("dividend")
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
        market_cap = row.get("market_cap")
        peg = row.get("peg")
        if market_cap is None:
            continue
        if close < params.min_price or close > params.max_price:
            continue

        dy = trailing_dividend_yield(
            div_df if div_df is not None else pd.DataFrame(),
            asof=asof,
            price=close,
        )
        if dy is None:
            dy = 0.0

        if params.require_dividend and dy <= params.min_dividend_yield:
            continue
        if params.require_peg:
            if peg is None or not (params.min_peg < float(peg) <= params.max_peg):
                continue

        candidates.append(
            {
                "symbol": symbol,
                "name": ctx.names.get(symbol, ""),
                "asof": asof,
                "fund_date": row.get("date"),
                "close": close,
                "market_cap": float(market_cap),
                "peg": float(peg) if peg is not None else None,
                "dividend_yield": float(dy),
                "pct_change": row.get("pct_change"),
            }
        )

    candidates.sort(key=lambda x: x["market_cap"])
    picked = candidates[: params.top_n]
    return [c["symbol"] for c in picked], picked


STRATEGY = PortfolioStrategySpec(
    id="market_auntie",
    name="菜场大妈",
    description="质好（股息率+PEG）价低市值小，周期等权调仓",
    params_model=MarketAuntieParams,
    select=select_market_auntie,
    default_universe="all_a",
    needs_dividend=True,
    default_top_n=10,
    warnings=(
        "股息率为近一年现金分红/调仓日收盘价估算；PEG 来自东财口径。",
        "小市值与低价股策略容量有限，滑点与冲击成本对大资金影响显著。",
    ),
)
