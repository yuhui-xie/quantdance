"""涨停回落埋伏：近 N 日有涨停但未大幅上涨，低价小盘盈利，低位均线整理后等权持有。"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field, model_validator

from app.portfolio.base import PortfolioSelectContext, PortfolioStrategySpec
from app.portfolio.common import asof_tradeable_row, is_st_stock


class LimitUpPullbackParams(BaseModel):
    top_n: int = Field(10, ge=1, le=50)
    lookback_days: int = Field(40, ge=10, le=120, description="涨停观察窗口（交易日）")
    min_limit_ups: int = Field(1, ge=1, le=20, description="窗口内最少涨停次数")
    limit_up_pct: float = Field(9.5, ge=5.0, le=30.0, description="涨停近似阈值（%）")
    require_pullback_after_limit: bool = Field(
        True, description="至少一次涨停后次日下跌（不要连涨）"
    )
    forbid_consecutive_limit_ups: bool = Field(
        True, description="排除窗口内出现连板（连续两日涨停）"
    )
    max_period_return: float = Field(
        0.35, ge=0.0, le=5.0, description="观察窗口累计涨幅上限（未大幅上涨）"
    )
    min_price: float = Field(2.0, ge=0)
    max_price: float = Field(20.0, gt=0)
    max_market_cap: float = Field(
        1.0e10, gt=0, description="总市值上限（元），默认约 100 亿"
    )
    require_profit: bool = Field(True, description="要求 PE(TTM)>0，近似非亏损")
    position_lookback: int = Field(
        120, ge=40, le=500, description="相对低位观察窗口（交易日，近似月线位置）"
    )
    max_price_position: float = Field(
        0.5, ge=0.05, le=1.0, description="现价在窗口高低点中的最高分位"
    )
    ma_fast: int = Field(5, ge=2, le=60)
    ma_mid: int = Field(10, ge=3, le=120)
    ma_slow: int = Field(20, ge=5, le=250)
    allow_mild_ma_up: bool = Field(True, description="允许略微多头排列")
    allow_platform: bool = Field(True, description="允许平台整理")
    platform_days: int = Field(20, ge=5, le=60)
    platform_max_range: float = Field(
        0.15, ge=0.02, le=1.0, description="平台振幅上限 (high-low)/mean"
    )
    ma_slope_lookback: int = Field(5, ge=1, le=20, description="慢线斜率回看交易日")
    concept_symbols: list[str] = Field(
        default_factory=list,
        description="可选概念叠加白名单（空=不启用；非空则仅保留名单内标的）",
    )
    exclude_st: bool = True
    exclude_limit: bool = True
    exclude_suspended: bool = True
    limit_pct_threshold: float = Field(9.5, ge=1.0, le=30.0)

    @model_validator(mode="after")
    def check_ranges(self) -> LimitUpPullbackParams:
        if self.max_price < self.min_price:
            raise ValueError("max_price 不能小于 min_price")
        if not (self.ma_fast < self.ma_mid < self.ma_slow):
            raise ValueError("须满足 ma_fast < ma_mid < ma_slow")
        if not self.allow_mild_ma_up and not self.allow_platform:
            raise ValueError("allow_mild_ma_up 与 allow_platform 不能同时为 false")
        return self


def _history_asof(value_df: pd.DataFrame, asof: str, n: int) -> pd.DataFrame | None:
    hist = value_df[value_df["date"] <= asof]
    if len(hist) < n:
        return None
    return hist.tail(n).copy()


def _limit_up_flags(pct: pd.Series, threshold: float) -> np.ndarray:
    arr = pd.to_numeric(pct, errors="coerce").to_numpy(dtype=float)
    return np.isfinite(arr) & (arr >= threshold)


def _has_pullback_after_limit(flags: np.ndarray, pct: pd.Series) -> bool:
    arr = pd.to_numeric(pct, errors="coerce").to_numpy(dtype=float)
    for i in range(len(flags) - 1):
        if flags[i] and np.isfinite(arr[i + 1]) and arr[i + 1] < 0:
            return True
    return False


def _has_consecutive_limit_ups(flags: np.ndarray) -> bool:
    return bool(np.any(flags[:-1] & flags[1:])) if len(flags) >= 2 else False


def _price_position(closes: pd.Series) -> float | None:
    vals = pd.to_numeric(closes, errors="coerce").dropna()
    if len(vals) < 2:
        return None
    lo = float(vals.min())
    hi = float(vals.max())
    last = float(vals.iloc[-1])
    if hi <= lo:
        return 0.5
    return (last - lo) / (hi - lo)


def _sma(closes: pd.Series, window: int) -> float | None:
    vals = pd.to_numeric(closes, errors="coerce")
    if len(vals) < window:
        return None
    chunk = vals.iloc[-window:]
    if chunk.isna().any():
        return None
    return float(chunk.mean())


def _mild_ma_up(
    closes: pd.Series,
    *,
    fast: int,
    mid: int,
    slow: int,
    slope_lookback: int,
) -> bool:
    need = slow + slope_lookback
    if len(closes) < need:
        return False
    sma_f = _sma(closes, fast)
    sma_m = _sma(closes, mid)
    sma_s = _sma(closes, slow)
    sma_s_prev = _sma(closes.iloc[: -slope_lookback], slow)
    if None in (sma_f, sma_m, sma_s, sma_s_prev):
        return False
    # 略微多头：允许小幅粘合，但整体向上
    ordered = sma_f >= sma_m * 0.995 and sma_m >= sma_s * 0.995
    sloping = sma_s >= sma_s_prev
    return bool(ordered and sloping)


def _is_platform(closes: pd.Series, days: int, max_range: float) -> bool:
    if len(closes) < days:
        return False
    chunk = pd.to_numeric(closes.iloc[-days:], errors="coerce").dropna()
    if len(chunk) < max(5, days // 2):
        return False
    mean = float(chunk.mean())
    if mean <= 0:
        return False
    amplitude = (float(chunk.max()) - float(chunk.min())) / mean
    return amplitude <= max_range


def select_limit_up_pullback(
    asof: str,
    ctx: PortfolioSelectContext,
    params: LimitUpPullbackParams,
) -> tuple[list[str], list[dict[str, Any]]]:
    concept = {s.strip() for s in params.concept_symbols if s and str(s).strip()}
    need_bars = max(
        params.lookback_days,
        params.position_lookback,
        params.ma_slow + params.ma_slope_lookback,
        params.platform_days,
    )

    candidates: list[dict[str, Any]] = []
    for symbol, payload in ctx.panel.items():
        if concept and symbol not in concept:
            continue
        value_df = payload.get("value")
        if value_df is None or value_df.empty:
            continue
        if params.exclude_st and is_st_stock(ctx.names.get(symbol)):
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

        close = float(row["close"])
        market_cap = row.get("market_cap")
        pe_ttm = row.get("pe_ttm")
        if market_cap is None:
            continue
        if close < params.min_price or close > params.max_price:
            continue
        if float(market_cap) > params.max_market_cap:
            continue
        if params.require_profit:
            if pe_ttm is None or not np.isfinite(float(pe_ttm)) or float(pe_ttm) <= 0:
                continue

        hist = _history_asof(value_df, asof, need_bars)
        if hist is None:
            continue

        look = hist.tail(params.lookback_days)
        flags = _limit_up_flags(look["pct_change"], params.limit_up_pct)
        n_limit = int(flags.sum())
        if n_limit < params.min_limit_ups:
            continue
        if params.forbid_consecutive_limit_ups and _has_consecutive_limit_ups(flags):
            continue
        if params.require_pullback_after_limit and not _has_pullback_after_limit(
            flags, look["pct_change"]
        ):
            continue

        c0 = float(pd.to_numeric(look["close"].iloc[0], errors="coerce"))
        c1 = float(pd.to_numeric(look["close"].iloc[-1], errors="coerce"))
        if not np.isfinite(c0) or not np.isfinite(c1) or c0 <= 0:
            continue
        period_ret = c1 / c0 - 1.0
        if period_ret > params.max_period_return:
            continue

        pos_hist = hist.tail(params.position_lookback)
        price_pos = _price_position(pos_hist["close"])
        if price_pos is None or price_pos > params.max_price_position:
            continue

        closes = hist["close"]
        mild_up = params.allow_mild_ma_up and _mild_ma_up(
            closes,
            fast=params.ma_fast,
            mid=params.ma_mid,
            slow=params.ma_slow,
            slope_lookback=params.ma_slope_lookback,
        )
        platform = params.allow_platform and _is_platform(
            closes, params.platform_days, params.platform_max_range
        )
        if not (mild_up or platform):
            continue

        candidates.append(
            {
                "symbol": symbol,
                "name": ctx.names.get(symbol, ""),
                "asof": asof,
                "fund_date": row.get("date"),
                "close": close,
                "market_cap": float(market_cap),
                "pe_ttm": float(pe_ttm) if pe_ttm is not None else None,
                "pct_change": row.get("pct_change"),
                "limit_up_count": n_limit,
                "period_return": float(period_ret),
                "price_position": float(price_pos),
                "mild_ma_up": bool(mild_up),
                "platform": bool(platform),
            }
        )

    candidates.sort(
        key=lambda x: (
            x["market_cap"],
            -x["limit_up_count"],
            x["price_position"],
            x["period_return"],
        )
    )
    picked = candidates[: params.top_n]
    return [c["symbol"] for c in picked], picked


STRATEGY = PortfolioStrategySpec(
    id="limit_up_pullback",
    name="涨停回落埋伏",
    description="近40日有涨停且回落、未大幅上涨；低价小盘盈利、月线相对低位、均线略多或平台整理",
    params_model=LimitUpPullbackParams,
    select=select_limit_up_pullback,
    default_universe="all_a",
    needs_dividend=False,
    default_top_n=10,
    warnings=(
        "涨停用日涨跌幅阈值近似，非交易所正式涨停状态；科创/创业板 20% 阈值需自行放宽 limit_up_pct。",
        "「相对低位」用日线高低分位近似月线位置；概念叠加请用 concept_symbols 白名单人工筛入。",
        "小盘低价股容量有限，建议保留较高滑点；截面与名称过滤存在幸存者偏差。",
    ),
)
