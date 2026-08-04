"""独立资金单票回测结果的净值聚合。"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from app.backtest_engine import metrics_from_equity


def equal_weight_equity_curve(
    curves: list[list[dict[str, Any]]],
    *,
    initial_cash: float,
    name: str = "Independent Backtests Equal Weight",
    description: str = "各股票独立资金回测净值归一化后，按可用日期横截面等权合成。",
) -> dict[str, Any] | None:
    """按日期对独立回测净值做等权平均，不模拟共享资金或调仓。"""
    if initial_cash <= 0:
        raise ValueError("initial_cash 必须为正")
    series_list: list[pd.Series] = []
    for curve in curves:
        points: dict[pd.Timestamp, float] = {}
        for row in curve:
            try:
                value = float(row.get("equity"))
            except (TypeError, ValueError):
                continue
            timestamp = pd.to_datetime(str(row.get("date", ""))[:10], errors="coerce")
            if pd.isna(timestamp) or not np.isfinite(value) or value <= 0:
                continue
            points[timestamp.normalize()] = value / initial_cash
        if len(points) >= 2:
            series_list.append(pd.Series(points, dtype=float).sort_index())

    if not series_list:
        return None

    normalized = (
        pd.concat(series_list, axis=1)
        .sort_index()
        .mean(axis=1, skipna=True)
        .dropna()
    )
    if normalized.empty:
        return None

    equity = normalized * initial_cash
    dates = [idx.date().isoformat() for idx in equity.index]
    values = equity.to_numpy(dtype=float)
    metrics = metrics_from_equity(values, initial_cash, [], dates)
    metrics["member_count"] = float(len(series_list))
    metrics["trading_days"] = float(len(equity))
    return {
        "name": name,
        "description": description,
        "metrics": metrics,
        "equity": [
            {"date": date, "equity": float(value)}
            for date, value in zip(dates, values, strict=True)
        ],
    }
