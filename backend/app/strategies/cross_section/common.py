"""组合选股共用过滤。"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from app.data_sources.em_fundamentals import asof_fundamental_row
from app.data_sources.market_data import normalize_a_share_symbol


def is_st_stock(name: str | None) -> bool:
    n = (name or "").upper()
    return "ST" in n or "退" in n


def is_hs_main_board_symbol(symbol: str) -> bool:
    """是否沪深主板 A 股（含原深市中小板）。

    保留：沪市 60xxxx、深市 000/001/002/003。
    排除：创业板 300/301、科创板 688/689、北交所 4/8/92 开头。
    """
    try:
        code = normalize_a_share_symbol(symbol)
    except ValueError:
        return False
    if code.startswith(("300", "301", "688", "689")):
        return False
    if code.startswith(("4", "8")) or code.startswith("92"):
        return False
    if code.startswith("60"):
        return True
    if code.startswith(("000", "001", "002", "003")):
        return True
    return False


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
        lag = (
            date.fromisoformat(str(asof)[:10])
            - date.fromisoformat(str(row["date"])[:10])
        ).days
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
