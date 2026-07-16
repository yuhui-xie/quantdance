"""组合选股共用过滤。"""

from __future__ import annotations

from typing import Any

import pandas as pd

from app.data_sources.em_fundamentals import asof_fundamental_row


def is_st_stock(name: str | None) -> bool:
    n = (name or "").upper()
    return "ST" in n or "退" in n


def asof_tradeable_row(
    value_df: pd.DataFrame,
    asof: str,
    *,
    exclude_suspended: bool = True,
    exclude_limit: bool = True,
    limit_pct_threshold: float = 9.5,
    max_lag_days: int = 10,
) -> dict[str, Any] | None:
    """取调仓日可用估值行；不满足可交易约束时返回 None。"""
    row = asof_fundamental_row(value_df, asof)
    if row is None:
        return None
    try:
        lag = (pd.Timestamp(asof) - pd.Timestamp(row["date"])).days
    except Exception:
        lag = 999
    if lag > max_lag_days:
        return None
    if exclude_suspended and lag > 0:
        return None
    pct = row.get("pct_change")
    if exclude_limit and pct is not None and abs(float(pct)) >= limit_pct_threshold:
        return None
    close = row.get("close")
    if close is None or float(close) <= 0:
        return None
    return row
