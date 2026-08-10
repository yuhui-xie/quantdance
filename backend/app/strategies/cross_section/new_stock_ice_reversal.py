"""次新情绪冰点反转：次新股板块跌停潮 → 情绪冰点 → 冰点次日买入止跌/反转次新篮。

理念（策略出处）：
- 次新股聚集了市场上风险偏好最高的一批短线资金，是市场情绪最敏锐的"温度计"。
- 当次新股板块出现连续跌停潮（如两个跌停），意味着短线资金被极限压制，市场情绪
  达到冰点；A 股只能做多赚钱，极致冰点后必然酝酿反转。
- 反转由这批高风偏资金选择次新作为突破口，尤其是"地天板"（前一日跌停、当日涨停）
  次新，最能带动情绪并促使大盘反弹。
- 本策略：动态识别次新股池 → 检测连续跌停潮冰点 → 冰点次日买入止跌/反转的次新篮
  （地天板优先、超跌次之）→ 持有固定决策日数后全仓退出。

实现说明：
- 次新股按"上市不超过 max_listed_days 个交易日"动态识别，只用价格数据
  （needs_fundamentals=False，加载快）。
- 冰点度量：limit_down_ratio=池内当日跌停占比（按板块 10%/20% 阈值判定）；
  avg_drop=池内当日平均涨跌幅。需连续 crash_days 日满足条件。
- 入场时机 entry_timing="next_day"：跌停潮刚结束（窗口截至前一交易日冰点、当日
  已非冰点）的次日买入，避免冰点日接飞刀；"on_ice" 则在冰点当日收盘买入。
- 持有期由 select 状态机管理（ctx.cache）：按 entry_prices 逐日检查止盈止损，
  到 hold_days 决策日数后清仓。建议请求使用 rebalance_mode="incremental"，避免
  换仓重置成本基准。
"""

from __future__ import annotations

from typing import Any, Literal, Sequence

import numpy as np
from pydantic import BaseModel, Field, model_validator

from app.data_sources.market_data import normalize_a_share_symbol
from app.strategies.base import (
    CrossSectionContext,
    CrossSectionStrategySpec,
    decision_dates_by_frequency,
)
from app.strategies.cross_section.common import is_st_stock
from app.strategies.cross_section.value_bars import (
    ValueBars,
    build_panel_value_bars,
)


_BARS_CACHE_KEY = "new_stock_ice_reversal_bars"
_STATE_KEY = "new_stock_ice_reversal_state"


class NewStockIceReversalParams(BaseModel):
    """次新情绪冰点反转策略参数。"""

    # ── 决策节奏 ──
    decision_frequency: Literal["daily", "weekly", "monthly"] = Field(
        "daily", description="决策频率：daily=每日 | weekly=每周末 | monthly=每自然月末"
    )
    decision_every_n: int = Field(1, ge=1, description="决策步长；daily 忽略")
    decision_warmup: int = Field(
        20, ge=0, le=250, description="日频决策冷启动期（交易日）：跳过区间前 N 日"
    )

    # ── 次新定义 ──
    max_listed_days: int = Field(
        250, ge=10, le=1250, description="次新定义：上市不超过 N 个交易日（约 1 年）"
    )
    min_listed_days: int = Field(
        5, ge=2, le=120, description="至少上市 N 个交易日，保证有足够 K 线"
    )

    # ── 冰点检测 ──
    ice_metric: Literal["limit_down_ratio", "avg_drop"] = Field(
        "avg_drop",
        description="冰点度量：limit_down_ratio=跌停占比 | avg_drop=板块平均涨跌幅",
    )
    crash_days: int = Field(
        2, ge=1, le=10, description="冰点需连续 N 日满足条件（默认 2=两个跌停）"
    )
    min_limit_down_ratio: float = Field(
        0.30, ge=0.05, le=1.0, description="跌停占比下限（limit_down_ratio 模式）"
    )
    crash_pct_threshold: float = Field(
        -3.0, gt=-30.0, le=0.0, description="板块平均涨跌幅阈值，%（avg_drop 模式）"
    )
    entry_timing: Literal["on_ice", "next_day"] = Field(
        "next_day",
        description="入场时机：on_ice=冰点当日收盘买入 | next_day=跌停潮结束次日企稳买入",
    )
    limit_pct_threshold: float = Field(
        9.5,
        ge=1.0,
        le=30.0,
        description="主板涨跌停判定阈值；创业板(300/301)/科创板(688/689) 自动 ×2",
    )

    # ── 选股与持有 ──
    top_n: int = Field(5, ge=1, le=20, description="单次买入只数")
    hold_days: int = Field(
        5, ge=1, le=60, description="持有决策日数（日频=交易日）"
    )
    min_price: float = Field(2.0, ge=0, description="最低股价（元）")
    max_price: float = Field(200.0, gt=0, description="最高股价（元）")
    min_pool_size: int = Field(
        4, ge=1, le=50, description="次新池最小标的数，不足则无法判定冰点"
    )
    exclude_st: bool = True
    prefer_ditianban: bool = Field(
        True, description="地天板（前收跌停+当日涨停）次新优先"
    )
    stop_loss_pct: float | None = Field(
        0.08, gt=0, le=0.5, description="持有期相对买入价止损；None 关闭"
    )
    take_profit_pct: float | None = Field(
        0.25, gt=0, le=2.0, description="持有期相对买入价止盈；None 关闭"
    )

    @model_validator(mode="after")
    def _check(self) -> "NewStockIceReversalParams":
        if self.max_price < self.min_price:
            raise ValueError("max_price 不能小于 min_price")
        return self


# ═══════════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════════


def _board_limit_pct(symbol: str, base: float) -> float:
    """按板块返回涨跌停判定阈值：创业板/科创板 ±20%，其余 ±10%。"""
    try:
        code = normalize_a_share_symbol(symbol)
    except ValueError:
        return base
    if code.startswith(("300", "301", "688", "689")):
        return 2.0 * base
    return base


def _panel_bars(ctx: CrossSectionContext) -> dict[str, ValueBars]:
    """获取或构建 ValueBars 缓存（同一截面复用）。"""
    cached = ctx.cache.get(_BARS_CACHE_KEY)
    if isinstance(cached, dict):
        return cached
    bars = build_panel_value_bars(ctx.panel)
    ctx.cache[_BARS_CACHE_KEY] = bars
    return bars


def _new_stock_pool(
    bars_map: dict[str, ValueBars],
    asof: str,
    ctx: CrossSectionContext,
    params: NewStockIceReversalParams,
) -> list[tuple[str, ValueBars, int, int]]:
    """返回截至 asof 的次新股池：[(symbol, bars, end_index, days_listed)]。"""
    pool: list[tuple[str, ValueBars, int, int]] = []
    asof_s = str(asof)[:10]
    for symbol, bars in bars_map.items():
        if params.exclude_st and is_st_stock(ctx.names.get(symbol)):
            continue
        i = bars.end_index(asof_s)
        if i < 0:
            continue
        days_listed = i + 1  # 上市以来的交易日数（含当日）
        if days_listed < params.min_listed_days or days_listed > params.max_listed_days:
            continue
        close = bars.close[i]
        if not np.isfinite(close) or close <= 0:
            continue
        pool.append((symbol, bars, i, days_listed))
    return pool


def _day_pct_values(
    bars_map: dict[str, ValueBars],
    pool: list[tuple[str, ValueBars, int, int]],
    day: str,
) -> tuple[list[float], list[str]]:
    """返回某交易日池内各次新的涨跌幅与代码；无行情/停牌日跳过。"""
    values: list[float] = []
    symbols: list[str] = []
    for symbol, bars, _i, _dl in pool:
        pos = int(np.searchsorted(bars.dates, day, side="right") - 1)
        if pos < 0 or str(bars.dates[pos]) != day:
            continue
        pct = bars.pct_change[pos]
        if not np.isfinite(pct):
            continue
        values.append(float(pct))
        symbols.append(symbol)
    return values, symbols


def _day_metric(
    bars_map: dict[str, ValueBars],
    pool: list[tuple[str, ValueBars, int, int]],
    day: str,
    params: NewStockIceReversalParams,
    limit_pct_map: dict[str, float],
) -> float:
    """某交易日的冰点度量：跌停占比或板块平均涨跌幅。"""
    values, symbols = _day_pct_values(bars_map, pool, day)
    if not values:
        return 0.0
    if params.ice_metric == "avg_drop":
        return float(np.mean(values))
    n_down = sum(
        1
        for symbol, pct in zip(symbols, values)
        if pct <= -limit_pct_map[symbol]
    )
    return n_down / len(values)


def _is_crash_day(metric_value: float, params: NewStockIceReversalParams) -> bool:
    if params.ice_metric == "limit_down_ratio":
        return metric_value >= params.min_limit_down_ratio
    return metric_value <= params.crash_pct_threshold


def _is_ice_point(
    asof: str,
    bars_map: dict[str, ValueBars],
    pool: list[tuple[str, ValueBars, int, int]],
    params: NewStockIceReversalParams,
    limit_pct_map: dict[str, float],
) -> bool:
    """次新板块连续 crash_days 日冰点是否成立（窗口截至 asof 或前一交易日）。"""
    if len(pool) < params.min_pool_size:
        return False

    # 参考标的：在 asof 当日有行情的池内标的，取历史最长者作为交易日历
    ref: tuple[int, ValueBars] | None = None
    asof_s = str(asof)[:10]
    for _symbol, bars, i, _dl in pool:
        if str(bars.dates[i]) == asof_s and (ref is None or i > ref[0]):
            ref = (i, bars)
    if ref is None:
        return False
    i_ref, bars_ref = ref
    if i_ref < params.crash_days:
        return False

    def is_crash(day: str) -> bool:
        return _is_crash_day(
            _day_metric(bars_map, pool, day, params, limit_pct_map), params
        )

    if params.entry_timing == "next_day":
        # 窗口截至前一交易日；且当日已非冰点（跌停潮已止）
        window = [
            str(bars_ref.dates[k]) for k in range(i_ref - params.crash_days, i_ref)
        ]
        today = str(bars_ref.dates[i_ref])
        return all(is_crash(d) for d in window) and not is_crash(today)

    # on_ice：窗口截至当日
    window = [
        str(bars_ref.dates[k])
        for k in range(i_ref - params.crash_days + 1, i_ref + 1)
    ]
    return all(is_crash(d) for d in window)


def _select_basket(
    asof: str,
    bars_map: dict[str, ValueBars],
    pool: list[tuple[str, ValueBars, int, int]],
    ctx: CrossSectionContext,
    params: NewStockIceReversalParams,
    limit_pct_map: dict[str, float],
) -> tuple[list[str], list[dict[str, Any]]]:
    """冰点触发后在次新池中选股：地天板优先 → 超跌 → 当日涨幅。"""
    asof_s = str(asof)[:10]
    candidates: list[dict[str, Any]] = []

    for symbol, bars, i, days_listed in pool:
        if str(bars.dates[i]) != asof_s:
            continue  # 停牌，无法成交
        close = float(bars.close[i])
        if not np.isfinite(close) or close <= 0:
            continue
        if close < params.min_price or close > params.max_price:
            continue
        pct_today = bars.pct_change[i]
        if not np.isfinite(pct_today):
            continue
        limit_pct = limit_pct_map[symbol]
        if pct_today <= -limit_pct:
            continue  # 今日封死跌停买不进

        prev_pct = bars.pct_change[i - 1] if i >= 1 else np.nan
        is_dtb = (
            np.isfinite(prev_pct)
            and prev_pct <= -limit_pct
            and pct_today >= limit_pct
        )
        # 累积跌幅：最近 crash_days 个有效涨跌幅之和（越负越超跌）
        depth = 0.0
        cnt = 0
        for k in range(params.crash_days):
            v = bars.pct_change[i - k] if (i - k) >= 0 else np.nan
            if np.isfinite(v):
                depth += float(v)
                cnt += 1

        candidates.append(
            {
                "symbol": symbol,
                "name": ctx.names.get(symbol, ""),
                "date": asof_s,
                "fund_date": str(bars.dates[i]),
                "close": close,
                "pct_change": float(pct_today),
                "pct_prev": float(prev_pct) if np.isfinite(prev_pct) else None,
                "days_listed": days_listed,
                "crash_depth": depth if cnt else 0.0,
                "is_ditianban": bool(is_dtb),
                "limit_pct": limit_pct,
            }
        )

    if not candidates:
        return [], []

    if params.prefer_ditianban:
        candidates.sort(
            key=lambda c: (
                -int(c["is_ditianban"]),
                c["crash_depth"],
                -c["pct_change"],
            )
        )
    else:
        candidates.sort(key=lambda c: (c["crash_depth"], -c["pct_change"]))

    picked = candidates[: params.top_n]
    return [c["symbol"] for c in picked], picked


def _continue_holding(
    asof: str,
    bars_map: dict[str, ValueBars],
    ctx: CrossSectionContext,
    params: NewStockIceReversalParams,
    state: dict[str, Any],
) -> tuple[list[str], list[dict[str, Any]]]:
    """持有期逐决策日检查止盈止损；到 hold_days 后清仓退出。"""
    asof_s = str(asof)[:10]
    hold_count = int(state["hold_count"]) + 1
    if hold_count >= params.hold_days:
        ctx.cache.pop(_STATE_KEY, None)
        return [], []

    basket: list[str] = []
    details: list[dict[str, Any]] = []
    for sym in state["basket"]:
        bars = bars_map.get(sym)
        if bars is None:
            continue
        i = bars.end_index(asof_s)
        if i < 0:
            continue
        close = bars.close[i]
        if not np.isfinite(close) or close <= 0:
            continue
        entry = state["entry_prices"].get(sym)
        if entry is None or entry <= 0:
            continue
        ret = float(close) / float(entry) - 1.0
        if params.stop_loss_pct is not None and ret <= -params.stop_loss_pct:
            continue  # 止损，目标篮剔除后由引擎卖出
        if params.take_profit_pct is not None and ret >= params.take_profit_pct:
            continue  # 止盈
        basket.append(sym)
        details.append(
            {
                "symbol": sym,
                "name": ctx.names.get(sym, ""),
                "date": asof_s,
                "close": float(close),
                "return_since_entry": float(ret),
                "hold_count": hold_count,
            }
        )

    state["hold_count"] = hold_count
    if not basket:
        ctx.cache.pop(_STATE_KEY, None)
        return [], []
    return basket, details


# ═══════════════════════════════════════════════════════════════════
# 选股主逻辑
# ═══════════════════════════════════════════════════════════════════


def select_new_stock_ice_reversal(
    asof: str,
    ctx: CrossSectionContext,
    params: NewStockIceReversalParams,
) -> tuple[list[str], list[dict[str, Any]]]:
    """在给定截面执行：持有期管理 → 冰点检测 → 选股入场。"""
    bars_map = _panel_bars(ctx)
    asof_s = str(asof)[:10]
    pool = _new_stock_pool(bars_map, asof_s, ctx, params)

    state = ctx.cache.get(_STATE_KEY)
    if state is not None:
        return _continue_holding(asof, bars_map, ctx, params, state)

    if len(pool) < params.min_pool_size:
        return [], []
    limit_pct_map = {
        symbol: _board_limit_pct(symbol, params.limit_pct_threshold)
        for symbol, _bars, _i, _dl in pool
    }
    if not _is_ice_point(asof, bars_map, pool, params, limit_pct_map):
        return [], []

    targets, details = _select_basket(asof, bars_map, pool, ctx, params, limit_pct_map)
    if not targets:
        return [], []

    ctx.cache[_STATE_KEY] = {
        "entry_asof": asof_s,
        "basket": targets,
        "entry_prices": {
            sym: float(bars_map[sym].close[bars_map[sym].end_index(asof_s)])
            for sym in targets
        },
        "hold_count": 0,
    }
    return targets, details


def decision_dates(
    calendar: Sequence[str],
    ctx: CrossSectionContext,
    params: NewStockIceReversalParams,
) -> list[str]:
    """决策日：默认日频每个交易日检查信号，跳过冷启动期。"""
    del ctx
    return decision_dates_by_frequency(
        calendar,
        frequency=params.decision_frequency,
        every_n=params.decision_every_n,
        warmup=params.decision_warmup,
    )


# ═══════════════════════════════════════════════════════════════════
# 策略注册
# ═══════════════════════════════════════════════════════════════════

STRATEGY = CrossSectionStrategySpec(
    id="new_stock_ice_reversal",
    name="次新情绪冰点反转",
    description=(
        "次新股板块连续跌停潮触发情绪冰点，冰点次日买入止跌/反转的次新篮"
        "（地天板优先、超跌次之），持有固定天数后全仓退出"
    ),
    params_model=NewStockIceReversalParams,
    select=select_new_stock_ice_reversal,
    decision_dates=decision_dates,
    default_universe="all_a",
    needs_fundamentals=True,
    needs_dividend=False,
    needs_financials=False,
    default_top_n=5,
    warnings=(
        "次新股为动态识别（上市不超过 max_listed_days 个交易日）；随机抽样股票池可能漏掉次新，"
        "建议增大 max_universe 或用 symbols 显式提供次新列表。",
        "板块冰点用样本内次新的跌停占比/均跌幅近似，样本过小时信号稀少或噪音大（min_pool_size 可调）。",
        "地天板用「前收跌停 + 当日收盘涨停」的收盘价近似，未使用盘中价，与实际打板成交有差异。",
        "持有期为决策日计数（日频=交易日）；信号属极端事件型，区间内无信号时净值保持现金。",
        "涨跌停按板块判定：创业板/科创板 20%，主板 10%；已排除封死跌停（买不进）的标的。",
    ),
)
