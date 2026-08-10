"""apply_hysteresis 防抖滞后带单元测试。"""

from __future__ import annotations

from app.strategies.base import CrossSectionContext
from app.strategies.cross_section.common import apply_hysteresis


def _cands(symbols: list[str]) -> list[dict]:
    """构造按优劣降序排列的候选（rank 即索引，score 单调递减）。"""
    return [{"symbol": s, "score": float(100 - i)} for i, s in enumerate(symbols)]


def _ctx(cache: dict | None = None) -> CrossSectionContext:
    return CrossSectionContext(panel={}, names={}, cache=cache or {})


def test_first_call_returns_top_n_and_caches():
    ctx = _ctx()
    symbols, details = apply_hysteresis(
        ctx, _cands(["A", "B", "C", "D", "E"]), top_n=3, threshold_rank=3
    )
    assert symbols == ["A", "B", "C"]
    assert [d["symbol"] for d in details] == ["A", "B", "C"]
    assert ctx.cache["hysteresis_holdings"] == ["A", "B", "C"]


def test_zero_threshold_disables_hysteresis():
    ctx = _ctx({"hysteresis_holdings": ["E"]})
    symbols, _ = apply_hysteresis(
        ctx, _cands(["A", "B", "C", "D", "E"]), top_n=2, threshold_rank=0
    )
    # 掉出 top-N 的旧持仓 E 不保留，退化为普通 top-N
    assert symbols == ["A", "B"]


def test_holding_within_top_n_is_kept():
    ctx = _ctx({"hysteresis_holdings": ["A", "C"]})
    symbols, _ = apply_hysteresis(
        ctx, _cands(["A", "B", "C", "D"]), top_n=2, threshold_rank=3
    )
    assert symbols == ["A", "C"]


def test_vulnerable_holding_kept_below_threshold():
    ctx = _ctx({"hysteresis_holdings": ["C"]})
    symbols, _ = apply_hysteresis(
        ctx, _cands(["A", "B", "C", "D"]), top_n=2, threshold_rank=3
    )
    # C 掉到第 3 名，最佳候选 A 只领先 2 位 < 阈值 3，保留 C
    assert symbols == ["C", "A"]


def test_clearly_better_candidate_replaces_holding():
    ctx = _ctx({"hysteresis_holdings": ["C"]})
    symbols, _ = apply_hysteresis(
        ctx, _cands(["A", "B", "D", "E", "C"]), top_n=2, threshold_rank=3
    )
    # C 掉到第 5 名，最佳候选 A 领先 4 位 >= 阈值 3，替换
    assert symbols == ["A", "B"]


def test_stable_matching_worst_holding_displaced_first():
    ctx = _ctx({"hysteresis_holdings": ["B", "A"]})
    symbols, _ = apply_hysteresis(
        ctx, _cands(["C", "D", "A", "B"]), top_n=2, threshold_rank=2
    )
    # 最差持仓 B(rank3) 被最佳候选 C(rank0) 换掉；
    # A(rank2) 与次佳候选 D(rank1) 差距 1 < 阈值 2，A 保留
    assert symbols == ["C", "A"]


def test_vanished_holding_is_dropped():
    ctx = _ctx({"hysteresis_holdings": ["X", "A"]})
    symbols, _ = apply_hysteresis(
        ctx, _cands(["A", "B", "C"]), top_n=2, threshold_rank=3
    )
    assert "X" not in symbols
    assert symbols == ["A", "B"]


def test_under_full_portfolio_fills_with_best():
    ctx = _ctx({"hysteresis_holdings": ["C"]})
    symbols, _ = apply_hysteresis(
        ctx, _cands(["A", "B", "C", "D"]), top_n=3, threshold_rank=3
    )
    assert symbols == ["C", "A", "B"]


def test_cache_updated_each_call():
    ctx = _ctx()
    _, _ = apply_hysteresis(ctx, _cands(["A", "B", "C", "D"]), top_n=2, threshold_rank=3)
    assert ctx.cache["hysteresis_holdings"] == ["A", "B"]
    _, _ = apply_hysteresis(ctx, _cands(["A", "B", "C", "D"]), top_n=2, threshold_rank=3)
    # 持仓仍在前两名，无需换仓，缓存保持稳定
    assert ctx.cache["hysteresis_holdings"] == ["A", "B"]
