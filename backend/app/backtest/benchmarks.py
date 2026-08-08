"""组合回测基准：沪深300指数买入持有净值。"""

from __future__ import annotations

from typing import Any

import pandas as pd

from app.backtest_engine import metrics_from_equity
from app.data_sources.market_data import MarketDataError, fetch_hs300_index_daily


def build_hs300_benchmark(
    dates: list[str],
    *,
    initial_cash: float = 100_000.0,
) -> dict[str, Any] | None:
    """按组合交易日对齐的沪深300买入持有基准（指数点位归一净值）。"""
    calendar = [str(d)[:10] for d in dates if str(d).strip()]
    if len(calendar) < 2 or initial_cash <= 0:
        return None

    start, end = calendar[0], calendar[-1]
    try:
        idx = fetch_hs300_index_daily(start, end)
    except MarketDataError:
        raise
    if idx.empty:
        return None

    close = pd.to_numeric(idx["close"], errors="coerce")
    close.index = pd.to_datetime(idx["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    close = close[close > 0].dropna()
    if close.empty:
        return None

    # 对齐到组合日历：缺日用前值填充，仍无则跳过前导空缺
    aligned = close.reindex(calendar).ffill()
    first_valid = aligned.first_valid_index()
    if first_valid is None:
        return None
    base = float(aligned.loc[first_valid])
    if base <= 0:
        return None

    nav = (aligned / base).astype(float)
    equity = (nav * float(initial_cash)).astype(float)
    # 前导 NaN 用首个有效净值填齐，保证与组合长度一致便于画图
    equity = equity.ffill().bfill()
    nav = (equity / float(initial_cash)).astype(float)
    eq_arr = equity.to_numpy(dtype=float)
    metrics = metrics_from_equity(eq_arr, float(initial_cash), trades=[], dates=calendar)

    return {
        "name": "沪深300",
        "description": "沪深300指数买入持有（按收盘点位归一），对齐组合交易日。",
        "metrics": metrics,
        "equity": [
            {
                "date": d,
                "equity": float(equity.loc[d]),
                "nav": float(nav.loc[d]),
            }
            for d in calendar
        ],
    }


def attach_hs300_benchmark(
    out: dict[str, Any],
    *,
    initial_cash: float | None = None,
) -> dict[str, Any]:
    """若结果缺少沪深300基准则尝试补齐；失败时写入 warning，不抛错。"""
    benchmarks = dict(out.get("benchmarks") or {})
    if benchmarks.get("hs300"):
        return out

    equity = list(out.get("equity") or [])
    dates = [str(r.get("date", ""))[:10] for r in equity if r.get("date")]
    if len(dates) < 2:
        return out

    metrics = dict(out.get("metrics") or {})
    cash = initial_cash
    if cash is None:
        cash = float(metrics.get("initial_cash") or 0.0) or None
    if cash is None or cash <= 0:
        cash = float(equity[0].get("equity") or 100_000.0)

    warnings = list(out.get("warnings") or [])
    try:
        bm = build_hs300_benchmark(dates, initial_cash=float(cash))
    except Exception as e:
        warnings.append(f"沪深300基准不可用: {e}")
        out = {**out, "warnings": warnings}
        return out

    if bm is None:
        warnings.append("沪深300基准不可用: 指数行情为空")
        out = {**out, "warnings": warnings}
        return out

    benchmarks["hs300"] = bm
    bm_ret = float(bm.get("metrics", {}).get("total_return") or 0.0)
    total_ret = float(metrics.get("total_return") or 0.0)
    metrics["benchmark_total_return"] = bm_ret
    metrics["excess_total_return"] = total_ret - bm_ret
    # 保证收益/回撤比存在
    dd = float(metrics.get("max_drawdown") or 0.0)
    if "return_drawdown_ratio" not in metrics:
        metrics["return_drawdown_ratio"] = (
            float(total_ret / dd) if dd > 1e-12 else 0.0
        )

    return {**out, "benchmarks": benchmarks, "metrics": metrics, "warnings": warnings}
