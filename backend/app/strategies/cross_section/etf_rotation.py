"""ETF 动量轮动：选择 LLT 拟合趋势最强且拟合质量高的 ETF。

用 LLT（Low-Lag Trendline）对价格做低延迟平滑，再对最近 ``llt_window`` 根
K 线的 LLT 趋势线做最小二乘线性回归，得到 ``(斜率, R²)``。综合得分取
``斜率 × R²``：既刻画趋势强弱（斜率），又按拟合质量（R²）加权，避免选中
涨跌混乱、拟合方差大、斜率不可信的标的。默认在 ``config/etf_core_pool.json``
精选 ETF 池上轮动，周期（默认每自然月首个交易日）等权调仓。

轮动带惰性（``rotate_threshold``，默认 0.9）：调仓日对仍可作为候选的当前持仓，
用其当前决策日的得分对比新入选标的的当前得分，不低于新得分×该阈值则继续持有、
不调仓，仅在候选明显更优时才轮动，从而降低标的间的来回切换频率。
  避免无量能配合的价格反弹；仍按 LLT 得分排序选 top-N。
"""

from __future__ import annotations

from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import Field

from app.factors.llt import llt, llt_slope_fit
from app.factors.momentum import linreg, price_history
from app.factors.volume_flow import vpt
from app.strategies.base import (
    CrossSectionContext,
    CrossSectionStrategySpec,
)
from app.strategies.cross_section.common import (
    apply_score_threshold_rotation,
    asof_tradeable_row,
)

from app.factors.cross_section import zscore
from app.factors.momentum import price_history, slope_momentum
from app.strategies.cross_section.decision import DecisionFrequencyParams


# ctx.cache 中保存上一调仓日持仓 symbol 列表的键；用于跨决策日的轮动惰性比较。
CACHE_KEY = "etf_rotation_prev_holdings"


class EtfRotationParams(DecisionFrequencyParams):
    decision_anchor: Literal["start", "end"] = Field(
        "start",
        description="月度决策锚点：start=每自然月首个交易日（默认）| end=每自然月末",
    )
    top_n: int = Field(1, description="持有 LLT 趋势得分最高的 ETF 数量")
    llt_period: int = Field(
        20, description="LLT 平滑周期"
    )
    llt_window: int = Field(
        20, description="LLT 拟合窗口：对最近该根 K 线的 LLT 趋势线做最小二乘线性回归取斜率",
    )
    min_r2: float = Field( 0.0,)
    llt_slope_filter: float = Field(
        0.0,
        description=(
            "LLT 对数斜率门槛：归一化 LLT 趋势线拟合斜率（≈每根 K 线对数涨幅）"
            "低于该值则剔除，0 关闭。比 MA 硬门限平滑，熊市不来回打脸。"
        ),
    )
    min_score: float = Field(
        0.0,
        description="最低综合得分（斜率×R²）；0 表示只持有上升趋势（得分>0）的标的",
    )
    slope_days: int = Field(
        60, description="斜率模式（score_mode=slope）：归一化价格线性回归的窗口（SLOPE_N）"
    )

    score_mode: Literal["llt", "slope"] = Field(
        "llt",
        description=(
            "排序得分来源：llt=LLT 拟合斜率×R²（默认）；slope=归一化收盘价线性回归"
            "斜率×R² 的横截面 z 值。"
        ),
    )
    rotate_threshold: float = Field(
        0.9,
        ge=0.0,
        le=1.0,
        description=(
            "轮动惰性阈值：对仍可作为候选的当前持仓，若其【当前决策日】的得分不低于"
            "新入选标的的当前得分×该阈值，则保持持仓、不调仓；仅当新候选明显更优"
            "（当前得分超过当前持仓的 1/阈值）才轮动。取值 1.0 表示关闭惰性、纯按"
            "得分轮动；越接近 0 惰性越强（0 时几乎永不调仓）。"
        ),
    )
    exclude_limit: bool = True
    exclude_suspended: bool = True
    limit_pct_threshold: float = Field(9.5, ge=1.0, le=30.0)


def select_etf_rotation(
    asof: str,
    ctx: CrossSectionContext,
    params: EtfRotationParams,
) -> tuple[list[str], list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    use_llt = params.score_mode == "llt"
    # 所需历史根数：llt 模式 = 平滑暖机 + 拟合窗口；slope 模式 = 斜率回归窗口
    required_bars = (
        params.llt_period + params.llt_window
        if use_llt
        else params.slope_days
    )
    for symbol, payload in ctx.panel.items():
        value_df = payload.get("value")
        if value_df is None or value_df.empty:
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

        history = price_history(value_df, asof)
        if len(history) < required_bars:
            continue

        close = float(history["close"].iloc[-1])
        closes = history["close"].astype(float).to_numpy()

        # LLT 趋势线（对趋势线取对数后拟合：回归斜率即每根 K 线的对数涨幅，
        # 天然无量纲、跨标的口径统一，无需再按首值归一化）。仅在 llt 模式作为打分依据。
        llt_fit_score = 0
        llt_slope_value = None
        llt_r2 = None
        if use_llt:
            trend = llt(closes, period=params.llt_period)
            if not np.isfinite(trend[0]) or trend[0] <= 0:
                continue
            slope_arr, r2_arr = llt_slope_fit(np.log(trend), params.llt_window)
            llt_slope_value = float(slope_arr[-1]) if np.isfinite(slope_arr[-1]) else None
            llt_r2 = float(r2_arr[-1]) if np.isfinite(r2_arr[-1]) else None

            if llt_slope_value is None or llt_r2 is None:
                llt_fit_score = -1
            # ── LLT 拟合质量与对数斜率过滤（仅 llt 模式生效）──
            elif llt_r2 < params.min_r2:
                llt_fit_score = -1
            elif params.llt_slope_filter > 0 and llt_slope_value < params.llt_slope_filter:
                llt_fit_score = -1
            else:
                llt_fit_score = llt_slope_value * llt_r2

        # 收盘价斜率（归一化线性回归斜率×R²），slope 模式作为打分依据
        slope_raw = slope_momentum(closes, params.slope_days)

        candidates.append(
            {
                "symbol": symbol,
                "name": ctx.names.get(symbol, ""),
                "asof": str(asof)[:10],
                "close": close,
                "llt_slope": llt_slope_value,
                "llt_r2": llt_r2,
                "score": llt_fit_score,
                # 生值保留量级（可超 1，供核查）；slope_score 下方改为横截面 z 值
                "slope_raw": slope_raw,
                "slope_score": slope_raw,
            }
        )

    # slope 模式：收盘价斜率横截面 z 标准化（均值0、标准差1，z 值可 >1）
    z_slope = zscore([c["slope_raw"] for c in candidates])

    for idx, cand in enumerate(candidates):
        cand["slope_score"] = z_slope[idx]
        if params.score_mode == "slope":
            cand["score"] = z_slope[idx]
        # 其余（score_mode=="llt"）score 已在循环内由 llt_fit_score 设定

    candidates.sort(key=lambda item: (-item["score"], item["symbol"]))

    # 每个候选标注排名，details 返回完整排序候选，便于报告展示全部标的的指标对比。
    for idx, cand in enumerate(candidates):
        cand["rank"] = idx + 1

    # ---- 轮动惰性（降低调仓频率）----
    # 对仍可作为候选的当前持仓，用其【当前决策日】得分对比新入选标的的【当前】得分，
    # 不低于新得分×rotate_threshold 时继续持有，仅当新候选明显更优才轮动，避免在
    # 得分相近的标的间来回切换。上一调仓日持仓从 ctx.cache 读取（runner 复用同一
    # context，cache 跨决策日保留），本次结果由该函数写回 cache。
    selected = apply_score_threshold_rotation(
        ctx,
        candidates,
        params.top_n,
        params.rotate_threshold,
        cache_key=CACHE_KEY,
    )

    selected_set = set(selected)
    for item in candidates:
        item["selected"] = item["symbol"] in selected_set
    return selected, candidates


STRATEGY = CrossSectionStrategySpec(
    id="etf_rotation",
    name="ETF 动量轮动",
    description=(
        "在 ETF 池（默认 config/etf_core_pool.json 精选池）中选择 LLT 拟合趋势"
        "（斜率×R²）最强的标的，每月首个交易日等权调仓；可选用 VPT 量价趋势做资金流入方向确认"
    ),
    params_model=EtfRotationParams,
    select=select_etf_rotation,
    default_universe="etf_core",
    requires_symbols=False,
    needs_fundamentals=False,
    default_top_n=1,
    warnings=(
        "默认在 config/etf_core_pool.json 精选 ETF 池上轮动；如需其他池，"
        "请通过 universe 或 symbols 覆盖。",
        "综合得分 = LLT 拟合斜率 × R²：斜率>0 表示上升趋势，R² 刻画拟合质量；"
        "min_r2 低于阈值时视为无可靠趋势。",
        "当所有 ETF 均未达到最低得分或趋势条件时，组合持有现金。",
        "volume_confirm=True 时额外要求 VPT 量价趋势斜率为正；VPT 为累积量纲量，"
        "仅用其符号做方向过滤，不参与排序。",
        "rotate_threshold 引入轮动惰性：对仍可作为候选的当前持仓，用其当前决策日得分"
        "对比新候选当前得分，不低于新得分×阈值时保持持仓、不调仓，仅在候选明显更优时"
        "轮动；1.0 关闭惰性，越接近 0 惰性越强。",
        "score_mode 决定排序得分来源：llt（默认）= LLT 拟合斜率×R²；slope = 归一化"
        "收盘价线性回归斜率×R² 的横截面 z 值（z 化后可 >1，含义为「高于当日池内平均」"
        "多少个标准差，属正常）。",
        "趋势判定仅使用调仓日及之前的收盘价，不使用未来数据。",
    ),
)
