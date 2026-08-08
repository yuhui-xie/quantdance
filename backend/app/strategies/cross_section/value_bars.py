"""估值日线的 numpy 视图：供截面选股热路径复用，避免反复 pandas 转换。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping

import numpy as np
import pandas as pd


@dataclass(frozen=True, slots=True)
class ValueBars:
    dates: np.ndarray  # str YYYY-MM-DD，已按升序
    close: np.ndarray
    pct_change: np.ndarray
    market_cap: np.ndarray
    pe_ttm: np.ndarray
    peg: np.ndarray
    pb: np.ndarray
    ps_ttm: np.ndarray

    def end_index(self, asof: str) -> int:
        """最后一个 date <= asof 的下标；无则 -1。"""
        asof_s = str(asof)[:10]
        i = int(np.searchsorted(self.dates, asof_s, side="right") - 1)
        return i if i >= 0 else -1


def value_bars_from_df(value_df: pd.DataFrame) -> ValueBars | None:
    if value_df is None or value_df.empty:
        return None
    dates = value_df["date"].astype(str).str[:10].to_numpy()
    # 缓存 CSV / 测试构造偶发乱序时保证 searchsorted 正确
    if len(dates) > 1 and np.any(dates[1:] < dates[:-1]):
        order = np.argsort(dates, kind="mergesort")
        dates = dates[order]
        idx = order
    else:
        idx = None

    def _col(name: str) -> np.ndarray:
        if name not in value_df.columns:
            return np.full(len(dates), np.nan, dtype=float)
        arr = pd.to_numeric(value_df[name], errors="coerce").to_numpy(dtype=float)
        return arr[idx] if idx is not None else arr

    return ValueBars(
        dates=dates,
        close=_col("close"),
        pct_change=_col("pct_change"),
        market_cap=_col("market_cap"),
        pe_ttm=_col("pe_ttm"),
        peg=_col("peg"),
        pb=_col("pb"),
        ps_ttm=_col("ps_ttm"),
    )


def build_panel_value_bars(
    panel: Mapping[str, Mapping[str, Any]],
) -> dict[str, ValueBars]:
    out: dict[str, ValueBars] = {}
    for symbol, payload in panel.items():
        bars = value_bars_from_df(payload.get("value"))
        if bars is not None and len(bars.dates) > 0:
            out[symbol] = bars
    return out


def calendar_lag_days(asof: str, fund_date: str) -> int:
    try:
        return (date.fromisoformat(str(asof)[:10]) - date.fromisoformat(str(fund_date)[:10])).days
    except Exception:
        return 999


def asof_tradeable_from_bars(
    bars: ValueBars,
    asof: str,
    *,
    exclude_suspended: bool = True,
    exclude_limit: bool = True,
    limit_pct_threshold: float = 9.5,
    max_lag_days: int = 10,
) -> tuple[int, dict[str, Any]] | None:
    """返回 (end_index, row_dict)；不可交易则 None。"""
    i = bars.end_index(asof)
    if i < 0:
        return None
    fund_date = str(bars.dates[i])
    lag = calendar_lag_days(asof, fund_date)
    if lag > max_lag_days:
        return None
    if exclude_suspended and lag > 0:
        return None
    close = float(bars.close[i])
    if not np.isfinite(close) or close <= 0:
        return None
    pct = bars.pct_change[i]
    pct_f = float(pct) if np.isfinite(pct) else None
    if exclude_limit and pct_f is not None and abs(pct_f) >= limit_pct_threshold:
        return None
    mcap = bars.market_cap[i]
    pe = bars.pe_ttm[i]
    peg = bars.peg[i]
    pb = bars.pb[i]
    ps = bars.ps_ttm[i]
    row = {
        "date": fund_date,
        "close": close,
        "pct_change": pct_f,
        "market_cap": float(mcap) if np.isfinite(mcap) else None,
        "pe_ttm": float(pe) if np.isfinite(pe) else None,
        "peg": float(peg) if np.isfinite(peg) else None,
        "pb": float(pb) if np.isfinite(pb) else None,
        "ps_ttm": float(ps) if np.isfinite(ps) else None,
    }
    return i, row
