"""可跨策略复用的技术指标与横截面因子。

- 指标（数组进数组出）：``bbi`` / ``bullish_alignment`` / ``chip_cost_distribution`` /
  ``ema_alpha`` / ``llt`` / ``rolling_standardized_moment``；
- 区间震荡：``adx``（趋势强度）/ ``choppiness_index``（碎形指数）；
- 动量与历史：``linreg`` / ``price_history`` / ``bias_momentum`` /
  ``slope_momentum`` / ``efficiency_momentum`` / ``simple_momentum``；
- 量价：``obv``（能量潮）/ ``vpt``（量价趋势）；
- 横截面归一化：``zscore`` / ``percentile_ranks``；
- 趋势/均线：``sma_last`` / ``trend_ma``。
"""

from app.factors.bbi import bbi
from app.factors.bullish_alignment import bullish_alignment
from app.factors.chip_distribution import chip_cost_distribution
from app.factors.cross_section import percentile_ranks, zscore
from app.factors.higher_moment import ema_alpha, rolling_standardized_moment
from app.factors.llt import llt, llt_factor, llt_slope_factor
from app.factors.momentum import (
    bias_momentum,
    bias_momentum_factor,
    efficiency_momentum,
    efficiency_momentum_factor,
    linreg,
    price_history,
    simple_momentum,
    simple_momentum_factor,
    slope_momentum,
    slope_momentum_factor,
)
from app.factors.range_motion import adx, adx_factor, choppiness_index, chop_factor
from app.factors.screen_factors import (
    activity_series,
    amihud_illiquidity_factor,
    ma_spread_factor,
    price_vs_ma5_factor,
    relative_volume_factor,
    return_factor,
    sma5_slope_factor,
    volatility_factor,
    volume_ratio_factor,
)
from app.factors.trend import sma_last, trend_ma
from app.factors.volume_flow import obv, vpt, vpt_factor, vpt_slope_factor

__all__ = [
    "activity_series",
    "adx",
    "adx_factor",
    "amihud_illiquidity_factor",
    "bbi",
    "bias_momentum",
    "bias_momentum_factor",
    "bullish_alignment",
    "chip_cost_distribution",
    "choppiness_index",
    "chop_factor",
    "efficiency_momentum",
    "efficiency_momentum_factor",
    "ema_alpha",
    "linreg",
    "llt",
    "llt_factor",
    "llt_slope_factor",
    "ma_spread_factor",
    "obv",
    "percentile_ranks",
    "price_history",
    "price_vs_ma5_factor",
    "relative_volume_factor",
    "return_factor",
    "rolling_standardized_moment",
    "simple_momentum",
    "simple_momentum_factor",
    "slope_momentum",
    "slope_momentum_factor",
    "sma5_slope_factor",
    "sma_last",
    "trend_ma",
    "volatility_factor",
    "volume_ratio_factor",
    "vpt",
    "vpt_factor",
    "vpt_slope_factor",
    "zscore",
]
