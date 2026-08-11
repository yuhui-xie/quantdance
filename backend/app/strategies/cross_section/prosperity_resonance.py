"""景气共振：盈利增长性价比(PEG) + 趋势确认 + 回调入场，三重共振选股。

核心思路——"好公司 + 好价格 + 好时机"的共振点：

1. 盈利质量层（PEG）：PEG = PE / 盈利增速。低 PEG 意味着以合理价格买入成长。
   过滤 PE≤0（亏损）、PEG 异常低（数据噪音）、PEG 过高（成长与价格不匹配）。
   辅以 PB/PS 上限防止估值极端。

2. 趋势确认层（MA + LLT + 动量）：价格须高于中期均线、窗口收益在合理区间、
   LLT 趋势向上。确保不在下跌趋势中接飞刀，也不在暴涨末端追高。

3. 入场时机层（MA 偏离度）：价格须在短期均线的窄带附近。在趋势确认的前提下，
   回调到均线支撑处入场，比追高有更好的风险收益比。

4. 综合评分：PEG 百分位排名 + 动量百分位排名 + 均线偏离度，按权重加总取 top N。

与现有横截面策略差异：
- vs limit_up_pullback：不需要涨停事件，适用面更广
- vs market_auntie：加入趋势确认和回调入场，持有体验更好
- vs etf_rotation：用 PEG 估值锚替代纯动量，避免追高泡沫
- vs order_inflection：用日常估值数据，不依赖财报披露

A 股适用逻辑：
- 小市值 + 低 PEG 是 A 股文献中最稳健的 alpha 因子组合之一
- 回调入场利用 A 股散户追涨杀跌行为提供更好的成交价格
- 趋势确认过滤掉因基本面恶化而持续下跌的"价值陷阱"
"""

from __future__ import annotations

from typing import Any

import numpy as np
from pydantic import Field, model_validator

from app.factors import llt
from app.factors.cross_section import percentile_ranks
from app.factors.momentum import simple_momentum
from app.factors.trend import sma_last
from app.strategies.base import (
    CrossSectionContext,
    CrossSectionStrategySpec,
)
from app.strategies.cross_section.common import (
    apply_hysteresis,
    is_hs_main_board_symbol,
    is_st_stock,
)
from app.strategies.cross_section.decision import DecisionFrequencyParams
from app.strategies.cross_section.value_bars import (
    ValueBars,
    asof_tradeable_from_bars,
    build_panel_value_bars,
)


class ProsperityResonanceParams(DecisionFrequencyParams):
    """景气共振策略参数。"""

    # ── 决策节奏 ──
    decision_frequency: str = Field(
        "daily", description="决策频率：daily=每日（冷启动期后）| weekly=每周末 | monthly=每自然月末"
    )
    decision_every_n: int = Field(1, ge=1, description="决策步长：monthly+3=季末、weekly+2=双周；daily 忽略")
    decision_warmup: int = Field(
        20, ge=0, le=250, description="日频决策的冷启动期（交易日）：跳过区间前 N 日不做决策"
    )
    hysteresis_rank_threshold: int = Field(
        3, ge=0, le=50, description="防抖滞后带：新候选须比持仓排名领先至少该名次才替换；0 关闭"
    )
    top_n: int = Field(10, ge=1, le=50, description="持仓只数")

    # ── 价格与市值 ──
    min_price: float = Field(5.0, ge=0, description="最低股价（元）")
    max_price: float = Field(100.0, gt=0, description="最高股价（元）")
    min_market_cap: float = Field(
        3e9, ge=0, description="总市值下限（元），默认 30 亿"
    )
    max_market_cap: float = Field(
        5e10, ge=0, description="总市值上限（元），默认 500 亿，中小盘 alpha 更强"
    )

    # ── 盈利质量（核心层：PEG + PE + PB + PS） ──
    require_profit: bool = Field(True, description="要求 PE(TTM)>0，排除亏损股")
    min_peg: float = Field(
        0.1, ge=0.0, le=5.0, description="PEG 下限（过低可能为数据异常）"
    )
    max_peg: float = Field(
        2.0, ge=0.1, le=10.0, description="PEG 上限（GARP 阈值：成长须有合理定价）"
    )
    max_pb: float = Field(8.0, ge=0.1, le=50.0, description="PB 上限，排除极端估值")
    max_ps: float = Field(
        10.0, ge=0.1, le=50.0, description="PS(TTM) 上限，排除市销率极端者"
    )

    # ── 趋势确认（中间层：MA + 动量 + LLT） ──
    lookback_days: int = Field(
        60, ge=10, le=250, description="趋势动量观察窗口（交易日）"
    )
    trend_ma: int = Field(
        25, ge=5, le=250, description="趋势过滤均线周期；收盘价须高于此线"
    )
    momentum_min: float = Field(
        0.0, ge=-1.0, le=5.0, description="窗口最低收益率；默认 0 即不持有下跌趋势股"
    )
    momentum_max: float = Field(
        0.60, ge=0.05, le=10.0, description="窗口最高收益率；排除已暴涨的标的"
    )
    llt_period: int = Field(20, ge=2, le=250, description="LLT 平滑周期")
    llt_slope_lookback: int = Field(
        5, ge=1, le=20, description="LLT 斜率回看交易日数"
    )
    require_llt_up: bool = Field(True, description="要求 LLT 趋势向上")

    # ── 入场时机（精细层：回调到均线支撑附近） ──
    entry_ma: int = Field(
        20, ge=2, le=120, description="入场参考均线周期；价格偏离此线须在带内"
    )
    max_ma_deviation: float = Field(
        0.12, ge=0.0, le=1.0, description="收盘价高于 entry_ma 的最大偏离（比例）"
    )
    min_ma_deviation: float = Field(
        -0.08, ge=-1.0, le=0.0, description="收盘价低于 entry_ma 的最大偏离（负值）"
    )

    # ── 综合评分权重 ──
    w_peg: float = Field(1.0, ge=0.0, le=10.0, description="PEG 因子权重")
    w_momentum: float = Field(0.8, ge=0.0, le=10.0, description="动量因子权重")
    w_entry: float = Field(0.8, ge=0.0, le=10.0, description="入场质量因子权重")

    # ── 板块与风控 ──
    main_board_only: bool = Field(
        False, description="仅沪深主板；默认 False 以覆盖创业板/科创板"
    )
    exclude_st: bool = True
    exclude_limit: bool = True
    exclude_suspended: bool = True
    limit_pct_threshold: float = Field(9.5, ge=1.0, le=30.0)

    @model_validator(mode="after")
    def _validate(self) -> "ProsperityResonanceParams":
        if self.max_price < self.min_price:
            raise ValueError("max_price 不能小于 min_price")
        if self.max_market_cap < self.min_market_cap:
            raise ValueError("max_market_cap 不能小于 min_market_cap")
        if self.max_peg <= self.min_peg:
            raise ValueError("max_peg 必须大于 min_peg")
        if self.momentum_max <= self.momentum_min:
            raise ValueError("momentum_max 必须大于 momentum_min")
        if self.max_ma_deviation < self.min_ma_deviation:
            raise ValueError(
                "max_ma_deviation 不能小于 min_ma_deviation"
            )
        if self.w_peg + self.w_momentum + self.w_entry <= 0:
            raise ValueError("评分权重之和须大于 0")
        if self.max_ma_deviation == 0.0 and self.min_ma_deviation == 0.0:
            raise ValueError("均线偏离带不能全为零（无候选通过）")
        return self


# ═══════════════════════════════════════════════════════════════════
# 选股辅助函数
# ═══════════════════════════════════════════════════════════════════


def _panel_bars(ctx: CrossSectionContext) -> dict[str, ValueBars]:
    """获取或构建 ValueBars 缓存（同一截面复用）。"""
    cached = ctx.cache.get("prosperity_resonance_bars")
    if isinstance(cached, dict):
        return cached
    bars = build_panel_value_bars(ctx.panel)
    ctx.cache["prosperity_resonance_bars"] = bars
    return bars


# ═══════════════════════════════════════════════════════════════════
# 选股主逻辑
# ═══════════════════════════════════════════════════════════════════


def select_prosperity_resonance(
    asof: str,
    ctx: CrossSectionContext,
    params: ProsperityResonanceParams,
) -> tuple[list[str], list[dict[str, Any]]]:
    """在给定截面对股票池逐层过滤 + 综合评分，返回 top N 标的。"""

    need_bars = max(
        params.lookback_days + 1,
        params.trend_ma,
        params.entry_ma,
        params.llt_period + params.llt_slope_lookback,
    )
    bars_map = _panel_bars(ctx)

    candidates: list[dict[str, Any]] = []

    for symbol, bars in bars_map.items():
        # ── 板块过滤 ──
        if params.main_board_only and not is_hs_main_board_symbol(symbol):
            continue
        if params.exclude_st and is_st_stock(ctx.names.get(symbol)):
            continue

        # ── 可交易性 ──
        tradeable = asof_tradeable_from_bars(
            bars,
            asof,
            exclude_suspended=params.exclude_suspended,
            exclude_limit=params.exclude_limit,
            limit_pct_threshold=params.limit_pct_threshold,
        )
        if tradeable is None:
            continue
        end_i, row = tradeable
        if end_i + 1 < need_bars:
            continue

        close = float(row["close"])
        market_cap = row.get("market_cap")
        pe_ttm = row.get("pe_ttm")
        peg = row.get("peg")
        pb = row.get("pb")
        ps_ttm = row.get("ps_ttm")

        # ── 价格 / 市值区间 ──
        if market_cap is None:
            continue
        if close < params.min_price or close > params.max_price:
            continue
        mcap = float(market_cap)
        if mcap < params.min_market_cap or mcap > params.max_market_cap:
            continue

        # ── 第 1 层：盈利质量 ──
        if params.require_profit:
            if pe_ttm is None or not np.isfinite(float(pe_ttm)) or float(pe_ttm) <= 0:
                continue

        peg_val = float(peg) if peg is not None and np.isfinite(float(peg)) else None
        if peg_val is None or peg_val < params.min_peg or peg_val > params.max_peg:
            continue

        pb_val = float(pb) if pb is not None and np.isfinite(float(pb)) else None
        if pb_val is not None and pb_val > params.max_pb:
            continue

        ps_val = (
            float(ps_ttm) if ps_ttm is not None and np.isfinite(float(ps_ttm)) else None
        )
        if ps_val is not None and ps_val > params.max_ps:
            continue

        # ── 第 2 层：趋势确认 ──
        start_i = end_i + 1 - need_bars
        closes = bars.close[start_i : end_i + 1]

        # 窗口收益
        period_ret = simple_momentum(closes, params.lookback_days)
        if period_ret is None:
            continue
        if period_ret < params.momentum_min or period_ret > params.momentum_max:
            continue

        # 趋势均线
        trend_sma = sma_last(closes, params.trend_ma)
        if trend_sma is None or close < trend_sma:
            continue

        # LLT 趋势方向
        if params.require_llt_up:
            trend_llt = llt(closes, period=params.llt_period)
            need_llt = params.llt_slope_lookback + 1
            if len(trend_llt) < need_llt:
                continue
            cur_llt = trend_llt[-1]
            prev_llt = trend_llt[-1 - params.llt_slope_lookback]
            if not np.isfinite(cur_llt) or not np.isfinite(prev_llt):
                continue
            if cur_llt < prev_llt:
                continue

        # ── 第 3 层：入场时机 ──
        entry_sma = sma_last(closes, params.entry_ma)
        if entry_sma is None:
            continue
        ma_dev = close / entry_sma - 1.0
        if ma_dev < params.min_ma_deviation or ma_dev > params.max_ma_deviation:
            continue

        # ── 通过全部过滤，记录候选 ──
        candidates.append(
            {
                "symbol": symbol,
                "name": ctx.names.get(symbol, ""),
                "asof": str(asof)[:10],
                "fund_date": str(row["date"])[:10],
                "close": close,
                "market_cap": mcap,
                "peg": peg_val,
                "pe_ttm": float(pe_ttm) if pe_ttm is not None else None,
                "pb": pb_val,
                "ps_ttm": ps_val,
                "period_return": float(period_ret),
                "ma_deviation": float(ma_dev),
                "trend_ma": float(trend_sma),
                "entry_ma": float(entry_sma),
            }
        )

    if not candidates:
        return [], []

    # ═══════════════════════════════════════════════════════════
    # 综合评分与排名
    # ═══════════════════════════════════════════════════════════

    peg_values = [c["peg"] for c in candidates]
    ret_values = [c["period_return"] for c in candidates]

    peg_ranks = percentile_ranks(peg_values)
    ret_ranks = percentile_ranks(ret_values)

    # 均线偏离度评分归一化用的带宽
    max_band = max(
        abs(params.min_ma_deviation), abs(params.max_ma_deviation), 0.001
    )

    for i, c in enumerate(candidates):
        # PEG：越低越好 → (1 - 百分位排名)
        peg_score = 1.0 - peg_ranks[i]
        # 动量：越强越好 → 百分位排名
        mom_score = ret_ranks[i]
        # 入场：越接近均线越好（偏离度越小分越高）
        entry_score = 1.0 - min(abs(c["ma_deviation"]) / max_band, 1.0)

        total = (
            params.w_peg * peg_score
            + params.w_momentum * mom_score
            + params.w_entry * entry_score
        )
        c["score"] = float(total)
        c["peg_score"] = float(peg_score)
        c["mom_score"] = float(mom_score)
        c["entry_score"] = float(entry_score)

    # 默认排序：PEG 升序 → 动量降序 → 偏离度升序（总分平局时的稳定序）
    candidates.sort(
        key=lambda x: (
            -x["score"],
            x["peg"],
            -x["period_return"],
            abs(x["ma_deviation"]),
        )
    )
    if params.hysteresis_rank_threshold > 0:
        return apply_hysteresis(
            ctx,
            candidates,
            params.top_n,
            params.hysteresis_rank_threshold,
            cache_key="prosperity_resonance_hysteresis",
        )
    picked = candidates[: params.top_n]
    return [c["symbol"] for c in picked], picked


# ═══════════════════════════════════════════════════════════════════
# 策略注册
# ═══════════════════════════════════════════════════════════════════

STRATEGY = CrossSectionStrategySpec(
    id="prosperity_resonance",
    name="景气共振",
    description=(
        "PEG 盈利性价比 + 趋势确认（MA/LLT/动量）+ 回调入场（均线偏离带），"
        "三重共振综合评分，日频检查 + 防抖滞后带增量调仓"
    ),
    params_model=ProsperityResonanceParams,
    select=select_prosperity_resonance,
    default_universe="zz500",
    needs_dividend=False,
    needs_financials=False,
    needs_fundamentals=True,
    default_top_n=10,
    warnings=(
        "PEG 来自东财口径，成分股 PEG 覆盖不全时候选可能偏少；"
        "可放宽 max_peg 或关闭 require_profit 扩大候选池。",
        "趋势确认依赖历史 K 线长度，新股/次新股可能被自动排除。",
        "回调入场在强趋势市场中可能导致候选过少，可放宽均线偏离带或降低 w_entry。",
        "小市值因子在小盘风格切换时可能阶段性跑输大市值指数。",
    ),
)
