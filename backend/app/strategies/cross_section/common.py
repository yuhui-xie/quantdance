"""组合选股共用过滤。"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from app.data_sources.em_fundamentals import asof_fundamental_row
from app.data_sources.market_data import normalize_a_share_symbol
from app.strategies.base import CrossSectionContext


def is_st_stock(name: str | None) -> bool:
    n = (name or "").upper()
    return "ST" in n or "退" in n


def is_hs_main_board_symbol(symbol: str) -> bool:
    """是否沪深主板 A 股（含原深市中小板）。

    保留：沪市 60xxxx、深市 000/001/002/003。
    排除：创业板 300/301、科创板 688/689、北交所 4/8/92 开头。
    """
    try:
        code = normalize_a_share_symbol(symbol)
    except ValueError:
        return False
    if code.startswith(("300", "301", "688", "689")):
        return False
    if code.startswith(("4", "8")) or code.startswith("92"):
        return False
    if code.startswith("60"):
        return True
    if code.startswith(("000", "001", "002", "003")):
        return True
    return False


def asof_tradeable_row(
    value_df: pd.DataFrame,
    asof: str,
    *,
    exclude_suspended: bool = True,
    exclude_limit: bool = True,
    limit_pct_threshold: float = 9.5,
    max_lag_days: int = 10,
) -> dict[str, Any] | None:
    """取调仓日可用估值行；不满足可交易约束时返回 None。"""
    row = asof_fundamental_row(value_df, asof)
    if row is None:
        return None
    try:
        lag = (
            date.fromisoformat(str(asof)[:10])
            - date.fromisoformat(str(row["date"])[:10])
        ).days
    except Exception:
        lag = 999
    if lag > max_lag_days:
        return None
    if exclude_suspended and lag > 0:
        return None
    pct = row.get("pct_change")
    if exclude_limit and pct is not None and abs(float(pct)) >= limit_pct_threshold:
        return None
    close = row.get("close")
    if close is None or float(close) <= 0:
        return None
    return row


def apply_hysteresis(
    ctx: CrossSectionContext,
    ranked_candidates: list[dict[str, Any]],
    top_n: int,
    threshold_rank: int,
    *,
    cache_key: str = "hysteresis_holdings",
) -> tuple[list[str], list[dict[str, Any]]]:
    """对已按优劣排序的候选池应用防抖滞后带，返回稳定的 top-N 持仓。

    滞后带语义（rank 0-based，越小越优）：
    - 前次持仓中排名仍在 top-N 内者原样保留；
    - 掉出 top-N 的持仓不立刻被换：只有当某个未持有候选比它领先至少
      ``threshold_rank`` 个名次（``持仓排名 - 候选排名 >= threshold_rank``）
      才让出位置，防止日频噪音引起来回换仓；
    - 持仓数量不足 top_N 时用排名最高的未持有候选补足。

    ``threshold_rank=0`` 表示关闭滞后带，退化为普通 top-N 选取。
    状态经 ``ctx.cache`` 跨决策日保存，须按决策日时序调用。
    """
    by_symbol = {c["symbol"]: i for i, c in enumerate(ranked_candidates)}
    prev = ctx.cache.get(cache_key, [])
    survivors = [s for s in prev if s in by_symbol]
    survivor_set = set(survivors)

    challengers = [c for c in ranked_candidates if c["symbol"] not in survivor_set]

    result: list[str] = []
    result_set: set[str] = set()

    # ── 第 1 步：保留仍在 top-N 内的持仓 ──
    for s in survivors:
        if by_symbol[s] < top_n and len(result) < top_n:
            result.append(s)
            result_set.add(s)

    # ── 第 2 步：处理掉出 top-N 的持仓（最差的先，逐个匹配最佳剩余候选） ──
    if threshold_rank > 0:
        vulnerable = [s for s in survivors if by_symbol[s] >= top_n]
        vulnerable.sort(key=lambda s: by_symbol[s], reverse=True)
        for s in vulnerable:
            if len(result) >= top_n:
                break
            if challengers:
                best = challengers[0]["symbol"]
                if by_symbol[s] - by_symbol[best] >= threshold_rank:
                    # 候选明显更优，占用该位置；持仓让位
                    result.append(best)
                    result_set.add(best)
                    challengers.pop(0)
                    continue
            result.append(s)
            result_set.add(s)

    # ── 第 3 步：用排名最高的未持有候选补足剩余空位 ──
    for c in challengers:
        if len(result) >= top_n:
            break
        if c["symbol"] not in result_set:
            result.append(c["symbol"])
            result_set.add(c["symbol"])

    ctx.cache[cache_key] = result
    details = [c for c in ranked_candidates if c["symbol"] in result_set]
    return result, details


def apply_score_threshold_rotation(
    ctx: CrossSectionContext,
    ranked_candidates: list[dict[str, Any]],
    top_n: int,
    threshold: float,
    *,
    cache_key: str = "rotation_lazy_holdings",
    score_key: str = "score",
) -> list[str]:
    """对已按得分降序排序的候选池应用得分比例惰性，返回稳定的 top-N 持仓。

    与 ``apply_hysteresis``（按排名名次的滞后带）不同，本函数按【得分比例】做惰性，
    适用于任何 top-N 轮动场景（股票、ETF 等）：
    先取得分最高的 top-N 作为默认入选，再对上一决策日持仓中、当前仍为候选但未
    入选的标的，用其【当前决策日】得分与当前入选里得分最低的新标的比较——当前持仓
    得分不低于新标的得分×``threshold`` 时保留该持仓并踢掉最弱的新入选，否则让位。
    仅当新候选明显更优才轮动，避免在得分相近的标的间来回切换。

    ``threshold >= 1.0`` 即关闭惰性、纯按得分轮动；越接近 0 惰性越强（0 时几乎
    永不调仓）。

    状态经 ``ctx.cache`` 跨决策日保存，须按决策日时序调用。
    """
    by_score = {c["symbol"]: float(c[score_key]) for c in ranked_candidates}
    selected = [c["symbol"] for c in ranked_candidates[:top_n]]
    held = ctx.cache.get(cache_key, [])
    if threshold < 1.0 and held and selected:
        held_set = set(held)
        selected_set = set(selected)
        for symbol in held:
            if symbol in selected_set:
                continue
            if symbol not in by_score:
                # 上一持仓当前时刻已跌出候选（得分≤0/停牌/涨跌停等），无法继续持有
                continue
            # 顶替基准：当前入选里、非当前持仓、当前得分最低的那只新标的
            displaceable = [
                sym for sym in selected
                if sym not in held_set and sym != symbol
            ]
            if not displaceable:
                continue
            weakest_new = min(displaceable, key=lambda sym: by_score[sym])
            if by_score[symbol] >= by_score[weakest_new] * threshold:
                # 当前持仓在当前时刻仍够强：保留它，踢掉最弱的新入选标的
                selected_set.discard(weakest_new)
                selected_set.add(symbol)
        selected = sorted(
            selected_set, key=lambda sym: by_score[sym], reverse=True
        )
    ctx.cache[cache_key] = list(selected)
    return selected
