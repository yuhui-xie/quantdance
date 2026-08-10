"""筹码分布分析（基于历史换手衰减算法）。

算法思路与通达信/同花顺的筹码成本分布一致：
- 每个交易日按换手率作为当日新筹码比例；
- 旧筹码随换手逐日衰减 ``(1 - 换手率)``；
- 当日新筹码在 [low, high] 区间按三角分布分配，峰顶位于当日均价
  ``(high + low + close) / 3``；
- 分布最终归一化为总和 1，表示占总流通筹码的比例。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def chip_cost_distribution(
    df: pd.DataFrame,
    *,
    bins: int = 200,
    include_distribution: bool = False,
) -> dict[str, Any]:
    """计算一只股票的筹码成本分布与派生指标。

    参数:
        df: 按时间升序排序的日线 DataFrame，DatetimeIndex 索引，
            须含列 open/high/low/close/volume/turnover_rate；
            turnover_rate 为小数形式（如 0.03 表示 3%）。
        bins: 价格网格数量。
        include_distribution: 为 True 时额外返回完整的价格网格与筹码分布数组，
            便于可视化。

    返回:
        指标字典，含 profit_ratio / average_cost / peak_price /
        interval_90_low|high / concentration_90 / interval_70_low|high /
        concentration_70；所有交易日换手率为 0 时附 warning；
        include_distribution=True 时附 price_grid 与 distribution 列表。
    """
    if isinstance(bins, bool) or not isinstance(bins, (int, np.integer)):
        raise TypeError("bins 必须为整数")
    if bins < 2:
        raise ValueError("bins 至少为 2")

    required = ("open", "high", "low", "close", "turnover_rate")
    missing = [col for col in required if col not in df.columns]
    if missing:
        raise ValueError(f"缺少列: {', '.join(missing)}")
    if len(df) < 2:
        raise ValueError("数据不足，至少需要2条K线")

    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    turnover = df["turnover_rate"].to_numpy(dtype=float)

    stacked = np.stack([high, low, close, turnover])
    if not np.isfinite(stacked).all():
        raise ValueError("输入数据含无效值（NaN/Inf）")
    if np.any(low > high):
        raise ValueError("输入数据异常：存在最低价高于最高价的K线")

    price_min = float(np.min(low)) * 0.95
    price_max = float(np.max(high)) * 1.05
    bin_edges = np.linspace(price_min, price_max, bins + 1)
    bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2.0

    # 筹码分布概率质量，初始为 0，逐日衰减 + 注入新筹码。
    distribution = np.zeros(bins, dtype=float)
    any_turnover = False

    for i in range(len(df)):
        t = turnover[i]
        if not np.isfinite(t) or t <= 0:
            continue
        any_turnover = True

        lo = low[i]
        hi = high[i]
        avg = np.clip((hi + lo + close[i]) / 3.0, lo, hi)

        # 三角分布权重：峰顶位于 avg，向 low/high 线性递减到 0。
        triangle = np.zeros(bins, dtype=float)
        in_range = (bin_centers >= lo) & (bin_centers <= hi)
        if in_range.any():
            p = bin_centers[in_range]
            triangle[in_range] = np.where(
                p < avg,
                (p - lo) / max(avg - lo, 1e-12),
                np.where(p > avg, (hi - p) / max(hi - avg, 1e-12), 1.0),
            )
        else:
            # 一字板等窄幅行情：当日全部筹码计入最近的价格格点。
            nearest = int(np.argmin(np.abs(bin_centers - (lo + hi) / 2.0)))
            triangle[nearest] = 1.0

        total = triangle.sum()
        if total > 1e-12:
            triangle /= total

        # 旧筹码随换手衰减，同时注入当日新筹码。
        distribution = distribution * (1.0 - t) + t * triangle

    distribution = np.maximum(distribution, 0.0)
    dist_sum = distribution.sum()
    if dist_sum > 1e-12:
        distribution /= dist_sum

    current_close = float(close[-1])
    cdf = np.cumsum(distribution)

    below = bin_centers <= current_close
    profit_ratio = float(cdf[below][-1]) if below.any() else 0.0
    average_cost = float(np.sum(distribution * bin_centers))
    peak_price = float(bin_centers[np.argmax(distribution)])

    def _cost_interval(quantile_low: float, quantile_high: float) -> tuple[float, float, float]:
        idx_low = int(np.searchsorted(cdf, quantile_low))
        idx_high = int(np.searchsorted(cdf, quantile_high))
        p_low = float(bin_centers[min(idx_low, bins - 1)])
        p_high = float(bin_centers[min(idx_high, bins - 1)])
        if p_low + p_high > 0:
            concentration = (p_high - p_low) / (p_high + p_low)
        else:
            concentration = 0.0
        return p_low, p_high, concentration

    interval_90_low, interval_90_high, concentration_90 = _cost_interval(0.05, 0.95)
    interval_70_low, interval_70_high, concentration_70 = _cost_interval(0.15, 0.85)

    result: dict[str, Any] = {
        "profit_ratio": profit_ratio,
        "average_cost": average_cost,
        "peak_price": peak_price,
        "interval_90_low": interval_90_low,
        "interval_90_high": interval_90_high,
        "concentration_90": concentration_90,
        "interval_70_low": interval_70_low,
        "interval_70_high": interval_70_high,
        "concentration_70": concentration_70,
    }
    if not any_turnover:
        result["warning"] = "所有交易日换手率为0，筹码分布可能不反映真实成本结构"
    if include_distribution:
        result["price_grid"] = bin_centers.tolist()
        result["distribution"] = distribution.tolist()
    return result
