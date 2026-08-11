"""顺周期行业轮动：以商品类 ETF 为先行信号，在有色/能源/农业周期行业间轮动。

思路来自经典的顺周期逻辑：
- 金银启动 → 有色（有色金属进入牛市周期，买入有色 ETF）；
- 金银中期调整或见顶 → 卖出有色；
- 油价深跌（近似“布伦特跌穿 60”）后反转 → 买入能源 ETF；
- 石油启动后农产品随后启动 → 买入农业 ETF（前置：能源周期先激活）。

信号资产（黄金/白银/原油/豆粕等）用 A 股商品类基金作为价格代理，仅用于周期
判定，不进入持仓。策略按决策日在各激活周期之间轮动，周期内按动量选龙头。
"""

from __future__ import annotations

from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, Field

from app.factors.momentum import price_history, simple_momentum
from app.factors.trend import sma_last
from app.strategies.base import (
    CrossSectionContext,
    CrossSectionStrategySpec,
)
from app.strategies.cross_section.decision import DecisionFrequencyParams
from app.strategies.cross_section.common import asof_tradeable_row


class CycleGroup(BaseModel):
    """一个周期行业：由信号资产驱动，持有对应持仓池。"""

    id: str = Field(description="周期唯一标识，如 nonferrous/energy/agriculture")
    name: str = Field("", description="周期名称，用于结果展示")
    signal_symbols: list[str] = Field(
        default_factory=list,
        description="先行信号资产代码（商品类 ETF/基金），仅用于周期判定",
    )
    signal_mode: Literal["any", "all"] = Field(
        "any", description="any=任一信号走强即激活 | all=全部信号走强才激活"
    )
    holdings: list[str] = Field(
        default_factory=list, description="该周期可持仓的 ETF 池（按动量选龙头）"
    )
    precondition_cycle: str | None = Field(
        None, description="前置周期 id：本周期须在前置周期激活后才允许入场"
    )


DEFAULT_CYCLES: list[CycleGroup] = [
    CycleGroup(
        id="nonferrous",
        name="有色金属",
        signal_symbols=["518880", "161226"],  # 华安黄金ETF / 国投瑞银白银基金
        signal_mode="any",
        holdings=["512400"],  # 南方中证有色金属ETF
    ),
    CycleGroup(
        id="energy",
        name="能源",
        signal_symbols=["501018"],  # 南方原油
        signal_mode="any",
        holdings=["515220"],  # 国泰中证煤炭ETF
    ),
    CycleGroup(
        id="agriculture",
        name="农业",
        signal_symbols=["159985"],  # 华夏豆粕ETF
        signal_mode="any",
        holdings=["159825"],  # 富国中证农业ETF
        precondition_cycle="energy",
    ),
]


class CyclicalRotationParams(DecisionFrequencyParams):
    top_n_per_cycle: int = Field(1, ge=1, le=10, description="每个激活周期内按动量持有的 ETF 数量")
    allocation: Literal["all", "strongest"] = Field(
        "all",
        description="all=所有激活周期一起持有（跨周期等权）| strongest=只持信号强度最高的周期",
    )

    signal_momentum_days: int = Field(20, ge=2, le=200, description="信号资产动量回看交易日数")
    signal_trend_days: int = Field(120, ge=20, le=504, description="信号资产长期均线交易日数（趋势启动确认）")
    exit_ma_days: int = Field(60, ge=5, le=504, description="信号资产中期均线交易日数（中期调整离场）")
    exit_drawdown_pct: float = Field(
        20.0, ge=1.0, le=90.0, description="信号资产距长期峰值回撤达该比例（%）视为见顶，离场"
    )
    energy_entry_drawdown_pct: float = Field(
        30.0,
        ge=1.0,
        le=90.0,
        description="能源周期入场门槛（%）：信号资产在回看窗口内须从峰值回撤达该比例（近似“布伦特跌穿60”）后才允许入场",
    )

    min_cycle_days: int = Field(20, ge=1, le=120, description="周期最小持有交易日数（防抖，避免刚入场即被短期噪音踢出）")

    cycles: list[CycleGroup] = Field(
        default_factory=lambda: DEFAULT_CYCLES, description="周期定义：信号资产与持仓池，可覆盖或扩展"
    )

    exclude_limit: bool = True
    exclude_suspended: bool = True
    limit_pct_threshold: float = Field(9.5, ge=1.0, le=30.0)


def _signal_bullish(
    history: pd.DataFrame,
    params: CyclicalRotationParams,
) -> dict[str, Any] | None:
    """单只信号资产的趋势判定；历史不足返回 None。"""
    required = max(
        params.signal_trend_days,
        params.exit_ma_days,
        params.signal_momentum_days + 1,
    )
    if len(history) < required:
        return None
    closes = history["close"].astype(float).values
    last = float(closes[-1])
    momentum = simple_momentum(closes, params.signal_momentum_days)
    if momentum is None:
        return None
    trend_ma = sma_last(closes, params.signal_trend_days)
    exit_ma = sma_last(closes, params.exit_ma_days)
    window = closes[-params.signal_trend_days:]
    peak = float(window.max())
    low = float(window.min())
    if trend_ma is None or exit_ma is None or peak <= 0:
        return None
    drawdown = last / peak - 1.0
    bullish = (
        momentum > 0
        and last > trend_ma
        and last > exit_ma
        and drawdown > -params.exit_drawdown_pct / 100.0
    )
    return {
        "momentum": momentum,
        "trend_ma": trend_ma,
        "exit_ma": exit_ma,
        "peak": peak,
        "low": low,
        "drawdown": drawdown,
        "bullish": bullish,
    }


def _cycle_holding(
    cycle: CycleGroup,
    ctx: CrossSectionContext,
    params: CyclicalRotationParams,
    asof: str,
) -> tuple[bool, list[dict[str, Any]], list[str]]:
    """判定周期当前是否处于可持有状态，并返回信号明细与交易日历。"""
    signals: list[dict[str, Any]] = []
    calendar: list[str] = []
    for symbol in cycle.signal_symbols:
        payload = ctx.panel.get(symbol)
        value_df = payload.get("value") if payload else None
        if value_df is None or value_df.empty:
            continue
        history = price_history(value_df, asof)
        res = _signal_bullish(history, params)
        if res is None:
            continue
        signals.append(res)
        calendar.extend(str(day)[:10] for day in history["date"])
    calendar = sorted(set(calendar))
    if not signals:
        return False, [], calendar
    if cycle.signal_mode == "all":
        holding = all(bool(s["bullish"]) for s in signals)
    else:
        holding = any(bool(s["bullish"]) for s in signals)
    return holding, signals, calendar


def select_cyclical_rotation(
    asof: str,
    ctx: CrossSectionContext,
    params: CyclicalRotationParams,
) -> tuple[list[str], list[dict[str, Any]]]:
    """按决策日生成目标持仓：激活周期的持仓池按动量选龙头。"""
    states: dict[str, dict[str, Any]] = ctx.cache.setdefault("cycle_states", {})
    active: list[dict[str, Any]] = []

    for cycle in params.cycles:
        holding, signals, calendar = _cycle_holding(cycle, ctx, params, asof)
        prev = states.get(cycle.id, {"on": False, "since": ""})
        prev_on = bool(prev.get("on"))
        prev_since = str(prev.get("since", ""))[:10]

        entry_gates = True
        if cycle.precondition_cycle:
            pre = states.get(cycle.precondition_cycle, {"on": False})
            entry_gates = entry_gates and bool(pre.get("on"))
        if cycle.id == "energy":
            entry_gates = entry_gates and any(
                s["low"] / s["peak"] <= 1.0 - params.energy_entry_drawdown_pct / 100.0
                for s in signals
            )

        if prev_on:
            if holding:
                on, since = True, prev_since or str(asof)[:10]
            elif sum(1 for d in calendar if d > prev_since) < params.min_cycle_days:
                # 保护期：刚入场不久的信号回摆不立刻离场
                on, since = True, prev_since or str(asof)[:10]
            else:
                on, since = False, ""
        else:
            if holding and entry_gates:
                on, since = True, str(asof)[:10]
            else:
                on, since = False, ""
        states[cycle.id] = {"on": on, "since": since}

        if on:
            strength = max((float(s["momentum"]) for s in signals), default=0.0)
            active.append({"cycle": cycle, "strength": strength})

    if params.allocation == "strongest" and len(active) > 1:
        active = [max(active, key=lambda item: item["strength"])]

    targets: list[str] = []
    details: list[dict[str, Any]] = []
    for item in active:
        cycle = item["cycle"]
        ranked: list[dict[str, Any]] = []
        for symbol in cycle.holdings:
            payload = ctx.panel.get(symbol)
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
            if len(history) < params.signal_momentum_days + 1:
                continue
            close_now = float(history["close"].iloc[-1])
            momentum = simple_momentum(
                history["close"].astype(float).values, params.signal_momentum_days
            )
            if momentum is None:
                continue
            ranked.append(
                {
                    "symbol": symbol,
                    "name": ctx.names.get(symbol, ""),
                    "asof": str(asof)[:10],
                    "close": close_now,
                    "momentum": momentum,
                    "lookback_days": params.signal_momentum_days,
                    "cycle_id": cycle.id,
                    "cycle_name": cycle.name,
                    "pct_change": row.get("pct_change"),
                }
            )
        ranked.sort(key=lambda item: (-item["momentum"], item["symbol"]))
        for picked in ranked[: params.top_n_per_cycle]:
            targets.append(picked["symbol"])
            details.append(picked)

    return targets, details


def _default_symbols() -> tuple[str, ...]:
    seen: list[str] = []
    for cycle in DEFAULT_CYCLES:
        for symbol in (*cycle.signal_symbols, *cycle.holdings):
            if symbol not in seen:
                seen.append(symbol)
    return tuple(seen)


STRATEGY = CrossSectionStrategySpec(
    id="cyclical_rotation",
    name="顺周期行业轮动",
    description=(
        "以黄金/白银、原油、豆粕等商品类 ETF 为先行信号，在有色、能源、农业三个"
        "周期行业间轮动；信号走强买入对应行业 ETF 龙头，信号见顶/中期调整离场，月频等权调仓"
    ),
    params_model=CyclicalRotationParams,
    select=select_cyclical_rotation,
    default_universe="all_a",
    default_symbols=_default_symbols(),
    requires_symbols=False,
    needs_fundamentals=False,
    default_top_n=1,
    warnings=(
        "信号资产（黄金/白银/原油/豆粕等）仅用于周期判定，不进入持仓；持仓来自各周期 holdings 池。",
        "默认 ETF 池可能随市场更新而失效，请通过 strategy_params.cycles 覆盖信号与持仓代码。",
        "能源周期须先经历信号资产深度回撤后才允许入场（近似“布伦特跌穿 60”）。",
        "所有周期信号均不满足时组合持有现金。",
        "mode=per_stock 对本策略无意义（资金按 ETF 共享轮动），请使用 universe 或 screen。",
    ),
)
