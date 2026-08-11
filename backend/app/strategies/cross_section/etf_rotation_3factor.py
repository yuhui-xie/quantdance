"""三因子ETF轮动：乖离动量 + 斜率动量 + 效率动量 加权融合的 ETF 轮动策略。

思路来自动量多因子集成（类似集成学习的投票机制）：
- 乖离动量：价格偏离长期均线的程度与趋势方向；
- 斜率动量：归一化价格的线性回归斜率 × R²，同时衡量趋势强度与质量；
- 效率动量：价格中枢的对数动量 × 效率系数（净移动/总移动），刻画走势流畅度。

三个因子在候选池内做 Z-Score 标准化后加权融合，消除量纲差异；再以
``rebalance_threshold``（默认 1.5×）做调仓滞后带，避免震荡期频繁换仓。

三个因子与 Z-Score 标准化从 ``app.factors`` 因子库复用。
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from app.factors.cross_section import zscore
from app.factors.momentum import (
    bias_momentum,
    efficiency_momentum,
    price_history,
    slope_momentum,
)
from app.strategies.base import (
    CrossSectionContext,
    CrossSectionStrategySpec,
)
from app.strategies.cross_section.common import asof_tradeable_row
from app.strategies.cross_section.decision import DecisionFrequencyParams


class ThreeFactorEtfRotationParams(DecisionFrequencyParams):
    # ── 三因子参数 ──
    bias_ma_days: int = Field(
        20, ge=2, le=504, description="乖离因子：计算乖离度所用的长周期均线（BIAS_N）"
    )
    bias_momentum_days: int = Field(
        25, ge=2, le=250, description="乖离因子：对最近 N 天乖离度做线性回归的窗口（MOMENTUM_DAY）"
    )
    slope_days: int = Field(
        60, ge=2, le=504, description="斜率因子：归一化价格线性回归的窗口（SLOPE_N）"
    )
    efficiency_days: int = Field(
        60, ge=2, le=504, description="效率因子：价格中枢（OHLC 均值）动量与波动窗口"
    )

    # ── 因子权重（内部自动归一化）──
    weight_bias: float = Field(1 / 3, ge=0.0, le=1.0, description="乖离动量因子权重")
    weight_slope: float = Field(1 / 3, ge=0.0, le=1.0, description="斜率动量因子权重")
    weight_efficiency: float = Field(1 / 3, ge=0.0, le=1.0, description="效率动量因子权重")

    # ── 调仓阈值与空仓 ──
    rebalance_threshold: float = Field(
        1.5, ge=1.0, le=10.0, description="调仓阈值：挑战者须超过当前持仓得分 × 该倍数才切换；1.0 关闭滞后"
    )
    min_score_to_hold: float = Field(
        0.0, description="最高加权得分低于该值时组合空仓（仅当无粘性持仓时生效）"
    )

    exclude_limit: bool = True
    exclude_suspended: bool = True
    limit_pct_threshold: float = Field(9.5, ge=1.0, le=30.0)


def select_three_factor(
    asof: str,
    ctx: CrossSectionContext,
    params: ThreeFactorEtfRotationParams,
) -> tuple[list[str], list[dict[str, Any]]]:
    required = max(
        params.bias_ma_days + params.bias_momentum_days,
        params.slope_days,
        params.efficiency_days,
    )
    candidates: list[dict[str, Any]] = []

    for symbol, payload in ctx.panel.items():
        value_df = payload.get("value") if payload else None
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
        if len(history) < required:
            continue

        closes = history["close"].astype(float).values
        bias_score = bias_momentum(closes, params.bias_ma_days, params.bias_momentum_days)
        slope_score = slope_momentum(closes, params.slope_days)
        efficiency_score = efficiency_momentum(history, params.efficiency_days)
        if bias_score is None or slope_score is None or efficiency_score is None:
            continue

        candidates.append(
            {
                "symbol": symbol,
                "name": ctx.names.get(symbol, ""),
                "asof": str(asof)[:10],
                "close": float(history["close"].iloc[-1]),
                "bias_score": bias_score,
                "slope_score": slope_score,
                "efficiency_score": efficiency_score,
                "pct_change": row.get("pct_change"),
            }
        )

    if not candidates:
        return [], []

    # ── 横截面 Z-Score 标准化 + 加权融合 ──
    z_bias = zscore([c["bias_score"] for c in candidates])
    z_slope = zscore([c["slope_score"] for c in candidates])
    z_eff = zscore([c["efficiency_score"] for c in candidates])

    total_w = params.weight_bias + params.weight_slope + params.weight_efficiency
    w_b = params.weight_bias / total_w if total_w > 0 else 1 / 3
    w_s = params.weight_slope / total_w if total_w > 0 else 1 / 3
    w_e = params.weight_efficiency / total_w if total_w > 0 else 1 / 3

    for i, cand in enumerate(candidates):
        cand["bias_z"] = z_bias[i]
        cand["slope_z"] = z_slope[i]
        cand["efficiency_z"] = z_eff[i]
        cand["final_score"] = w_b * z_bias[i] + w_s * z_slope[i] + w_e * z_eff[i]

    candidates.sort(key=lambda c: (-c["final_score"], c["symbol"]))
    ranked = candidates
    best = ranked[0]

    # ── 调仓阈值（1.5×）滞后带：状态跨决策日保存在 ctx.cache ──
    holding = ctx.cache.get("three_factor_holding")
    held_symbol = holding.get("symbol") if holding else None
    held_entry = next(
        (c for c in ranked if c["symbol"] == held_symbol), None
    ) if held_symbol else None

    threshold = float(params.rebalance_threshold)
    chosen: str | None
    if len(ranked) == 1:
        # 单一候选：无截面可比，Z-Score 恒为 0，直接持有该标的
        chosen = ranked[0]["symbol"]
    elif (
        threshold > 1.0
        and held_entry is not None
        and held_entry["final_score"] > 0
    ):
        # 粘性持有：挑战者须超过当前持仓得分 × 阈值才切换
        if best["final_score"] > held_entry["final_score"] * threshold:
            chosen = best["symbol"]
        else:
            chosen = held_entry["symbol"]
    else:
        # 无粘性持仓（首日 / 原持仓失效 / 关闭阈值）→ 取最高分；全负分时空仓
        chosen = best["symbol"] if best["final_score"] > params.min_score_to_hold else None

    for cand in ranked:
        cand["held"] = cand["symbol"] == chosen
    chosen_detail = next((c for c in ranked if c["symbol"] == chosen), None)
    ctx.cache["three_factor_holding"] = {
        "symbol": chosen,
        "score": chosen_detail["final_score"] if chosen_detail is not None else 0.0,
    }

    targets = [chosen] if chosen else []
    return targets, ranked


STRATEGY = CrossSectionStrategySpec(
    id="etf_rotation_3factor",
    name="三因子ETF轮动",
    description=(
        "以乖离动量、斜率动量、效率动量三个动量因子做 Z-Score 标准化后加权融合评分，"
        "在跨市场 ETF 池（红利低波/创业板50/纳指/黄金）间轮动，1.5× 阈值防抖"
    ),
    params_model=ThreeFactorEtfRotationParams,
    select=select_three_factor,
    default_universe="all_a",
    default_symbols=("512890", "159949", "513100", "518880"),
    requires_symbols=False,
    needs_fundamentals=False,
    default_top_n=1,
    warnings=(
        "三因子均为相对动量指标，仅在候选池内做 Z-Score 标准化；候选越少，标准化越粗糙。",
        "当最高加权得分低于 min_score_to_hold 时组合持有现金；可通过调高 rebalance_threshold 进一步降低换手。",
        "默认 ETF 池可能随市场更新而失效，请通过 symbols 或覆盖 default_symbols 更换标的。",
        "排名仅使用调仓日及之前的 OHLCV 数据，不使用未来数据。",
    ),
)
