"""回测股票池的基本面（估值）筛选。

在 resolve_universe_rows 解析出股票池之后、运行回测之前调用，
用单个 as-of 时点当时可知的最新估值数据过滤掉不满足条件的股票。
"""

from __future__ import annotations

from typing import Any, Sequence

import pandas as pd

from app.data_sources.em_fundamentals import (
    asof_fundamental_row,
    load_fundamentals_panel,
    trailing_dividend_yield,
)
from app.schemas import FundamentalFilterRule

VALUATION_FIELDS = (
    "pe_ttm",
    "pb",
    "ps_ttm",
    "peg",
    "market_cap",
    "float_market_cap",
    "close",
    "dividend_yield",
)


def _has_value(val: Any) -> bool:
    return val is not None and not (isinstance(val, float) and pd.isna(val))


def row_passes_fundamental_filter(
    asof_row: dict[str, Any],
    rules: Sequence[FundamentalFilterRule],
    *,
    dividends: pd.DataFrame | None = None,
    price: float | None = None,
) -> bool:
    """判断一行 asof 估值数据是否满足全部规则。

    缺数据(NaN)的字段按不满足处理（该股票被过滤掉），避免用错误数据选入。
    dividend_yield 字段由 dividends + price 现场合成。
    """
    if not asof_row:
        return False
    for rule in rules:
        if rule.field == "dividend_yield":
            if dividends is None:
                return False
            value = trailing_dividend_yield(dividends, asof=asof_row.get("date"), price=price)
        else:
            value = asof_row.get(rule.field)
        if not _has_value(value):
            return False
        if rule.min is not None and float(value) < rule.min:
            return False
        if rule.max is not None and float(value) > rule.max:
            return False
    return True


def apply_fundamental_filter(
    rows: list[dict[str, str]],
    rules: Sequence[FundamentalFilterRule],
    *,
    asof: str,
    panel: dict[str, dict[str, pd.DataFrame]] | None = None,
    use_cache: bool = True,
    force_refresh: bool = False,
    max_workers: int = 8,
) -> tuple[list[dict[str, str]], list[str]]:
    """按估值字段过滤股票池，返回 (保留的 rows, warnings)。

    rows 为 resolve_universe_rows 的输出（含 symbol/name）。若传入已载入的
    panel（横截面路径复用），则不触发二次加载。
    """
    symbols = [row["symbol"] for row in rows]
    if panel is None:
        needs_dividend = any(rule.field == "dividend_yield" for rule in rules)
        panel = load_fundamentals_panel(
            symbols,
            use_cache=use_cache,
            force_refresh=force_refresh,
            max_workers=max_workers,
            include_dividend=needs_dividend,
        )

    kept: list[dict[str, str]] = []
    missing: list[str] = []
    dropped = 0
    for row in rows:
        symbol = row["symbol"]
        payload = panel.get(symbol)
        value = payload.get("value") if payload else None
        if value is None or value.empty:
            missing.append(symbol)
            continue
        asof_row = asof_fundamental_row(value, asof)
        dividends = payload.get("dividend") if payload else None
        price = asof_row.get("close") if asof_row else None
        if not row_passes_fundamental_filter(
            asof_row, rules, dividends=dividends, price=price
        ):
            dropped += 1
            continue
        kept.append(row)

    warnings: list[str] = []
    if missing:
        warnings.append(
            f"{len(missing)} 只股票缺少基本面数据（{asof} 时点无估值），已从股票池剔除。"
        )
    if dropped:
        warnings.append(
            f"{dropped} 只股票不满足基本面筛选条件，已从股票池剔除。"
        )
    return kept, warnings
