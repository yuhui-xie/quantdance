"""ETF 动量轮动：选择趋势向上且中期动量最强的 ETF。"""

from __future__ import annotations

from typing import Any, Literal, Sequence

import pandas as pd
from pydantic import BaseModel, Field

from app.strategies.base import (
    CrossSectionContext,
    CrossSectionStrategySpec,
    decision_dates_by_frequency,
)
from app.strategies.cross_section.common import asof_tradeable_row


class EtfRotationParams(BaseModel):
    decision_frequency: Literal["daily", "weekly", "monthly"] = Field(
        "monthly", description="决策频率：daily=每日（冷启动期后）| weekly=每周末 | monthly=每自然月末"
    )
    decision_every_n: int = Field(1, ge=1, description="决策步长：monthly+3=季末、weekly+2=双周；daily 忽略")
    decision_warmup: int = Field(20, ge=0, le=250, description="冷启动期（交易日），仅 daily 生效")
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


def _price_history(value_df: pd.DataFrame, asof: str) -> pd.DataFrame:
    history = value_df.loc[
        value_df["date"].astype(str).str[:10] <= str(asof)[:10],
        ["date", "close"],
    ].copy()
    history["date"] = pd.to_datetime(history["date"], errors="coerce")
    history["close"] = pd.to_numeric(history["close"], errors="coerce")
    history = history.dropna(subset=["date", "close"])
    history = history[history["close"] > 0]
    return history.sort_values("date").drop_duplicates("date", keep="last")


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

        history = _price_history(value_df, asof)
        if len(history) < required_bars:
            continue

        close = float(history["close"].iloc[-1])
        base_close = float(history["close"].iloc[-params.lookback_days - 1])
        momentum = close / base_close - 1.0
        if momentum < params.min_momentum:
            continue

        trend_ma: float | None = None
        if params.trend_days > 0:
            trend_ma = float(history["close"].iloc[-params.trend_days :].mean())
            if close < trend_ma:
                continue

        candidates.append(
            {
                "symbol": symbol,
                "name": ctx.names.get(symbol, ""),
                "asof": str(asof)[:10],
                "close": close,
                "momentum": momentum,
                "lookback_days": params.lookback_days,
                "trend_ma": trend_ma,
                "trend_days": params.trend_days,
                "pct_change": row.get("pct_change"),
            }
        )

    candidates.sort(key=lambda item: (-item["momentum"], item["symbol"]))
    picked = candidates[: params.top_n]
    return [item["symbol"] for item in picked], picked


def decision_dates(
    calendar: Sequence[str],
    ctx: CrossSectionContext,
    params: EtfRotationParams,
) -> list[str]:
    del ctx
    return decision_dates_by_frequency(
        calendar,
        frequency=params.decision_frequency,
        every_n=params.decision_every_n,
        warmup=params.decision_warmup,
    )


STRATEGY = CrossSectionStrategySpec(
    id="etf_rotation",
    name="ETF 动量轮动",
    description="在显式 ETF 池中选择趋势向上且中期动量最强的标的，周期等权调仓",
    params_model=EtfRotationParams,
    select=select_etf_rotation,
    decision_dates=decision_dates,
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
