"""可跨策略复用的技术指标与横截面因子。

- 指标（数组进数组出）：``bbi`` / ``chip_cost_distribution`` / ``ema_alpha`` /
  ``llt`` / ``rolling_standardized_moment``；
- 动量与历史：``linreg`` / ``price_history`` / ``bias_momentum`` /
  ``slope_momentum`` / ``efficiency_momentum`` / ``simple_momentum``；
- 横截面归一化：``zscore`` / ``percentile_ranks``；
- 趋势/均线：``sma_last`` / ``trend_ma``。
"""

from app.factors.bbi import bbi
from app.factors.chip_distribution import chip_cost_distribution
from app.factors.cross_section import percentile_ranks, zscore
from app.factors.higher_moment import ema_alpha, rolling_standardized_moment
from app.factors.llt import llt
from app.factors.momentum import (
    bias_momentum,
    efficiency_momentum,
    linreg,
    price_history,
    simple_momentum,
    slope_momentum,
)
from app.factors.trend import sma_last, trend_ma

__all__ = [
    "bbi",
    "bias_momentum",
    "chip_cost_distribution",
    "efficiency_momentum",
    "ema_alpha",
    "linreg",
    "llt",
    "percentile_ranks",
    "price_history",
    "rolling_standardized_moment",
    "simple_momentum",
    "slope_momentum",
    "sma_last",
    "trend_ma",
    "zscore",
]
