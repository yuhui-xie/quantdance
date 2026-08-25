"""ETF 动量轮动：选择 LLT 拟合趋势最强且拟合质量高的 ETF。

用 LLT（Low-Lag Trendline）对价格做低延迟平滑，再对最近 ``llt_window`` 根
K 线的 LLT 趋势线做最小二乘线性回归，得到 ``(斜率, R²)``。综合得分取
``斜率 × R²``：既刻画趋势强弱（斜率），又按拟合质量（R²）加权，避免选中
涨跌混乱、拟合方差大、斜率不可信的标的。默认在 ``config/etf_core_pool.json``
精选 ETF 池上轮动，周期（默认每自然月首个交易日）等权调仓。

轮动带惰性（``rotate_threshold``，默认 0.9）：调仓日对仍可作为候选的当前持仓，
用其当前决策日的得分对比新入选标的的当前得分，不低于新得分×该阈值则继续持有、
不调仓，仅在候选明显更优时才轮动，从而降低标的间的来回切换频率。

可选的量价确认（``volume_confirm``）复用 ``vpt`` 量价趋势，对候选做资金
流入方向过滤：

- ``volume_confirm=False``（默认）：纯 LLT 趋势选股。
- ``volume_confirm=True``：额外要求 VPT 量价趋势的回归斜率为正才纳入候选，
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
from app.strategies.cross_section.common import asof_tradeable_row
from app.strategies.cross_section.decision import DecisionFrequencyParams


# ctx.cache 中保存上一调仓日持仓 symbol 列表的键；用于跨决策日的轮动惰性比较。
CACHE_KEY = "etf_rotation_prev_holdings"


class EtfRotationParams(DecisionFrequencyParams):
    decision_anchor: Literal["start", "end"] = Field(
        "start",
        description="月度决策锚点：start=每自然月首个交易日（默认）| end=每自然月末",
    )
    top_n: int = Field(1, ge=1, le=10, description="持有 LLT 趋势得分最高的 ETF 数量")
    llt_period: int = Field(20, ge=2, le=400, description="LLT 平滑周期")
    llt_window: int = Field(
        20,
        ge=2,
        le=250,
        description="LLT 拟合窗口：对最近该根 K 线的 LLT 趋势线做最小二乘线性回归取斜率",
    )
    min_r2: float = Field(
        0.0,
        ge=0.0,
        le=1.0,
        description=(
            "回归拟合质量（R²）门槛：R² 低于此值视为无可靠趋势（窗口内涨跌混乱、"
            "斜率不可信），不纳入候选；0 表示关闭拟合质量过滤。"
        ),
    )
    min_score: float = Field(
        0.0,
        description="最低综合得分（斜率×R²）；0 表示只持有上升趋势（得分>0）的标的",
    )
    volume_confirm: bool = Field(
        False,
        description="是否启用量能确认：要求 VPT 量价趋势回归斜率为正才纳入候选",
    )
    volume_days: int = Field(
        20,
        ge=2,
        le=250,
        description="量能确认的 VPT 斜率回看交易日数",
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


def _volume_history(
    value_df: pd.DataFrame, asof: str
) -> pd.Series | None:
    """取 asof 及之前的成交量序列（按日期排序去重，索引为 date）。

    与 ``price_history`` 的日期口径保持一致，便于按日对齐；面板无成交量列时返回 None。
    """
    if "volume" not in value_df.columns:
        return None
    v = value_df[["date", "volume"]].copy()
    v["date"] = pd.to_datetime(v["date"], errors="coerce")
    v["volume"] = pd.to_numeric(v["volume"], errors="coerce")
    v = v[v["date"] <= pd.Timestamp(str(asof)[:10])]
    v = v.dropna(subset=["date"])
    v = v.sort_values("date").drop_duplicates("date", keep="last")
    return v.set_index("date")["volume"].astype(float)


def _vpt_slope(history: pd.DataFrame, volumes: pd.Series, window: int) -> float | None:
    """最近 ``window`` 日 VPT 量价趋势的回归斜率；数据不足或无有效值返回 None。

    VPT 是逐日累积的量纲量，但斜率的正负代表资金流入/流出的方向，跨标的比较时
    只取符号做方向过滤，不参与排序（排序仍由 LLT 得分决定）。
    """
    if volumes is None or len(history) < window + 1:
        return None
    # 按日期对齐收盘价与成交量（history 与 volumes 均已按日排序去重）
    aligned = history[["date", "close"]].copy()
    aligned["date"] = pd.to_datetime(aligned["date"], errors="coerce")
    merged = aligned.join(volumes, on="date", how="left")
    if merged["close"].isna().any() or merged["volume"].isna().any():
        return None
    c = merged["close"].astype(float).to_numpy()
    vol = merged["volume"].astype(float).to_numpy()
    vpt_series = vpt(c, vol)
    recent = vpt_series[-window:]
    if not np.isfinite(recent).all():
        return None
    x = np.arange(window)
    slope, _ = linreg(x, recent)
    return slope


def select_etf_rotation(
    asof: str,
    ctx: CrossSectionContext,
    params: EtfRotationParams,
) -> tuple[list[str], list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    # LLT 平滑暖机 + 拟合窗口；量价确认另需 volume_days+1 根
    required_bars = params.llt_period + params.llt_window
    if params.volume_confirm:
        required_bars = max(required_bars, params.volume_days + 1)

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

        # LLT 趋势线（按首值归一化，消除价格水平差异，使斜率跨标的可比）
        trend = llt(closes, period=params.llt_period)
        base = trend[0]
        if not np.isfinite(base) or base <= 0:
            continue
        slope_arr, r2_arr = llt_slope_fit(trend / base, params.llt_window)
        llt_slope_value = float(slope_arr[-1]) if np.isfinite(slope_arr[-1]) else None
        llt_r2 = float(r2_arr[-1]) if np.isfinite(r2_arr[-1]) else None
        if llt_slope_value is None or llt_r2 is None:
            continue
        if llt_r2 < params.min_r2:
            continue
        score = llt_slope_value * llt_r2
        if score <= params.min_score:
            continue

        # 可选量能确认：要求 VPT 量价趋势回归斜率为正，过滤无量能配合的反弹
        vpt_slope_value: float | None = None
        if params.volume_confirm:
            volumes = _volume_history(value_df, asof)
            vpt_slope_value = _vpt_slope(history, volumes, params.volume_days)
            if vpt_slope_value is None or vpt_slope_value <= 0:
                continue

        candidates.append(
            {
                "symbol": symbol,
                "name": ctx.names.get(symbol, ""),
                "asof": str(asof)[:10],
                "close": close,
                "llt_slope": llt_slope_value,
                "llt_r2": llt_r2,
                "score": score,
                "vpt_slope": vpt_slope_value,
                "pct_change": row.get("pct_change"),
            }
        )

    candidates.sort(key=lambda item: (-item["score"], item["symbol"]))
    # 每个候选标注排名，details 返回完整排序候选，便于报告展示全部标的的指标对比。
    for idx, item in enumerate(candidates):
        item["rank"] = idx + 1

    # ---- 轮动惰性（降低调仓频率）----
    # 从 ctx.cache 读取上一调仓日持仓的 symbol 列表（runner 复用同一 context，
    # cache 跨决策日保留）。对仍可作为候选的当前持仓，用它在【当前决策日】的
    # 得分对比新入选标的的【当前】得分：不低于新得分×rotate_threshold 时继续持有，
    # 仅当新候选明显更优才轮动，避免在得分相近的标的间来回切换。
    score_by_symbol = {item["symbol"]: item["score"] for item in candidates}
    selected = [item["symbol"] for item in candidates[: params.top_n]]
    held_symbols: list[str] = ctx.cache.get(CACHE_KEY, [])
    threshold = params.rotate_threshold
    # 1.0（或以上）即关闭惰性，纯按得分轮动；0 表示几乎永不调仓
    if threshold < 1.0 and held_symbols and selected:
        held_set = set(held_symbols)
        selected_set = set(selected)
        for symbol in held_symbols:
            if symbol in selected_set:
                continue
            if symbol not in score_by_symbol:
                # 上一持仓当前时刻已跌出候选（得分≤0/停牌/涨跌停等），无法继续持有
                continue
            # 顶替基准：当前入选里、非当前持仓、当前得分最低的那只新标的
            displaceable = [
                sym for sym in selected
                if sym not in held_set and sym != symbol
            ]
            if not displaceable:
                continue
            weakest_new = min(displaceable, key=lambda sym: score_by_symbol[sym])
            if score_by_symbol[symbol] >= score_by_symbol[weakest_new] * threshold:
                # 当前持仓在当前时刻仍够强：保留它，踢掉最弱的新入选标的
                selected_set.discard(weakest_new)
                selected_set.add(symbol)
        selected = sorted(
            selected_set, key=lambda sym: score_by_symbol[sym], reverse=True
        )

    for item in candidates:
        item["selected"] = item["symbol"] in set(selected)
    # 记录本次持仓的 symbol 供下一决策日惰性比较（runner 复用同一 context，cache 跨日保留）
    ctx.cache[CACHE_KEY] = list(selected)
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
        "趋势判定仅使用调仓日及之前的收盘价，不使用未来数据。",
    ),
)
