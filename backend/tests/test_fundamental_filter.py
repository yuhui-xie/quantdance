"""回测股票池基本面筛选测试（不访问外网）。"""

from __future__ import annotations

import pandas as pd
import pytest

from app.backtest.cross_section_runner import _apply_universe_fundamental_filter
from app.fundamental_filter import apply_fundamental_filter, row_passes_fundamental_filter
from app.schemas import BacktestRequest, FundamentalFilterRule


def _value_frame(pe: float, *, date: str = "2024-06-30") -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": [date],
            "close": [10.0],
            "market_cap": [1e9],
            "float_market_cap": [8e8],
            "pe_ttm": [pe],
            "pb": [2.0],
            "peg": [1.5],
            "ps_ttm": [3.0],
        }
    )


def _panel(pe_map: dict[str, float], *, date: str = "2024-06-30") -> dict[str, dict[str, pd.DataFrame]]:
    return {
        sym: {"value": _value_frame(pe, date=date), "dividend": pd.DataFrame()}
        for sym, pe in pe_map.items()
    }


# ────────────────────────── 规则模型校验 ──────────────────────────


def test_rule_requires_bound():
    with pytest.raises(ValueError):
        FundamentalFilterRule(field="pe_ttm")
    with pytest.raises(ValueError):
        FundamentalFilterRule(field="pe_ttm", min=50, max=10)


def test_rule_accepts_single_bound():
    assert FundamentalFilterRule(field="pe_ttm", max=40).max == 40
    assert FundamentalFilterRule(field="pe_ttm", min=0).min == 0


# ────────────────────────── 纯评估器 ──────────────────────────


def test_row_passes_min_max():
    rule = FundamentalFilterRule(field="pe_ttm", min=0, max=40)
    assert row_passes_fundamental_filter(_value_frame(30).iloc[0].to_dict(), [rule])
    assert not row_passes_fundamental_filter(_value_frame(80).iloc[0].to_dict(), [rule])
    assert not row_passes_fundamental_filter(_value_frame(-5).iloc[0].to_dict(), [rule])


def test_row_passes_missing_value_fails():
    rule = FundamentalFilterRule(field="pe_ttm", max=40)
    row = _value_frame(30).iloc[0].to_dict()
    row["pe_ttm"] = None
    assert not row_passes_fundamental_filter(row, [rule])


def test_row_passes_dividend_yield():
    div = pd.DataFrame({"ex_date": ["2024-06-07"], "cash_per_share": [0.5]})
    rule = FundamentalFilterRule(field="dividend_yield", min=0.05)
    asof_row = {"date": "2024-12-31", "close": 10.0}
    assert row_passes_fundamental_filter(asof_row, [rule], dividends=div, price=10.0)
    assert not row_passes_fundamental_filter(
        asof_row, [rule], dividends=None, price=10.0
    )


# ────────────────────────── 批量过滤 ──────────────────────────


def test_apply_fundamental_filter_keeps_passing_rows():
    rules = [FundamentalFilterRule(field="pe_ttm", max=40)]
    rows = [{"symbol": "600000", "name": ""}, {"symbol": "600001", "name": ""}]
    kept, warnings = apply_fundamental_filter(
        rows, rules, asof="2024-06-30", panel=_panel({"600000": 30, "600001": 80})
    )
    assert [r["symbol"] for r in kept] == ["600000"]
    assert any("不满足" in w for w in warnings)


def test_apply_fundamental_filter_missing_data_dropped():
    rules = [FundamentalFilterRule(field="pe_ttm", max=40)]
    rows = [{"symbol": "600000", "name": ""}, {"symbol": "600002", "name": ""}]
    # 600002 无估值数据
    kept, warnings = apply_fundamental_filter(
        rows, rules, asof="2024-06-30", panel=_panel({"600000": 30})
    )
    assert [r["symbol"] for r in kept] == ["600000"]
    assert any("缺少基本面数据" in w for w in warnings)


def test_apply_fundamental_filter_asof_out_of_range():
    # asof 早于最早数据 → asof_fundamental_row 返回 None → 全部剔除
    rules = [FundamentalFilterRule(field="pe_ttm", max=40)]
    rows = [{"symbol": "600000", "name": ""}]
    kept, _ = apply_fundamental_filter(
        rows, rules, asof="2020-01-01", panel=_panel({"600000": 30})
    )
    assert kept == []


# ────────────────────────── 横截面接线 ──────────────────────────


def test_cross_section_helper_filters_universe():
    """共享的 _apply_universe_fundamental_filter：复用面板、收缩股票池并生成说明。"""
    universe = [{"symbol": "600000", "name": ""}, {"symbol": "600001", "name": ""}]
    request = BacktestRequest(
        mode="universe",
        start_date="2024-01-01",
        end_date="2024-12-31",
        fundamental_filter=[FundamentalFilterRule(field="pe_ttm", max=40)],
    )
    kept, note = _apply_universe_fundamental_filter(
        universe,
        _panel({"600000": 30, "600001": 80}, date="2023-12-31"),
        request,
        start="2024-01-01",
    )
    assert [r["symbol"] for r in kept] == ["600000"]
    assert "基本面筛选" in note


def test_cross_section_helper_noop_without_filter():
    request = BacktestRequest(mode="screen", end_date="2024-12-31")
    universe = [{"symbol": "600000", "name": ""}]
    kept, note = _apply_universe_fundamental_filter(
        universe, _panel({"600000": 30}), request, start=None
    )
    assert kept == universe
    assert note == ""
