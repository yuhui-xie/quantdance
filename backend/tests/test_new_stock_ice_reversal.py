"""次新情绪冰点反转策略测试：冰点检测、入场-持有-退出状态机、止损与地天板排序。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.strategies.base import CrossSectionContext
from app.strategies.cross_section.new_stock_ice_reversal import (
    NewStockIceReversalParams,
    select_new_stock_ice_reversal,
)
from app.strategies.registry import CROSS_SECTION_STRATEGIES

DATES = [
    "2024-01-02",
    "2024-01-03",
    "2024-01-04",
    "2024-01-05",
    "2024-01-08",
    "2024-01-09",
    "2024-01-10",
]


def _make_value(closes: list[float], pcts: list[float | None]) -> pd.DataFrame:
    df = pd.DataFrame({"date": DATES, "close": closes})
    df["pct_change"] = [np.nan if p is None else float(p) for p in pcts]
    return df


def _make_ctx(payload: dict[str, pd.DataFrame]) -> CrossSectionContext:
    panel = {sym: {"value": df} for sym, df in payload.items()}
    return CrossSectionContext(panel=panel, names={sym: sym for sym in payload})


def _params(**overrides: object) -> NewStockIceReversalParams:
    base = dict(
        decision_frequency="daily",
        decision_warmup=0,
        max_listed_days=250,
        min_listed_days=2,
        ice_metric="limit_down_ratio",
        crash_days=2,
        min_limit_down_ratio=0.30,
        entry_timing="next_day",
        limit_pct_threshold=9.5,
        top_n=5,
        hold_days=2,
        min_price=2.0,
        max_price=200.0,
        min_pool_size=4,
        exclude_st=True,
        prefer_ditianban=True,
        stop_loss_pct=0.08,
        take_profit_pct=0.25,
    )
    base.update(overrides)
    return NewStockIceReversalParams(**base)


def _crash_panel() -> dict[str, pd.DataFrame]:
    """A/B/C 在 01-04、01-05 连续两个跌停，01-08 起企稳；D 全程跌幅温和。"""
    a = _make_value(
        [10.0, 10.1, 9.09, 8.18, 8.43, 8.60, 8.69],
        [None, 1.0, -10.0, -10.0, 3.0, 2.0, 1.0],
    )
    b = _make_value(
        [10.0, 10.1, 9.09, 8.18, 8.43, 8.60, 8.69],
        [None, 1.0, -10.0, -10.0, 3.0, 2.0, 1.0],
    )
    c = _make_value(
        [10.0, 10.1, 9.09, 8.18, 8.43, 8.60, 8.69],
        [None, 1.0, -10.0, -10.0, 3.0, 2.0, 1.0],
    )
    d = _make_value(
        [10.0, 10.1, 9.90, 9.60, 9.79, 9.89, 9.99],
        [None, 1.0, -2.0, -3.0, 2.0, 1.0, 1.0],
    )
    return {"A": a, "B": b, "C": c, "D": d}


def test_registry_contains_new_stock_ice_reversal():
    assert "new_stock_ice_reversal" in CROSS_SECTION_STRATEGIES


def test_ice_point_entry_hold_exit_state_machine():
    ctx = _make_ctx(_crash_panel())
    params = _params(hold_days=2)
    results = [
        select_new_stock_ice_reversal(day, ctx, params) for day in DATES
    ]

    # 冰点形成前/跌停潮进行中：不入场
    assert [t for t, _ in results[:4]] == [[], [], [], []]
    # 01-08 跌停潮结束次日：买入 A/B/C/D（地天板无，按超跌排序）
    targets, details = results[4]
    assert targets == ["A", "B", "C", "D"]
    assert {d["symbol"] for d in details} == {"A", "B", "C", "D"}
    # 01-09 持有（同一目标篮 → 引擎不重复交易）
    assert results[5][0] == ["A", "B", "C", "D"]
    # 01-10 达到 hold_days → 清仓
    assert results[6][0] == []


def test_stop_loss_drops_stock_during_hold():
    panel = _crash_panel()
    # D 在 01-09 跌破止损（相对 01-08 买入价 -15%）
    panel["D"] = _make_value(
        [10.0, 10.1, 9.90, 9.60, 9.79, 8.32, 8.30],
        [None, 1.0, -2.0, -3.0, 2.0, -15.0, -0.2],
    )
    ctx = _make_ctx(panel)
    params = _params(hold_days=5)
    targets, _ = select_new_stock_ice_reversal("2024-01-08", ctx, params)
    assert targets == ["A", "B", "C", "D"]

    targets_hold, _ = select_new_stock_ice_reversal("2024-01-09", ctx, params)
    assert "D" not in targets_hold
    assert targets_hold == ["A", "B", "C"]


def test_ditianban_ranked_first():
    panel = _crash_panel()
    # A 在 01-08 地天板：前收 01-05 跌停、当日涨停
    panel["A"] = _make_value(
        [10.0, 10.1, 9.09, 8.18, 9.00, 9.10, 9.20],
        [None, 1.0, -10.0, -10.0, 10.0, 1.0, 1.0],
    )
    ctx = _make_ctx(panel)
    params = _params()
    targets, details = select_new_stock_ice_reversal("2024-01-08", ctx, params)

    assert targets[0] == "A"
    assert details[0]["is_ditianban"] is True
    # B/C 超跌更深的排在 D 之前
    assert targets[1:3] == ["B", "C"]
    assert targets[3] == "D"


def test_prefer_ditianban_off_ranks_by_oversold():
    panel = _crash_panel()
    panel["A"] = _make_value(
        [10.0, 10.1, 9.09, 8.18, 9.00, 9.10, 9.20],
        [None, 1.0, -10.0, -10.0, 10.0, 1.0, 1.0],
    )
    ctx = _make_ctx(panel)
    params = _params(prefer_ditianban=False)
    targets, _ = select_new_stock_ice_reversal("2024-01-08", ctx, params)

    # 关闭地天板优先后，超跌最深的 B/C 排在 A 之前
    assert targets[0] in {"B", "C"}
    assert "A" in targets


def test_no_signal_without_crash():
    # 无跌停潮 → 整个区间都不入场
    panel = {
        sym: _make_value(
            [10.0, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6],
            [None, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
        )
        for sym in ("A", "B", "C", "D")
    }
    ctx = _make_ctx(panel)
    params = _params()
    for day in DATES:
        assert select_new_stock_ice_reversal(day, ctx, params) == ([], [])
